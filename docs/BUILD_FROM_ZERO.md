# 从 0 到 1 搭建 ERP 采购智能助手

> **这份讲义按"搭建顺序"写，不是按"文件列表"写。**
> 每一章回答三个问题：这一步要加什么能力 → 代码怎么长出来 → 为什么必须这么长。
>
> 章节骨架对齐课程《从 Agent 到手撕 OpenClaw 的企业实战》的七章：
> 概念 → DeepAgents → 文件后端 → 子 Agent → 沙箱 → 上下文工程 → 项目实战。
> 每章末尾都有「这一版还缺什么」，把学习路径连成一条链。

---

## 第 0 章 先看终局：你要造出来的东西长什么样

### 0.1 一句话

一个能听懂"帮我看看这个台面的料，哪家便宜、库存够不够、这个项目花了多少"，并且能在**人工审批**下真的把订单写进数据库的采购助手。

### 0.2 最终形态（照抄这张图，后面每章补一块）

```
浏览器（Vue3 + SSE）
      │  POST /api/chat/stream   （X-User-Id）
      ▼
FastAPI（api_view）── lifespan：连接池 → 沙箱预热 → 等第一个请求
      │
      ▼
主 Agent（协调者，只有 4 个通用工具）
      ├── task ──▶ 分析子 Agent（比价 / 库存 / 出图 / 知识检索）
      │                 └── MCP 只读工具：supplier_ / material_ / inventory_ / project_
      ├── task ──▶ 订单子 Agent（下单 / 改单 / 查单）
      │                 └── MCP 写工具：order_*  → interrupt（HITL 审批）
      │
      ├── 沙箱（OpenSandbox，一人一个，启动预热，挂了自愈）
      │       ├── /AGENTS.md        主 Agent 的行为准则
      │       ├── /skills/          技能包（SKILL.md + 脚本）
      │       ├── /memories/{user}/ 用户偏好（落 StoreBackend，按 user 隔离）
      │       └── /analysis/        出图与中间产物
      │
      └── 持久化：PostgreSQL（checkpoint / 展示消息 / 沙箱注册表 / erp 业务库）
```

### 0.3 为什么是这个形状（先记住三条，后面每章会回到它们）

| 原则 | 具体表现 | 违反会怎样 |
|---|---|---|
| **上下文要省着花** | 主 Agent 不持有业务工具，只做协调；明细交给子 Agent | 一个 40 个工具的 Agent，光工具描述就吃掉几千 token，且极易跑偏 |
| **写操作必须有闸** | 下单/改单走 `interrupt` 人工审批 + 幂等指纹 | Agent 幻觉一次，库里就多一张错单，且可能重复下单 |
| **外部资源当作会挂的** | 沙箱池化 + 预热 + 探活 + 重建 + 熔断 | 一次沙箱抖动 = 用户看到 500，或更糟：无限重试把机器打满 |

### 0.4 先跑起来还是先理解

**先跑起来**。这份讲义的每一章都给你一段"最小可跑"的代码，先让它跑出输出，再去读项目里的实现——
因为 Agent 框架的概念密度很高，先有体感再谈设计，效率差三倍。

---

## 第 1 章 概念：Agent 到底是什么

### 1.1 一句话区分「调用大模型」和「Agent」

```
调用大模型：输入 → 模型 → 输出            （一次函数调用）
Agent：     输入 → 模型 → 决定调工具 → 看工具结果 → 再思考 → …… → 输出
```

模型是**大脑**，工具是**手**，Agent 是那个"根据观察反复决定下一步"的循环。

### 1.2 ReAct 循环（整套框架的地基）

```
Thought（我该干什么） → Action（调哪个工具、参数是什么） → Observation（工具返回）
      ↑                                                         │
      └──────────────────── 带着新观察继续 ─────────────────────┘
```

三个工程含义，直接决定你后面怎么写代码：

1. **工具描述就是提示词**。模型靠 `docstring` 决定"什么时候用、参数怎么填"，
   所以本项目每个 MCP 工具都写了详细的口径说明（见第 7 章的 `project_query`）。
2. **观察会进上下文**。工具返回多长，上下文就吃多长——这是"子 Agent 隔离"的根本动机（第 4 章）。
3. **循环必须有刹车**。`ModelCallLimitMiddleware(50)` 和 `ToolCallLimitMiddleware(200)`
   不是可选项，是防止模型陷入死循环的保险丝。

### 1.3 Workflow 与 Agent 的边界

| | Workflow（编排） | Agent（自主） |
|---|---|---|
| 路径 | 预设（串联/并行/路由） | 运行时决定 |
| 可控性 | 高 | 低 |
| 适合 | 步骤稳定的业务（审批流、报表） | 开放问题（"这个料哪家便宜"） |

本项目是**混血**：外层是 Workflow（FastAPI 端点 → SSE 流 → 持久化），
内层是 Agent（主 Agent 自主决定委派给谁）。这个组合在工程上最稳——
**边界用 Workflow 锁住，中间用 Agent 放开**。

### 1.4 Harness Engineering：为什么最近都在说"脚手架"

模型能力差不多的时候，差距在**脚手架**：

```
Harness = 提示词 + 工具契约 + 上下文管理 + 记忆 + 权限闸门 + 可观测
```

本项目的 Harness 清单（每一项后面都有对应章节）：

| 组件 | 作用 | 章节 |
|---|---|---|
| AGENTS.md | 主 Agent 的行为准则（精简版协调者） | 第 6 章 |
| 子 Agent + 工具前缀分组 | 上下文隔离 + 权限隔离 | 第 4 章 |
| 虚拟文件系统（后端） | 让 Agent 有"工作目录" | 第 3 章 |
| 沙箱 | 代码执行 + 隔离边界 | 第 5 章 |
| 中间件 | 注入上下文、摘要、记忆、熔断 | 第 6 章 |
| HITL + 幂等 | 写操作的两道闸 | 第 7 章 |

### 1.5 OpenClaw 是什么（以及"手撕"意味着什么）

OpenClaw 是课程里给"一个能读写文件、能执行代码、能自己装技能的个人 Agent"起的代号。
它的能力拆开就三样：

1. **有文件系统**（所以能写代码、存产物）→ 第 3 章的 Backend
2. **有执行环境**（所以能跑代码、装依赖）→ 第 5 章的 Sandbox
3. **有分工**（主 Agent 协调、子 Agent 干活）→ 第 4 章的 SubAgent

**"手撕"的意思是：不用魔法，用三章时间把这三件事各自实现一遍。**

> 这一章还缺什么 → 你知道了 Agent 是什么，但一个真实的 Agent 循环长什么样、怎么跑起来。下一章开工。

---

## 第 2 章 第一个 DeepAgent：从 20 行到能跑

### 2.1 环境（本项目实际版本约束）

```bash
python -V            # 3.12+（本项目在 3.12/3.13 上验证）
docker -v            # 沙箱与 PostgreSQL 都跑在 docker 里
node -v              # 18+（前端）
```

依赖里最需要注意的不是"装多少"，而是**版本上限**：

```
deepagents>=0.7.0
langchain-mcp-adapters  # 要 mcp<2.0.0
fastmcp>=3.2.0,<4.0.0  # fastmcp 4.x 要求 mcp>=2 —— 与上面直接冲突
mcp>=1.24.0,<2.0.0
```

> 这是真的会卡住新人的坑：全新安装时 pip 会把你拉到 `fastmcp 4 + mcp 2`，
> 然后 `langchain-mcp-adapters` 起不来。**先把上限写进 requirements，再装**。

### 2.2 最小可跑：三个工具 + 一个 Agent

```python
# minimal_agent.py —— 这段能直接跑，用来建立体感
import os
from langchain.chat_models import init_chat_model
from langchain_core.tools import tool
from deepagents import create_deep_agent

model = init_chat_model("deepseek-chat", model_provider="openai",
                        base_url="https://api.deepseek.com",
                        api_key=os.environ["DEEPSEEK_API_KEY"])

@tool
def add(a: int, b: int) -> int:
    """把两个整数相加。"""
    return a + b

@tool
def multiply(a: int, b: int) -> int:
    """把两个整数相乘。"""
    return a * b

agent = create_deep_agent(model=model, tools=[add, multiply],
                          system_prompt="你是一个算术助手，只做计算，不要猜。")

result = agent.invoke({"messages": [("user", "12 乘 7 再加 5 是多少")]})
print(result["messages"][-1].content)
```

跑通以后你会观察到三件事，这三件事就是后面所有设计的原因：

1. 模型**先调工具再回答**（可以在日志里看到 tool_calls）；
2. 工具描述写错（比如 docstring 写成"加法"却做乘法），模型就会用错；
3. 只有两三个工具时很顺，**工具一多就开始乱**——这就是子 Agent 存在的理由。

### 2.3 项目里的那一次调用（逐参数）

`src/agent/main_agent.py` 的 Phase 9：

```python
agent_graph = create_deep_agent(
    model=MAIN_MODEL,                       # 主模型（deepseek-v4-pro）
    system_prompt=system_prompt,            # 来自 memory/prompts.py
    skills=["/skills/main/"],               # 技能目录（沙箱内路径）
    memory=[AGENTS_MD_FILENAME],            # /AGENTS.md 作为记忆型指令
    tools=[web_search, knowledge_search,    # 主 Agent 只留 4 个通用工具
           assign_skill, download_sandbox_file],
    subagents=subagents,                    # 两个子 Agent（第 4 章）
    middleware=main_middleware,             # 中间件栈（第 6 章）
    backend=backend,                        # 文件后端（第 3 章）
    store=get_store(),                      # 长期存储（PostgresStore）
    checkpointer=get_checkpointer(),        # 对话状态持久化（PostgresSaver）
    context_schema=ProcurementContext,      # 运行时上下文类型（user_id/username）
)
```

四个参数值得单独说：

**① `tools` 只有 4 个，这是刻意的。**
业务查询和下单工具一个都不给主 Agent，全部通过 `task` 委派。原因是"协调者的上下文要干净"：
主 Agent 每多一个工具，就多一段工具描述 + 多一种走错路的可能。

**② `store` 与 `checkpointer` 是两件事，常见混淆。**

| | 存什么 | 谁读 |
|---|---|---|
| `checkpointer` | 每个 thread 的消息与图状态 | LangGraph 恢复对话、HITL 中断 |
| `store` | 跨 thread 的键值（技能、用户偏好） | 中间件按 namespace 读写 |

**③ `context_schema` 让"用户"成为一等公民。**
`ProcurementContext(user_id, username)` 一经传入，后端路由、记忆命名空间、日志都从它取值——
不用全局变量，也不会把 A 用户的记忆写进 B 用户的空间。

**④ `backend` 是函数而不是实例。**
```python
def backend(rt):
    return CompositeBackend(default=sandbox_backend, routes={...})
```
因为 `/memories/` 的命名空间依赖运行时上下文，只有拿到 `rt` 才知道给谁开哪个空间。这是第 3 章的主题。

### 2.4 怎么验证"它真的在按我想的工作"

```bash
# 1) 语法与导入
python -m compileall -q src

# 2) 纯标准库单测（不碰模型、不碰网络）
python -m unittest discover -s tests

# 3) 静态检查
ruff check src tests scripts

# 4) 起 MCP，用协议层自检确认工具契约
python -m mcp_server.server_main          # 终端 1
python scripts/smoke_mcp.py               # 终端 2
```

这四条命令是本书所有章节的"回归闸门"。**每加一块能力就跑一次**，别攒到最后。

> 这一章还缺什么 → Agent 能思考、能调工具了，但它的"手"还伸不到文件系统，读不了 AGENTS.md，存不了产物。下一章补文件后端。

---

## 第 3 章 文件后端：给 Agent 一双手和一个工作目录

### 3.1 为什么 Agent 需要"文件"

模型本身没有状态。它要"记住"上一轮写的分析结果、要读一份 SKILL.md、要把图表存下来，
都需要一个**能被读写的地址空间**。DeepAgents 把这层抽象成 `backend`：

```
Agent 看到的：   /AGENTS.md  /skills/x/SKILL.md  /memories/u1/preferences.md  /analysis/a.png
实际存在哪：     沙箱容器?   Store 数据库?       本地磁盘?         ——  由 backend 决定
```

同一个路径可以映射到完全不同的物理存储，这就是 Compose 的力量。

### 3.2 最小实现：一个能读写的假文件系统

```python
# 先用内存字典跑通"Agent 能写文件并读回来"，再去接真实存储
class MemoryBackend:
    def __init__(self):
        self.files = {}
    def write(self, path, content):
        self.files[path] = content
        return {"ok": True}
    def read(self, path, offset=0, limit=2000):
        return self.files.get(path, "")
    def ls_info(self, path):
        return [p for p in self.files if p.startswith(path)]
```

把 `backend=MemoryBackend()` 传进 `create_deep_agent`，然后让模型"写一个待办清单到 /todo.md 再读出来"——
你会看到它真的分两步调用了 write_file / read_file。**这一步跑通，Backend 的概念就成立了。**

### 3.3 项目里的四层后端

`src/agent/main_agent.py` Phase 1.5：

```python
def backend(rt):
    """按运行时上下文构造后端：只有拿到 context 才能确定记忆命名空间。"""
    return CompositeBackend(
        default=sandbox_backend,                     # 其余路径 → 沙箱
        routes={
            "/memories/": StoreBackend(              # 用户记忆 → 数据库
                runtime=rt,
                namespace=lambda inner_rt: (
                    getattr(inner_rt.runtime.context, "user_id", None) or DEFAULT_USER_ID,
                ),
            ),
            "/persisted-skills/": StoreBackend(      # 持久技能 → 数据库
                runtime=rt,
                namespace=lambda inner_rt: SKILLS_STORE_NAMESPACE,
            ),
        },
    )
```

三条路由，三种生命周期：

| 路径 | 落到哪 | 生命周期 | 为什么 |
|---|---|---|---|
| `/AGENTS.md`、`/skills/`、`/analysis/` | OpenSandbox 容器 | 沙箱存活期（数小时） | 要能被执行、被脚本读；沙箱重建后重新播种 |
| `/memories/{user_id}/` | PostgreSQL StoreBackend | 永久 | 用户偏好必须跨会话、跨沙箱存活；且**按 user 隔离** |
| `/persisted-skills/` | PostgreSQL StoreBackend | 永久 | 用户"教"给 Agent 的技能要留下来 |

**这里有个容易踩的坑**：`namespace` 是 lambda，不是常量。
如果写成 `namespace=("memories",)`，所有用户会共享一个记忆空间——
评测时看不出来，上线后就是"张三看到李四的偏好"。本项目的写法是
`lambda inner_rt: (context.user_id or DEFAULT_USER_ID,)`，取不到身份就落到一个**可见的**兜底用户，
绝不静默共用。

### 3.4 播种：沙箱里那些文件是谁写的

`src/agent/backends/sandbox_setup.py` 里 `setup_sandbox()` 做四件事：

```python
backend = OpenSandboxBackend(sandbox=sandbox)
_ensure_dirs(backend)     # 预创建 /analysis/temp 等目录
_seed_files(backend)      # 上传 SKILL.md 等技能文件
_create_venv(backend)     # 建 /opt/skills-venv 并预装依赖
return backend
```

**为什么要在创建沙箱时就把 venv 建好**：技能脚本要跑 Python，
如果第一次执行时才发现没依赖，那一步会超时失败；
把它放进"创建期"（用户看不见的 10~20 秒里），运行时就是热的。

**为什么 AGENTS.md 不在 sandbox_setup 里传**：它属于"Agent 装配"而不是"沙箱装配"。
所以它在 `main_agent.py` 的 Phase 1.4 单独上传——这样健康检查重建沙箱后，
中间件能重新上传一次（见第 5 章），职责边界清楚。

### 3.5 验证这一章

```python
# 用脚本直接驱动，不经过 Web
import asyncio
from agent.main_agent import create_main_agent
graph = await create_main_agent(user_id="demo")   # 会真的建沙箱
out = await graph.ainvoke({"messages": [("user", "把 /todo.md 写成三行待办再读回来")]})
print(out["messages"][-1].content)
```

看日志里应该有：`write` → `/todo.md`、`read` → `/todo.md`。
如果模型说"我无法访问文件系统"，说明 backend 没挂上，或 `skills=/memory=` 路径写错了。

> 这一章还缺什么 → Agent 有了文件系统，但所有工具和所有任务都在一个上下文里。工具一多就会互相干扰。下一章把上下文切开。

---

## 第 4 章 子 Agent：把上下文和权限一起切开

### 4.1 为什么必须切

一个真实的采购 Agent 有 8 个 MCP 工具 + 6 个通用工具。全塞给一个 Agent 会出三个问题：

1. **工具描述吃上下文**：8 个工具的 docstring 轻松上千 token，每轮都要重发；
2. **模型会走错路**：让它"查一下价格"，它可能顺手调了 `order_create`；
3. **权限无法收敛**：只读的分析任务和写操作混在一个 Agent 里，没法单独加闸。

子 Agent 一次解决三件事：**上下文隔离 + 工具隔离 + 中间件隔离**。

### 4.2 YAML 声明（配置与代码分离）

`src/agent/subagents/configs/procurement_analyst.yaml`：

```yaml
name: procurement-analyst

description: >
  实验设备材料采购分析专家。负责供应商比价、物料行情分析、采购策略建议、
  市场调研、成本评估、库存预警补货建议等需要多步数据收集和深度分析的任务。
  当用户需求涉及"分析"、"对比"、"报告"、"建议"、"评估"、"行情"、"比价"、
  "筛选"等关键词时，应委派给此子 Agent。

tools:
  - supplier_query
  - material_query
  - material_price_compare
  - inventory_warning
  - project_query
  - generate_visualization
  - web_search
  - knowledge_search
```

`description` 不是注释，**它是路由提示词**——主 Agent 靠它决定"这个活该给谁"。
写"分析专家"没用，要写"什么时候该找我"（"涉及…关键词时，应委派给此子 Agent"）。

### 4.3 工具怎么分组：前缀即权限

`src/agent/tools/mcp_client.py`：

```python
ANALYST_TOOL_PREFIXES = ("supplier_", "material_", "inventory_", "project_")
ORDER_TOOL_PREFIXES   = ("order_",)
CHART_TOOL_PREFIXES   = ("generate_",)
```

这不是命名美观问题，而是**物理隔离**：

```
supplier_query / material_price_compare / inventory_warning / project_query
        └── 进分析子 Agent（只读）

order_create / order_update / order_search_details
        └── 进订单子 Agent（写，且必然经过 HITL）
```

前缀是"命名即契约"：新加一个工具，名字对了就自动归位，不用改两份配置。
第 7 章加 `project_query` 时，改的就是这一个元组 + 一行 YAML。

### 4.4 解析要 fail-fast，不要静默降级

`src/agent/subagents/loader.py` 支持四种声明语法：

```
supplier_query     精确名（默认，语义最明确）
prefix:order_      前缀匹配（一次收编一组）
suffix:_query      后缀匹配
glob:material_*    通配
```

关键设计：**未命中直接抛异常，启动失败**。

```python
class SubagentToolResolutionError(RuntimeError):
    """子 Agent 声明的工具名无法解析为真实工具时抛出（fail fast）。"""
```

为什么不做成"打条 WARNING 继续跑"：
工具名写错时，Agent 不会崩，它只会表现成"这个子 Agent 说它没有这个工具"，
你在联调时看到的是模型胡言乱语，排查要花几小时。**配置错误就该在启动时炸。**

### 4.5 主 Agent 怎么"派活"

`create_deep_agent(subagents=[...])` 之后，框架会给主 Agent 注入一个 `task` 工具。
主 Agent 的调用长这样（日志里能看到）：

```
task(subagent_type="procurement-analyst",
     description="比较 PP 台面在 3 家供应商的最新报价，给出最低价与价差")
```

注意 `description` 是**任务描述**而不是"用户原话"。好的委派会把约束带过去：
"只要 2026 年的报价，排除起订量大于 100 的"。这一层写不好，子 Agent 就会查一堆没用的数据回来。

### 4.6 子 Agent 也能有独立中间件

`src/agent/middleware_config.py`：

```python
def create_analyst_middleware(model, backend) -> list:
    return [
        create_summarization_tool_middleware(model, backend),  # 阶段完成就压缩上下文
        ModelCallLimitMiddleware(run_limit=50),
        ToolCallLimitMiddleware(run_limit=200),
    ]

def create_order_middleware() -> list:
    return [                              # 订单 Agent 不需要摘要：它只做简单调用
        ModelCallLimitMiddleware(run_limit=20),
        ToolCallLimitMiddleware(run_limit=50),
    ]
```

同一个项目里，两种角色给了不同的"预算"：分析要跑很多步（50/200），订单要快（20/50）。
**这就是"分工"的实际价值——不是结构好看，是每组参数可以分别调。**

### 4.7 验证这一章

```bash
# 1) 配置能加载、工具能解析（启动时就会炸，炸了说明配置错）
python -c "from agent.subagents.loader import load_subagent_configs; print([c['name'] for c in load_subagent_configs()])"

# 2) 故意写错一个工具名，确认它会抛异常而不是静默跳过
#    在 YAML 里把 supplier_query 改成 supplier_queryx，然后重启
#    → 期望：SubagentToolResolutionError
```

第 2 步是本项目最值得复述的测试——**它证明"静默降级"这条路被堵死了**。

> 这一章还缺什么 → 分工有了，但子 Agent 要跑代码、要装依赖、要隔离文件，都在哪跑？下一章进沙箱。

---

## 第 5 章 沙箱：代码执行、池化与自愈（本项目的核心亮点）

> 这一章是课程 PDF 里没有、但工程上最容易被追问的部分：
> **沙箱不是一个"能跑代码的工具"，而是一份外部有状态资源。** 凡是外部资源，就要考虑
> 创建耗时、复用、健康、重建、熔断。这四件事构成了本章。

### 5.1 先起一个沙箱服务

沙箱服务本身是独立进程（OpenSandbox Server），Agent 通过 HTTP 调它：

```bash
docker run --rm -d --name opensandbox-server -p 8080:8080 \
  opensandbox/opensandbox-server:latest
```

客户端连接配置（`src/agent/config.py`）：

```python
SANDBOX_CONFIG = ConnectionConfigSync(
    domain=os.environ.get("SANDBOX_DOMAIN", "http://127.0.0.1:8080"),
    use_server_proxy=True,
    request_timeout=timedelta(seconds=60),
    transport=httpx.HTTPTransport(limits=httpx.Limits(max_connections=20)),
)
```

> 课程原版这里写死了一个远程地址 `http://39.100.100.28:8080`。
> **换成本地 docker 只需要改这个环境变量**——这也是为什么它必须可配。

### 5.2 最小封装：只实现三个方法

DeepAgents 的 `BaseSandbox` 已经实现了 read / write / edit / ls / glob / grep（它们本质是"用 execute 调 shell"），
子类只要实现**三件真正需要和沙箱对话的事**：

```python
class OpenSandboxBackend(BaseSandbox):
    def execute(self, command, *, timeout=None): ...      # 跑命令
    def upload_files(self, files): ...                    # 传文件进去
    def download_files(self, paths): ...                  # 取文件出来
```

项目实现里有两个细节值得学（`custom_opensandbox.py`）：

```python
# 超时只能通过 RunCommandOpts 传（SDK 的 commands.run() 没有 timeout 参数）
try:
    from opensandbox.models.execd import RunCommandOpts   # 0.1.x 的真实位置
except Exception:
    from opensandbox.models import RunCommandOpts         # 其他版本的兼容位置
...
polling_strategy  # 轮询间隔，命令跑久了要退避，不能 0.1 秒硬转
```

**为什么容错导入**：SDK 小版本间导出位置会变。写死一个路径，
升级 SDK 那天整个项目起不来；两段 try 只多 4 行，换来升级不炸。

### 5.3 单沙箱方案的三个硬伤

本项目最初就是"每次创建 Agent 就新建一个沙箱"。跑起来后暴露三个问题：

| 问题 | 现象 | 根因 |
|---|---|---|
| 首请求慢 | 用户第一次提问要等 10~20 秒 | 建容器 + 装依赖都在请求链路上 |
| 文件不共享 | "刚才生成的分析结果"下一轮没了 | 每次请求换新沙箱 |
| 挂了就 500 | docker 重启后所有对话失败 | 没有探活与重建 |

于是有了 `sandbox_manager`。

### 5.4 沙箱池 + 预热：五态生命周期

```python
async def ensure_sandbox_for_user(user_id: str) -> SandboxBackendProxy:
    # 状态 1：进程内缓存
    proxy = SANDBOX_BACKENDS.get(user_id)
    if proxy is not None:
        if await _is_alive(proxy):        # echo ok 探活
            return proxy
        return await _recreate_sandbox(user_id, proxy)   # 挂了就重建

    # 状态 0：认领预热沙箱
    async with _warm_lock:
        if _warm_reserve is not None:
            proxy = _warm_reserve
            globals()["_warm_reserve"] = None
            SANDBOX_BACKENDS[user_id] = proxy
            await sandbox_registry.bind(user_id, proxy.id)
            asyncio.create_task(_replenish_warm())       # 后台补一个
            return proxy

    # 状态 2：注册表重连
    sandbox_id = await sandbox_registry.get_sandbox_id(user_id)
    if sandbox_id:
        proxy = await _connect(sandbox_id)
        if proxy is not None and await _is_alive(proxy):
            return proxy

    # 状态 3：新建
    return await _create_sandbox_for_user(user_id)
```

四个设计点，每个都可以被追问：

**① 为什么启动时预热？**
冷启动 10~20 秒。预热把这段耗时挪到"服务启动"（运维可接受），而不是"用户第一次点击"（不可接受）。
`pre_warm()` 失败只打 WARNING、不阻塞启动——否则"沙箱服务没起 = Web 服务也起不来"，运维连日志都看不到。

**② 为什么认领后要"补一个"？**
只预热一个，第一个用户拿走后池子就空了，第二个人照样等 20 秒。
`_replenish_warm()` 在后台异步补，让池子始终有一个备用。
补的时候如果发现别人已经补上了，就**立刻删掉自己多建的那个**——否则容器泄漏。

**③ 为什么要"注册表"而不是只靠内存？**
进程重启后内存里的映射全没了。注册表存 `user_id → sandbox_id`，
重启后可以**重连回原来的沙箱**（技能文件、分析产物还在）。
连不上怎么办？重建并覆盖绑定——**宁可多建一个，也不让用户卡住**。

**④ 为什么状态 1 的探活失败要"重建"而不是"直接报错"？**
沙箱是外部资源，挂了是常态不是异常。Agent 的正确反应是"换一个"，不是"告诉用户系统错误"。

### 5.5 换芯不换壳：SandboxBackendProxy

重建沙箱时有个隐蔽问题：`agent graph`、中间件、工具的闭包**早就持有了那个 backend 对象**。
如果直接换对象，它们手里的引用全指向已死的沙箱。

解法是稳定句柄：

```python
class SandboxBackendProxy(SandboxBackendProtocol):
    """代理所有 SandboxBackendProtocol 方法，支持热替换内部 backend。"""

    def __init__(self, backend):
        self._backend = backend

    def replace_backend(self, backend):   # ← 重建时只改这一行
        self._backend = backend

    def execute(self, command, *, timeout=None):
        return self._backend.execute(command, timeout=timeout)

    def upload_files(self, files):  return self._backend.upload_files(files)
    def download_files(self, paths): return self._backend.download_files(paths)
    # …其余 15 个方法逐一转发
```

**为什么把 18 个方法全写出来，而不是用 `__getattr__` 兜底？**
因为协议基类里那些方法有默认实现（抛 `NotImplementedError`）。
只靠 `__getattr__`，Python 的 MRO 会先命中基类的默认实现，代理形同虚设。
显式转发 + `__getattr__` 兜未来新增方法，这个组合才安全。

### 5.6 健康检查：每步探活 + 就地恢复

```python
class SandboxHealthMiddleware(AgentMiddleware):
    async def abefore_agent(self, state, runtime):
        if await self._check():          # echo ok
            return None
        await self._recover()            # 重建 + 重新播种 AGENTS.md
        return None

    async def _recover(self):
        from agent.backends.sandbox_manager import recreate_user_sandbox
        await recreate_user_sandbox(self._user_id)     # 代理换芯
        await asyncio.to_thread(
            self._backend.upload_files, [("/AGENTS.md", self._agents_md)]
        )
```

两个细节：
- **探活用 `echo ok`，不用 SDK 的状态接口**。状态接口在小版本间不稳定，
  而"能不能执行一条命令"才是我们真正关心的能力；
- **重建后必须重新上传 AGENTS.md**。所以它不在 `sandbox_setup` 里传（那是沙箱装配），
  而是由中间件负责（那是 Agent 装配）。

### 5.7 熔断：承认救不回来

健康检查能处理"偶发挂掉"，但如果沙箱服务整体不可用（docker 挂了、镜像拉不到），
每次重建都失败，而 Agent 每次失败都会再走一步 → 一次提问可能触发几十次建沙箱。

```python
class SandboxCircuitBreakerMiddleware(AgentMiddleware):
    @hook_config(can_jump_to=["end"])
    def before_model(self, state, runtime):
        count = count_consecutive_sandbox_errors(state.get("messages", []))
        if count <= self.threshold:
            return None
        return {"jump_to": "end",
                "messages": [AIMessage(content=f"{CIRCUIT_BREAKER_MARKER}: 连续 {count} 次沙箱操作失败…")]}
```

判断逻辑被刻意抽到纯函数（`middlewares/sandbox_error_signals.py`）：

```python
def count_consecutive_sandbox_errors(messages) -> int:
    count = 0
    for msg in reversed(messages):
        if msg.type == "tool":
            if msg.status == "error" and looks_like_sandbox_error(msg.content):
                count += 1
                continue
            break                     # 只数"连续"，不是累计
        if msg.type == "ai":
            if CIRCUIT_BREAKER_MARKER in msg.content:
                return 0              # 已经熔断过 → 计数归零
            continue
        if msg.type in {"human", "system"}:
            break                     # 用户重新提问 → 新回合
    return count
```

**为什么抽出来**：熔断是止损的最后一道闸，必须能被单测覆盖；
但中间件本体要 import `langchain.agents.middleware`（重依赖），
在 CI 的"纯标准库"那层跑不了。抽成零依赖纯函数后，裸环境也能跑 7 条断言。

这个改动本身就是一个真实的工程教训：**第一次推 CI 就挂了**（`ModuleNotFoundError: langchain_core`），
因为测试把重依赖带进了不该有依赖的那一层。

### 5.8 注册表：为什么从 MongoDB 换成 PostgreSQL

课程原版用 MongoDB 存 `sandbox_registry`。本项目改成 PostgreSQL，理由三条：

1. 项目其余持久化（checkpoint、展示消息、业务库）**全在 PG 上**，多一套 Mongo = 多一个容器、多一条备份链路、多一种故障模式；
2. 注册表只有一个"键值 + 时间戳"的形状，`INSERT ... ON CONFLICT` 完全够用，且原子；
3. 少一个能挂的服务。

```sql
CREATE TABLE IF NOT EXISTS ops.sandbox_registry (
    user_id    TEXT PRIMARY KEY,
    sandbox_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

**一致性取舍（面试爱问）**：数据库里的 `sandbox_id` 只是**索引，不是真相**。
真相是"这个容器还活着吗"。所以读到 id 但连不上时，不报错、直接重建并覆盖绑定。

### 5.9 出网白名单：沙箱的边界

沙箱能执行代码，就必须限制它能访问什么（否则等于给 Agent 一个自由的内网跳板）：

```python
SANDBOX_EGRESS_MODE = os.environ.get("SANDBOX_EGRESS_MODE", "allowlist")
# allowlist：默认拒绝，只放行必要主机（供应商网站等）
# 策略构造失败时 fail-closed 抛错；只有 SANDBOX_EGRESS_FAIL_OPEN=1 才降级放行
```

**fail-closed 的选择**：拿不到策略就"不给出网"，而不是"随便出网"。
代价是配置错了会连不上供应商站（功能不可用）；收益是**绝不会静默放开**。

### 5.10 验证这一章（不需要 LLM）

```bash
# 1) 沙箱服务在不在
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8080/

# 2) 管理器能不能建/认领（脚本）
python - <<'PY'
import asyncio
from agent.backends import sandbox_manager as m
async def main():
    await m.initialize("postgresql://postgres:postgres@127.0.0.1:5432/langgraph_db")
    await m.pre_warm()
    p1 = await m.ensure_sandbox_for_user("u1")   # 认领预热池
    p2 = await m.ensure_sandbox_for_user("u1")   # 命中缓存
    print("同一个沙箱:", p1.id == p2.id, p1.id)
    print(await m.stats())
    await m.shutdown()
asyncio.run(main())
PY
```

期望输出：`同一个沙箱: True`，`stats()` 里 `warm_reserve` 已被后台补上、
`registry_bindings` 里有 `u1`。

### 5.11 已知限制（诚实清单）

- **池是单机内存池**：多副本部署时每个副本各自预热、各自缓存；注册表共享但沙箱不跨副本复用。
  要做成共享池，得把分配/回收改成 `SELECT ... FOR UPDATE SKIP LOCKED` 的原子抢占；
- **`echo ok` 只能证明容器活着**，证明不了 venv/依赖完好；依赖坏掉要靠"连续报错 → 熔断 → 人工介入"兜底；
- **预热一个够单机低并发**，几十个用户同时在线时需要池大小 > 1 与 LRU 回收。

> 这一章还缺什么 → Agent 有了手（沙箱）、有了分工（子 Agent）、有了文件系统，但它的"脑子"里塞什么、什么时候该压缩、什么该记住，还没设计。下一章。

---

## 第 6 章 上下文工程：决定 Agent 好不好用的一层

### 6.1 上下文是稀缺资源

模型的上下文窗口看起来很大（十几万 token），但 Agent 每轮都在消耗：

```
system prompt + AGENTS.md + 技能清单 + 工具描述（8 个 MCP + 4 个本地）
+ 历史消息 + 工具返回（一次查询返回 30 行就是 1000 token）……
```

跑到第 10 轮，历史本身就能吃掉一半窗口。所以上下文工程不是"优化"，是**能不能用**。

四条常用手段，本项目全用了：

| 手段 | 目的 | 项目文件 |
|---|---|---|
| 精简 system prompt | 只写"怎么决策"，不写业务细节 | `agent/memory/prompts.py` |
| 技能渐进加载 | 只在需要时读 SKILL.md 全文 | `src/skills/**/SKILL.md` |
| 摘要压缩 | 长任务阶段完成就压 | `middlewares/tools_summarization.py` |
| 记忆外置 | 偏好写文件，不塞对话 | `middlewares/memory_update.py` |

### 6.2 AGENTS.md：主 Agent 的"岗位说明书"

```markdown
# ERP 采购智能助手 — 通用准则（明远实验装备）

你是**明远实验装备有限公司**的 ERP 采购智能助手。公司制造钢木/全钢实验台、通风柜…

## 你的角色
你是一个协调者，不直接查数据、不直接下单。
- 需要比价/分析/出图 → 委派 procurement-analyst
- 需要下单/改单/查单 → 委派 procurement-order
…
```

三条写作原则（从失败里总结出来的）：

1. **写"不要做什么"比"要做什么"更有用**：模型很擅长"多干活"，所以要明确"你不查数据"；
2. **写委派判据**，不写职责形容词："涉及比价/分析/报告 → 给 analyst"；
3. **不要把业务明细写进来**：供应商分级、审批金额这些放知识库（RAG），进 prompt 就撑爆。

### 6.3 技能：SKILL.md 是"按需加载的说明书"

```
src/skills/procurement/
├── procurement-analysis/SKILL.md      # 怎么做比价分析
├── supplier-price-urls/SKILL.md       # 各家供应商报价页在哪（含 url_mapping.yaml）
├── web-content-fetcher/SKILL.md       # 怎么抓网页正文
└── web-scraper/SKILL.md               # 反爬与解析策略
```

Agent 启动时只看到技能**清单**（名字 + 一句话描述），真正需要时才 `read /skills/xxx/SKILL.md`。
这就是"渐进式披露"——**清单吃 50 token，全文吃 2000 token，只在用的时候付**。

配套还有 `assign_skill` 工具：用户说"以后都按这个格式出报表"，
Agent 会把技能复制到 `/persisted-skills/` 并写进 Store——**用户能"教"Agent 新技能**。

### 6.4 摘要：长任务的呼吸阀

```python
def create_analyst_middleware(model, backend) -> list:
    return [
        create_summarization_tool_middleware(model, backend),
        ModelCallLimitMiddleware(run_limit=50),
        ToolCallLimitMiddleware(run_limit=200),
    ]
```

摘要把"已完成阶段的中间过程"压成结论，写回文件系统，然后从上下文里丢掉。
**用便宜的小模型做摘要**（`deepseek-v4-flash`）——摘要不需要聪明，需要稳定。

### 6.5 记忆：偏好要落在文件里

`MemoryUpdateMiddleware` 在对话结束后抽取"值得记住的事"，写进
`/memories/{user_id}/preferences.md`：

```markdown
- 输出偏好: table
- 常用币种: CNY
- recent_suppliers: [华东精密, 鑫达五金]
- recent_queries: [PP 台面比价, 3 月订单汇总]
```

**为什么不直接塞进 system prompt**：偏好是每个用户不同的，
如果塞 prompt，就得给每个用户生成一份 prompt（缓存全废）。
写进文件 + 按需读取，才能既不污染 prompt 又能个性化。

### 6.6 工具错误：单个工具失败不该炸掉整轮

```python
class ToolErrorMiddleware(AgentMiddleware):
    async def awrap_tool_call(self, request, handler):
        try:
            return await handler(request)
        except Exception as e:
            return ToolMessage(
                content=json.dumps({"error": str(e)[:500], "status": "error"}, ensure_ascii=False),
                tool_call_id=_tool_call_id(request),
                status="error",
            )
```

把异常变成 `ToolMessage(status="error")` 的意义：**模型能看到错误并自我纠正**
（比如换个参数重试），而不是整个请求 500。
注意这个转换也是第 5 章熔断的前提——熔断靠的就是数"连续的 error ToolMessage"。

### 6.7 中间件顺序（这一层最容易讲不清）

`main_agent.py` Phase 8 的实际顺序：

```python
main_middleware = [
    ContextInjectionMiddleware(),        # 1. 把 user_id/username 注入 system prompt
    SkillsSyncMiddleware(sandbox_backend),        # 2. 本地技能 → 沙箱（增量同步）
    UserSkillsRestoreMiddleware(...),             # 3. 持久技能 → 沙箱（恢复）
    build_summarization_middleware(backend, SUMMARY_MODEL),  # 4. 摘要
    MemoryUpdateMiddleware(model=SUMMARY_MODEL),  # 5. 记忆更新
]
if user_id:
    main_middleware += [
        SandboxHealthMiddleware(...),           # 6. 沙箱探活 + 自愈
        SandboxCircuitBreakerMiddleware(...),   # 7. 连续失败熔断
    ]
main_middleware += [
    ToolErrorMiddleware(),                      # 8. 工具异常 → ToolMessage
    ModelCallLimitMiddleware(run_limit=50),     # 9. 模型调用上限
    ToolCallLimitMiddleware(run_limit=200),     # 10. 工具调用上限
]
```

顺序的三条理由：

1. **上下文注入必须最先**：后面所有中间件都依赖 `user_id`；
2. **健康检查在熔断之前**：先尝试恢复（乐观），恢复不了才熔断（悲观）；
3. **工具错误包装在限流之前**：这样限流统计的是"真实调用"，而不是把异常也算一次失败。

### 6.8 验证这一章

```bash
# 1) 中间件能否装配（启动不报错即通过）
python -c "import asyncio; from agent.main_agent import create_main_agent; \
asyncio.run(create_main_agent(user_id='demo')); print('OK')"

# 2) 记忆是否真的按 user 隔离
#    用 u1 说"我喜欢表格"，再用 u2 问"我喜欢什么格式"，u2 应该不知道
```

第 2 条是**多用户场景必测项**——namespace 写错时，本地单人测试永远发现不了。

> 这一章还缺什么 → 单机 Agent 齐了。但一个能用的产品还差：业务工具（MCP）、数据层（PG）、写操作闸门（HITL）、流式前端、评测。最后一章把它们装到一起。

---

## 第 7 章 项目实战：把六章能力装成一台机器

### 7.1 11 阶段装配（看懂这一节，整个项目就通了）

`create_main_agent()` 每次调用走一条固定流水线（日志里会逐步打印）：

```
Phase 1    拿到沙箱（manager 分配 or 自建）→ 包成 SandboxBackendProxy
Phase 1.4  上传 /AGENTS.md 到沙箱
Phase 1.5  CompositeBackend 分流（/memories/、/persisted-skills/ → Store）
Phase 2    连接 MCP Server，加载 8 个业务工具 + 图表工具
Phase 2.x  图表工具 26→1 合并（generate_visualization）
Phase 3.6  创建技能工具（assign_skill）与下载工具（download_sandbox_file）
Phase 4    构建工具解析池（分析类 + 订单类 + 图表 + 本地工具）
Phase 5    加载子 Agent YAML
Phase 6    为子 Agent 装配中间件
Phase 7    解析工具名（fail-fast）
Phase 8    装配主 Agent 中间件栈（第 6 章那 10 个）
Phase 9    create_deep_agent() 编译
```

为什么要分阶段而不是写一个 500 行的函数：**每个阶段都可能失败，而失败原因完全不同**。
沙箱失败（Phase 1）→ 检查 docker；MCP 失败（Phase 2）→ 检查 8000 端口；
YAML 失败（Phase 5）→ 配置写错。日志里 Phase 编号就是排查入口。

### 7.2 MCP 工具层：三层分离

```
tools/*.py        （契约层）  工具名、参数、docstring、错误契约
    ↓
repository.py     （领域层）  SQL 聚合、状态机、金额校验、供应商分级
    ↓
db.py / PG        （存储层）  连接池、事务
```

**为什么不让工具直接写 SQL**：工具层是"给模型看的"，它该关心"参数怎么描述才不容易填错"；
SQL 聚合和业务规则（比如 D 级供应商禁止下单）是领域知识，放在 repository 里才能被复用和单测。

八个工具的命名就是权限边界：

| 工具 | 分组 | 谁用 |
|---|---|---|
| `supplier_query` | supplier_ | 分析子 Agent |
| `material_query` / `material_price_compare` | material_ | 分析子 Agent |
| `inventory_warning` | inventory_ | 分析子 Agent |
| `project_query` | project_ | 分析子 Agent |
| `order_create` / `order_update` / `order_search_details` | order_ | 订单子 Agent（写操作） |

### 7.3 数据层：PG 建库 + 领域规则

```bash
docker compose up -d       # 起 PG 并自动执行 db/init/01_schema.sql + 02_seed.sql
psql -f db/verify.sql      # 33 行自检：表存在吗、行数对吗、订单总额和明细对得上吗
```

演示数据规模（评测与自检都靠它，数字要能张口报）：

```
12 供应商 / 37 物料 / 45 报价 / 3 工程单 / 12 订单 / 24 出入库流水
6 张业务表 + 2 个视图（v_material_price_compare / v_stock_warning）
库存预警 8 条，订单总额与明细不一致 0 条
```

三条领域规则写在 `repository.py` 里，而不是提示词里：

```python
STATUS_FLOW = {1: [2, 5], 2: [3, 5], 3: [4], 4: [], 5: []}   # 1待审→2已审→3采购中→4入库；1/2可取消→5
# 总额一律由服务端按明细用 Decimal 重算，不接受调用方传入
# D 级供应商禁单（评级来自 supplier.grade）
```

**"金额由服务端重算"这条为什么重要**：如果信任模型传进来的 total，
它可能算错、也可能被提示词注入诱导改价。**模型可以建议，代码负责定稿。**

### 7.4 HITL：写操作的闸门

```
order_create 被调用
   ↓
工具内部 interrupt(...)        ← 图在这里挂起，状态存 PostgreSQL checkpoint
   ↓
SSE 推 interrupt 事件 → 前端弹审批卡片（金额/供应商/明细）
   ↓
用户点 approve → POST /api/chat/resume
   ↓
_has_pending_interrupt() 先确认真的有待处理中断（fail-closed）
   ↓
恢复执行 → 真正落库
```

**`fail-closed` 的取舍**（`chat.py` 里的注释写得很细）：
状态查询异常时返回 `False`（"无法证明有中断就不恢复"），
而不是 `True`（"查不到就放行"）。后者在 checkpointer 抖动时会让 approve 执行两遍。
**写操作宁可少执行一次，也不能重复执行。**

### 7.5 幂等：防重复下单的第二道闸

MCP 是 Agent 唯一的写入口，所以幂等放在工具层：

```python
# 业务指纹：剔除 orderNumber / orderTime 这类每次都会变的字段
fingerprint = sha256(json.dumps({k: v for k, v in payload.items()
                                if k not in ("orderNumber", "orderTime")}, sort_keys=True))
claimed, token = await idem.claim(fingerprint)   # 抢占 pending 占位（PRIMARY KEY 冲突即原子）
```

实测（10 线程同指纹并发）：

```
created=1, in_progress=9      ← 只有一个真的下单，其余拿到"处理中"
```

### 7.6 SSE 前端：让"Agent 在干活"可见

后端推的事件类型（`chat.py` 里的 `"type"` 字段）：

```
token        流式文本
tool_start   开始调工具（前端显示"正在查询供应商…"）
tool_args    工具参数（可折叠展示）
tool_result  工具返回（截断后展示）
tool_end     工具结束
interrupt    需要人工审批 → 前端弹卡片
done         本轮结束
error        出错
```

**为什么要把工具调用也推给用户**：Agent 的思考过程不可见时，用户只会觉得"卡住了"。
把 `tool_start/tool_result` 渲染出来，等待就变成了"能看见的进展"——这是体验上最划算的一件事。

### 7.7 评测：三层，只有两层能离线跑

```
src/eval/run_eval.py
  1. 检索层  rag_retrieval   期望来源是否出现在 knowledge_search 的结果里   ← 可离线（需 embedding 或降级）
  2. 答案层  domain_answer   把检索片段拼进 prompt，看答案是否含关键要点      ← 需 LLM
  3. 路由层  agent_routing   完整链路（沙箱 + MCP + PG）                      ← 标 skipped
```

13 条用例覆盖：审批金额分级、比价口径、D 级供应商、库存预警、订单状态机、台面选型、供应商 QSDC。

```bash
python src/eval/run_eval.py --skip-answer     # 只跑检索层，最快
python -m unittest discover -s tests          # 38 条纯标准库单测（CI 跑这套）
```

**为什么老实标注"路由层未实现"**：评测里最容易注水的地方就是"我有一套评测"。
把"实现了两层、第三层 TODO"写清楚，比假装全覆盖更能证明你知道评测该怎么分层。

### 7.8 RAG：这个项目到底需不需要（你问过的问题）

**结论：需要，但要小、要诚实地定位。**

需要，因为项目里确实有一类问题不该让模型凭记忆答："5 千的订单要谁审批""PP 台面和环氧树脂台面怎么选"
"供应商 QSDC 怎么评"。这类知识有明确出处（制度文件、行业手册），
让模型自由发挥就是纯幻觉。

不需要做大，因为这**不是**一个以检索为核心的系统：
- 语料只有 14 条（`knowledge_docs.py`），几万字的量级；
- 检索用余弦相似度即可，不需要 rerank、不需要混合检索、不需要向量数据库运维；
- 真正的难点在工具链路与写操作闸门，不在检索精度。

所以本项目对 RAG 的处理是：
1. `knowledge_search` 只做 top-k 检索并**带 source 出处**（回答里能引用）；
2. 文档长度写了断言（180–320 字），**保证每段都是"可检索的完整语义单元"**，不是一句话碎片；
3. embedding 不可用时**降级**而不是崩：`knowledge_search` 吞异常返回"检索失败"，
   Agent 可以继续用通用知识回答；
4. 评测里用"命中期望来源"做判据（客观、可自动化），不用"答案好不好"（主观、不可复现）。

**面试如果被问"你的 RAG 有什么特别之处"**，诚实的答法是：
"它不是这个项目的核心，我刻意没把它做大——14 条语料上做混合检索是自我感动。
我花的力气在'检索结果必须带出处'和'embedding 挂了要降级'这两件工程问题上。"
这比硬吹一个用不上的复杂检索链路更能加分。

### 7.9 部署与 CI

```bash
docker compose up -d          # PostgreSQL（自动灌 schema + seed）
python start_web.py           # 后端 8090 + 前端 3000（会先等 /health）
cd src && python -m mcp_server.server_main    # MCP 8000
```

CI 三个 job（`.github/workflows/ci.yml`）：

| job | 干什么 |
|---|---|
| 后端检查 | compileall + ruff + 38 条纯标准库单测 |
| 前端构建 | npm ci + vite build |
| 业务库 | 起真 PostgreSQL → 灌 schema/seed → 跑 30+ 数据断言 |

**第三个 job 是最值的**：它证明"schema / seed / repository 三者一致"，
不是靠肉眼读 SQL，而是每次提交都真跑一遍。

### 7.10 完整验证流程（交付前必须全绿）

```bash
python -m compileall -q src                      # 语法
ruff check src tests scripts start_web.py        # 静态
python -m unittest discover -s tests             # 38 条单测
ERP_DB_URI=... python scripts/smoke_db.py        # 30+ 数据断言（需 PG）
python scripts/smoke_mcp.py                      # 8 个工具契约（需 MCP 在跑）
cd frontend && npm run build                     # 前端
```

---

## 附录 A：7 天搭建路线（按章推进，每天可验收）

| 天 | 目标 | 产出 | 验收 |
|---|---|---|---|
| 1 | 概念 + 最小 Agent | 20 行 Agent 能加法 | 看到 tool_calls |
| 2 | 文件后端 | MemoryBackend 能读写 | 模型写出 /todo.md |
| 3 | 子 Agent + 工具分组 | 两个子 Agent 各带 3 个工具 | 主 Agent 会 task 委派 |
| 4 | 沙箱单机版 | setup_sandbox 能跑 echo | `docker ps` 里有容器 |
| 5 | 沙箱池化 + 自愈 | manager + proxy + health + breaker | 杀容器后 Agent 自动恢复 |
| 6 | MCP + PG | 8 个工具直连业务库 | smoke_mcp 全 PASS |
| 7 | HITL + 前端 + 评测 | 审批卡片能 approve | 端到端下一单 |

每一格的验收都是**可执行命令**，不是"我看了一下"。

## 附录 B：真踩过的坑（只收实际发生过的）

| 现象 | 根因 | 修法 |
|---|---|---|
| 首次访问数据库 `RecursionError` | `get_pg_pool()` 与 `_ensure_tables_async()` 互相 await | 建表函数直接用已建好的池，谁完成谁置位 |
| 子 Agent 说"我没有这个工具" | YAML 工具名写错，只打 WARNING | 严格模式：未命中直接抛 `SubagentToolResolutionError` |
| `pip install fastmcp` 后整个项目起不来 | `fastmcp 4` 要 `mcp>=2`，`langchain-mcp-adapters` 要 `mcp<2` | 双向上限锁版本 |
| SSE 流中 approve 执行两遍 | `_has_pending_interrupt` 异常时返回 True | 改 fail-closed |
| 用户看到别人的偏好 | `StoreBackend.namespace` 写成常量 | 改成按 `context.user_id` 的 lambda |
| 沙箱挂一次，一次提问建了十几个容器 | 只有健康检查、没有熔断 | 加连续错误阈值 + jump to end |
| CI 报 `No module named numpy` | 包入口 eager import 拉起重依赖 | PEP 562 惰性导出 |
| CI 报 `No module named langchain_core` | 新测试把重依赖带进"纯标准库"那层 | 把纯逻辑抽成零依赖模块 |
| 前端 `session.title` 为空时白屏 | 直接 `.slice()` 调用 | `(x \|\| '')` 兜底 |
| Windows 下连接池超时 30 秒 | ProactorEventLoop 与 psycopg 异步不兼容 | 导入期切 `WindowsSelectorEventLoopPolicy` |

## 附录 C：面试问答 → 章节映射

| 可能被问 | 去哪一章找答案 | 一句话答法 |
|---|---|---|
| 你这个 Agent 和直接调 API 有什么区别 | 第 1 章 | ReAct 循环 + 工具契约 + 刹车 |
| 为什么用 DeepAgents 而不是自己写 | 第 2 章 | 它把 backend/subagent/middleware 三个抽象做完了，我只需要填业务 |
| 子 Agent 解决了什么问题 | 第 4 章 | 上下文隔离 + 工具隔离 + 中间件隔离（不是结构好看） |
| 沙箱为什么不用完就删 | 第 5 章 | 创建 10~20 秒 + 文件要跨轮共享 + 挂了要能自愈 |
| 沙箱挂了怎么办 | 第 5 章 | 每步探活 → 换芯重建 → 连续失败熔断 |
| 为什么熔断还要单独做 | 第 5 章 | 恢复解决"偶发"，熔断解决"整体不可用"，没有熔断会无限重建 |
| MongoDB 换 PG 图什么 | 第 5 章 | 少一套组件少一种故障模式，注册表只是 KV |
| 上下文怎么省的 | 第 6 章 | 精简 prompt + 技能渐进加载 + 摘要 + 记忆外置 |
| 怎么防重复下单 | 第 7.5 | HITL 审批 + 业务指纹 + pending 原子抢占 |
| 评测做到什么程度 | 第 7.7 | 检索层/答案层可回归，路由层未实现（如实标注） |
| RAG 在这个项目里重要吗 | 第 7.8 | 不重要但要小、要带出处、要能降级 |
| 这个项目的最大缺陷 | 第 8 章 + README 已知限制 | 无鉴权、无权限模型、沙箱池是单机内存池 |

## 附录 D：文件导航（想深挖哪块看哪）

| 想看 | 文件 |
|---|---|
| 装配主线 | `src/agent/main_agent.py` |
| 沙箱池/预热/重建 | `src/agent/backends/sandbox_manager.py` |
| 代理换芯 | `src/agent/backends/sandbox_proxy.py` |
| 注册表（PG） | `src/agent/backends/sandbox_registry.py` |
| 沙箱创建与播种 | `src/agent/backends/sandbox_setup.py` |
| 健康检查 / 熔断 | `src/agent/middlewares/sandbox_health.py` / `sandbox_breaker.py` / `sandbox_error_signals.py` |
| 工具分组 | `src/agent/tools/mcp_client.py` |
| 子 Agent 解析 | `src/agent/subagents/loader.py` + `configs/*.yaml` |
| MCP 工具契约 | `src/mcp_server/tools/*.py` |
| 领域规则 | `src/mcp_server/repository.py` |
| 幂等 | `src/mcp_server/idempotency.py` |
| 会话与 SSE | `src/api_view/api/chat.py` |
| 数据层自检 | `scripts/smoke_db.py` / `db/verify.sql` |
| 评测 | `src/eval/run_eval.py` / `eval_cases.yaml` |

---

## 结语：从 0 到 1 的真正含义

这个项目的"1"不是一个能跑的 demo，而是**七件互相咬合的事都成立**：

1. Agent 有手（工具）也有工作目录（backend）；
2. 分工清楚（子 Agent），上下文不爆；
3. 沙箱能跑代码，且**会挂、会慢、会被重建**——这三件事都有对应机制；
4. 写操作有两道闸（HITL + 幂等）；
5. 知识有出处（RAG 带 source）；
6. 质量可回归（38 单测 + 30 数据断言 + 13 评测用例进 CI）；
7. 缺陷写清楚（README 已知限制那一节）。

**能讲清第 3 条和第 7 条的人，已经和"交作业"的人区分开了。**
