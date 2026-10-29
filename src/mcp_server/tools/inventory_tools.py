"""
库存工具（MCP）：inventory_warning。

预警口径固化在数据库视图 erp.v_stock_warning 里（见 db/init/01_schema.sql）：
    OUT_OF_STOCK 库存 <= 0
    CRITICAL     低于安全库存的 50%
    LOW          低于安全库存
这样 MCP、报表、前端如果将来各自取数，用的是同一份判定，不会出现
"Agent 说缺货、报表说不缺"的双口径问题。
"""

from __future__ import annotations

import logging
from typing import Optional

from fastmcp import FastMCP, Context

from mcp_server import repository as repo

logger = logging.getLogger(__name__)

GROUP_NAME = "inventory"


def register_inventory_tools(mcp: FastMCP) -> None:
    """注册库存分组工具。"""

    @mcp.tool(name=f"{GROUP_NAME}_warning")
    async def list_inventory_warnings(
        level: Optional[str] = None,
        limit: int = 50,
        ctx: Context = None,
    ) -> dict:
        """查询库存预警：当前库存低于安全库存的物料，按缺口从大到小排序。

        返回里带 shortage（缺口数量）与 leadTimeDays（采购提前期），
        这两个字段决定"先补哪个"：缺口大且提前期长的应该最优先。

        Args:
            level: 只看某一级预警：OUT_OF_STOCK / CRITICAL / LOW，不传则全部
            limit: 返回条数上限，默认 50

        返回：{"ok": true, "count": n, "data": [{materialId, code, materialName, spec,
                category, unit, currentStock, safetyStock, shortage, stockRatio,
                leadTimeDays, warningLevel}]}
        """
        try:
            return await repo.stock_warnings(level=level, limit=limit)
        except Exception as exc:
            logger.exception("库存预警查询失败")
            return {"ok": False, "count": 0, "data": [], "error": f"库存预警查询失败: {exc}"}
