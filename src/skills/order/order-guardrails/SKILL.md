---
name: order-guardrails
description: >
  采购订单操作的硬性校验清单。下单/改单前逐条核对必填字段、状态值域、金额与交期约束，
  避免因字段缺失或状态非法导致工具报错、重复下单或审批反复。
  当准备调用 order_create / order_update，或用户要求"改交期/改数量/取消订单"时加载。
---

# 采购订单操作规范（下单 / 改单）

后台工具（MCP）：`order_create` / `order_update` / `order_search_details`，数据落在 PostgreSQL 的 `erp` schema。
参数名是 snake_case，明细项字段是 camelCase。

## 零、工具签名（照抄，不要凭记忆改写）

```
order_create(supplier_id, order_detail[], project_id?, expected_delivery_date?,
             status=1, order_number?, created_by?, remark?, idempotency_key?)

order_update(order_id, order_detail?[], supplier_id?, project_id?, status?,
             expected_delivery_date?, actual_delivery_date?, remark?, idempotency_key?)

order_search_details(material_name?, start_date?, end_date?, supplier_id?, project_code?, limit?)
```

- `order_detail` 每项固定为 `{materialId, quantity, unitPrice}`（可选 `remark`）
- `order_number` 不传则后台自动生成 `PO + 年月日 + 3位随机数`
- **没有 `totalAmount` 参数**：总额由后台按 `Σ(quantity × unitPrice)` 用 Decimal 重算

## 一、下单前必过的 5 道校验

1. **物料已确认**：`order_detail[].materialId` 必须是 `material_query` 返回的真实物料 `id`（整数），
   不能是物料名称、型号或物料编码（`MAT-0006` 也不行）。名称 → ID 的映射必须先查后填。
2. **数量与单价**：`quantity` 必须 **> 0**（按物料单位：张/平方米/米/副/个/套/盒/kg/支/包）；
   `unitPrice` ≥ 0，且必须来自 `material_price_compare` 的有效报价或 `material_query` 的 `quotedPrice`。
   **不要用历史均值或"大概这个价"替代**；过期报价（`isExpired: true`）不能当现价用。
3. **金额一致**：不传总额、不自己算总额，以返回的 `order.totalAmount` 与明细 `subtotal` 为准；
   回复用户时照抄后台数字，不要与后台"对赌"。
4. **交期必填且合理**：`expected_delivery_date` 用 `yyyy-MM-dd`，**下单必传**，且不早于今天；
   取值口径 `今天 + max(leadTimeDays, avgDeliveryDays)`，用户指定则以用户为准；
   与供应商书面承诺交期不一致时，在 `remark` 里注明差异。
5. **供应商确认**：`supplier_id` 必须来自 `supplier_query`（**不接受供应商名称**），并检查：
   - 信用等级 **D = 黑名单，禁止下单**（后台会直接拒绝），改推同品类 A/B 级供应商
   - 信用等级 **C 需预付款**（`paymentTerms` 多为预付 30%~50%），必须提前告知用户
   - 供应商已停用不可下单；换供应商属于**改供应商**，需重新确认单价与交期，不能只改 ID

## 二、改单的最小变更原则

- 只传要改的字段，**不要整单回传**：整单回传会把并发修改的其他字段覆盖掉。
- 注意：`order_detail` 一旦传入就是**整单替换**（原明细删除后重建），要传就传完整明细。
- 改状态必须先核对流转合法性（见下表），非法流转后台会拒绝并返回允许的目标状态：

| 当前状态 | 允许流转到 | 说明 |
|---|---|---|
| 1 待审核 | 2 已审核、5 已取消 | 未审批可直接取消 |
| 2 已审核 | 3 采购中、5 已取消 | 已审核后取消要记录原因 |
| 3 采购中 | 4 已入库 | 采购中不可直接取消，需走退货流程 |
| 4 已入库 | — | 终态，只能开退货单 |
| 5 已取消 | — | 终态 |

- 不能跳级（1→3）、不能回退（3→2）。
- **已入库（4）订单的金额/数量不可修改**；确需变更走退货 + 重新下单，形成两条记录。
  终态（4/5）订单任何字段都不可再改，不要尝试"改回来"。

## 三、审批前的自检（提交给人工审批之前）

- 用一句话复述**要做什么**：物料（名称 + materialId）/ 数量 / 单价 / 总额 / 供应商 / 交期；
- 明确标出**与上次操作不同的地方**（改单场景），让审批人一眼看出变更点；
- 数据来源写清楚（哪个工具、哪次查询的哪个字段），不要把推测写成事实；
- 供应商为 C 级时，写明"需预付款 x%"。

## 四、重复提交的自我保护

- 同一笔业务请求重复提交时，MCP 层幂等会返回首次结果并带 `idempotent_replay=true`；
  看到该标记要**如实告诉用户"这单没有重复下单，返回的是上一次的结果"**，并复述那笔订单的
  订单号与金额，不要当成新单汇报，也不要再调一次工具试图"覆盖"。
- 若返回 "该请求正在处理中"（`in_progress=true`），说明有并发同指纹请求在执行：
  提示用户稍后用 `order_search_details` 查询订单是否已落地，**不要立刻重试**。

## 五、失败处理

| 情况 | 处理方式 |
|---|---|
| 返回校验类错误（物料不存在 / 状态非法 / 黑名单 / 缺 materialId） | 读错误信息，修正参数后重试 **最多 1 次**，仍失败则如实汇报 |
| 返回异常或超时 | 不要盲目重试：先 `order_search_details`（按物料名/供应商/日期）查该单是否已落地，再决定 |
| 关键字段拿不到（物料 ID、单价、供应商 ID、交期） | 调 `request_order_info` 暂停等人补，**绝不编造** materialId / 单价 / 供应商 / 订单号 |
| 审批被拒绝 | 停止执行，回复"已取消本次操作"，不要换个措辞再试一次 |
