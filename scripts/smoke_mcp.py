"""MCP 层端到端自检：连上真实的 MCP Server，逐个调用 7 个采购工具。

与 scripts/smoke_db.py 的分工：
    smoke_db.py  验数据层（repository + SQL + 领域规则），不需要起服务；
    smoke_mcp.py 验协议层（工具是否注册、参数能否被正确接收、返回结构是否成型）。

用法（两个终端）：
    # 终端 1
    cd src && python -m mcp_server.server_main
    # 终端 2
    python scripts/smoke_mcp.py                     # 默认连 127.0.0.1:8000/mcp
    MCP_URL=http://127.0.0.1:8000/mcp python scripts/smoke_mcp.py

退出码：0 = 全部通过；1 = 有断言失败。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid

from fastmcp import Client

MCP_URL = os.environ.get("MCP_URL", "http://127.0.0.1:8000/mcp")

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        failures.append(f"{name} {detail}")


def payload(result) -> dict:
    """从 CallToolResult 里取出结构化结果（不同版本字段名不同，逐个兜）。"""
    for attr in ("structured_content", "data"):
        value = getattr(result, attr, None)
        if isinstance(value, dict):
            return value
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"raw": text}
    return {}


async def main() -> int:
    print(f"连接 MCP Server: {MCP_URL}")
    async with Client(MCP_URL) as client:
        # ---- 1. 工具注册 ----
        tools = await client.list_tools()
        names = sorted(t.name for t in tools)
        print(f"\n1) 工具注册（{len(names)} 个）: {', '.join(names)}")
        expected = {
            "supplier_query", "material_query", "material_price_compare", "project_query",
            "order_create", "order_update", "order_search_details", "inventory_warning",
        }
        check("8 个业务工具全部注册", expected.issubset(set(names)), f"缺 {expected - set(names)}")

        # ---- 2. 只读工具 ----
        print("\n2) 只读工具")
        s = payload(await client.call_tool("supplier_query", {"limit": 3}))
        check("supplier_query 返回 ok 且非空", s.get("ok") is True and s.get("count", 0) > 0, f"{s.get('count')} 条")
        check("供应商带 QSDC 评分", bool((s.get("data") or [{}])[0].get("scores")))

        m = payload(await client.call_tool("material_query", {"name": "理化板", "size": 3}))
        check("material_query 命中理化板", m.get("total", 0) >= 1, f"total={m.get('total')}")
        check("物料带首选供应商", bool((m.get("records") or [{}])[0].get("preferredSupplierName")))

        c = payload(await client.call_tool("material_price_compare", {"material_id": 1}))
        summary = ((c.get("data") or [{}])[0]).get("summary") or {}
        check("material_price_compare 给出最低价", summary.get("lowestPrice") == 745.0, str(summary.get("lowestPrice")))
        check("比价带价差百分比", summary.get("spreadPct") is not None, f"{summary.get('spreadPct')}%")

        w = payload(await client.call_tool("inventory_warning", {"limit": 5}))
        check("inventory_warning 有预警", w.get("count", 0) >= 1, f"{w.get('count')} 条")
        check("预警带缺口与提前期", all("shortage" in d and "leadTimeDays" in d for d in w.get("data") or []))

        pj = payload(await client.call_tool("project_query", {"limit": 10}))
        check("project_query 返回 3 个工程单", pj.get("count") == 3, f"实际 {pj.get('count')}")
        check("工程单带预算占用率",
              any(x.get("budgetUsagePct") for x in pj.get("data") or []),
              str([x.get("budgetUsagePct") for x in pj.get("data") or []]))

        d = payload(await client.call_tool("order_search_details", {"project_code": "LHK-2026-001", "limit": 5}))
        check("order_search_details 按项目查得到", d.get("count", 0) >= 1, f"{d.get('count')} 行")

        # ---- 3. 写工具（含幂等与状态机）----
        print("\n3) 写工具：下单 → 审批 → 幂等复放")
        key = f"smoke-{uuid.uuid4()}"
        args = {
            "supplier_id": 1,
            "order_detail": [{"materialId": 6, "quantity": 5, "unitPrice": 165.00}],
            "expected_delivery_date": "2026-12-15",
            "remark": "MCP 自检下单",
            "idempotency_key": key,
        }
        created = payload(await client.call_tool("order_create", args))
        order = created.get("order") or {}
        check("order_create 成功", created.get("ok") is True, str(created.get("error")))
        check("总额由后台重算 = 825", order.get("totalAmount") == 825.0, str(order.get("totalAmount")))

        replay = payload(await client.call_tool("order_create", args))
        check("同参数重放命中幂等", replay.get("idempotent_replay") is True)
        check("重放返回同一个订单号", (replay.get("order") or {}).get("orderNo") == order.get("orderNo"))

        approved = payload(await client.call_tool("order_update", {"order_id": order.get("id"), "status": 2}))
        check("order_update 1→2 合法", approved.get("ok") is True, str(approved.get("error")))

        illegal = payload(await client.call_tool("order_update", {"order_id": order.get("id"), "status": 4}))
        check("order_update 跳级被拒", "error" in illegal, str(illegal.get("error"))[:60])

    print("\n" + "=" * 56)
    if failures:
        print(f"FAILED: {len(failures)} 项未通过")
        for f in failures:
            print("  -", f)
        return 1
    print("全部通过：MCP 工具契约与数据层一致")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
