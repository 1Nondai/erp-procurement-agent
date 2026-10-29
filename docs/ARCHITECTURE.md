# 架构与取舍（Architecture）

> 本文是 [README](../README.md) 的展开：讲清分层、数据模型、请求链路，
> 以及每个决策**否掉了什么**。代码为准，摘录若与 `src/` 不一致以代码为准。

## 1. 为什么是这个形状

采购 Agent 天生的矛盾是：**它需要自由度（理解自然语言需求），又必须没有自由度（不能乱写账）**。
本项目用"职责分层"来解决：越靠近写操作，自由度越低。

```
┌──────────────────────────────────────────────────────────┐
│ 前端 Vue3          自然语言输入 / SSE 渲染 / 审批卡片        │  ← 自由度最高
├──────────────────────────────────────────────────────────┤
│ 主 Agent           理解意图、拆任务、委派、汇总              │
├──────────────────────────────────────────────────────────┤
│ 子 Agent           分析类（只读）/ 订单类（写，带审批）      │
├──────────────────────────────────────────────────────────┤
│ MCP 工具层         工具契约 + 参数校验 + 幂等                │
├──────────────────────────────────────────────────────────┤
│ repository 层      业务 SQL + 领域规则（状态机、金额重算）    │
├──────────────────────────────────────────────────────────┤
│ PostgreSQL         约束（CHECK/FK/UNIQUE）兜最后一道底        │  ← 自由度最低
└──────────────────────────────────────────────────────────┘
```

关键判断：**"查"与"写"在物理上分到两个子 Agent**。不是靠提示词让模型"不要乱写"，
而是让负责分析的那个 Agent 根本拿不到写工具。

## 2. 数据模型

`erp` schema 独立于 LangGraph 的 checkpoint 表，靠 schema 隔离，便于分别备份与授权。

| 表 | 作用 | 设计要点 |
|---|---|---|
| `erp.supplier` | 供应商主数据 | QSDC 四维评分（质量/服务/交付/成本，0-10）+ 信用等级 A/B/C/D；等级是**序关系**，`min_credit='B'` 表示 A 或 B |
| `erp.material` | 物料主数据 | 存 `ref_price`（参考价）、`safety_stock`、`current_stock`、`lead_time_days`——库存预警与补货优先级都靠这四个字段 |
| `erp.material_supplier` | 物料-供应商报价 | 多对多 + `valid_until`。**有效期是字段不是逻辑**：进口板材的报价 15 天就失效，过期的价不能参与比价 |
| `erp.project` | 工程单 | 采购按项目归集，才能回答"这个项目料钱花了多少" |
| `erp.purchase_order` | 采购订单 | `status` 用 smallint + CHECK 而不是 enum：加状态值不用改类型 |
| `erp.purchase_order_detail` | 订单明细 | `subtotal` 落库而不是查询时算——报表与页面不会各算一套 |
| `erp.inventory_movement` | 出入库流水 | 让"库存"有账可查：入库对应 PO、出库对应生产领用、盘点记 A |

两个视图把口径固化：

- `erp.v_material_price_compare`：物料 × 供应商报价 + 与参考价的差额/百分比 + 是否过期；
- `erp.v_stock_warning`：低于安全库存的物料 + 缺口 + `OUT_OF_STOCK / CRITICAL / LOW` 分级。

为什么用视图：如果 Agent、报表、前端各写一套 SQL，迟早出现"Agent 说缺货、报表说不缺"。
口径只写一次，才谈得上一致。

**金额一律 `numeric(12,2)`**，全程 `Decimal`。用 float 的话，1000 张板子的累计误差
足以让采购单和发票对不上——这是财务数据的硬约束，不是风格问题。

## 3. 一次下单的链路

```
POST /api/chat/stream  (X-User-Id: liyan)
  → 主 Agent 读取 /memories/liyan/preferences.md
  → task(procurement-order)
      → material_query(name="冷轧")      # 拿 ID 与报价
      → supplier_query(name="宝钢")      # 拿供应商 ID 与信用等级
      → order_create(supplier_id, order_detail=[...], expected_delivery_date)
          → LangGraph interrupt  → SSE 推 interrupt 事件 → 前端弹审批
  → 用户 approve → POST /api/chat/{thread_id}/resume
      → _has_pending_interrupt()  (fail-closed: 查询失败就拒绝恢复)
      → MCP order_create 继续执行
          → 指纹去重 get() → claim() 抢占 → repository.create_order()
              → 单事务：INSERT order + INSERT details（Decimal 重算小计/总额）
          → put() 缓存首次响应
  → 展示消息落库 + SSE 推 done
```

几处容易做错的点：

- **首轮就中断**时 thread_id 还没回给前端：`onInterrupt` 里必须立刻绑定 thread_id，
  否则审批按钮点了没反应（`frontend/src/App.vue`）。
- **重复点审批**：`_has_pending_interrupt()` 是硬判据，且查询异常时 fail-closed——
  原实现返回 True（fail-open），恰好在最需要防护的场景放行。
- **断线重连**：靠幂等指纹兜住，第二次调用返回首次结果并带 `idempotent_replay=true`。

## 4. MCP 工具契约

| 工具 | 参数 | 返回要点 |
|---|---|---|
| `supplier_query` | name / category / min_credit / limit | `{ok, count, data:[{id, shortName, creditRating, avgScore, scores{...}, avgDeliveryDays, paymentTerms}]}` |
| `material_query` | current / size / name / category / supplier_id | `{ok, total, current, size, records:[{id, name, spec, unit, refPrice, quotedPrice, currentStock, safetyStock, leadTimeDays, preferredSupplier...}]}` |
| `material_price_compare` | material_id / material_name / category / limit | `{ok, count, data:[{materialId, quotes:[...], summary:{supplierCount, lowestSupplier, lowestPrice, spreadPct, expiredQuotes, recommendation}}]}` |
| `order_create` | supplier_id, order_detail[], project_id?, expected_delivery_date?, status?, idempotency_key? | `{ok, order:{orderNo, status, totalAmount, details:[...]}}` |
| `order_update` | order_id, order_detail?, supplier_id?, status?, ... | 同上；违反状态流转直接返回 `{error}` |
| `order_search_details` | material_name? / start_date? / end_date? / supplier_id? / project_code? | `{ok, count, data:[明细+供应商+项目]}` |
| `inventory_warning` | level? / limit? | `{ok, count, data:[{materialName, shortage, leadTimeDays, warningLevel}]}` |

约定：

- **写操作只认主键**。物料、供应商都必须传 ID；名称相似度判断留在查询工具里做。
- **统一错误契约**：查询类返回 `{ok:false, error}`，让模型能区分"查不到"与"查询失败"，
  从而决定继续分析还是换条件重试。
- **summary 由工具算，不让模型心算**：比价结论最需要的几个数字（几家、谁最低、价差多少）
  直接给出来，模型只负责解释。
- **总额不接受调用方传入**：`repository` 按明细用 Decimal 重算，避免"库里金额和明细对不上"。

## 5. 幂等设计

三层防线，缺一不可：

1. **指纹**：业务字段规范化 JSON 的 sha256，**剔除** `orderNumber`/`orderTime`/`requestId`；
   数值统一成 `num:xxx.xxxxxx` 固定精度，避免 `25.5` 与 `25.50` 被当成两笔请求；
2. **缓存**：同指纹在 TTL（默认 24h）内直接返回首次响应，带 `idempotent_replay=true`；
3. **抢占**：`claim()` 往 `idempotency_pending` 插占位，用 PRIMARY KEY 冲突做原子抢占。
   抢不到 = 有同指纹请求在跑 → 返回"处理中"，**绝不再打一次库**。
   失败要 `release()` 释放占位，否则同指纹请求要等到占位过期才能重试。

为什么不用数据库唯一索引直接去重：订单号是自动生成的，数据库层面两笔"同业务不同单号"
的请求都是合法 INSERT，只有业务指纹能识别它们是同一笔。

## 6. 沙箱与出网

代码执行在 OpenSandbox 里，出网策略默认拒绝 + 主机白名单：

- 白名单 = 基础清单（PyPI 镜像）+ 报价站点域名（从 `url_mapping.yaml` 自动提取）+
  环境变量 `SANDBOX_EGRESS_EXTRA_HOSTS`；
- 策略构造失败时 **fail-closed**（抛错让启动失败），只有显式
  `SANDBOX_EGRESS_FAIL_OPEN=1` 才降级；
- 拼 shell 的动态值（技能名、路径）一律 `shlex.quote` + 名字白名单——
  这些值来自模型输出，等于外部输入。

## 7. RAG

- 语料：`src/rag/knowledge_docs.py`，14 条采购领域知识（审批权限、比价制度、台面选型、
  五金/水暖/电气/通风选型、供应商 QSDC、库存策略、验收索赔）；
- 检索：智谱 `embedding-3` + 本地 JSONL 向量库 + numpy 余弦（百条级 <1ms），
  归一化矩阵在 `add/load` 时预算并缓存；
- 抽象：接口只有 `add / search / count` 三个方法，规模上去换 Chroma/pgvector 时上层零改动；
- 为什么不用向量库：百条级语料引入向量库是纯运维负担；用 JSONL 还能直接进 Git、
  肉眼看、手工改。

## 8. 前端

- SSE 按空行切事件、支持多行 `data:`、断流时 flush 残留、`finally` 释放 reader；
- 渲染 `html: false`（工具结果来自外部网页，开 HTML 等于开 XSS）+ 链接统一
  `target=_blank rel="noopener noreferrer"`；
- `highlight.js/lib/core` 按需注册 9 种语言：全量引入是 2.1MB，按需是 255KB；
- 切会话先 abort 在途请求，`loadMessages` 用自增 reqId 丢弃过期响应；
- 中文输入法：`isComposing` 时回车不发送。

## 9. 现状与未做

- 身份只有 `X-User-Id`（白名单校验防穿越），**没有鉴权**：生产要换 JWT/网关注入；
- 路由层评测是 TODO（需要完整 Agent 环境），检索层与答案层可回归；
- 幂等库是 SQLite，多副本需换 PostgreSQL 唯一索引；
- 改单是明细整单替换，没有行级审计；
- 审批权限靠知识库 + 提示词约束，后端没有 RBAC。
