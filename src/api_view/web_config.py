"""
Web 层配置：数据库连接、CORS 白名单、API 元信息。

安全变更（P0）：PG 连接串不再硬编码密码，统一从 env_utils 读取环境变量，
web_config 与 agent.config 使用同一份配置来源，杜绝两处漂移。
"""

import os

# 从统一入口读取 POSTGRES_URI（进程环境变量 > 项目根 .env > 本地默认值）
from agent.env_utils import POSTGRES_URI as _PG_URI

# ============================================================
# PostgreSQL 配置 - 用于存储 Agent 的短期记忆（checkpoint）和展示消息
# ============================================================
POSTGRES_URI = _PG_URI
POSTGRES_DB_NAME = "langgraph_db"

# ============================================================
# CORS 允许的前端来源（白名单）
# ============================================================
# 原实现 allow_origins=["*"] + allow_credentials=True 是无效组合：
# 浏览器规范规定带凭据的跨域请求不允许通配符来源，会直接拒绝。
# 这里改成显式白名单；本地开发的前端地址 + 可用环境变量扩展。
_extra_origins = os.getenv("CORS_EXTRA_ORIGINS", "")
CORS_ALLOWED_ORIGINS = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:3001",
] + [o.strip() for o in _extra_origins.split(",") if o.strip()]

# ============================================================
# 服务配置
# ============================================================
# 路径常量（沙箱内的技能/记忆/AGENTS.md 位置）统一由 agent.config 提供，
# 这里不再保留已失效的副本，避免两处漂移。
API_TITLE = "QingchiERP 采购智能助手 API"
API_VERSION = "1.1.0"
API_DESCRIPTION = (
    "擎驰机车 ERP 采购智能助手：DeepAgents 多 Agent 编排 + MCP 工具桥接 + RAG 知识检索 + "
    "Human-in-the-Loop 订单审批。前端通过 /api/chat/stream 以 SSE 接收 token、工具调用与中断事件。"
)
