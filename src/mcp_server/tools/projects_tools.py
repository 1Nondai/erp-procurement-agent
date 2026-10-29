"""
工程单工具（MCP）：project_query。

为什么采购 Agent 需要"项目"这个维度：
    实验台厂商是按工程单交付的（疾控中心改造、高校实验台、药企通风柜……）。
    采购员最常被问的两句话是"这个项目料钱花了多少"和"还剩多少预算"，
    而这两个答案跨了 project / purchase_order / purchase_order_detail 三张表，
    让模型自己拼 SQL 不现实，所以做成工具。
"""

from __future__ import annotations

import logging
from typing import Optional

from fastmcp import FastMCP, Context

from mcp_server import repository as repo

logger = logging.getLogger(__name__)

GROUP_NAME = "project"


def register_project_tools(mcp: FastMCP) -> None:
    """注册工程单分组工具。"""

    @mcp.tool(name=f"{GROUP_NAME}_query")
    async def query_projects(
        project_code: Optional[str] = None,
        status: Optional[int] = None,
        limit: int = 20,
        ctx: Context = None,
    ) -> dict:
        """查询工程单及其采购情况：订单数、采购额、预算占用率、未交/已入库单数。

        典型用法：
          - "这个项目料钱花了多少" → project_code="LHK-2026-001"，看 purchasedAmount
          - "哪个项目预算快超了"   → 不传参数，按 budgetUsagePct 从高到低看
          - "还在进行中的项目有哪些" → status=1

        口径说明（回答用户时要带上，否则数字会被误读）：
          - 采购额**不含已取消订单**（status=5），否则预算占用率会虚高；
          - budgetUsagePct 为 None 表示合同额未填，不是"占用 0%"；
          - 未交订单 = 已审核(2) + 采购中(3)，已入库 = 4。

        Args:
            project_code: 工程单编号，如 LHK-2026-001；不传则返回全部
            status: 项目状态 1 进行中 / 2 已交付 / 3 已关闭
            limit: 返回条数上限，默认 20

        返回：{"ok": true, "count": n, "data": [{projectId, code, name, customer,
                projectType, contractAmount, purchasedAmount, budgetUsagePct,
                status, statusText, orderCount, pendingOrders, deliveredOrders,
                startDate, dueDate}]}
        失败：{"ok": false, "count": 0, "data": [], "error": "原因"}
        """
        try:
            return await repo.list_projects(project_code=project_code, status=status, limit=limit)
        except Exception as exc:
            logger.exception("工程单查询失败")
            return {"ok": False, "count": 0, "data": [], "error": f"工程单查询失败: {exc}"}
