"""
采购订单工具（MCP）：order_create / order_update / order_search_details。

设计要点（面试可直接讲的部分）：
    1. 幂等在工具层，不在数据库层：MCP 是 Agent 唯一能触达的写入口，
       把"业务指纹 → 首次响应"缓存放这里，Agent 重试、用户连点审批、
       SSE 断线重连都不会重复下单；数据库层保持纯粹。
    2. 指纹剔除易变字段：orderNumber / orderTime 每次调用都会变，
       不剔除的话同一笔业务请求每次指纹都不同，去重形同虚设。
    3. 抢占而非仅查询：get → 写库 → put 之间无互斥，两个并发同指纹请求会双双
       miss。claim() 用 pending 占位表的 PRIMARY KEY 冲突做原子抢占，
       抢不到就直接返回"处理中"，绝不再打一次库。
    4. 总额由 repository 用 Decimal 重算：不接受调用方自算的总额，
       避免"库里金额和明细对不上"这种最难查的账。
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List, Optional

from fastmcp import FastMCP, Context

from mcp_server import repository as repo
from mcp_server.idempotency import IdempotencyStore, compute_fingerprint
from mcp_server.server_config import IDEMPOTENCY_DB_PATH, IDEMPOTENCY_TTL_SECONDS

logger = logging.getLogger(__name__)

GROUP_NAME = "order"

# 幂等存储惰性单例：import 本模块不产生磁盘/连接副作用，便于单测直接导入
_store_instance: Optional[IdempotencyStore] = None
_store_lock = threading.Lock()


def _store() -> IdempotencyStore:
    global _store_instance
    if _store_instance is None:
        with _store_lock:
            if _store_instance is None:
                _store_instance = IdempotencyStore(IDEMPOTENCY_DB_PATH, IDEMPOTENCY_TTL_SECONDS)
    return _store_instance


def _replay_or_busy(store: IdempotencyStore, key: str, fingerprint: str) -> Dict[str, Any]:
    """抢占失败时的统一响应：已落库则重放，正在处理则让调用方稍后重试。"""
    cached = store.get(key)
    if cached is not None and cached.fingerprint == fingerprint:
        logger.info("幂等命中，按首次结果重放: %s", key)
        if isinstance(cached.response, dict):
            return {**cached.response, "idempotent_replay": True, "idempotency_key": key}
        return cached.response
    return {"error": "该请求正在处理中，请稍后重试（不要重复提交）", "in_progress": True, "idempotency_key": key}


def register_order_tools(mcp: FastMCP) -> None:
    """注册订单分组工具。"""

    @mcp.tool(name=f"{GROUP_NAME}_create")
    async def create_order(
        supplier_id: int,
        order_detail: List[dict],
        project_id: Optional[int] = None,
        expected_delivery_date: Optional[str] = None,
        status: int = 1,
        order_number: Optional[str] = None,
        created_by: str = "agent",
        remark: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        ctx: Context = None,
    ) -> dict:
        """创建采购订单（写入 PostgreSQL 的 erp.purchase_order 与明细表）。

        调用前请先用 material_query 确认物料 ID 与当前报价，用 supplier_query 确认供应商 ID。
        物料必须用 ID，不接受物料名称——名称相似度判断放在查询工具里做，写操作只认主键。

        Args:
            supplier_id: 供应商 ID（必填，来自 supplier_query）
            order_detail: 明细数组，每项必须含 materialId、quantity、unitPrice，可选 remark
            project_id: 关联项目 ID（可选，通用备料可不传）
            expected_delivery_date: 预计交货日期 yyyy-MM-dd（建议传，便于跟催）
            status: 初始状态，默认 1=待审核
            order_number: 订单编号，不传则自动生成 PO+年月日+3位随机数
            created_by: 创建人标识
            remark: 备注
            idempotency_key: 调用方幂等键（可选）；不传则用业务内容指纹自动推导

        幂等：相同业务内容在去重时间窗内重复调用只真正下单一次，
        第二次起返回首次结果并带 idempotent_replay=true。

        返回：{"ok": true, "order": {...}} 或 {"error": "..."}
        """
        request_data: Dict[str, Any] = {
            "supplierId": supplier_id,
            "projectId": project_id,
            "expectedDeliveryDate": expected_delivery_date,
            "status": status,
            "remark": remark,
            "details": [
                {
                    "materialId": d.get("materialId") or d.get("material_id"),
                    "quantity": d.get("quantity"),
                    "unitPrice": d.get("unitPrice") or d.get("unit_price"),
                }
                for d in order_detail
            ],
        }
        fingerprint = compute_fingerprint(request_data)
        key = (idempotency_key or "").strip() or f"{GROUP_NAME}_create:{fingerprint}"

        store = _store()
        cached = store.get(key)
        if cached is not None and cached.fingerprint == fingerprint:
            logger.info("order_create 命中幂等记录，跳过重复下单: %s", key)
            return _replay_or_busy(store, key, fingerprint)
        if not store.claim(key, fingerprint):
            logger.info("order_create 同指纹请求正在处理中，拒绝重复下单: %s", key)
            return _replay_or_busy(store, key, fingerprint)

        try:
            result = await repo.create_order(
                details=order_detail,
                supplier_id=supplier_id,
                project_id=project_id,
                expected_delivery_date=expected_delivery_date,
                status=status,
                order_no=order_number,
                created_by=created_by,
                remark=remark,
            )
            if not result.get("ok"):
                # 校验失败：释放占位，允许修正后立即重试（失败结果绝不进缓存）
                store.release(key)
                return {"error": result.get("error", "下单失败")}
            store.put(key, fingerprint, result)
            return {**result, "idempotency_key": key}
        except Exception as exc:
            store.release(key)
            logger.exception("order_create 执行异常")
            return {"error": f"下单失败：{exc}"}

    @mcp.tool(name=f"{GROUP_NAME}_update")
    async def update_order(
        order_id: int,
        order_detail: Optional[List[dict]] = None,
        supplier_id: Optional[int] = None,
        project_id: Optional[int] = None,
        status: Optional[int] = None,
        expected_delivery_date: Optional[str] = None,
        actual_delivery_date: Optional[str] = None,
        remark: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        ctx: Context = None,
    ) -> dict:
        """修改采购订单（最小变更：只落传进来的字段）。

        状态流转：1 待审核 → 2 已审核 / 5 已取消；2 → 3 采购中 / 5；3 → 4 已入库；
        4、5 为终态不可再改。已入库要改金额必须走退货重开，不能直接改单。

        Args:
            order_id: 订单 ID（必填）
            order_detail: 新明细数组；传入则整单替换原明细（每项含 materialId/quantity/unitPrice）
            supplier_id: 更换供应商（换供方会改变价格，请同时更新明细单价）
            project_id: 关联项目
            status: 目标状态 1-5，必须符合上述流转规则
            expected_delivery_date: 预计交货日期 yyyy-MM-dd
            actual_delivery_date: 实际交货日期 yyyy-MM-dd
            remark: 备注
            idempotency_key: 调用方幂等键（可选）

        返回：{"ok": true, "order": {...}} 或 {"error": "..."}
        """
        request_data: Dict[str, Any] = {
            "orderId": order_id,           # 参与指纹：改 A 单和改 B 单不是同一笔请求
            "supplierId": supplier_id,
            "projectId": project_id,
            "status": status,
            "expectedDeliveryDate": expected_delivery_date,
            "actualDeliveryDate": actual_delivery_date,
            "remark": remark,
            "details": [
                {
                    "materialId": d.get("materialId") or d.get("material_id"),
                    "quantity": d.get("quantity"),
                    "unitPrice": d.get("unitPrice") or d.get("unit_price"),
                }
                for d in (order_detail or [])
            ] or None,
        }
        fingerprint = compute_fingerprint(request_data)
        key = (idempotency_key or "").strip() or f"{GROUP_NAME}_update:{fingerprint}"

        store = _store()
        cached = store.get(key)
        if cached is not None and cached.fingerprint == fingerprint:
            logger.info("order_update 命中幂等记录，跳过重复更新: %s", key)
            return _replay_or_busy(store, key, fingerprint)
        if not store.claim(key, fingerprint):
            logger.info("order_update 同指纹请求正在处理中，拒绝重复更新: %s", key)
            return _replay_or_busy(store, key, fingerprint)

        try:
            result = await repo.update_order(
                order_id=order_id,
                details=order_detail,
                supplier_id=supplier_id,
                project_id=project_id,
                status=status,
                expected_delivery_date=expected_delivery_date,
                actual_delivery_date=actual_delivery_date,
                remark=remark,
            )
            if not result.get("ok"):
                store.release(key)
                return {"error": result.get("error", "改单失败")}
            store.put(key, fingerprint, result)
            return {**result, "idempotency_key": key}
        except Exception as exc:
            store.release(key)
            logger.exception("order_update 执行异常")
            return {"error": f"改单失败：{exc}"}

    @mcp.tool(name=f"{GROUP_NAME}_search_details")
    async def search_order_details(
        material_name: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        supplier_id: Optional[int] = None,
        project_code: Optional[str] = None,
        limit: int = 50,
        ctx: Context = None,
    ) -> dict:
        """查询采购订单明细（按物料名称 / 下单日期区间 / 供应商 / 项目筛选）。

        返回结构（统一错误契约，让模型能区分"查不到"与"查询失败"）：
            成功 → {"ok": true, "count": n, "data": [...]}
            失败 → {"ok": false, "count": 0, "data": [], "error": "原因"}

        Args:
            material_name: 物料名称或规格（模糊匹配），如"理化板"、"1.0mm"
            start_date: 下单日期起（yyyy-MM-dd，含当天）
            end_date: 下单日期止（yyyy-MM-dd，含当天）
            supplier_id: 供应商 ID
            project_code: 项目编号，如 LHK-2026-001
            limit: 返回条数上限，默认 50
        """
        try:
            return await repo.search_order_details(
                material_name=material_name,
                start_date=start_date,
                end_date=end_date,
                supplier_id=supplier_id,
                project_code=project_code,
                limit=limit,
            )
        except Exception as exc:
            logger.exception("订单明细查询失败")
            return {"ok": False, "count": 0, "data": [], "error": f"订单明细查询失败: {exc}"}
