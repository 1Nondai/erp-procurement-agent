"""
MCP Server 入口（FastMCP + streamable-http）。

启动：
    cd src && python -m mcp_server.server_main
    （必须在 src/ 下以模块方式启动——内部使用 mcp_server.* 绝对导入）

生命周期：
    lifespan 里 open_pool() 一次、退出时 close_pool()，
    连接池跨所有工具调用复用；不要在每个工具里新建连接。
"""

import logging
from contextlib import asynccontextmanager

from fastmcp import FastMCP

from mcp_server.db import close_pool, open_pool
from mcp_server.server_config import MCP_HOST, MCP_PATH, MCP_PORT

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(server: FastMCP):
    """启动时建池并探活，退出时关闭。

    探活放在启动阶段：数据库连不上要在启动时报错，
    而不是等第一个工具调用才 500——排障方向会完全不同。
    """
    await open_pool()
    logger.info("ERP 业务 MCP Server 已就绪")
    try:
        yield {}
    finally:
        await close_pool()


mcp = FastMCP(
    name="lihuankang-erp-mcp-server",
    instructions=(
        "明远实验装备 · ERP 采购工具集。按业务分组提供供应商、物料比价、"
        "采购订单与库存预警能力，数据源为 PostgreSQL 的 erp schema。"
    ),
    version="2.0.0",
    lifespan=lifespan,
)

# 工具模块必须在 FastMCP 实例之后再导入：@mcp.tool 装饰器依赖 mcp 对象已存在
from mcp_server.tools.suppliers_tools import register_supplier_tools  # noqa: E402
from mcp_server.tools.materials_tools import register_material_tools  # noqa: E402
from mcp_server.tools.order_tools import register_order_tools  # noqa: E402
from mcp_server.tools.inventory_tools import register_inventory_tools  # noqa: E402
from mcp_server.tools.projects_tools import register_project_tools  # noqa: E402

register_supplier_tools(mcp)
register_material_tools(mcp)
register_project_tools(mcp)
register_order_tools(mcp)
register_inventory_tools(mcp)


def main() -> None:
    mcp.run(
        transport="streamable-http",
        host=MCP_HOST,
        port=MCP_PORT,
        path=MCP_PATH,
    )
    # run() 会阻塞；lifespan 在服务关闭时自动执行清理


if __name__ == "__main__":
    main()
