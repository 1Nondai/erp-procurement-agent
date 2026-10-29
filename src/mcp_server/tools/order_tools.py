import logging
import random
import threading
from datetime import datetime
from decimal import Decimal

from fastmcp import FastMCP, Context

from typing import Optional, List

from mcp_server.idempotency import IdempotencyStore, compute_fingerprint
from mcp_server.server_config import IDEMPOTENCY_DB_PATH, IDEMPOTENCY_TTL_SECONDS

logger = logging.getLogger(__name__)

GROUP_NAME = "order"


# ============================================================
# 幂等存储（P0）
# ============================================================
# 惰性单例：不在 import 阶段就建库文件/开连接，
# 这样本模块可以被单测直接导入而不产生副作用。
_store_instance: Optional[IdempotencyStore] = None
_store_lock = threading.Lock()


def _store() -> IdempotencyStore:
    """获取幂等存储单例（双重检查锁，兼容多线程调用）。"""
    global _store_instance
    if _store_instance is None:
        with _store_lock:
            if _store_instance is None:
                _store_instance = IdempotencyStore(IDEMPOTENCY_DB_PATH, IDEMPOTENCY_TTL_SECONDS)
    return _store_instance


def _idempotency_key(tool_name: str, request_data: dict, explicit_key: Optional[str]) -> tuple[str, str]:
    """计算 (幂等键, 请求指纹)。

    键的来源优先级：
      1. 调用方显式传入的 idempotency_key（前端/上游有业务唯一号时最准确）；
      2. 由业务字段指纹推导（无需上游改造，天然覆盖"Agent 重试/用户重复点审批"）。

    指纹排除 orderNumber/orderTime 等每次调用都会变的字段 —— 否则重试时指纹不同，
    去重会完全失效（见 idempotency.compute_fingerprint）。
    """
    fingerprint = compute_fingerprint(request_data)
    key = (explicit_key or "").strip() or f"{tool_name}:{fingerprint}"
    return key, fingerprint


def _claim_failed_response(store: IdempotencyStore, idem_key: str, fingerprint: str) -> dict:
    """抢占失败时的统一响应。

    抢占失败的两种可能：
      1. 同指纹请求正在处理中 → 返回"处理中"，让调用方稍后重试（绝不能再去打后端）；
      2. 该请求刚好已经执行完成（首次响应已落库）→ 直接把首次结果当重放返回。
    第二步必须复查一次 get()，否则第 2 种情况会误报"处理中"。
    """
    cached = store.get(idem_key)
    if cached is not None and cached.fingerprint == fingerprint:
        logger.info("幂等抢占失败但结果已落库，按重放返回: %s", idem_key)
        if isinstance(cached.response, dict):
            return {**cached.response, "idempotent_replay": True, "idempotency_key": idem_key}
        return cached.response
    return {
        "error": "该请求正在处理中，请稍后重试",
        "in_progress": True,
        "idempotency_key": idem_key,
    }


def _generate_order_number() -> str:
    """生成订单编号：PO + 年月日(8位) + 3位随机数字"""
    today = datetime.now().strftime("%Y%m%d")
    suffix = str(random.randint(0, 999)).zfill(3)
    return f"PO{today}{suffix}"


def _prepare_request(data: dict) -> dict:
    """填充默认值并序列化请求体：Decimal→float，date→ISO字符串"""
    # 自动生成 orderNumber
    if not data.get("orderNumber"):
        data["orderNumber"] = _generate_order_number()

    # 默认 orderTime（格式：yyyy-MM-ddTHH:mm:ss.SSS，匹配后端 CustomLocalDateTimeDeserializer）
    if not data.get("orderTime"):
        now = datetime.now()
        data["orderTime"] = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}"

    # 递归序列化
    return _serialize_request(data)


def _serialize_request(data: dict) -> dict:
    """递归处理：Decimal → float，date/datetime → ISO 字符串"""
    for key, value in data.items():
        if isinstance(value, Decimal):
            data[key] = float(value)
        elif hasattr(value, "isoformat"):
            data[key] = value.isoformat()
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    _serialize_request(item)
        elif isinstance(value, dict):
            _serialize_request(value)
    return data


def register_order_tools(mcp: FastMCP):
    """注册订单分组的所有工具"""

    @mcp.tool(name=f"{GROUP_NAME}_create")
    async def create_order(
        order_detail: List[dict],
        order_number: Optional[str] = None,
        total_amount: Optional[float] = None,
        status: Optional[int] = None,
        order_time: Optional[str] = None,
        expected_delivery_date: Optional[str] = None,
        actual_delivery_date: Optional[str] = None,
        created_by: Optional[int] = None,
        remark: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        ctx: Context = None,
    ) -> dict:
        """
        创建采购订单（POST /orders/create）。

        orderNumber 不传则自动生成（规则：PO+年月日+3位随机数字）。
        orderTime 不传则默认当前时间（格式：yyyy-MM-ddTHH:mm:ss.SSS）。
        orderDetail 必填，至少包含一个明细项；每项需提供 partId、quantity、unitPrice。

        幂等：相同业务内容（相同明细/金额/交期等）在去重时间窗内重复调用只会真正下单一次，
        第二次起返回首次结果并带 idempotent_replay=true。

        Args:
            order_detail: 订单明细列表，每项需提供 partId, quantity, unitPrice，可选 subtotal, remark
            order_number: 订单编号（唯一标识），不传则自动生成
            total_amount: 订单总金额，不传则自动根据明细计算
            status: 订单状态(1-待审核, 2-已审核, 3-采购中, 4-已入库, 5-已取消)，默认1
            order_time: 下单时间，格式 yyyy-MM-ddTHH:mm:ss.SSS，不传则默认当前时间
            expected_delivery_date: 预计交货日期，格式 yyyy-MM-dd
            actual_delivery_date: 实际交货日期，格式 yyyy-MM-dd
            created_by: 创建人 ID
            remark: 备注
            idempotency_key: 调用方提供的幂等键（可选）。不传则用业务内容指纹自动推导
        """
        http_client = ctx.request_context.lifespan_context.get("http_client")

        # 构建请求体（映射到 API 字段名）
        request_data = {}
        if order_number is not None:
            request_data["orderNumber"] = order_number
        if total_amount is not None:
            request_data["totalAmount"] = total_amount
        if status is not None:
            request_data["status"] = status
        if order_time is not None:
            request_data["orderTime"] = order_time
        if expected_delivery_date is not None:
            request_data["expectedDeliveryDate"] = expected_delivery_date
        if actual_delivery_date is not None:
            request_data["actualDeliveryDate"] = actual_delivery_date
        if created_by is not None:
            request_data["createdBy"] = created_by
        if remark is not None:
            request_data["remark"] = remark
        if order_detail is not None:
            request_data["orderDetail"] = order_detail

        request_data = _prepare_request(request_data)

        # ---- 幂等前置检查（P0）----
        # 命中且指纹一致 → 说明这笔业务请求已经真正执行过，直接返回首次响应，
        # 不再打后端。这样"用户重复点审批""SSE 断线后重试"都不会产生重复订单。
        idem_key, fingerprint = _idempotency_key(f"{GROUP_NAME}_create", request_data, idempotency_key)
        cached = _store().get(idem_key)
        if cached is not None and cached.fingerprint == fingerprint:
            logger.info("order_create 命中幂等记录，跳过重复下单: %s", idem_key)
            if isinstance(cached.response, dict):
                return {**cached.response, "idempotent_replay": True, "idempotency_key": idem_key}
            return cached.response

        # ---- 抢占执行权（并发去重）----
        # get→打后端→put 之间没有互斥：两个同指纹请求并发进来会双双 miss、各下一单。
        # claim() 用 PRIMARY KEY 冲突做原子抢占，抢不到说明已有同指纹请求在处理中，
        # 此时绝不能再去打后端（否则照样重复下单）。
        store = _store()
        if not store.claim(idem_key, fingerprint):
            logger.info("order_create 同指纹请求正在处理中，拒绝重复下单: %s", idem_key)
            return _claim_failed_response(store, idem_key, fingerprint)

        try:
            # 请求体在 debug 级日志里已足够排查问题，不再 print 到控制台
            # （原 print 会把订单明细整包打进 stdout，生产环境属于敏感信息泄漏）
            logger.debug("order_create 请求体: %s", request_data)
            response = await http_client.post(
                "/orders/create",
                json=request_data
            )
            response.raise_for_status()
            result = response.json()

            if result.get("code") != 200:
                # 业务失败要释放占位，否则同指纹请求要被挡到占位过期
                store.release(idem_key)
                return {
                    "error": f"API error: code={result.get('code')}, message={result.get('message')}"
                }

            data = result.get("data", {})
            # 仅在成功时登记：失败要允许重试，不能把失败结果也缓存成"首次响应"
            # （put 内部会顺手删掉占位记录）
            store.put(idem_key, fingerprint, data)
            return data
        except Exception as e:
            # 异常同样释放占位，让调用方可以立即重试
            store.release(idem_key)
            return {"error": str(e)}

    @mcp.tool(name=f"{GROUP_NAME}_update")
    async def update_order(
        order_id: int,
        order_detail: Optional[List[dict]] = None,
        order_number: Optional[str] = None,
        total_amount: Optional[float] = None,
        status: Optional[int] = None,
        order_time: Optional[str] = None,
        expected_delivery_date: Optional[str] = None,
        actual_delivery_date: Optional[str] = None,
        created_by: Optional[int] = None,
        remark: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        ctx: Context = None,
    ) -> dict:
        """
        更新采购订单（PUT /orders/update/{id}）。

        orderDetail 为可选，传入则替换原有明细。
        其他字段与创建订单格式一致。
        幂等：相同字段集合在去重时间窗内重复提交只会真正执行一次。

        Args:
            order_id: 订单 ID（路径参数，必填）
            order_detail: 订单明细列表（可选），每项需提供 partId, quantity, unitPrice
            order_number: 订单编号
            total_amount: 订单总金额
            status: 订单状态(1-待审核, 2-已审核, 3-采购中, 4-已入库, 5-已取消)
            order_time: 下单时间
            expected_delivery_date: 预计交货日期
            actual_delivery_date: 实际交货日期
            created_by: 创建人 ID
            remark: 备注
            idempotency_key: 调用方提供的幂等键（可选）。不传则用订单ID+变更字段指纹自动推导
        """
        http_client = ctx.request_context.lifespan_context.get("http_client")

        # 构建请求体（映射到 API 字段名，只包含非 None 字段）
        request_data = {}
        if order_number is not None:
            request_data["orderNumber"] = order_number
        if total_amount is not None:
            request_data["totalAmount"] = total_amount
        if status is not None:
            request_data["status"] = status
        if order_time is not None:
            request_data["orderTime"] = order_time
        if expected_delivery_date is not None:
            request_data["expectedDeliveryDate"] = expected_delivery_date
        if actual_delivery_date is not None:
            request_data["actualDeliveryDate"] = actual_delivery_date
        if created_by is not None:
            request_data["createdBy"] = created_by
        if remark is not None:
            request_data["remark"] = remark
        if order_detail is not None:
            request_data["orderDetail"] = order_detail

        request_data = _prepare_request(request_data)

        # ---- 幂等前置检查（P0）----
        # 更新的幂等键必须包含 order_id：否则"给 A 单改备注"和"给 B 单改备注"
        # 内容相同会被误判为同一笔请求。
        idem_key, fingerprint = _idempotency_key(
            f"{GROUP_NAME}_update:{order_id}", request_data, idempotency_key
        )
        cached = _store().get(idem_key)
        if cached is not None and cached.fingerprint == fingerprint:
            logger.info("order_update 命中幂等记录，跳过重复更新: %s", idem_key)
            if isinstance(cached.response, dict):
                return {**cached.response, "idempotent_replay": True, "idempotency_key": idem_key}
            return cached.response

        # ---- 抢占执行权（并发去重）：抢不到说明同指纹更新正在处理中 ----
        store = _store()
        if not store.claim(idem_key, fingerprint):
            logger.info("order_update 同指纹请求正在处理中，拒绝重复更新: %s", idem_key)
            return _claim_failed_response(store, idem_key, fingerprint)

        try:
            logger.debug("order_update 请求体: %s", request_data)
            response = await http_client.put(
                f"/orders/update/{order_id}",
                json=request_data
            )
            response.raise_for_status()
            result = response.json()

            if result.get("code") != 200:
                # 业务失败释放占位，允许修正后立即重试
                store.release(idem_key)
                return {
                    "error": f"API error: code={result.get('code')}, message={result.get('message')}"
                }

            data = result.get("data", {})
            store.put(idem_key, fingerprint, data)
            return data
        except Exception as e:
            store.release(idem_key)
            return {"error": str(e)}

    @mcp.tool(name=f"{GROUP_NAME}_search_details")
    async def search_order_details(
        part_name: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        ctx: Context = None,
    ) -> dict:
        """
        搜索采购订单明细。

        支持按零部件名称、日期范围筛选，所有参数均为可选。
        返回的每条明细包含零部件详情（partDetail）及供应商信息（supplier）。

        返回结构（P0 统一错误契约）：
            成功 → {"ok": true, "count": n, "data": [ ...明细... ]}
            失败 → {"ok": false, "count": 0, "error": "原因"}
        这样模型能区分"查询结果为空"与"接口报错"，从而决定是继续分析还是换条件重试。

        Args:
            part_name: 零部件名称（模糊查询），可选
            start_date: 开始日期（yyyy-MM-dd 格式），可选
            end_date: 结束日期（yyyy-MM-dd 格式），可选
        """
        http_client = ctx.request_context.lifespan_context.get("http_client")

        # 构建请求参数（过滤 None 值，映射到 API 字段名）
        request_params = {}
        if part_name is not None:
            request_params["partName"] = part_name
        if start_date is not None:
            request_params["startDate"] = start_date
        if end_date is not None:
            request_params["endDate"] = end_date

        try:
            response = await http_client.get(
                "/orders/search-details",
                params=request_params
            )
            response.raise_for_status()
            result = response.json()

            if result.get("code") != 200:
                return {
                    "ok": False,
                    "count": 0,
                    "data": [],
                    "error": f"API error: code={result.get('code')}, message={result.get('message')}",
                }

            data = result.get("data", []) or []
            return {"ok": True, "count": len(data), "data": data}
        except Exception as e:
            return {"ok": False, "count": 0, "data": [], "error": f"订单明细查询失败: {e}"}