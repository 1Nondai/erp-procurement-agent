# ERP 采购智能助手 · Procurement Copilot

![CI](../../actions/workflows/ci.yml/badge.svg)
![Python](https://img.shields.io/badge/Python-3.12-3776ab)
![Vue](https://img.shields.io/badge/Vue-3-42b883)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16-4169e1)
![Observability](https://img.shields.io/badge/Observability-Langfuse-6d28d9)
![License](https://img.shields.io/badge/License-MIT-yellow)

> 面向实验台器具制造企业的采购 Agent：用自然语言完成**查料、比价、看库存、下单改单**，
> 写操作经人工审批后落到 PostgreSQL，全链路可追溯、可重放、不重复下单。

> 演示企业「明远实验装备」为**虚构主体**，仅用于构造真实的采购场景与数据；
> 与任何真实公司无关。

明远生产钢木/全钢实验台、通风柜、药品柜。一张实验台背后是几十种材料——
理化板、环氧树脂板、冷轧钢板、滑轨铰链、水暖五金、电气件、通风管件。
采购员的日常是：这个台面现在谁家便宜、库存够不够、这个项目料钱花了多少、能不能改交期。

这个项目把这些问题收敛成一个对话入口，并用四道闸门防止 Agent 闯祸。

---

## 一、四道闸门

| 闸门 | 机制 | 代码位置 |
|---|---|---|
| 越权调用 | 主 Agent 不持有业务工具，只能 `task` 委派 | `src/agent/main_agent.py` |
| 误写 | `order_create` / `order_update` 执行前强制人工审批 | `src/api_view/api/chat.py` + `InterruptBanner.vue` |
| 重复写 | 业务指纹去重 + pending 占位原子抢占 | `src/mcp_server/idempotency.py` |
| 越界执行 | 代码只在 OpenSandbox 跑，出网默认拒绝 + 主机白名单 | `src/agent/backends/sandbox_setup.py` |
| 沙箱长时间不可用 | 每步探活 → 就地换芯重建 → 连续失败熔断 | `src/agent/middlewares/sandbox_health.py` · `sandbox_breaker.py` |

再加一条**数据正确性**的闸门：订单总额不接受调用方传入，一律由 `repository` 用
`Decimal` 按明细重算后落库——库里的账和明细永远对得上（`src/mcp_server/repository.py`）。

## 二、核心能力

| 能力 | 说明 | 代码位置 |
|---|---|---|
| **多 Agent 协作** | 主 Agent 只协调；`procurement-analyst` 做比价/分析/出图，`procurement-order` 管下单改单 | `src/agent/subagents/configs/` |
| **MCP 工具化** | FastMCP 把采购能力包成 7 个工具，按 `supplier_`/`material_`/`inventory_`/`order_` 前缀分给两个子 Agent | `src/mcp_server/tools/` |
| **PostgreSQL 业务库** | 独立 `erp` schema：供应商、物料、报价、项目、订单、明细、出入库流水；比价与库存预警固化成视图 | `db/init/01_schema.sql` |
| **报价比价** | 同一物料多供应商报价，排除过期报价后给出最低价、价差百分比、起订量 | `repository.compare_prices` |
| **订单状态机** | 1 待审核 → 2 已审核 → 3 采购中 → 4 已入库；1/2 可取消；终态不可改 | `repository.STATUS_FLOW` |
| **写操作幂等** | 指纹剔除 `orderNumber`/`orderTime`；并发同指纹请求只放行一个 | `src/mcp_server/tools/order_tools.py` |
| **RAG 知识检索** | 采购制度、台面选型、五金/水暖/电气/通风选型、供应商 QSDC、库存策略 | `src/rag/knowledge_docs.py` |
| **评测集** | 13 条用例：检索层 / 答案层可离线回归，路由层标 skipped | `src/eval/` |
| **沙箱池 + 预热** | 启动预热一个沙箱，首个用户直接认领（省掉 10~20s 创建）；同一用户的多条会话共享同一沙箱文件系统 | `src/agent/backends/sandbox_manager.py` |
| **沙箱自愈** | 代理句柄"换芯不换壳"：沙箱挂了就地重建，graph/中间件/工具闭包全部无感知；连续失败熔断止损 | `sandbox_proxy.py` · `middlewares/sandbox_health.py` |
| **多用户隔离** | `X-User-Id` → `/memories/{user_id}/`，白名单校验防路径穿越 | `src/api_view/identity.py` |
| **流式前端** | Vue 3 + SSE：token、工具调用过程、审批卡片、深浅主题 | `frontend/src/` |
| **可观测性** | 一轮对话一条 trace：主/子 Agent、每次 LLM 与工具调用的输入输出、token 与耗时；未配置密钥时自动降级，业务零影响 | `src/agent/observability/` |

## 三、架构

```mermaid
flowchart LR
    U[浏览器<br/>Vue 3 + SSE] -->|POST /api/chat/stream| API[FastAPI<br/>api_view]
    API --> A[主 Agent<br/>协调者]
    A -->|task| S1[procurement-analyst<br/>比价 · 分析 · 图表]
    A -->|task| S2[procurement-order<br/>下单 · 改单 · 查询]
    A -.per-user agent graph.-> MGR[sandbox_manager<br/>预热池 · 注册表 · 重建]
    MGR --> SB[OpenSandbox<br/>代码执行 · 出网白名单]
    MGR -.绑定关系.-> REG[(PostgreSQL<br/>ops.sandbox_registry)]
    S1 --> SB
    S2 --> HITL{HITL 审批}
    HITL -->|approve| MCP
    S1 --> MCP[FastMCP Server<br/>7 个采购工具]
    MCP --> REPO[repository 层<br/>参数校验 · 领域规则]
    REPO --> PG[(PostgreSQL<br/>erp schema)]
    A --> RAG[(RAG 知识库<br/>JSONL + 余弦检索)]
    A --> CKPT[(PostgreSQL<br/>checkpoint · Store)]
    A -.LangChain callback.-> LF[Langfuse<br/>trace · span · 成本]
```

一次下单的完整链路：

1. 前端带 `X-User-Id` 发消息 → FastAPI 以 SSE 建流；
2. 主 Agent 读 `/memories/{user_id}/preferences.md` → 委派 `procurement-order`；
3. 子 Agent 先 `material_query` 确认物料 ID 与报价、`supplier_query` 确认供应商；
4. 字段不全 → `request_order_info` 中断等人补；齐了 → `order_create` 触发 `interrupt`；
5. 前端弹审批卡片 → approve → 恢复执行；
6. MCP 层算业务指纹，`claim()` 抢占 → `repository` 单事务写主表+明细 → `put()` 缓存首次结果；
7. 展示消息与 checkpoint 落库，前端渲染成可回溯的对话。

## 四、快速开始

### 0. 依赖

- Python 3.12+、Node.js 18+
- Docker Desktop（PostgreSQL 与 OpenSandbox）
- 模型 Key：DeepSeek（主模型）+ 智谱（embedding / web_search）

### 1. 起数据库（含业务库初始化）

```bash
cp .env.example .env      # 填 DEEPSEEK_API_KEY / ZHIPU_API_KEY
docker compose up -d
```

首次启动会自动执行 `db/init/01_schema.sql`（建 `erp` schema、表、索引、视图）和
`02_seed.sql`（12 家供应商 / 37 种物料 / 45 条报价 / 3 个工程单 / 12 张订单 / 24 条流水）。

```bash
# 验证数据真的进去了
docker compose exec postgres psql -U postgres -d langgraph_db -f /dev/stdin < db/verify.sql
```

> 想重建演示数据：`docker compose down -v && docker compose up -d`
> （`db/init/*.sql` 只在数据卷为空时执行）

### 2. 起 OpenSandbox

```bash
docker run --rm -d --name opensandbox-server -p 8080:8080 \
  opensandbox/opensandbox-server:latest
```

> ⚠️ **端口冲突**：OpenSandbox 默认 8080，很多本机服务也占 8080。
> 冲突时换端口并设置 `SANDBOX_DOMAIN=http://127.0.0.1:8081`。

沙箱由 `sandbox_manager` 托管（不是每次请求新建）：

- 启动时预热一个沙箱放进池里，**第一个用户直接认领**，不用等创建；
- 认领后后台异步补一个，保证下一个人也大概率零等待；
- 绑定关系写 `ops.sandbox_registry`，进程重启后能按 id 重连（连不上就重建并覆盖绑定）；
- 本地调试嫌预热慢：`SANDBOX_WARM_POOL=0`；熔断阈值：`SANDBOX_BREAKER_THRESHOLD=2`。

### 3. 起 MCP Server（8000）

```bash
cd src && python -m mcp_server.server_main
```

必须在 `src/` 下以模块方式启动（内部用 `mcp_server.*` 绝对导入）。
它直连 `ERP_DB_URI`（未设置时回落到 `POSTGRES_URI`）读写 `erp` schema。

### 4. 起 Web 后端 + 前端

```bash
python start_web.py            # 后端 8090 + 前端 3000
```

- 前端 <http://localhost:3000>，API 文档 <http://localhost:8090/docs>
- 首次启动要建沙箱、加载 MCP 工具，**30–60 秒属正常**
- `--backend-only` / `--frontend-only` 可单独起

### 5.（可选）打开 Langfuse 追踪

不配也能正常跑。想看到每轮对话的完整调用链（哪次 LLM 慢、哪个工具返回了什么、这轮花了多少 token），
在 `.env` 里取消注释并填上：

```ini
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_BASE_URL=https://cloud.langfuse.com
```

重启后启动日志会打印 `Langfuse 链路追踪: 已启用`，`/health` 也能看到状态。

> 内置价目表没有 DeepSeek：要在控制台 Settings → Models 手动加一条 `deepseek-v4-pro` 并填单价，
> 成本统计才准（不加的话 token 数正常、成本显示 0）。

## 五、目录结构

```
.
├── db/
│   ├── init/01_schema.sql      # erp schema：供应商/物料/报价/项目/订单/明细/流水 + 两个视图
│   ├── init/02_seed.sql        # 演示数据（小计与总额由 UPDATE 统一算出，不手抄）
│   └── verify.sql              # 数据自检 SQL（行数、总额一致性、比价、预警）
├── scripts/
│   ├── smoke_db.py             # 数据层自检：直接调 repository，30+ 断言
│   └── smoke_mcp.py            # MCP 层自检：连真实 Server 逐个调 7 个工具
├── src/
│   ├── agent/                  # Agent 核心（主 Agent / 中间件 / 子 Agent YAML / 沙箱 / 工具）
│   │   └── observability/      #   Langfuse 追踪：降级闸门 / trace 注入 / 脱敏 / 生命周期
│   ├── api_view/               # FastAPI（SSE 流式 / 历史 / 身份 / 连接池）
│   ├── mcp_server/
│   │   ├── db.py               #   PostgreSQL 连接池（含 Windows 事件循环兼容）
│   │   ├── repository.py       #   业务 SQL + 领域规则（状态机、Decimal 重算）
│   │   ├── idempotency.py      #   订单幂等（指纹 + pending 占位）
│   │   └── tools/              #   7 个 MCP 工具
│   ├── rag/                    # 知识库（embedding + JSONL 向量库 + 余弦检索）
│   ├── eval/                   # 评测用例与脚本
│   └── skills/                 # 渐进式技能包（采购分析 / 询价站点映射 / 订单规范）
├── frontend/                   # Vue 3 + Vite
├── tests/                      # 纯标准库单测（身份校验 / 订单幂等），CI 直接跑
├── docs/ARCHITECTURE.md        # 架构与取舍
├── docs/EVALUATION.md          # 评测体系设计
├── docs/Langfuse可观测性接入.md  # 追踪接入、trace 维度、实测坑与排错
└── start_web.py                # 一键启动
```

## 六、怎么验证（不用 LLM 也能验）

```bash
# 1) 数据层自检：schema / seed / repository 是否对得上（需要 PG）
export ERP_DB_URI=postgresql://postgres:postgres@localhost:5432/langgraph_db
python scripts/smoke_db.py

# 1b) MCP 协议层自检：7 个工具是否都注册、参数与返回结构是否成型
#     （先按第 3 步起 MCP Server，再跑）
python scripts/smoke_mcp.py

# 2) 纯标准库单测（CI 里跑的那套）
python -m unittest discover -s tests -v

# 3) 静态检查与前端构建
python -m ruff check src tests start_web.py
cd frontend && npm run build

# 4) RAG 评测（需要 embedding Key）
python src/eval/run_eval.py --skip-answer   # 只测检索层

# 5) 追踪是否生效（只读本地状态，不发网络请求）
curl -s localhost:8090/health | python -c "import sys,json;print(json.load(sys.stdin)['tracing'])"
```

`run_eval.py` 的三层：检索层（期望来源是否在 top-k）、答案层（要点覆盖）、
路由层（需要完整 Agent 环境，**离线会标 skipped**，在线模式尚未实现）。

## 七、文档

- [架构与取舍](docs/ARCHITECTURE.md) — 分层、数据模型、请求链路、每个决策否掉了什么
- [评测体系设计](docs/EVALUATION.md) — 分层评测思路、用例设计原则、脚本说明
- [Langfuse 可观测性接入](docs/Langfuse可观测性接入.md) — 接入步骤、trace 怎么用、实测踩过的坑

## License

[MIT](LICENSE)
