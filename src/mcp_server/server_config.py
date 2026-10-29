"""MCP Server 配置。

数据源变更说明（本轮）：
    业务数据从"调 Java ERP 的 REST 接口"改为"直连 PostgreSQL 的 erp schema"。
    好处是评审者 docker compose up 就能跑通全链路，不必依赖一个他拿不到的外部
    Java 后端；若将来要接回真实 ERP，只需替换 repository 的实现，工具契约不变。
"""

import os
from pathlib import Path

# 业务库连接串（与 Agent 的 POSTGRES_URI 可以是同一个库，靠 schema 隔离）
ERP_DB_URI = os.environ.get("ERP_DB_URI") or os.environ.get("POSTGRES_URI", "")

# MCP 服务监听配置（容器化部署时把 host 改成 0.0.0.0）
MCP_HOST = os.environ.get("MCP_HOST", "127.0.0.1")
MCP_PORT = int(os.environ.get("MCP_PORT", "8000"))
MCP_PATH = os.environ.get("MCP_PATH", "/mcp")

# ============================================================
# 写操作幂等配置
# ============================================================
# 说明：MCP Server 是独立进程，不 import agent.config（那会拉起 langchain/opensandbox），
# 因此这里自带一份配置，取值口径与 agent/config.py 中的同名配置保持一致。
# 幂等记录库用 SQLite：单进程、数据量小，不想为一个计数表引入额外运维组件。
IDEMPOTENCY_DB_PATH = os.environ.get(
    "IDEMPOTENCY_DB_PATH",
    str(Path(__file__).resolve().parent / ".state" / "idempotency.sqlite3"),
)
# 去重时间窗：同一业务请求在该窗口内重复提交只会真正执行一次（默认 24 小时）
IDEMPOTENCY_TTL_SECONDS = int(os.environ.get("IDEMPOTENCY_TTL_SECONDS", str(24 * 3600)))
