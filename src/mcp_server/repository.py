"""
采购业务数据访问（repository 层）。

职责边界：
    这一层只做"取数 + 落库 + 领域规则校验"，不做参数解析（那是工具层的事），
    也不返回 Decimal / datetime 这类无法 JSON 序列化的对象（在边界统一转成
    float / ISO 字符串，MCP 传输和 LLM 都更好消费）。

领域规则（不是数据库约束，写在这里更清楚）：
    - 订单状态只能按 1→2→3→4 前进，1/2 允许取消（→5），终态不可再改；
    - 已入库（4）的订单不允许再改明细或金额——真实业务里改金额必须走退货重开；
    - 明细小计与订单总额一律由本层用 Decimal 计算后落库，不接受调用方传入的
      "自己算好的总额"，否则库里迟早出现对不上的账。
"""

from __future__ import annotations

import logging
import random
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from mcp_server.db import fetch_all, fetch_one, transaction

logger = logging.getLogger(__name__)

# 订单状态：值 → 文案（与前端/提示词共用同一份口径）
STATUS_TEXT = {1: "待审核", 2: "已审核", 3: "采购中", 4: "已入库", 5: "已取消"}

# 合法状态流转：1 待审核 → 2 已审核 / 5 取消；2 → 3 / 5；3 → 4；4、5 为终态
STATUS_FLOW: Dict[int, set[int]] = {1: {2, 5}, 2: {3, 5}, 3: {4}, 4: set(), 5: set()}


# ============================================================
# 边界转换
# ============================================================

def _num(value: Any) -> Optional[float]:
    """Decimal/int/str → float（None 原样返回）。"""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _iso(value: Any) -> Optional[str]:
    """date/datetime → ISO 字符串。"""
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _like(keyword: str) -> str:
    """把用户关键词转成安全的 LIKE 模式。

    必须转义 % 与 _：搜索"2.5mm² 电源线"时，未转义的 _ 会把任意字符都匹配上，
    返回一堆不相关的结果——这在物料检索里非常常见。
    """
    escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _order_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """订单主表行 → 对外的 dict。"""
    return {
        "id": row["id"],
        "orderNo": row["order_no"],
        "supplierId": row["supplier_id"],
        "supplierName": row.get("supplier_name"),
        "projectCode": row.get("project_code"),
        "projectName": row.get("project_name"),
        "status": row["status"],
        "statusText": STATUS_TEXT.get(row["status"], "未知"),
        "totalAmount": _num(row["total_amount"]),
        "orderTime": _iso(row["order_time"]),
        "expectedDeliveryDate": _iso(row.get("expected_delivery_date")),
        "actualDeliveryDate": _iso(row.get("actual_delivery_date")),
        "createdBy": row.get("created_by"),
        "remark": row.get("remark"),
    }


# ============================================================
# 供应商
# ============================================================

async def search_suppliers(
    name: Optional[str] = None,
    category: Optional[str] = None,
    min_credit: Optional[str] = None,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """按名称 / 主供品类 / 最低信用等级查供应商，按综合评分降序。"""
    sql = [
        """
        SELECT id, code, name, short_name, category, contact, phone, email, address,
               quality_score, service_score, delivery_score, cost_score, credit_rating,
               avg_delivery_days, payment_terms,
               ROUND((quality_score + service_score + delivery_score + cost_score) / 4, 2) AS avg_score
          FROM erp.supplier
         WHERE status = 1
        """
    ]
    params: List[Any] = []
    if name:
        sql.append(" AND name ILIKE %s")
        params.append(_like(name))
    if category:
        sql.append(" AND category = %s")
        params.append(category)
    if min_credit:
        # 'A' 表示只要 A；'B' 表示 A 或 B（等级是序关系，用 IN 表达"不低于"）
        allowed = [c for c in ("A", "B", "C", "D") if c <= min_credit.upper() and c in ("A", "B", "C", "D")]
        if allowed:
            sql.append(" AND credit_rating = ANY(%s)")
            params.append(allowed)
    sql.append(" ORDER BY avg_score DESC, id ASC LIMIT %s")
    params.append(max(1, min(int(limit), 100)))

    rows = await fetch_all("".join(sql), params)
    return [
        {
            "id": r["id"],
            "code": r["code"],
            "name": r["name"],
            "shortName": r["short_name"],
            "category": r["category"],
            "contact": r["contact"],
            "phone": r["phone"],
            "creditRating": r["credit_rating"],
            "avgScore": _num(r["avg_score"]),
            "scores": {
                "quality": _num(r["quality_score"]),
                "service": _num(r["service_score"]),
                "delivery": _num(r["delivery_score"]),
                "cost": _num(r["cost_score"]),
            },
            "avgDeliveryDays": r["avg_delivery_days"],
            "paymentTerms": r["payment_terms"],
        }
        for r in rows
    ]


# ============================================================
# 物料
# ============================================================

async def page_materials(
    current: int = 1,
    size: int = 10,
    name: Optional[str] = None,
    category: Optional[str] = None,
    supplier_id: Optional[int] = None,
) -> Dict[str, Any]:
    """分页查询物料，附带首选供应商与该供应商报价。"""
    current = max(1, int(current))
    size = max(1, min(int(size), 100))

    where = ["m.status = 1"]
    params: List[Any] = []
    if name:
        where.append("(m.name ILIKE %s OR m.spec ILIKE %s OR m.brand ILIKE %s)")
        pattern = _like(name)
        params.extend([pattern, pattern, pattern])
    if category:
        where.append("m.category = %s")
        params.append(category)
    if supplier_id:
        where.append("EXISTS (SELECT 1 FROM erp.material_supplier ms WHERE ms.material_id = m.id AND ms.supplier_id = %s)")
        params.append(int(supplier_id))

    where_sql = " WHERE " + " AND ".join(where)
    total_row = await fetch_one(f"SELECT count(*) AS n FROM erp.material m{where_sql}", params)
    total = int(total_row["n"]) if total_row else 0

    rows = await fetch_all(
        f"""
        SELECT m.id, m.code, m.name, m.spec, m.category, m.unit, m.brand,
               m.ref_price, m.current_stock, m.safety_stock, m.lead_time_days,
               s.id AS supplier_id, s.short_name AS supplier_name, ms.quoted_price
          FROM erp.material m
          LEFT JOIN LATERAL (
                SELECT ms.supplier_id, ms.quoted_price
                  FROM erp.material_supplier ms
                 WHERE ms.material_id = m.id
                 ORDER BY ms.is_preferred DESC, ms.quoted_price ASC
                 LIMIT 1
          ) ms ON true
          LEFT JOIN erp.supplier s ON s.id = ms.supplier_id
         {where_sql}
         ORDER BY m.id
         LIMIT %s OFFSET %s
        """,
        params + [size, (current - 1) * size],
    )

    return {
        "ok": True,
        "total": total,
        "current": current,
        "size": size,
        "records": [
            {
                "id": r["id"],
                "code": r["code"],
                "name": r["name"],
                "spec": r["spec"],
                "category": r["category"],
                "unit": r["unit"],
                "brand": r["brand"],
                "refPrice": _num(r["ref_price"]),
                "quotedPrice": _num(r["quoted_price"]),
                "currentStock": _num(r["current_stock"]),
                "safetyStock": _num(r["safety_stock"]),
                "leadTimeDays": r["lead_time_days"],
                "preferredSupplierId": r["supplier_id"],
                "preferredSupplierName": r["supplier_name"],
            }
            for r in rows
        ],
    }


async def compare_prices(
    material_id: Optional[int] = None,
    material_name: Optional[str] = None,
    category: Optional[str] = None,
    limit: int = 20,
) -> Dict[str, Any]:
    """比价：同一物料在各供应商的报价、与参考价的差异、以及最低价/价差。

    返回里带 summary，是因为模型做"选哪家"的决策时最需要的就是
    "有几家、最低是谁、价差多少"，而不是让它自己从 20 行里心算。
    """
    where = ["1 = 1"]
    params: List[Any] = []
    if material_id:
        where.append("material_id = %s")
        params.append(int(material_id))
    if material_name:
        where.append("(material_name ILIKE %s OR spec ILIKE %s)")
        params.extend([_like(material_name), _like(material_name)])
    if category:
        where.append("material_id IN (SELECT id FROM erp.material WHERE category = %s)")
        params.append(category)

    rows = await fetch_all(
        f"""
        SELECT * FROM erp.v_material_price_compare
         WHERE {' AND '.join(where)}
         ORDER BY material_id, quoted_price ASC
         LIMIT %s
        """,
        params + [max(1, min(int(limit), 200))],
    )
    if not rows:
        return {"ok": True, "count": 0, "data": [], "summary": None}

    grouped: Dict[int, Dict[str, Any]] = {}
    for r in rows:
        g = grouped.setdefault(
            r["material_id"],
            {
                "materialId": r["material_id"],
                "code": r["code"],
                "materialName": r["material_name"],
                "spec": r["spec"],
                "unit": r["unit"],
                "refPrice": _num(r["ref_price"]),
                "quotes": [],
            },
        )
        g["quotes"].append(
            {
                "supplierId": r["supplier_id"],
                "supplierName": r["supplier_name"],
                "creditRating": r["credit_rating"],
                "quotedPrice": _num(r["quoted_price"]),
                "moq": _num(r["moq"]),
                "priceDate": _iso(r["price_date"]),
                "validUntil": _iso(r["valid_until"]),
                "isPreferred": r["is_preferred"],
                "isExpired": r["is_expired"],
                "diffToRef": _num(r["diff_to_ref"]),
                "diffPct": _num(r["diff_pct"]),
            }
        )

    data = []
    for g in grouped.values():
        # 过期报价不参与"最低价"判定：拿一个失效价去比价会给出错误建议
        effective = [q for q in g["quotes"] if not q["isExpired"]] or g["quotes"]
        lowest = min(effective, key=lambda q: q["quotedPrice"])
        highest = max(effective, key=lambda q: q["quotedPrice"])
        spread = round((highest["quotedPrice"] - lowest["quotedPrice"]) / lowest["quotedPrice"] * 100, 2) if lowest["quotedPrice"] else None
        g["summary"] = {
            "supplierCount": len(g["quotes"]),
            "lowestSupplier": lowest["supplierName"],
            "lowestPrice": lowest["quotedPrice"],
            "highestSupplier": highest["supplierName"],
            "highestPrice": highest["quotedPrice"],
            "spreadPct": spread,
            "expiredQuotes": sum(1 for q in g["quotes"] if q["isExpired"]),
            "recommendation": (
                f"{lowest['supplierName']} 报价最低（{lowest['quotedPrice']}），"
                f"较最高价低 {spread}%，起订量 {lowest['moq']:g}"
            ),
        }
        data.append(g)

    return {"ok": True, "count": len(data), "data": data}


# ============================================================
# 订单
# ============================================================

async def _load_order(order_id: int) -> Optional[Dict[str, Any]]:
    head = await fetch_one(
        """
        SELECT o.*, s.short_name AS supplier_name, p.code AS project_code, p.name AS project_name
          FROM erp.purchase_order o
          JOIN erp.supplier s ON s.id = o.supplier_id
          LEFT JOIN erp.project p ON p.id = o.project_id
         WHERE o.id = %s
        """,
        [order_id],
    )
    if head is None:
        return None
    details = await fetch_all(
        """
        SELECT d.*, m.code AS material_code, m.name AS material_name, m.spec, m.unit
          FROM erp.purchase_order_detail d
          JOIN erp.material m ON m.id = d.material_id
         WHERE d.order_id = %s
         ORDER BY d.id
        """,
        [order_id],
    )
    result = _order_row(head)
    result["details"] = [
        {
            "materialId": d["material_id"],
            "materialCode": d["material_code"],
            "materialName": d["material_name"],
            "spec": d["spec"],
            "unit": d["unit"],
            "quantity": _num(d["quantity"]),
            "unitPrice": _num(d["unit_price"]),
            "subtotal": _num(d["subtotal"]),
            "remark": d["remark"],
        }
        for d in details
    ]
    return result


def _gen_order_no() -> str:
    """订单号：PO + yyyymmdd + 3 位随机数（唯一性由 DB 的 UNIQUE 兜底，冲突重试）。"""
    return f"PO{datetime.now():%Y%m%d}{random.randint(0, 999):03d}"


async def _material_exists(material_id: int) -> bool:
    row = await fetch_one("SELECT 1 AS ok FROM erp.material WHERE id = %s AND status = 1", [material_id])
    return row is not None


async def create_order(
    details: List[Dict[str, Any]],
    supplier_id: int,
    project_id: Optional[int] = None,
    expected_delivery_date: Optional[str] = None,
    status: int = 1,
    order_no: Optional[str] = None,
    created_by: str = "agent",
    remark: Optional[str] = None,
) -> Dict[str, Any]:
    """创建采购订单（主表 + 明细，单事务）。

    Returns:
        成功 → {"ok": True, "order": {...}}
        失败 → {"ok": False, "error": "人话原因"}
    """
    if not details:
        return {"ok": False, "error": "订单明细为空：至少需要一条物料"}
    if status not in STATUS_TEXT:
        return {"ok": False, "error": f"非法状态 {status}，取值范围 1-5"}

    supplier = await fetch_one(
        "SELECT id, short_name, credit_rating, status FROM erp.supplier WHERE id = %s", [supplier_id]
    )
    if supplier is None:
        return {"ok": False, "error": f"供应商 {supplier_id} 不存在"}
    if supplier["status"] != 1:
        return {"ok": False, "error": f"供应商 {supplier['short_name']} 已停用，不能下单"}
    if supplier["credit_rating"] == "D":
        return {"ok": False, "error": f"供应商 {supplier['short_name']} 为黑名单（D 级），禁止下单"}

    # 明细校验：物料必须存在、数量与单价合法（不信任调用方传入的小计/总额）
    normalized: List[Dict[str, Any]] = []
    total = Decimal("0")
    for item in details:
        try:
            material_id = int(item.get("materialId") or item.get("material_id") or item.get("partId"))
        except (TypeError, ValueError):
            return {"ok": False, "error": f"明细缺少合法的 materialId: {item}"}
        if not await _material_exists(material_id):
            return {"ok": False, "error": f"物料 {material_id} 不存在或已停用"}
        quantity = Decimal(str(item.get("quantity")))
        unit_price = Decimal(str(item.get("unitPrice") or item.get("unit_price")))
        if quantity <= 0:
            return {"ok": False, "error": f"物料 {material_id} 数量必须大于 0"}
        if unit_price < 0:
            return {"ok": False, "error": f"物料 {material_id} 单价不能为负"}
        subtotal = (quantity * unit_price).quantize(Decimal("0.01"))
        total += subtotal
        normalized.append(
            {
                "material_id": material_id,
                "quantity": quantity,
                "unit_price": unit_price,
                "subtotal": subtotal,
                "remark": item.get("remark"),
            }
        )

    async with transaction() as conn:
        async with conn.cursor() as cur:
            order_id = None
            # 订单号冲突时重试（3 位随机数，同一天并发下单才有可能撞）
            for _ in range(5):
                candidate = order_no or _gen_order_no()
                await cur.execute(
                    """
                    INSERT INTO erp.purchase_order
                        (order_no, supplier_id, project_id, status, total_amount,
                         order_time, expected_delivery_date, created_by, remark)
                    VALUES (%s, %s, %s, %s, %s, now(), %s, %s, %s)
                    ON CONFLICT (order_no) DO NOTHING
                    RETURNING id
                    """,
                    (candidate, supplier_id, project_id, status, total,
                     expected_delivery_date, created_by, remark),
                )
                row = await cur.fetchone()
                if row is not None:
                    order_id = row["id"]
                    break
                if order_no:      # 调用方指定了单号还冲突 → 是真实错误，不再重试
                    return {"ok": False, "error": f"订单号 {candidate} 已存在"}
            if order_id is None:
                return {"ok": False, "error": "生成订单号失败，请重试"}

            for item in normalized:
                await cur.execute(
                    """
                    INSERT INTO erp.purchase_order_detail
                        (order_id, material_id, quantity, unit_price, subtotal, remark)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (order_id, item["material_id"], item["quantity"],
                     item["unit_price"], item["subtotal"], item["remark"]),
                )

    order = await _load_order(order_id)
    logger.info("订单已创建: %s（%s 项明细，合计 %s）", order["orderNo"], len(normalized), total)
    return {"ok": True, "order": order}


async def update_order(
    order_id: int,
    details: Optional[List[Dict[str, Any]]] = None,
    supplier_id: Optional[int] = None,
    project_id: Optional[int] = None,
    status: Optional[int] = None,
    expected_delivery_date: Optional[str] = None,
    actual_delivery_date: Optional[str] = None,
    remark: Optional[str] = None,
) -> Dict[str, Any]:
    """修改订单（最小变更原则：只落传进来的字段）。

    领域规则：
        - 状态只能按 STATUS_FLOW 前进，不能回退；
        - 已入库（4）/已取消（5）不允许再改明细或金额；
        - 传入明细则整单替换（原明细删除），小计与总额重算。
    """
    current = await fetch_one(
        "SELECT id, status, order_no FROM erp.purchase_order WHERE id = %s", [order_id]
    )
    if current is None:
        return {"ok": False, "error": f"订单 {order_id} 不存在"}

    cur_status = current["status"]
    if cur_status in (4, 5):
        return {
            "ok": False,
            "error": f"订单 {current['order_no']} 当前为「{STATUS_TEXT[cur_status]}」，"
                     f"终态订单不可修改（已入库需走退货重开，已取消需重新下单）",
        }
    if status is not None and status != cur_status:
        if status not in STATUS_FLOW.get(cur_status, set()):
            allowed = "、".join(f"{s}({STATUS_TEXT[s]})" for s in sorted(STATUS_FLOW.get(cur_status, set()))) or "无"
            return {
                "ok": False,
                "error": f"状态不能从 {cur_status}({STATUS_TEXT[cur_status]}) 变为 {status}"
                         f"({STATUS_TEXT.get(status, '未知')})，允许流转：{allowed}",
            }

    sets: List[str] = []
    params: List[Any] = []
    if supplier_id is not None:
        sets.append("supplier_id = %s")
        params.append(supplier_id)
    if project_id is not None:
        sets.append("project_id = %s")
        params.append(project_id)
    if status is not None:
        sets.append("status = %s")
        params.append(status)
    if expected_delivery_date is not None:
        sets.append("expected_delivery_date = %s")
        params.append(expected_delivery_date)
    if actual_delivery_date is not None:
        sets.append("actual_delivery_date = %s")
        params.append(actual_delivery_date)
    if remark is not None:
        sets.append("remark = %s")
        params.append(remark)

    total: Optional[Decimal] = None
    normalized: List[Dict[str, Any]] = []
    if details is not None:
        if not details:
            return {"ok": False, "error": "明细不能为空；如需取消订单请把 status 置为 5"}
        total = Decimal("0")
        for item in details:
            try:
                material_id = int(item.get("materialId") or item.get("material_id") or item.get("partId"))
            except (TypeError, ValueError):
                return {"ok": False, "error": f"明细缺少合法的 materialId: {item}"}
            if not await _material_exists(material_id):
                return {"ok": False, "error": f"物料 {material_id} 不存在或已停用"}
            quantity = Decimal(str(item.get("quantity")))
            unit_price = Decimal(str(item.get("unitPrice") or item.get("unit_price")))
            if quantity <= 0 or unit_price < 0:
                return {"ok": False, "error": f"物料 {material_id} 的数量或单价不合法"}
            subtotal = (quantity * unit_price).quantize(Decimal("0.01"))
            total += subtotal
            normalized.append(
                {"material_id": material_id, "quantity": quantity,
                 "unit_price": unit_price, "subtotal": subtotal, "remark": item.get("remark")}
            )
        sets.append("total_amount = %s")
        params.append(total)

    if not sets:
        return {"ok": False, "error": "没有需要修改的字段"}

    async with transaction() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"UPDATE erp.purchase_order SET {', '.join(sets)} WHERE id = %s", params + [order_id]
            )
            if normalized:
                await cur.execute("DELETE FROM erp.purchase_order_detail WHERE order_id = %s", [order_id])
                for item in normalized:
                    await cur.execute(
                        """
                        INSERT INTO erp.purchase_order_detail
                            (order_id, material_id, quantity, unit_price, subtotal, remark)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        (order_id, item["material_id"], item["quantity"],
                         item["unit_price"], item["subtotal"], item["remark"]),
                    )

    order = await _load_order(order_id)
    logger.info("订单已更新: %s", order["orderNo"])
    return {"ok": True, "order": order}


async def search_order_details(
    material_name: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    supplier_id: Optional[int] = None,
    project_code: Optional[str] = None,
    limit: int = 50,
) -> Dict[str, Any]:
    """按物料名称 / 下单日期区间 / 供应商 / 项目查订单明细。"""
    where = ["1 = 1"]
    params: List[Any] = []
    if material_name:
        where.append("(m.name ILIKE %s OR m.spec ILIKE %s)")
        params.extend([_like(material_name), _like(material_name)])
    if start_date:
        where.append("o.order_time >= %s::date")
        params.append(start_date)
    if end_date:
        # 结束日期按"当天含"处理：+1 天再取小于，避免漏掉最后一天 18:00 的单
        where.append("o.order_time < (%s::date + interval '1 day')")
        params.append(end_date)
    if supplier_id:
        where.append("o.supplier_id = %s")
        params.append(int(supplier_id))
    if project_code:
        where.append("p.code = %s")
        params.append(project_code)

    rows = await fetch_all(
        f"""
        SELECT o.id AS order_id, o.order_no, o.status, o.order_time,
               s.short_name AS supplier_name, p.code AS project_code,
               m.code AS material_code, m.name AS material_name, m.spec, m.unit,
               d.quantity, d.unit_price, d.subtotal
          FROM erp.purchase_order_detail d
          JOIN erp.purchase_order o ON o.id = d.order_id
          JOIN erp.supplier s       ON s.id = o.supplier_id
          JOIN erp.material m       ON m.id = d.material_id
          LEFT JOIN erp.project p   ON p.id = o.project_id
         WHERE {' AND '.join(where)}
         ORDER BY o.order_time DESC, d.id
         LIMIT %s
        """,
        params + [max(1, min(int(limit), 500))],
    )
    data = [
        {
            "orderId": r["order_id"],
            "orderNo": r["order_no"],
            "status": r["status"],
            "statusText": STATUS_TEXT.get(r["status"], "未知"),
            "orderTime": _iso(r["order_time"]),
            "supplierName": r["supplier_name"],
            "projectCode": r["project_code"],
            "materialCode": r["material_code"],
            "materialName": r["material_name"],
            "spec": r["spec"],
            "unit": r["unit"],
            "quantity": _num(r["quantity"]),
            "unitPrice": _num(r["unit_price"]),
            "subtotal": _num(r["subtotal"]),
        }
        for r in rows
    ]
    return {"ok": True, "count": len(data), "data": data}


# ============================================================
# 库存
# ============================================================

async def stock_warnings(level: Optional[str] = None, limit: int = 50) -> Dict[str, Any]:
    """库存预警：低于安全库存的物料（按缺口从大到小）。"""
    sql = "SELECT * FROM erp.v_stock_warning"
    params: List[Any] = []
    if level:
        sql += " WHERE warning_level = %s"
        params.append(level.upper())
    sql += " LIMIT %s"
    params.append(max(1, min(int(limit), 200)))

    rows = await fetch_all(sql, params)
    data = [
        {
            "materialId": r["material_id"],
            "code": r["code"],
            "materialName": r["material_name"],
            "spec": r["spec"],
            "category": r["category"],
            "unit": r["unit"],
            "currentStock": _num(r["current_stock"]),
            "safetyStock": _num(r["safety_stock"]),
            "shortage": _num(r["shortage"]),
            "stockRatio": _num(r["stock_ratio"]),
            "leadTimeDays": r["lead_time_days"],
            "warningLevel": r["warning_level"],
        }
        for r in rows
    ]
    return {"ok": True, "count": len(data), "data": data}


async def material_categories() -> List[str]:
    """物料大类（给提示词/前端做筛选用，避免模型自己编分类名）。"""
    rows = await fetch_all("SELECT DISTINCT category FROM erp.material WHERE status = 1 ORDER BY category")
    return [r["category"] for r in rows]


# ============================================================
# 工程单（项目）
# ============================================================

async def list_projects(
    project_code: Optional[str] = None,
    status: Optional[int] = None,
    limit: int = 20,
) -> Dict[str, Any]:
    """查工程单及其采购情况：订单数、采购额、预算占用率、未交/已入库单数。

    为什么把"预算占用率"放在 SQL 里算而不是让模型算：
        它 = 采购额 / 合同额，两个数都可能为 NULL（未签合同 / 还没采购）。
        模型做除法时最常犯的错是把 NULL 当 0，算出"预算占用 0%"这种误导性结论。
        在 SQL 里用 NULLIF 显式处理，返回 None 让调用方自己决定怎么表达。

    为什么排除取消单（status = 5）：
        业务口径上取消单不计入采购额，否则预算占用率会虚高，导致误判"这个项目没钱了"。
    """
    where = ["1 = 1"]
    params: List[Any] = []
    if project_code:
        where.append("p.code = %s")
        params.append(project_code)
    if status is not None:
        where.append("p.status = %s")
        params.append(int(status))

    rows = await fetch_all(
        f"""
        SELECT p.id, p.code, p.name, p.customer, p.project_type,
               p.contract_amount, p.status, p.start_date, p.due_date,
               COUNT(o.id) FILTER (WHERE o.status <> 5)              AS order_count,
               COUNT(o.id) FILTER (WHERE o.status IN (2, 3))        AS pending_orders,
               COUNT(o.id) FILTER (WHERE o.status = 4)              AS delivered_orders,
               COALESCE(SUM(o.total_amount) FILTER (WHERE o.status <> 5), 0) AS purchased_amount
          FROM erp.project p
          LEFT JOIN erp.purchase_order o ON o.project_id = p.id
         WHERE {' AND '.join(where)}
         GROUP BY p.id
         ORDER BY p.id
         LIMIT %s
        """,
        params + [max(1, min(int(limit), 100))],
    )

    data = []
    for r in rows:
        contract = _num(r["contract_amount"])
        purchased = _num(r["purchased_amount"]) or 0.0
        usage = round(purchased / contract * 100, 2) if contract else None
        data.append(
            {
                "projectId": r["id"],
                "code": r["code"],
                "name": r["name"],
                "customer": r["customer"],
                "projectType": r["project_type"],
                "contractAmount": contract,
                "purchasedAmount": purchased,
                "budgetUsagePct": usage,
                "status": r["status"],
                "statusText": {1: "进行中", 2: "已交付", 3: "已关闭"}.get(r["status"], "未知"),
                "orderCount": r["order_count"],
                "pendingOrders": r["pending_orders"],
                "deliveredOrders": r["delivered_orders"],
                "startDate": _iso(r["start_date"]),
                "dueDate": _iso(r["due_date"]),
            }
        )
    return {"ok": True, "count": len(data), "data": data}
