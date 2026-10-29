"""
DeepAgent Chat API - FastAPI 主应用

提供基于 DeepAgent 的 AI 对话系统后端 API。

启动链路（理解本文件即可理解整个后端的启动过程）：
    1. uvicorn 加载本模块 → 执行模块级代码（路由注册、CORS 配置）；
    2. lifespan 启动 → agent_loader.initialize()：
       a. 建立异步连接池并建表（agent_loader.get_pg_pool，幂等且仅一次）；
       b. 懒加载主 Agent（main_agent.get_agent_async → create_main_agent）：
          沙箱创建 → AGENTS.md 上传 → MCP 工具加载 → 子 Agent 解析 →
          中间件栈组装 → create_deep_agent 编译（checkpointer 此时才连 PG）；
    3. /api/chat/stream 收到请求 → SSE 流式返回 token / 工具事件 / interrupt。

耗时说明：Agent 初始化 30-60 秒（沙箱创建与依赖安装占大头），
因此 lifespan 里同步完成，避免首个请求承担冷启动。
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api_view.web_config import API_TITLE, API_VERSION, API_DESCRIPTION, CORS_ALLOWED_ORIGINS
from api_view.api import chat, history
from api_view.agent_loader import agent_loader

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    应用生命周期管理

    在应用启动时初始化 Agent，在应用关闭时清理资源
    """
    # ============================================================
    # 应用启动时执行
    # ============================================================
    logger.info("正在启动 %s v%s ...", API_TITLE, API_VERSION)

    # 初始化 Agent（沙箱创建 + 工具加载，冷启动 30-60s）
    await agent_loader.initialize()

    logger.info("%s 启动成功，监听 8090", API_TITLE)

    yield

    # ============================================================
    # 应用关闭时执行
    # ============================================================
    logger.info("正在关闭 %s ...", API_TITLE)

    # 清理所有沙箱（含预热池）并关闭沙箱注册表连接。
    # 不清理的后果：docker 里持续残留容器，几轮重启后把机器内存吃光。
    try:
        await agent_loader.shutdown()
    except Exception:  # noqa: BLE001
        logger.warning("关闭时清理沙箱失败", exc_info=True)


# 创建 FastAPI 应用
app = FastAPI(
    title=API_TITLE,
    description=API_DESCRIPTION,
    version=API_VERSION,
    lifespan=lifespan,
)

# ============================================================
# CORS 中间件配置
# ============================================================
# 白名单来源（见 web_config.CORS_ALLOWED_ORIGINS）。
# 之前的 allow_origins=["*"] 与 allow_credentials=True 组合会被浏览器拒绝，
# 且通配符等于对所有站点开放跨域调用；改成显式列表后两处问题同时解决。
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================
# 注册路由
# ============================================================
# 对话相关接口
app.include_router(chat.router, prefix="/api", tags=["对话"])
# 历史记录相关接口
app.include_router(history.router, prefix="/api", tags=["历史记录"])


# ============================================================
# 根路径
# ============================================================
@app.get("/", tags=["首页"])
async def root():
    """
    根路径

    返回 API 基本信息
    """
    return {
        "name": API_TITLE,
        "version": API_VERSION,
        "description": API_DESCRIPTION,
        "docs": "/docs",
        "redoc": "/redoc"
    }


# ============================================================
# 健康检查
# ============================================================
@app.get("/health", tags=["系统"])
async def health_check():
    """
    健康检查接口

    用于检查服务是否正常运行
    """
    return {
        "status": "healthy",
        "service": API_TITLE,
        "version": API_VERSION
    }


# ============================================================
# 启动命令
# ============================================================
# 推荐：python start_web.py
# 或单起后端（需先把 src 加入 PYTHONPATH）：
# uvicorn api_view.web_main:app --reload --host 0.0.0.0 --port 8090
