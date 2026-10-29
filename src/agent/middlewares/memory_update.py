"""
自动记忆更新中间件。

在每轮 Agent 回复完成后（aafter_agent 钩子），自动提取对话中涉及的
供应商名称和查询摘要，更新 StoreBackend 中的用户偏好文件。

Agent 无需手动维护 recent_suppliers / recent_queries —— 系统自动处理。

使用方式:
    from agent.middlewares.memory_update import MemoryUpdateMiddleware
    middleware = MemoryUpdateMiddleware(model=SUMMARY_MODEL)

P0 改进：偏好文件从"自己扫行的 Markdown 列表"改为
「YAML frontmatter（结构化，给 Agent 读）+ 正文（说明，给人读）」。
原因：
  1. 原实现靠行首判断 + 缩进解析 recent_suppliers:/recent_queries:，
     用户或 Agent 一旦把文件写成别的形状就可能解析错，甚至吃掉无关内容；
  2. 记忆里既有结构化字段（供程序与模型读取）又有说明文字（供人阅读），
     混在同一层 Markdown 里，任何一方改格式都会污染另一方；
  3. YAML 有标准解析器与转义规则，供应商名里带冒号、破折号、中文标点都不会破。
  向后兼容：读到旧格式时按 legacy 规则解析，下次写回自动迁移为新格式，无需人工干预。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import yaml
from pydantic import BaseModel, Field

from langchain.agents.middleware import AgentMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage

logger = logging.getLogger(__name__)


# ============================================================
# 实体抽取的结构化输出 Schema（P1：with_structured_output 改造）
# ============================================================
# 原实现的问题：prompt 里要求"Return ONLY a JSON object"，然后手动截取
# 第一个 { 到最后一个 } 之间解析——模型偶尔在 JSON 前后加解释文字、
# 输出截断或格式漂移时解析失败，只能静默丢弃本轮记忆。
#
# 新实现：用 Pydantic 模型定义输出结构，通过 with_structured_output
# 让模型直接返回已验证的实体对象（框架层处理 JSON 模式 / 工具调用模式，
# 不同模型后端自动适配）。手动解析保留为降级路径（见 _extract_entities）。
class _ExtractedEntities(BaseModel):
    """LLM 实体抽取的输出结构（仅内部使用，故以下划线命名）。"""
    suppliers: List[str] = Field(
        default_factory=list,
        description="对话中提到的公司/供应商名称，中英文均可，没有则为空列表",
    )
    query: str = Field(
        default="",
        description="用户采购需求的一句话摘要；与采购无关时为空字符串",
    )

# 触发自动更新的 ERP 业务关键词（中文）
_TRIGGER_KEYWORDS = [
    "供应商", "物料", "采购", "订单", "价格", "分析", "对比",
    "比价", "报价", "评估", "筛选", "推荐", "行情", "预算",
    "库存", "交货", "交期", "质量", "成本", "报价", "招标",
    "supplier", "part", "order", "price", "analysis",
]

# 跳过更新的无意义消息模式
_SKIP_PATTERNS = [
    "你好", "在吗", "嗨", "hello", "hi", "hey",
    "你能做什么", "你有哪些功能", "你是谁",
    "我之前的偏好", "我的偏好", "我的记忆",
]

# ---- 记忆合并上限 ----------------------------------------------------------
# 列表无限增长会把偏好文件变成流水账：既浪费每次读取的 token，也会稀释模型对
# 真正有用的偏好（输出格式、图表类型）的注意力。因此只保留最近 N 条。
MAX_RECENT_SUPPLIERS = 10
MAX_RECENT_QUERIES = 5

# 由本中间件维护的字段；frontmatter 里的其他键（Agent 或用户手写的偏好）原样保留
_MANAGED_KEYS = ("recent_suppliers", "recent_queries", "updated_at")

_DEFAULT_BODY = """# 用户偏好

- `preferred_output`: 输出形式（chart / table）
- `preferred_chart_type`: 首选图表类型（bar / line / pie ...）
- `preferred_currency`: 货币（CNY / USD ...）
- `preferred_language`: 语言（zh / en ...）

> `recent_suppliers` / `recent_queries` 由系统自动维护，Agent 无需手动修改。
"""


def _content_to_text(content: Any) -> str:
    """把 LangChain 消息的 content（str / list[dict] / 其他）压平成纯文本。

    消息内容在不同模型与不同链路下形态不一（纯字符串、内容块数组），
    这里做一次统一，避免每个调用点各写一遍 isinstance 判断。
    """
    if isinstance(content, list):
        return " ".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in content
        )
    return str(content) if content is not None else ""


def _is_meaningful_erp_exchange(messages: List[BaseMessage]) -> Optional[str]:
    """检查最后一条用户消息是否为有意义的 ERP 交互。

    Returns:
        用户消息文本（有意义时），或 None（应跳过）。
    """
    # 从后往前找最后一条用户消息
    last_user_msg = None
    for msg in reversed(messages):
        msg_type = getattr(msg, "type", None)
        if msg_type == "human":
            last_user_msg = msg
            break

    if last_user_msg is None:
        return None

    content = _content_to_text(last_user_msg.content).strip()

    if not content:
        return None

    # 跳过无意义消息
    content_lower = content.lower().replace(" ", "")
    for pattern in _SKIP_PATTERNS:
        if pattern.lower().replace(" ", "") in content_lower:
            return None

    # 检查是否包含 ERP 关键词
    has_erp_keyword = any(
        kw.lower() in content_lower for kw in _TRIGGER_KEYWORDS
    )
    if not has_erp_keyword:
        # 兜底：检查是否委派了子 Agent（messages 中有 task 工具调用）
        has_subagent_call = False
        for msg in messages:
            if hasattr(msg, "tool_calls") and msg.tool_calls:
                for tc in msg.tool_calls:
                    if tc.get("name") == "task":
                        has_subagent_call = True
                        break
            if has_subagent_call:
                break
        if not has_subagent_call:
            return None

    return content


def _extract_ai_summary(messages: List[BaseMessage]) -> str:
    """提取最后一条 AI 消息的前 300 字符作为摘要。"""
    for msg in reversed(messages):
        if getattr(msg, "type", None) == "ai":
            return _content_to_text(msg.content)[:300]
    return ""


async def _extract_entities(
    model: BaseChatModel, user_message: str, ai_summary: str
) -> Dict[str, Any]:
    """使用 LLM 从对话中提取供应商和查询摘要。

    双路径设计（P1 改造）：
      1. 首选 with_structured_output(Pydantic schema)：框架保证返回已验证的
         _ExtractedEntities 实例，不再依赖"模型乖乖只输出 JSON"；
      2. 降级路径：部分模型/网关不支持结构化输出（会抛异常），此时回退到
         原有的"截取花括号手动解析"——牺牲一点健壮性，但不让记忆功能整体失效。

    Returns:
        {"suppliers": [...], "query": "..."} 或 {"suppliers": [], "query": ""}
    """
    prompt = f"""Extract procurement-related entities from this conversation.

Rules:
1. "suppliers": Company/supplier names mentioned. Include both Chinese and English names. Empty list if none.
2. "query": One-line summary of the user's procurement need. Empty string if not procurement-related.

User message: {user_message}

Assistant response summary: {ai_summary}"""

    # ---- 路径 1：结构化输出（首选）----
    try:
        structured_model = model.with_structured_output(_ExtractedEntities)
        result = await structured_model.ainvoke(prompt)
        if result is not None:
            return {
                "suppliers": list(result.suppliers),
                "query": result.query,
            }
        # 某些后端在无法结构化时返回 None 而不抛错 → 走降级
        logger.info("MemoryUpdateMiddleware: 结构化输出返回空，降级为手动解析")
    except Exception as e:
        # 结构化输出不可用（模型不支持 / method 参数不兼容等）→ 降级
        logger.info(f"MemoryUpdateMiddleware: 结构化输出不可用（{e}），降级为手动解析")

    # ---- 路径 2：手动 JSON 解析（降级，兼容所有模型）----
    legacy_prompt = prompt + """

Return ONLY a JSON object, no other text:
{"suppliers": ["CompanyA", "CompanyB"], "query": "brief summary"}"""
    try:
        response = await model.ainvoke(legacy_prompt)
        text = _content_to_text(response.content).strip()

        # 模型常会在 JSON 前后加解释文字，这里取"第一个 { 到最后一个 }"之间作为兜底解析。
        # 解析失败只静默跳过（记忆是附带能力，不该影响用户这一轮回复）。
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            parsed = json.loads(text[start:end + 1])
            return {
                "suppliers": parsed.get("suppliers", []),
                "query": parsed.get("query", ""),
            }
    except Exception:
        logger.warning("MemoryUpdateMiddleware: LLM 提取失败，跳过本次更新", exc_info=True)

    return {"suppliers": [], "query": ""}


def _create_file_value(content_str: str) -> dict:
    """创建 StoreBackend 兼容的文件值（与 deepagents.backends.utils.create_file_data 一致）。"""
    lines = content_str.split("\n")
    now = datetime.now(timezone.utc).isoformat()
    return {
        "content": lines,
        "created_at": now,
        "modified_at": now,
    }


def _value_to_text(value: Any) -> str:
    """把 Store 里取出的 value 还原成文本。

    StoreBackend 的文件值是 {"content": [行, 行, ...]}（按行存），
    同时兼容历史写入的纯字符串形态。
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        content = value.get("content", [])
        if isinstance(content, list):
            return "\n".join(str(line) for line in content)
        return str(content)
    return str(value)


class MemoryUpdateMiddleware(AgentMiddleware):
    """在 Agent 回复后自动更新用户记忆文件中的 recent_suppliers / recent_queries。

    不依赖 Agent 自觉——中间件自动提取、合并、写回。
    """

    def __init__(self, model: BaseChatModel) -> None:
        super().__init__()
        self.model = model

    # ---- 同步钩子（不执行操作）----
    def after_agent(
        self, state: Dict[str, Any], runtime: Any
    ) -> Optional[Dict[str, Any]]:
        return None

    # ---- 异步钩子（核心逻辑）----
    async def aafter_agent(
        self, state: Dict[str, Any], runtime: Any
    ) -> Optional[Dict[str, Any]]:
        """Agent 回复完成后触发：提取实体并更新记忆。

        设计要点：本方法吞掉所有异常（只打日志）。
        记忆是"锦上添花"的能力，绝不能因为它失败而让用户这一轮的回复失败。
        """
        try:
            # 1. 获取 user_id
            ctx = getattr(runtime, "context", None)
            if ctx is None:
                return None
            user_id = getattr(ctx, "user_id", None)
            if not user_id:
                return None

            # 2. 获取消息列表
            messages: List[BaseMessage] = state.get("messages", [])
            if not messages:
                return None

            # 3. 判断是否需要更新
            user_message = _is_meaningful_erp_exchange(messages)
            if user_message is None:
                return None

            # 4. 提取 AI 摘要
            ai_summary = _extract_ai_summary(messages)

            # 5. LLM 提取实体
            extracted = await _extract_entities(self.model, user_message, ai_summary)
            suppliers = extracted.get("suppliers", [])
            query = extracted.get("query", "")

            if not suppliers and not query:
                return None

            logger.info(
                f"MemoryUpdateMiddleware: user={user_id}, "
                f"suppliers={suppliers}, query={query[:50]}"
            )

            # 6. 从 store 读取当前偏好文件
            store = getattr(runtime, "store", None)
            if store is None:
                logger.warning("MemoryUpdateMiddleware: runtime.store 不可用")
                return None

            # namespace 必须与 CompositeBackend 的 /memories/ 路由保持一致：(user_id,)
            namespace = (user_id,)
            key = f"/{user_id}/preferences.md"

            try:
                item = await store.aget(namespace, key)
            except Exception:
                item = None

            # 7. 解析现有内容（新格式 / 旧格式 / 空）→ 合并 → 渲染
            current_text = _value_to_text(getattr(item, "value", None) if item else None)
            data, body = parse_preferences(current_text)
            merged = merge_preferences(data, suppliers, query)
            updated_content = render_preferences(merged, body)

            # 8. 写回 store（整体覆盖：解析-合并-渲染是无损的，frontmatter 的其他键原样保留）
            file_value = _create_file_value(updated_content)
            await store.aput(namespace, key, file_value)

            logger.info(
                f"MemoryUpdateMiddleware: 已更新 {user_id} 的记忆 "
                f"(suppliers={len(merged.get('recent_suppliers', []))}, "
                f"queries={len(merged.get('recent_queries', []))})"
            )

        except Exception:
            logger.warning("MemoryUpdateMiddleware: 更新失败", exc_info=True)

        return None


# ============================================================
# 偏好文件：解析 / 合并 / 渲染
# ============================================================
# 文件形态（新格式）：
#   ---
#   preferred_output: chart
#   recent_suppliers:
#     - 博世
#   ---
#
#   # 用户偏好
#   （给人看的说明文字）
# frontmatter 是权威数据源，正文只承载说明文字，两者互不干扰。


def parse_preferences(text: str) -> tuple[Dict[str, Any], str]:
    """把偏好文件解析成 (frontmatter 字典, 正文)。

    三种输入形态都要吃得下：
      1. 新格式：`---` 包裹的 YAML frontmatter + 正文；
      2. 旧格式（历史数据）：正文里散落 recent_suppliers:/recent_queries: 列表项；
      3. 空文件或 YAML 损坏：返回 ({}, 原文)——宁可不动，也不要把用户偏好改错。
    """
    raw = text or ""
    stripped = raw.lstrip("\ufeff \t\r\n")

    if stripped.startswith("---"):
        lines = stripped.splitlines()
        # 只认"独立成行的 ---"作为结束分隔符，避免把正文里的破折号误判成分隔符
        end_idx = None
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                end_idx = i
                break
        if end_idx is not None:
            fm_text = "\n".join(lines[1:end_idx])
            body = "\n".join(lines[end_idx + 1:]).strip()
            try:
                data = yaml.safe_load(fm_text) or {}
                if isinstance(data, dict):
                    return data, body
            except yaml.YAMLError:
                logger.warning("MemoryUpdateMiddleware: frontmatter 解析失败，按旧格式兜底")

    # 旧格式：列表块本身就是数据，正文需要把它摘出去
    return _parse_legacy_preferences(raw)


def _parse_legacy_preferences(text: str) -> tuple[Dict[str, Any], str]:
    """解析历史格式（无 frontmatter 的 Markdown 列表），返回 (数据, 移除数据块后的正文)。

    只在首次迁移旧文件时用到；下一次写回即自动转为新格式。
    """
    lines = (text or "").splitlines()
    data: Dict[str, Any] = {}
    keep: List[str] = []
    i = 0

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        matched_key = None
        for key in ("recent_suppliers", "recent_queries"):
            if stripped.startswith(f"{key}:"):
                matched_key = key
                break

        if matched_key is None:
            keep.append(line)
            i += 1
            continue

        # 行内数组形式：recent_suppliers: [A, B]
        tail = stripped.split(":", 1)[1].strip()
        values: List[str] = []
        consumed = 1
        if tail.startswith("[") and tail.endswith("]"):
            inner = tail[1:-1].strip()
            if inner:
                values = [s.strip().strip("'\"") for s in inner.split(",") if s.strip()]
        elif not tail:
            # 多行列表：紧随其后的 "  - xxx"；遇到第一个非列表行即结束
            for j in range(i + 1, len(lines)):
                nxt = lines[j].strip()
                if nxt.startswith("- "):
                    values.append(nxt[2:].strip().strip("'\""))
                    consumed += 1
                elif nxt == "":
                    consumed += 1
                else:
                    break

        data[matched_key] = values
        i += consumed

    return data, "\n".join(keep).strip()


def merge_preferences(
    data: Dict[str, Any],
    new_suppliers: List[str],
    new_query: str,
) -> Dict[str, Any]:
    """把新抽取的实体合并进 frontmatter 字典（最近优先，超过上限截断）。"""
    # 内部标记键（如迁移标记）不写回文件
    merged = {k: v for k, v in (data or {}).items() if not k.startswith("_")}

    old_suppliers = [str(s) for s in (merged.get("recent_suppliers") or []) if str(s).strip()]
    old_queries = [str(q) for q in (merged.get("recent_queries") or []) if str(q).strip()]

    # 最近出现的排最前：读文件的人与模型都更容易注意到"刚聊过什么"
    suppliers: List[str] = []
    for s in list(new_suppliers) + old_suppliers:
        s = str(s).strip()
        if s and s not in suppliers:
            suppliers.append(s)

    queries: List[str] = []
    for q in ([new_query] if new_query else []) + old_queries:
        q = str(q).strip()
        if q and q not in queries:
            queries.append(q)

    merged["recent_suppliers"] = suppliers[:MAX_RECENT_SUPPLIERS]
    merged["recent_queries"] = queries[:MAX_RECENT_QUERIES]
    merged["updated_at"] = datetime.now(timezone.utc).isoformat()
    return merged


def render_preferences(data: Dict[str, Any], body: str) -> str:
    """渲染成 frontmatter + 正文的最终文件内容。"""
    clean = {k: v for k, v in (data or {}).items() if not k.startswith("_")}
    # sort_keys=False 保持插入顺序（人读起来更自然）；allow_unicode 避免中文被转义成 \uXXXX
    frontmatter = yaml.safe_dump(
        clean, allow_unicode=True, sort_keys=False, default_flow_style=False
    ).strip()
    body_text = (body or "").strip() or _DEFAULT_BODY
    return f"---\n{frontmatter}\n---\n\n{body_text}\n"
