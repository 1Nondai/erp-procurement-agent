# Langfuse 可观测性接入说明

> 本文对应 `agent/observability/` 模块，实测版本 **langfuse 5.2.0**。
> 文档里的结论都在本机跑过，不是照抄官方示例——官方文档有几处写法对 5.x 已经过时，坑记在第 8 节。

---

## 1. 加了之后能看到什么

每一轮用户对话在 Langfuse 上是一条完整 trace，包含：

- 主 Agent 调了几次模型、每次的完整 prompt 与返回、token 消耗、耗时
- 哪个子 Agent（`procurement-analyst` / `procurement-order`）被唤起、跑了多久
- 每次工具调用（MCP 查供应商/物料/订单、图表生成、web_search）的入参和返回
- 整轮对话的总耗时，以及时间到底花在模型、工具还是沙箱上

**业务代码只多了一行**：`chat.py` 里 `config = instrument_config(...)`。追踪没启用时这行等于没写。

---

## 2. 挂在链路的哪个位置

```
前端 Vue (chat.js, SSE)
   │  POST /api/chat/stream   （带身份请求头）
   ▼
api_view/api/chat.py   stream_chat_response()
   │  resolve_identity() 已校验 user_id
   │  config = agent_loader.create_config(thread_id, user_id)
   │  ★ config = instrument_config(...)      ← 只挂这一处
   ▼
user_graph.astream(config=config, ...)
   ▼
LangGraph 主 Agent ──► 子 Agent（task 工具 / 中间件）
   │                        │
   ├── LLM (DeepSeek)       ├── LLM
   ├── MCP 工具             └── 沙箱内脚本
   └── PostgreSQL checkpointer
```

为什么挂在 `config` 上而不是包一层装饰器：LangChain 的 callback 是**顺着 config 往下传**的，在入口挂一次，主 Agent、子 Agent、每一次模型调用、每一次工具调用全都会被覆盖到，不用在几十个地方各加一遍。

实测一轮「问报价 → 调工具 → 出答案」的流程，Langfuse 收到 **6 个 chain span、2 个 LLM span、1 个 tool span**。

---

## 3. 三步接上

### 3.1 装包

```bash
pip install -r requirements.txt      # 已包含 langfuse>=5.0,<6.0
# 或者只装它
pip install "langfuse>=5.0,<6.0"
```

> langfuse 5.x 以 OpenTelemetry 为底座，会拉 `opentelemetry-* >= 1.45`。
> 如果你环境里 pin 过更旧的 OTel 版本，先升，否则 pip 解析会冲突。

### 3.2 填密钥

```bash
cp .env.example .env
```

打开 `.env`，把 `.env.example` 末尾那段 Langfuse 配置取消注释并填值：

```ini
LANGFUSE_ENABLED=true
LANGFUSE_PUBLIC_KEY=pk-lf-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
LANGFUSE_SECRET_KEY=sk-lf-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
LANGFUSE_BASE_URL=https://cloud.langfuse.com
```

密钥在 Langfuse 控制台 Settings → API Keys。没有账号就用云版免费额度；要内网隔离就自托管，把 `LANGFUSE_BASE_URL` 指到 `http://内网地址:3000`。

> 按项目约定，所有敏感配置只从 `src/agent/env_utils.py` 读取，本模块新增的 `LANGFUSE_*` 变量也放在那里，没有在别处散落 `os.getenv`。

### 3.3 启动，看这一行日志

```bash
python start_web.py
```

```
Langfuse 链路追踪: 已启用
```

如果显示 `未启用（已降级）`，先看第 9 节的排错清单，别改代码——基本都是密钥或网络问题。

也可以直接看 `/health`：

```bash
curl http://localhost:8090/health
```

```json
{
  "status": "healthy",
  "service": "DeepAgent Chat API",
  "version": "1.0.0",
  "tracing": {
    "enabled": true,
    "sdk_installed": true,
    "base_url": "https://cloud.langfuse.com",
    "environment": "production",
    "sample_rate": 1.0,
    "mask_enabled": true,
    "last_error": null
  }
}
```

---

## 4. 控制台里怎么用

trace 自带的维度：

| 维度 | 值 | 用途 |
|---|---|---|
| Trace 名 | `erp-chat` / `erp-resume` | 区分首次提问与 HITL 中断后恢复 |
| Session | `thread_id` | **多轮对话串成一条会话**，点进去看完整上下文 |
| User | `user_id` | 按用户排查；与 `identity.py` 的白名单口径一致 |
| Tags | `erp-openclaw`、`stage:chat`、`env:production` | 按环境/阶段过滤 |
| Metadata | `erp_stage`、`erp_user_id`、`erp_thread_id` | 原始值，方便自己写查询 |

三个最常用的动作：

1. **复现线上问题**：用户说昨天某句话答错了，用 user_id + 时间范围筛到那条 trace，当时的 prompt、模型返回、工具原始 JSON 全在里面。
2. **看成本**：主模型配的是 `max_tokens=2560000`，实际花了多少只有这里能看到。按 session 分组还能看出哪个会话特别贵。
3. **看慢在哪**：一条 trace 里每个 span 都有耗时条，一眼看出是模型慢（LLM span 长）、工具慢（tool span 长）还是沙箱慢。

---

## 5. 配置项全表

| 变量 | 默认值 | 说明 |
|---|---|---|
| `LANGFUSE_ENABLED` | 配了密钥就为 true | 总开关 |
| `LANGFUSE_PUBLIC_KEY` | 空 | 项目公钥 |
| `LANGFUSE_SECRET_KEY` | 空 | 项目私钥 |
| `LANGFUSE_BASE_URL` | `https://cloud.langfuse.com` | 服务地址，自托管改这里；兼容旧名 `LANGFUSE_HOST` |
| `LANGFUSE_SAMPLE_RATE` | `1.0` | 采样率 0~1；写非法值自动纠正为 1.0（宁多勿丢） |
| `LANGFUSE_ENVIRONMENT` | `APP_ENV` 或 `development` | 环境标签 |
| `LANGFUSE_RELEASE` | `dev` | 版本标签，发布时填 git tag |
| `LANGFUSE_FLUSH_AT` | `512` | 攒够多少条上报一次 |
| `LANGFUSE_FLUSH_INTERVAL` | `5.0` | 每隔多少秒上报一次 |
| `LANGFUSE_TIMEOUT` | `10.0` | 单次 HTTP 超时（秒） |
| `LANGFUSE_MASK_ENABLED` | `true` | 敏感信息脱敏 |
| `LANGFUSE_AUTH_CHECK` | `false` | 启动时校验密钥（发一次请求） |
| `LANGFUSE_DEBUG` | `false` | SDK 调试日志 |

---

## 6. 代码是怎么走的

### 6.1 三个入口

```python
from agent.observability import (
    init_tracing,        # 启动调一次（web_main.lifespan）
    instrument_config,   # 每次请求调一次（chat.py）
    shutdown_tracing,    # 关闭调一次（web_main.lifespan）
    tracing_status,      # /health 用
)
```

`web_main.py` 的 lifespan：

```python
tracing_ready = init_tracing()          # 必须在 agent_loader.initialize() 之前
logger.info("Langfuse 链路追踪: %s", "已启用" if tracing_ready else "未启用（已降级）")

await agent_loader.initialize()
yield
await agent_loader.shutdown()
shutdown_tracing()                      # 退出前把缓冲区推干净
```

`chat.py` 的 `stream_chat_response()`：

```python
config = agent_loader.create_config(thread_id, user_id=user_id)

config = instrument_config(
    config,
    user_id=user_id,
    thread_id=config["configurable"]["thread_id"],
    stage="resume" if resume_data is not None else "chat",
)
```

`instrument_config` 只干两件事：往 `config["callbacks"]` 塞 handler，往 `config["metadata"]` 塞 trace 属性。

### 6.2 为什么 trace 属性走 metadata，不用 `propagate_attributes`

官方示例常用 `with propagate_attributes(user_id=..., session_id=...)`。它本质是 **OpenTelemetry 的 context manager**，靠 contextvar 传播。而我们这条链路是 **async generator（SSE 流式）**，`with` 块要跨过几十上百次 `yield`，contextvar 在协程切换时有丢的风险。

改成走 `config["metadata"]`。这是翻 SDK 源码才确认的——langfuse 的 LangChain handler 直接读这四个键：

```python
metadata["langfuse_user_id"]     # → trace.user_id
metadata["langfuse_session_id"]  # → trace.session_id
metadata["langfuse_trace_name"]  # → trace.name
metadata["langfuse_tags"]        # → trace.tags
```

纯数据传递，不依赖任何上下文状态。实测在 LangGraph 里从根 run 一路传到子节点都没丢：

```
trace attributes: {
  'session_id': 'thread-abc',
  'user_id': 'user-xyz',
  'trace_name': 'erp-chat',
  'tags': ['erp-openclaw', 'stage:chat']
}
```

---

## 7. 企业级是怎么保证不出事的

接可观测性最怕的是：**观测系统挂了把业务一起带走**。这里做了五层。

### 7.1 四道降级闸门

```python
if not settings.enabled:          return False   # 显式关闭
if not settings.has_credentials:  return False   # 没密钥
if not _sdk_installed():          return False   # 没装包
try: build client ...             except: return False   # 初始化异常
```

任何一关没过，`get_callback_handler()` 返回 `None`，`instrument_config()` 原样返回 config。实测无密钥时：

```
init_tracing -> False | handler: None
config 未改动(应无 callbacks): True ['configurable']
```

config 一个字段都没被污染，对话照常跑。

### 7.2 不制造日志噪音

SDK 在缺密钥时会自己打一条 `Authentication error ... Client will be disabled`。所以代码里**先判断密钥存在再建客户端**，不让这条噪音进日志。真正需要排查时看 `tracing_status()["last_error"]`。

### 7.3 单例复用

客户端和 handler 全局只建一次。Langfuse 客户端背后是 OTel tracer provider + 批量上报线程池，每个请求建一个会直接把连接数和内存打爆。

### 7.4 shutdown 锁存，防「僵尸上报」

实测抓到的坑：`shutdown_tracing()` 之后再收到请求，`get_callback_handler()` 看到 `_initialized=False` 会**重新初始化**，进程明明在退出，又起了一个上报线程。

加 `_shutdown_latched` 解决：

```python
def init_tracing(*, force=False):
    if _shutdown_latched and not force:
        return False    # 已显式关闭，不再自动复活
```

验证：关闭后 `config 是否被改动: False`，图照常跑完出答案。

### 7.5 敏感信息脱敏

trace 里存的是用户原话和工具返回的原始数据，可能带密钥、手机号、数据库口令。默认开 mask：

```
入: sk-abcdefghijklmnop123456
出: sk-***MASKED***

入: postgresql://postgres:p@ssw0rd@db:5432/langgraph_db
出: postgresql://postgres:***MASKED***@db:5432/langgraph_db

入: 13812345678              出: ***MASKED_PHONE***
入: zhang.san@example.com    出: ***MASKED_EMAIL***
```

覆盖 5 类：模型 API Key、Bearer token、`postgresql/mysql/redis/mongodb` 连接串口令、大陆手机号、邮箱。递归处理 dict/list，限制深度 6 层、单层 200 项、单字符串 2 万字符，避免大 payload 拖慢上报。

---

## 8. 实测记录

在 Python 3.13.16 + langfuse 5.2.0 上跑的，不是估的：

| 验证项 | 结果 |
|---|---|
| 无密钥 | `init_tracing=False`，config 原样返回，业务不受影响 |
| 有密钥 | handler 为 `LangchainCallbackHandler`，注入成功 |
| 幂等性 | 同一 config 重复注入，callbacks 仍只有 1 个 |
| 真实 LangGraph 图（tool-calling） | 6 chain / 2 LLM / 1 tool span 全部捕获 |
| 根 trace 命名 | `['erp-chat', 'agent', 'RunnableCallable', 'tools', ...]` |
| trace 属性 | user_id / session_id / trace_name / tags 全落到根 run |
| 关闭追踪后 | config 无污染，图正常运行出答案 |
| 含 `@` 的数据库口令脱敏 | ✅（踩过坑，见 9.3） |

---

## 9. 踩过的坑

### 9.1 `CallbackHandler` 不接受 userId / sessionId

网上示例（尤其 JS 版）写的是：

```python
CallbackHandler(session_id="...", user_id="...")   # ❌ TypeError
```

5.x 的真实签名只有两个参数：

```python
CallbackHandler(*, public_key: Optional[str] = None, trace_context: Optional[TraceContext] = None)
```

trace 属性只能走 `config.metadata`（或 `propagate_attributes`）。

### 9.2 `MaskFunction` 的参数是 keyword-only 的 `data`

照着直觉会写成 `def mask(text: str) -> str`，实际协议是：

```python
class MaskFunction(Protocol):
    def __call__(self, *, data: Any, **kwargs: Any) -> Any: ...
```

返回值还必须可 JSON 序列化，不能返回自定义对象。

### 9.3 口令里带 `@` 会把脱敏截断

第一版正则用 `([^@/\s]+)@`，遇到这种口令就翻车：

```
postgresql://postgres:p@ssw0rd@db:5432/langgraph_db
                     ↑ 只吃到 p，剩下 ssw0rd@ 露在外面
```

改成贪婪匹配到**最后一个** `@`：

```python
r"(?i)\b(mongodb(?:\+srv)?|redis|mysql|postgres(?:ql)?)://([^:@/\s]+):([^\s\"']*)@"
```

### 9.4 模型价格表里没有 DeepSeek

Langfuse 内置价目表只覆盖主流厂商，`deepseek-v4-pro` 不在里面，结果是 **token 数正常、成本显示 0**。

解决：控制台 Settings → Models 手动加一条自定义模型，填上输入/输出单价，之后成本才准。

另外可能看到 `Langfuse was not able to parse the LLM model` 警告——那是测试用假模型没有模型名，真实链路 `ChatOpenAI(model="deepseek-v4-pro")` 不会出现。

### 9.5 初始化顺序错了会漏掉 span

`init_tracing()` 必须在 `agent_loader.initialize()` **之前**。放后面的话，启动阶段跑过的 LangChain run 不会被采集。

### 9.6 退出时不 flush 会丢最后几秒数据

SDK 是攒批上报（默认 512 条 / 5 秒）。进程被 kill 或正常退出前不调 `shutdown_tracing()`，缓冲区里的 trace 全部丢失。所以它挂在 lifespan 的关闭段里。

---

## 10. 排错清单

**1. 启动日志显示「未启用（已降级）」**

```bash
curl http://localhost:8090/health
```

看 `tracing.last_error`，它会直接告诉你卡在哪一关：缺密钥、没装包，还是初始化异常。

**2. 显示已启用，但控制台没数据**

- `LANGFUSE_BASE_URL` 区域对不对（欧盟/美国/日本三个区地址不通用）
- 进程退出前有没有走 `shutdown_tracing()`
- 临时 `LANGFUSE_DEBUG=true` 重启，SDK 会打上报细节
- `LANGFUSE_AUTH_CHECK=true` 重启一次，密钥错的话启动就报

**3. 有 trace 但没有 user / session**

metadata 没传到。确认 `instrument_config()` 在 `astream()` **之前**调用，且用的是它的返回值（不要调完还传旧 config）。

**4. 有 trace 但没 token / 成本**

模型名 Langfuse 不认，按 9.4 加自定义价目表。

**5. 数据延迟十几秒才出现**

正常。默认攒 512 条或每 5 秒推一次。要立刻看就调小 `LANGFUSE_FLUSH_INTERVAL=1.0`，或在关键路径后手动 `flush_tracing()`。

---

## 11. 成本与性能

- **延迟**：异步批量上报，请求线程上只做 span 落盘，实测对单次对话影响在个位数毫秒级。
- **内存**：缓冲区默认 512 条 span，正常量级下几 MB。
- **流量**：一次完整对话（含子 Agent）大约几十 KB 上报数据。
- **费用**：云版按 trace 数计费，有免费额度；自托管不额外收费。

流量大的时候把 `LANGFUSE_SAMPLE_RATE` 设成 0.1，10% 采样足够排查问题，成本直接降到十分之一。

---

## 12. 后续可以加什么

按性价比排序：

1. **用户反馈打分**：前端加 👍/👎，后端调 `langfuse.create_score(trace_id=..., name="user_feedback", value=1)`，之后能看「哪类问题答得差」。需要先把 trace_id 通过 SSE 的 done 事件回传给前端。
2. **把评测集接进来**：仓库里已有 `tests/test_eval_consistency.py`，把线上答错的 case 沉淀成 dataset，改 prompt 后跑对比。
3. **Prompt 管理**：`subagents/configs/*.yaml` 里的 system prompt 挪到 Langfuse，改 prompt 不用发版。
4. **补非 LLM 环节的 span**：给沙箱创建、MCP 建连加 `@observe()`，能看到「模型之外」的耗时分布。
5. **告警**：按 error 率、P95 耗时配告警，比翻日志快。

---

## 附：本次改动清单

| 文件 | 变更 |
|---|---|
| `src/agent/observability/__init__.py` | 新增，对外导出 |
| `src/agent/observability/langfuse_tracing.py` | 新增，全部接入逻辑（约 460 行） |
| `src/agent/env_utils.py` | 新增 `LANGFUSE_*` 变量；顺带修正 `POSTGRES_URI` 默认值里重复拼接的 host/db |
| `src/api_view/api/chat.py` | 加 1 行 import + 1 处 `instrument_config` |
| `src/api_view/web_main.py` | lifespan 初始化/关闭 + `/health` 暴露追踪状态 |
| `requirements.txt` | 加 `langfuse>=5.0,<6.0` |
| `.env.example` | 追加 Langfuse 配置段（默认注释，按需取消） |
| `docs/Langfuse可观测性接入.md` | 本文档 |
