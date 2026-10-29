"""
主 Agent 入口模块。

使用 DeepAgents `create_deep_agent` 将所有组件串联为一个可运行的
ERP 采购智能助手。采用 async graph factory 模式，每次调用创建新沙箱。

架构总览（为什么这样分层）：
    主 Agent = 协调者，只保留通用工具（web_search / knowledge_search /
    技能与文件管理）；所有 ERP 业务查询与订单写操作通过 `task` 工具
    委派给两个子 Agent（procurement-analyst / procurement-order）。
    好处：1) 主 Agent 上下文小、不易跑偏；2) 子 Agent 可独立配置模型与
    中间件；3) 订单写操作集中在 order 子 Agent 上，便于挂 HITL 审批。

使用方式:
    from agent.main_agent import agent

    # 本地调用
    agent_graph = await agent()
    result = await agent_graph.ainvoke(
        {"messages": [HumanMessage(content="...")]},
        {"context": ProcurementContext(user_id="zhangsan", username="张三")},
    )
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import Optional

from deepagents import create_deep_agent
from deepagents.backends import CompositeBackend, StoreBackend
from langchain.agents.middleware import (
    ModelCallLimitMiddleware,
    ToolCallLimitMiddleware,
)
from langchain_core.runnables import RunnableConfig

from agent.backends.sandbox_proxy import SandboxBackendProxy
from agent.backends.sandbox_setup import setup_sandbox

from agent.config import (
    AGENTS_MD_FILENAME,
    DEFAULT_USER_ID,
    DOWNLOAD_DIR,
    LOCAL_AGENTS_MD,
    MAIN_MODEL,
    SANDBOX_CONFIG,
    SKILLS_STORE_NAMESPACE,
    SUMMARY_MODEL,
    get_checkpointer,
    get_store,
)
from agent.memory.prompts import system_prompt
from agent.middleware_config import (
    create_analyst_middleware,
    create_order_middleware,
)
from agent.middlewares.context_injection import ContextInjectionMiddleware
from agent.middlewares.memory_update import MemoryUpdateMiddleware
from agent.middlewares.sandbox_breaker import SandboxCircuitBreakerMiddleware
from agent.middlewares.sandbox_health import SandboxHealthMiddleware
from agent.middlewares.skills_sync import SkillsSyncMiddleware
from agent.middlewares.tool_error import ToolErrorMiddleware
from agent.middlewares.tools_summarization import build_summarization_middleware
from agent.middlewares.user_skills_restore import UserSkillsRestoreMiddleware
from agent.schema import ProcurementContext
from agent.subagents.loader import load_subagent_configs, resolve_subagent_tools
from agent.tools.chart_generator import create_generate_chart_tool
from agent.tools.hitl_tools import request_order_info
from agent.tools.assign_skill import create_assign_skill_tool
from agent.tools.download_sandbox_file import create_download_tool
from agent.tools.knowledge_search import knowledge_search
from agent.tools.mcp_client import load_mcp_tools
from agent.tools.web_search import web_search

def _setup_logging() -> None:
    """配置日志：开发环境输出到控制台，生产环境输出到文件。"""
    env = os.environ.get("APP_ENV", "development")
    if env == "production":
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            filename="erp_agent.log",
            filemode="a",
        )
    else:
        logging.basicConfig(
            level=logging.ERROR,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            stream=sys.stdout,
        )

_setup_logging()
logger = logging.getLogger(__name__)


async def create_main_agent(
    config: Optional[RunnableConfig] = None,
    *,
    sandbox_id: Optional[str] = None,
    sandbox_backend: Optional[SandboxBackendProxy] = None,
    user_id: Optional[str] = None,
):
    """
    创建 ERP 采购智能助手的 async graph factory。

    每次调用执行完整的 11 阶段初始化流水线：
      1. 沙箱配置 → 1.4 AGENTS.md 写入 Store → 1.5 CompositeBackend 分流 → 2. MCP 工具加载
      → 3. 可视化工具合并 → 4. 工具池构建 → 5. 子 Agent YAML 加载
      → 6. 子 Agent 中间件 → 7. 工具名称解析
      → 8. 主 Agent 中间件栈 → 9. create_deep_agent()

    Args:
        config: LangGraph RunnableConfig，由 langgraph 平台注入。
        sandbox_id: 可选，复用已有沙箱 ID（脚本/CLI 场景）。
        sandbox_backend: 可选，由 sandbox_manager 分配好的 per-user 沙箱代理。
            传入时不再自行创建沙箱 —— Web 端走这条路径，同一用户的多条会话
            因此共享同一个沙箱文件系统（技能、分析中间产物都在里面）。
        user_id: 可选，仅用于日志与健康检查中间件定位沙箱。

    Returns:
        编译后的 LangGraph StateGraph，可调用 .ainvoke() / .astream()。
    """
    logger.info("=== 开始创建 ERP 采购智能助手 ===")

    # ---- Phase 1: 沙箱配置 ----
    # 两条路径：
    #   a) Web 端传入 per-user 代理（sandbox_backend）—— 沙箱由 sandbox_manager
    #      统一管理，同一用户复用同一个沙箱，挂了能被健康检查自动换芯；
    #   b) 脚本/CLI 自建 —— 没有 manager 时的兜底，行为与原来一致。
    logger.info("Phase 1/10: 配置沙箱...")
    if sandbox_backend is None:
        try:
            sandbox_backend = SandboxBackendProxy(
                setup_sandbox(SANDBOX_CONFIG, sandbox_id=sandbox_id)
            )
        except Exception:
            logger.exception("沙箱配置失败")
            raise RuntimeError("沙箱配置失败，无法创建 Agent")

    # ---- Phase 1.4: 上传 AGENTS.md 到沙箱 ----
    # AGENTS.md 存储到 OpenSandbox 后端 /AGENTS.md，不走 StoreBackend。
    # 沙箱内的 /AGENTS.md 由 CompositeBackend 的 default 路由命中。
    logger.info("Phase 1.4/10: 上传 AGENTS.md 到沙箱...")
    ag_md_content = LOCAL_AGENTS_MD.read_text(encoding="utf-8")
    sandbox_backend.upload_files([("/AGENTS.md", ag_md_content.encode("utf-8"))])
    logger.info("  AGENTS.md 已上传到沙箱")

    # ---- Phase 1.5: CompositeBackend 分流 ----
    # /AGENTS.md          → 沙箱 default 路由（OpenSandbox，Phase 1.4 已上传）
    # /memories/          → StoreBackend（按 user_id 隔离用户偏好）
    # /persisted-skills/  → StoreBackend（按 Agent scope 组织技能）
    # 其余路径（临时文件、代码执行）保留在沙箱。
    logger.info("Phase 1.5/10: 配置 CompositeBackend (memories + persisted-skills → Store)...")

    def backend(rt):
        """按运行时上下文构造后端：只有拿到 context 才能确定记忆命名空间。"""
        return CompositeBackend(
            default=sandbox_backend,
            routes={
                "/memories/": StoreBackend(
                    runtime=rt,
                    # namespace 即用户隔离边界：每个 user_id 一个独立命名空间。
                    # 取不到 context 时回退到 DEFAULT_USER_ID（可见的兜底），
                    # 绝不把不同用户的记忆写进同一个空间。
                    namespace=lambda inner_rt: (
                        getattr(inner_rt.runtime.context, "user_id", None) or DEFAULT_USER_ID,
                    ),
                ),
                "/persisted-skills/": StoreBackend(
                    runtime=rt,
                    namespace=lambda inner_rt: SKILLS_STORE_NAMESPACE,
                ),
            },
        )

    # ---- Phase 2: MCP 工具加载 ----
    logger.info("Phase 2/10: 加载 MCP 工具...")
    try:
        all_mcp_tools, analyst_mcp_tools, order_mcp_tools, chart_mcp_tools = (
            await load_mcp_tools()
        )
    except Exception:
        logger.exception("MCP 工具加载失败")
        raise RuntimeError("MCP 工具加载失败，无法创建 Agent")

    # ---- Phase 3: 可视化工具合并 ----
    logger.info("Phase 3/10: 合并可视化工具 (26→1)...")
    generate_visualization, extra_mcp_tools = create_generate_chart_tool(chart_mcp_tools)
    if extra_mcp_tools:
        logger.info(f"  保留独立工具: {[t.name for t in extra_mcp_tools]}")

    # ---- Phase 3.6: 创建技能管理工具 ----
    assign_skill = create_assign_skill_tool(
        sandbox_backend,
        store=get_store(),   # PostgresStore（PG 不可用时降级 InMemoryStore）
        skills_namespace=SKILLS_STORE_NAMESPACE,
    )
    download_sandbox_file = create_download_tool(sandbox_backend, DOWNLOAD_DIR)

    # ---- Phase 4: 构建工具解析池 ----
    logger.info("Phase 4/10: 构建工具解析池...")
    available_tools = (
        list(analyst_mcp_tools)
        + list(order_mcp_tools)
        + list(extra_mcp_tools)
        + [generate_visualization]
        + [web_search]
        + [knowledge_search]      # RAG 知识库检索（采购制度/物料知识/供应商评估）
        + [request_order_info]
        + [assign_skill]
        + [download_sandbox_file]
    )
    logger.info(f"  工具池: {len(available_tools)} 个工具")

    # ---- Phase 5: 子 Agent YAML 加载 ----
    logger.info("Phase 5/10: 加载子 Agent YAML 配置...")
    raw_configs = load_subagent_configs()
    if not raw_configs:
        logger.warning("  未找到任何子 Agent 配置，Agent 将在无子 Agent 模式下运行")
    else:
        logger.info(f"  已加载 {len(raw_configs)} 个子 Agent 配置")

    # ---- Phase 6: 子 Agent 中间件 ----
    logger.info("Phase 6/10: 创建子 Agent 中间件...")
    extra_middleware = {
        "procurement-analyst": create_analyst_middleware(SUMMARY_MODEL, backend),
        "procurement-order": create_order_middleware(),
    }

    # ---- Phase 7: 子 Agent 工具解析 ----
    logger.info("Phase 7/10: 解析子 Agent 工具名称...")
    subagents = resolve_subagent_tools(
        raw_configs,
        available_tools,
        extra_middleware=extra_middleware,
    )
    logger.info(f"  已解析 {len(subagents)} 个子 Agent")

    # ---- Phase 8: 主 Agent 中间件栈 ----
    logger.info("Phase 8/10: 构建主 Agent 中间件栈...")
    main_middleware = [
        ContextInjectionMiddleware(),
        SkillsSyncMiddleware(sandbox_backend),
        UserSkillsRestoreMiddleware(sandbox_backend, SKILLS_STORE_NAMESPACE),
        build_summarization_middleware(backend, SUMMARY_MODEL),
        MemoryUpdateMiddleware(model=SUMMARY_MODEL),
    ]

    # 沙箱自愈三件套（只在 manager 托管沙箱时启用）：
    #   SandboxHealthMiddleware      —— 每步开始前探活，挂了就地重建（换芯不换壳）
    #   SandboxCircuitBreakerMiddleware —— 连续失败超阈值熔断，避免"重建→再挂"死循环
    #   ToolErrorMiddleware          —— 单个工具异常转成 ToolMessage，别炸掉整轮
    # 顺序有意为之：健康检查在最前（先保证沙箱可用），熔断在其后（判断是否该放弃）。
    if user_id:
        main_middleware.extend([
            SandboxHealthMiddleware(
                sandbox_backend=sandbox_backend,
                user_id=user_id,
                agents_md_content=ag_md_content.encode("utf-8"),
            ),
            SandboxCircuitBreakerMiddleware(
                threshold=int(os.environ.get("SANDBOX_BREAKER_THRESHOLD", "2"))
            ),
        ])
    main_middleware.extend([
        ToolErrorMiddleware(),
        ModelCallLimitMiddleware(run_limit=50),
        ToolCallLimitMiddleware(run_limit=200),
    ])

    # ---- Phase 9: create_deep_agent ----
    logger.info("Phase 9/10: 创建 Deep Agent...")
    agent_graph = create_deep_agent(
        model=MAIN_MODEL,
        system_prompt=system_prompt,
        skills=["/skills/main/"],
        memory=[AGENTS_MD_FILENAME],
        # 主 Agent 自留工具：web_search（行情/供应商背景）、knowledge_search（领域知识 RAG）、
        # assign_skill / download_sandbox_file（技能与文件管理）。
        # 业务查询与下单工具全部通过子 Agent 委派，主 Agent 保持"协调者"定位。
        tools=[web_search, knowledge_search, assign_skill, download_sandbox_file],
        subagents=subagents,
        middleware=main_middleware,
        backend=backend,
        # store 同样延迟初始化：PostgresStore 优先，PG 不可用降级 InMemoryStore
        store=get_store(),
        # checkpointer 延迟到此刻才真正连接 PostgreSQL（见 config.get_checkpointer）
        checkpointer=get_checkpointer(),
        context_schema=ProcurementContext,
    )

    logger.info("=== ERP 采购智能助手创建完成 ===")
    return agent_graph


# ============================================================
# Agent 懒加载代理（兼容同步/异步两种初始化场景）
# ============================================================

async def _create_agent():
    """创建 Agent 实例（供 _AgentProxy 调用）"""
    return await create_main_agent()


class _AgentProxy:
    """
    懒加载 Agent 代理类

    兼容以下两种使用场景：
    1. 同步环境（如 agent_test.py 控制台）：在模块导入后、事件循环启动前初始化
    2. 异步环境（如 FastAPI 后端）：通过 get_agent_async() 在事件循环中初始化

    当直接访问 agent 对象的属性/方法时，代理会自动触发初始化并委托调用。
    """

    def __init__(self):
        self._agent = None

    @property
    def _is_initialized(self):
        """检查底层 agent 是否已初始化"""
        return self._agent is not None

    def _ensure_initialized(self):
        """
        确保 agent 已初始化（同步方式）

        如果没有运行中的事件循环，使用 asyncio.run() 创建 agent。
        如果事件循环正在运行，抛出 RuntimeError 提示使用 get_agent_async()。
        """
        if self._agent is not None:
            return self._agent

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                raise RuntimeError(
                    "Agent 尚未初始化且当前在事件循环中，"
                    "请使用 await get_agent_async() 获取 agent"
                )
        except RuntimeError as e:
            if "Agent 尚未初始化" in str(e):
                raise
            # 没有事件循环，继续初始化

        self._agent = asyncio.run(_create_agent())
        return self._agent

    def __getattr__(self, name):
        return getattr(self._ensure_initialized(), name)

    def __repr__(self):
        if self._agent is None:
            return "<AgentProxy (not initialized)>"
        return repr(self._agent)


# agent 实例，初始化为懒加载代理，由 get_agent() / get_agent_async() 函数触发初始化
agent = _AgentProxy()


def get_agent():
    """
    获取 agent 实例，懒加载方式（同步）

    如果 agent 尚未初始化，则同步创建它。
    注意：不能在运行中的事件循环内调用此函数。

    Returns:
        CompiledStateGraph: Agent 实例
    """
    global agent
    if isinstance(agent, _AgentProxy):
        if agent._is_initialized:
            return agent._agent
        return agent._ensure_initialized()
    return agent


async def get_agent_async():
    """
    异步获取 agent 实例，懒加载方式

    适用于在事件循环中运行时调用（如 FastAPI 的 lifespan）。
    如果 agent 已通过 get_agent() 同步初始化，则直接返回。

    Returns:
        CompiledStateGraph: Agent 实例
    """
    global agent
    if isinstance(agent, _AgentProxy):
        if agent._is_initialized:
            return agent._agent
        agent._agent = await _create_agent()
        return agent._agent
    return agent