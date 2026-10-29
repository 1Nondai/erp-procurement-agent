"""
供应商工具（MCP）：supplier_query。

评分口径（QSDC）：
    Quality 质量 / Service 服务 / Delivery 交付 / Cost 成本，各 0-10，
    avgScore 是四维算术平均——故意不做加权：不同品类权重不同（台面看质量、
    辅材看成本），加权系数写死反而会误导决策，让模型结合场景自己判断更合理。
"""

from __future__ import annotations

import logging
from typing import Optional

from fastmcp import FastMCP, Context

from mcp_server import repository as repo

logger = logging.getLogger(__name__)

GROUP_NAME = "supplier"


def register_supplier_tools(mcp: FastMCP) -> None:
    """注册供应商分组工具。"""

    @mcp.tool(name=f"{GROUP_NAME}_query")
    async def query_suppliers(
        name: Optional[str] = None,
        category: Optional[str] = None,
        min_credit: Optional[str] = None,
        limit: int = 20,
        ctx: Context = None,
    ) -> dict:
        """按名称 / 主供品类 / 最低信用等级查询供应商，返回 QSDC 评分与账期。

        信用等级是序关系：min_credit="B" 表示只要 A、B 两级（不低于 B），
        "D" 级供应商为黑名单，下单会被拒绝。

        Args:
            name: 供应商名称模糊查询，如"宝钢"、"水暖"
            category: 主供品类，取值：台面/钢材/板材/五金/玻璃/水暖/电气/通风/涂装/辅材
            min_credit: 最低信用等级 A/B/C/D，如 "A" 只要 A 级
            limit: 返回条数上限，默认 20

        返回：{"ok": true, "count": n, "data": [{id, name, shortName, creditRating,
                avgScore, scores:{quality,service,delivery,cost}, avgDeliveryDays, paymentTerms}]}
        失败：{"ok": false, "count": 0, "data": [], "error": "原因"}
        """
        try:
            rows = await repo.search_suppliers(
                name=name, category=category, min_credit=min_credit, limit=limit
            )
            return {"ok": True, "count": len(rows), "data": rows}
        except Exception as exc:
            logger.exception("供应商查询失败")
            return {"ok": False, "count": 0, "data": [], "error": f"供应商查询失败: {exc}"}
