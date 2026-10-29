"""
物料与比价工具（MCP）：material_query / material_price_compare。

为什么"比价"要单独做一个工具，而不是让模型拿 material_query 的结果自己算：
    比价是采购场景里最高频、也最容易算错的动作（要比的是同一个物料的多个报价，
    还要排除过期报价）。把它做成工具 = 口径只写一次，模型只负责解释结论。
"""

from __future__ import annotations

import logging
from typing import Optional

from fastmcp import FastMCP, Context

from mcp_server import repository as repo

logger = logging.getLogger(__name__)

GROUP_NAME = "material"


def register_material_tools(mcp: FastMCP) -> None:
    """注册物料分组工具。"""

    @mcp.tool(name=f"{GROUP_NAME}_query")
    async def query_materials(
        current: int = 1,
        size: int = 10,
        name: Optional[str] = None,
        category: Optional[str] = None,
        supplier_id: Optional[int] = None,
        ctx: Context = None,
    ) -> dict:
        """分页查询物料（实验台/通风柜用板材、钢材、五金、水暖、电气、通风、涂装、辅材）。

        支持按名称/规格/品牌模糊查询、按分类筛选、按供应商筛选。
        返回里带 preferredSupplier 与 quotedPrice，方便直接判断"当前走哪家"。

        Args:
            current: 页码，从 1 开始，默认 1
            size: 每页条数，默认 10，最大 100
            name: 名称/规格/品牌模糊查询，如"理化板"、"1.0mm"、"冷轧"
            category: 分类，取值：台面/钢材/板材/五金/玻璃/水暖/电气/通风/涂装/辅材
            supplier_id: 只看某供应商供的物料

        返回：{"ok": true, "total": n, "current": 1, "size": 10, "records": [...]}
        失败：{"ok": false, "error": "原因"}
        """
        try:
            return await repo.page_materials(
                current=current, size=size, name=name,
                category=category, supplier_id=supplier_id,
            )
        except Exception as exc:
            logger.exception("物料查询失败")
            return {"ok": False, "total": 0, "records": [], "error": f"物料查询失败: {exc}"}

    @mcp.tool(name=f"{GROUP_NAME}_price_compare")
    async def compare_material_prices(
        material_id: Optional[int] = None,
        material_name: Optional[str] = None,
        category: Optional[str] = None,
        limit: int = 20,
        ctx: Context = None,
    ) -> dict:
        """物料比价：同一物料在各供应商的报价、与参考价差异、最低价与价差。

        三种用法（任选其一）：
          - 指定 material_id：看这一种物料的所有报价（最常用）
          - 指定 material_name：按名称/规格模糊匹配一组物料
          - 指定 category：看整类物料（如"台面"）的报价分布

        summary 里已给出结论所需的关键数字：
          supplierCount（几家）、lowestSupplier/lowestPrice（最低是谁、多少）、
          spreadPct（最高与最低的价差百分比）、expiredQuotes（已过期报价数）。
        过期报价不参与最低价判定——拿失效价去比价会给出错误建议。

        Args:
            material_id: 物料 ID
            material_name: 物料名称或规格（模糊匹配）
            category: 物料分类
            limit: 报价行数上限，默认 20

        返回：{"ok": true, "count": n, "data": [{materialId, materialName, spec, unit,
                refPrice, quotes: [...], summary: {...}}]}
        """
        try:
            return await repo.compare_prices(
                material_id=material_id,
                material_name=material_name,
                category=category,
                limit=limit,
            )
        except Exception as exc:
            logger.exception("比价查询失败")
            return {"ok": False, "count": 0, "data": [], "error": f"比价查询失败: {exc}"}
