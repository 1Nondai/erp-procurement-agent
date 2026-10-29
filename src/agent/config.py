from datetime import timedelta
import os
from pathlib import Path
from typing import Any, Optional

import httpx
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.store.memory import InMemoryStore
from langgraph.store.postgres import PostgresStore
from opensandbox.config import ConnectionConfigSync

from agent.env_utils import (
    DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL,
    POSTGRES_URI,
)

# ---------- 模型配置 ----------
# 主 Agent 模型：负责规划、委派与最终答复
# 参数取舍（P0 修正）：
#   - temperature：编排类任务要的是"稳定复现"，不是"发挥创意"。工具名、参数、委派决策
#     一旦抖动就会连锁出错，因此从 1.1 降到 0.3（需要文采的部分交给提示词而不是采样温度）。
#   - max_tokens：这是【单次输出】上限，不是上下文窗口。原值 2560000 是把上下文长度
#     误填进了输出预算，超出模型能力、会被服务端拒绝或忽略。按"够写一段总结/一张表格"设 16k。
MAIN_MODEL = ChatOpenAI(
    model="deepseek-v4-pro",
    temperature=0.3,
    openai_api_key=DEEPSEEK_API_KEY,
    openai_api_base=DEEPSEEK_BASE_URL,
    max_tokens=16384,
    model_kwargs={
        "extra_body": {
            "thinking": {"type": "disabled"}
        }
    }
)
# 摘要专用模型：用于上下文摘要、记忆实体抽取等"结构化小任务"
# 低温 + 小输出预算：这些任务只需稳定输出 JSON/短摘要，用大模型和高温度都是浪费
SUMMARY_MODEL = ChatOpenAI(
    model="deepseek-v4-flash",
    temperature=0.2,
    openai_api_key=DEEPSEEK_API_KEY,
    openai_api_base=DEEPSEEK_BASE_URL,
    max_tokens=4096,
    model_kwargs={
        "extra_body": {
            "thinking": {"type": "disabled"}
        }
    }
)
# 备用模型：全项目零引用，已删除（保留说明以免有人再翻文档去找）。
# 当前不启用 fallback 链路：主模型故障时直接报错，比静默降级到弱模型更容易被发现；
# 若将来要做降级，请在这里新增一个按需构造的函数，而不是模块级常驻客户端。

# ---------- 沙箱配置 ----------
# OpenSandbox 沙箱配置连接（本地 Docker 部署）
# 地址可用 SANDBOX_DOMAIN 覆盖；默认 8080 与不少本机服务撞端口，
# 联调时改这里或改对方端口即可。
SANDBOX_CONFIG = ConnectionConfigSync(
    domain=os.environ.get("SANDBOX_DOMAIN", "http://127.0.0.1:8080"),
    use_server_proxy=True,
    request_timeout=timedelta(seconds=60),
    transport=httpx.HTTPTransport(limits=httpx.Limits(max_connections=20)),
)

# ---------- 路径常量 ----------
EXAMPLE_DIR = Path(__file__).parent.parent
# 沙箱内技能根路径
SANDBOX_SKILLS_ROOT = "/skills"
# 沙箱内记忆根路径（用户私有记忆存放处）
SANDBOX_MEMORIES_ROOT = "/memories"
# 沙箱内分析中间文件存放目录
SANDBOX_ANALYSIS_ROOT = "/analysis"
# 沙箱内数据文件存放目录
SANDBOX_DATA_ROOT = "/data"
# 本地技能资源目录（项目内的路径，相对于项目根）
LOCAL_SKILLS_DIR = EXAMPLE_DIR / "skills"
# 本地下载目录（从沙箱下载文件的目标路径）
DOWNLOAD_DIR = EXAMPLE_DIR / "download"
# 本地子 Agent 配置目录
LOCAL_SUBAGENT_CONFIG_DIR = EXAMPLE_DIR / "agent/subagents"
# 本地的Agent记忆文件
LOCAL_AGENTS_MD = EXAMPLE_DIR / "agent/memory/AGENTS.md"

# ---------- 文件名常量 ----------
# 主 Agent 只读指引文件（上传到沙箱 /AGENTS.md）
AGENTS_MD_FILENAME = "/AGENTS.md"
# 用户偏好文件名（在 /memories/{user_id}/ 下）
USER_PREFERENCES_FILENAME = "preferences.md"

# ---------- 用户技能持久化 ----------
# 技能持久化 StoreBackend 路由路径
PERSISTED_SKILLS_ROOT = "/persisted-skills"
# 技能 StoreBackend 命名空间（按 Agent scope 组织，无用户隔离）
SKILLS_STORE_NAMESPACE = ("skills",)
# 子 Agent 名称 → 技能 scope 目录映射
SCOPE_MAP = {
    "main": "main",
    "procurement-analyst": "procurement",
    "procurement-order": "order",
}

# ---------- 身份与多用户（P0） ----------
# 默认用户：仅在请求未携带身份信息时兜底（本地开发、脚本调用）。
# 注意：这是"兜底值"而不是"硬编码身份"——正常运行时应由 HTTP 请求头 X-User-Id 注入，
# 详见 api_view/identity.py。user_id 会拼进 /memories/{user_id}/ 路径，因此必须过白名单校验。
DEFAULT_USER_ID = "dev_default"
DEFAULT_USERNAME = "开发默认用户"

# ---------- 子 Agent 工具解析策略（P0） ----------
# True：YAML 里声明的工具若匹配不到任何真实工具，启动即抛错（fail fast）。
# 为什么默认严格：工具名写错时，原实现只打一行 WARNING，运行期表现为
# "子 Agent 说它没有某个工具"，排障成本极高——配置错误就该在启动时暴露。
# 若只想临时放行（例如某个 MCP Server 尚未启动），可用环境变量 SUBAGENT_TOOL_STRICT=0 降级。
SUBAGENT_TOOL_STRICT = os.environ.get("SUBAGENT_TOOL_STRICT", "1") not in ("0", "false", "False")

# ---------- 沙箱出网策略（P0） ----------
# 背景：原实现把 network_policy 整段注释掉了，等价于沙箱可以访问任意公网地址，
# 数据外泄与 SSRF 都无法拦截。这里改为"默认拒绝 + 白名单放行"。
#   allowlist（默认）：只允许白名单主机出网，其余一律拒绝
#   open             ：等价于旧行为，仅用于本地排障（会打 WARNING）
# 白名单组成 = 下面这份基础清单 + url_mapping.yaml 里出现的报价站点主机（自动提取）+
#             环境变量 SANDBOX_EGRESS_EXTRA_HOSTS（逗号分隔）
SANDBOX_EGRESS_MODE = os.environ.get("SANDBOX_EGRESS_MODE", "allowlist").strip().lower()
SANDBOX_EGRESS_ALLOWED_HOSTS = [
    "mirrors.aliyun.com",      # 阿里云 PyPI 镜像（技能内 pip install 走这里）
    "pypi.org",                # 兜底源
    "files.pythonhosted.org",  # 兜底源（下载实际包体）
]

# 报价站点映射表：用于自动提取需要放行的抓取目标主机
SUPPLIER_URL_MAPPING = (
    EXAMPLE_DIR / "skills" / "procurement" / "supplier-price-urls" / "data" / "url_mapping.yaml"
)

# ---------- 写操作幂等（P0） ----------
# 订单类写操作的重试/重复审批会产生重复下单。MCP Server 侧用 SQLite 记录
# "请求指纹 → 首次响应"，相同指纹在 TTL 内直接返回缓存结果，不再真正打后端。
IDEMPOTENCY_DB_PATH = os.environ.get(
    "IDEMPOTENCY_DB_PATH",
    str(EXAMPLE_DIR / "src" / "mcp_server" / ".state" / "idempotency.sqlite3"),
)
IDEMPOTENCY_TTL_SECONDS = int(os.environ.get("IDEMPOTENCY_TTL_SECONDS", str(24 * 3600)))

# ---------- 中间件参数 ----------

# ---------- PostgreSQL 配置（用于持久化 Agent 短期记忆/checkpoint） ----------
# 连接串统一从环境变量读取（env_utils 是全项目唯一的敏感配置入口），
# 代码里不再出现明文密码；本地默认值仅供开发环境兜底。
POSTGRES_URI = POSTGRES_URI
POSTGRES_DB_NAME = "langgraph_db"

# ---------- 持久化存储 ----------
# STORE 承载两类数据：用户长期记忆（/memories/{user_id}/）与持久化技能
# （/persisted-skills/）。原实现是 InMemoryStore——进程重启即全部丢失，
# 用户花时间养成的偏好和下载的技能会凭空消失。
#
# 升级方案（P1）：get_store() 惰性创建 PostgresStore（与 checkpointer 同库），
# 数据库不可用时降级回 InMemoryStore 并打 WARNING——开发/单测环境不必强依赖 PG，
# 但生产环境的持久化必须是默认行为。
#
# 为什么惰性：与 get_checkpointer 同理，import 不应有副作用；
# 为什么降级而不是报错：checkpointer 不可用 Agent 无法运行（必须有状态），
# 而 Store 不可用只影响记忆/技能持久化（锦上添花），系统仍可工作。
_STORE: Optional[Any] = None
_STORE_DEGRADED = False


def get_store():
    """获取全局 Store 单例（PostgresStore 优先，不可用降级 InMemoryStore）。

    Returns:
        BaseStore: 已 setup 的 PostgresStore，或降级的 InMemoryStore。

    行为说明：
        - 首次调用：尝试创建 PostgresStore 并 setup()（幂等建表 store 表）；
        - PG 不可用：打 WARNING 后降级 InMemoryStore（进程内有效，重启丢失）；
        - 降级只在首次发生时告警一次（_STORE_DEGRADED 标记），避免每次调用刷屏。
    """
    global _STORE, _STORE_DEGRADED
    if _STORE is not None:
        return _STORE
    try:
        ctx = PostgresStore.from_conn_string(POSTGRES_URI)
        store = ctx.__enter__()
        store.setup()
        _STORE = store
        print("[config] Store: PostgresStore（持久化）初始化成功")
    except Exception as e:
        # 降级是"看得见的降级"：WARNING 必须打，让运维知道记忆不会跨重启保留
        _STORE = InMemoryStore()
        if not _STORE_DEGRADED:
            _STORE_DEGRADED = True
            print(
                f"[config][WARNING] PostgresStore 不可用（{e}），"
                "已降级为 InMemoryStore——用户记忆与技能仅本进程有效，重启即丢失。"
                "生产环境请检查 POSTGRES_URI 配置。"
            )
    return _STORE


# ---------- Checkpointer（延迟初始化） ----------
# 原实现的问题：在模块顶层直接 `PostgresSaver.from_conn_string(...).__enter__()`，
# 任何模块只要 `import agent.config` 就会立刻建立 PG 连接（且永不关闭）——
# 这让单测/脚本也被迫依赖数据库，还可能产生连接泄漏。
#
# 改进：get_checkpointer() 惰性创建 + functools.lru_cache 保证全局唯一实例。
# 首次真正需要 checkpointer 时（即 Agent 创建时）才连数据库；连不上时报清晰的错误。
_CHECKPOINTER: Optional[PostgresSaver] = None


def get_checkpointer() -> PostgresSaver:
    """获取（并按需创建）PostgresSaver 单例。

    - 首次调用：建立 PG 连接、执行 setup() 建表（幂等）；
    - 后续调用：直接返回已创建的实例，避免重复建连接；
    - 失败时：抛出 RuntimeError 并带上下文，而不是让调用方拿到半初始化对象。

    Returns:
        PostgresSaver: 已完成 setup 的 checkpointer 实例。
    """
    global _CHECKPOINTER
    if _CHECKPOINTER is None:
        try:
            ctx = PostgresSaver.from_conn_string(POSTGRES_URI)
            _CHECKPOINTER = ctx.__enter__()
            _CHECKPOINTER.setup()  # 幂等建表（checkpoints / blobs / writes）
        except Exception as e:
            _CHECKPOINTER = None
            raise RuntimeError(
                f"初始化 PostgresSaver 失败（POSTGRES_URI={POSTGRES_URI[:40]}...）: {e}"
            ) from e
    return _CHECKPOINTER


