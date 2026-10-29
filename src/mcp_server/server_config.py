import os
from pathlib import Path

# Java 后端 API 地址（ERP 业务系统，默认本机 8080）
JAVA_API_BASE_URL = os.environ.get("JAVA_API_BASE_URL", "http://localhost:8080/api")

# MCP 服务监听配置（容器化部署时需把 host 改成 0.0.0.0）
MCP_HOST = os.environ.get("MCP_HOST", "127.0.0.1")
MCP_PORT = int(os.environ.get("MCP_PORT", "8000"))
MCP_PATH = os.environ.get("MCP_PATH", "/mcp")

# ============================================================
# 写操作幂等配置（P0）
# ============================================================
# 说明：MCP Server 是独立进程，不能 import agent.config（那会拉起 langchain/opensandbox），
# 因此这里自带一份配置，取值与 agent/config.py 中的同名配置保持一致的口径。
# 幂等记录库：SQLite 文件，记录"业务请求指纹 → 首次响应"
IDEMPOTENCY_DB_PATH = os.environ.get(
    "IDEMPOTENCY_DB_PATH",
    str(Path(__file__).resolve().parent / ".state" / "idempotency.sqlite3"),
)
# 去重时间窗：同一业务请求在该窗口内重复提交只会真正执行一次（默认 24 小时）
IDEMPOTENCY_TTL_SECONDS = int(os.environ.get("IDEMPOTENCY_TTL_SECONDS", str(24 * 3600)))