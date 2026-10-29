"""
沙箱错误信号识别 —— 熔断中间件的"纯逻辑"部分。

为什么单独拆一个模块（不是为了好看）：
    熔断的判断是**能否止损**的最后一道闸，它必须能被单测覆盖。
    但中间件本体依赖 `langchain.agents.middleware`（重依赖），
    在 CI 的"纯标准库"测试步骤里根本 import 不了。
    所以把"数错误""认关键字"这类纯函数挪到这里：
    零第三方依赖 → 可以在裸环境里跑测试；
    中间件只负责把它挂到 `before_model` 钩子上。

鸭子类型而非 isinstance：
    LangGraph 的消息对象类型很多（ToolMessage / AIMessage / HumanMessage ...），
    但它们都有稳定的 `.type` 取值（"tool" / "ai" / "human" / "system"）。
    按 `.type` 判断，测试里用一行假的 stub 就能覆盖，不必构造真实消息。
"""
from __future__ import annotations

from collections.abc import Sequence

CIRCUIT_BREAKER_MARKER = "SandboxCircuitBreaker"

# 这些词出现在"状态为 error 的工具返回"里时，判定为沙箱类故障。
# 注意刻意包含 "execute" 与 "连接/超时"：沙箱挂了通常表现为
# "执行命令超时"或"连接被拒"，而不是字面上的 "sandbox error"。
SANDBOX_ERROR_KEYWORDS = (
    "SandboxBackendProxy",
    "sandbox",
    "SandboxError",
    "ConnectionError",
    "ConnectionRefusedError",
    "TimeoutError",
    "execute",
    "沙箱",
    "不可达",
    "连接",
    "超时",
)


def looks_like_sandbox_error(text: str) -> bool:
    """这条错误文本像不像沙箱故障。"""
    lowered = (text or "").lower()
    return any(kw.lower() in lowered for kw in SANDBOX_ERROR_KEYWORDS)


def _content_text(message: object) -> str:
    """把消息内容统一成字符串（LangChain 的 content 可能是 str 或 block 列表）。"""
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                parts.append(str(block.get("text", "")))
            else:
                parts.append(str(block))
        return " ".join(parts)
    return str(content)


def count_consecutive_sandbox_errors(messages: Sequence[object]) -> int:
    """从消息列表末尾往前，数"连续"的沙箱错误工具返回。

    三条规则（都来自真实事故）：
      1. 只数 `type == "tool"` 且 `status == "error"` 且文本像沙箱故障的消息；
         业务类错误（"库存不足"）不参与熔断，否则正常对话会被误熔断。
      2. 遇到第一条非错误消息就停 —— 要的是"连续"，不是"累计"；
      3. 遇到熔断标记或 human/system 消息立刻归零 —— 用户重新提问说明是新回合，
         不应该继承上一轮的失败计数。
    """
    count = 0
    for msg in reversed(messages or ()):
        msg_type = getattr(msg, "type", "")
        if msg_type == "tool":
            if (
                getattr(msg, "status", None) == "error"
                and looks_like_sandbox_error(_content_text(msg))
            ):
                count += 1
                continue
            break
        if msg_type == "ai":
            if CIRCUIT_BREAKER_MARKER in _content_text(msg):
                return 0
            continue
        if msg_type in {"human", "system"}:
            break
    return count
