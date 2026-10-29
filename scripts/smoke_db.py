"""业务库自检脚本：直接调用 repository 层，验证工具背后的 SQL 都能跑通。

用途：
    - 改完 schema / seed / repository 后，一条命令确认"数据层没坏"；
    - 不依赖 Agent、不依赖 LLM，只要有 PostgreSQL 就能跑。

用法：
    docker compose up -d                     # 起 PG 并自动灌入 db/init/*.sql
    export ERP_DB_URI=postgresql://postgres:postgres@localhost:5432/langgraph_db
    python scripts/smoke_db.py

退出码：0 = 全部通过；1 = 有断言失败（可直接接 CI）。
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# 让脚本能 import src/ 下的包
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from mcp_server import repository as repo  # noqa: E402
from mcp_server.db import close_pool, open_pool  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        failures.append(f"{name} {detail}")


async def main() -> int:
    print(f"连接: {os.environ.get('ERP_DB_URI', '(默认)')}")
    await open_pool()

    print("\n1) 供应商查询")
    suppliers = await repo.search_suppliers(limit=50)
    check("供应商总数 = 12", len(suppliers) == 12, f"实际 {len(suppliers)}")
    check("按品类筛选（台面）", len(await repo.search_suppliers(category="台面")) == 3)
    check("信用等级 A 过滤", all(s["creditRating"] == "A" for s in await repo.search_suppliers(min_credit="A")))
    check("按名称模糊（宝钢）", len(await repo.search_suppliers(name="宝钢")) >= 1)
    top = suppliers[0]
    check("综合评分降序", suppliers[0]["avgScore"] >= suppliers[-1]["avgScore"],
          f"{top['shortName']}={top['avgScore']}")

    print("\n2) 物料分页查询")
    page = await repo.page_materials(current=1, size=5)
    check("总数 = 37", page["total"] == 37, f"实际 {page['total']}")
    check("单页 5 条", len(page["records"]) == 5)
    check("带首选供应商", page["records"][0]["preferredSupplierName"] is not None)
    cold = await repo.page_materials(name="冷轧")
    check("名称模糊（冷轧）命中 2 条", cold["total"] == 2, f"实际 {cold['total']}")
    by_supplier = await repo.page_materials(supplier_id=6)
    check("按供应商筛选（恒隆）", by_supplier["total"] >= 1, f"{by_supplier['total']} 条")
    # LIKE 转义：2.5mm² 里的 _ 不能当通配符
    esc = await repo.page_materials(name="2.5mm")
    check("LIKE 转义生效（2.5mm 只命中电源线）", esc["total"] == 1, f"实际 {esc['total']}")

    print("\n3) 比价")
    cmp1 = await repo.compare_prices(material_id=1)
    check("理化板有 2 家报价", cmp1["data"][0]["summary"]["supplierCount"] == 2)
    check("最低价 = 745（创星）", cmp1["data"][0]["summary"]["lowestPrice"] == 745.0,
          f"{cmp1['data'][0]['summary']}")
    cmp2 = await repo.compare_prices(material_name="环氧树脂板")
    check("环氧板最低价 = 2600", cmp2["data"][0]["summary"]["lowestPrice"] == 2600.0)
    cmp3 = await repo.compare_prices(category="台面")
    check("台面类共 5 种物料", cmp3["count"] == 5, f"实际 {cmp3['count']}")

    print("\n4) 库存预警")
    warn = await repo.stock_warnings()
    check("预警条数 = 8", warn["count"] == 8, f"实际 {warn['count']}")
    check("带缺口数量", all(w["shortage"] > 0 for w in warn["data"]))
    critical = await repo.stock_warnings(level="CRITICAL")
    check("CRITICAL 子集可用", critical["count"] >= 1, f"{critical['count']} 条")

    print("\n5) 订单查询")
    details = await repo.search_order_details(material_name="理化板")
    check("按物料查明细非空", details["count"] >= 1, f"{details['count']} 行")
    by_project = await repo.search_order_details(project_code="LHK-2026-001")
    check("按项目查明细", by_project["count"] >= 5, f"{by_project['count']} 行")
    by_date = await repo.search_order_details(start_date="2026-10-01", end_date="2026-10-31")
    check("按下单日期区间", by_date["count"] >= 1, f"{by_date['count']} 行")

    print("\n6) 下单（含总额重算与状态校验）")
    created = await repo.create_order(
        details=[{"materialId": 6, "quantity": 10, "unitPrice": 165.00},
                 {"materialId": 10, "quantity": 40, "unitPrice": 22.00}],
        supplier_id=1, project_id=1, expected_delivery_date="2026-11-30",
        created_by="smoke", remark="自检脚本创建",
    )
    check("下单成功", created.get("ok") is True, str(created.get("error")))
    order = created.get("order") or {}
    check("总额 = 10*165 + 40*22 = 2530", order.get("totalAmount") == 2530.0, str(order.get("totalAmount")))
    check("明细小计落库", [d["subtotal"] for d in order.get("details", [])] == [1650.0, 880.0])
    check("自动生成订单号", str(order.get("orderNo", "")).startswith("PO"))

    print("\n7) 领域规则（非法操作必须被拒绝）")
    bad_supplier = await repo.create_order(
        details=[{"materialId": 6, "quantity": 1, "unitPrice": 165}], supplier_id=999)
    check("不存在的供应商被拒", bad_supplier.get("ok") is False, str(bad_supplier.get("error")))
    bad_material = await repo.create_order(
        details=[{"materialId": 9999, "quantity": 1, "unitPrice": 1}], supplier_id=1)
    check("不存在的物料被拒", bad_material.get("ok") is False)
    bad_qty = await repo.create_order(
        details=[{"materialId": 6, "quantity": 0, "unitPrice": 165}], supplier_id=1)
    check("数量 <= 0 被拒", bad_qty.get("ok") is False)
    jump = await repo.update_order(order["id"], status=4)   # 1 待审核不能直接跳到已入库
    check("跳过审批的状态流转被拒", jump.get("ok") is False, str(jump.get("error")))
    ok_flow = await repo.update_order(order["id"], status=2)
    check("1 → 2 合法", ok_flow.get("ok") is True)
    after_in = await repo.update_order(order["id"], status=3)
    check("2 → 3 合法", after_in.get("ok") is True)
    done = await repo.update_order(order["id"], status=4, actual_delivery_date="2026-11-28")
    check("3 → 4 合法（终态）", done.get("ok") is True)
    frozen = await repo.update_order(order["id"], status=2)
    check("终态不可改", frozen.get("ok") is False, str(frozen.get("error")))

    print("\n8) 改单：整单替换明细并重算总额")
    upd = await repo.update_order(
        3,   # 用一张真实的历史单（PO20260802003，状态 3 采购中）
        details=[{"materialId": 13, "quantity": 300, "unitPrice": 37.50}],
        remark="自检：改数量与单价",
    )
    check("改单成功", upd.get("ok") is True, str(upd.get("error")))
    new_order = upd.get("order") or {}
    check("总额按新明细重算 = 11250", new_order.get("totalAmount") == 11250.0, str(new_order.get("totalAmount")))
    check("明细被整单替换为 1 行", len(new_order.get("details", [])) == 1)

    await close_pool()

    print("\n" + "=" * 56)
    if failures:
        print(f"FAILED: {len(failures)} 项未通过")
        for f in failures:
            print("  -", f)
        return 1
    print("全部通过：数据层行为符合预期")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
