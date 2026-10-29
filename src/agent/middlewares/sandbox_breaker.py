"""
沙箱熔断中间件。

在 `before_model` 检查最近的工具返回：连续出现沙箱类错误、且次数超过阈值时，
直接跳到 `end` 并注入一条通知消息，让这一轮对话"体面地结束"，
而不是让 Agent 在"重建沙箱 → 再失败 → 再重建"里无限循环烧钱。

为什么需要熔断（而不是只做健康检查自动恢复）：
    自动恢复能处理"偶发挂掉"，但它有个前提——恢复后能好。
    如果沙箱服务整个不可用（docker 挂了、镜像拉不到、宿主机没内存），
    健康检查每次都会重建失败，而 Agent 每次失败都会再试一遍：
    一次用户提问可能触发几十次建沙箱，单次 10~20 秒，直接把请求拖死。
    熔断的作用就是"承认救不回来，先停下来告诉人"。

与 SandboxHealthMiddleware 的分工：
    - SandboxHealthMiddleware：主动探活 → 恢复（乐观路径）
    - SandboxCircuitBreakerMiddleware：恢复失败次数超限 → 熔断（悲观路径）

判断逻辑本身在 `sandbox_error_signals`（纯函数，可在裸环境离线单测）。
"""
from __future__ import annotations

import logging
from typing import Any

from langchain.agents.middleware import AgentMiddleware, AgentState, hook_config
from langchain_core.messages import AIMessage
from langgraph.runtime import Runtime

from agent.middlewares.sandbox_error_signals import (
    CIRCUIT_BREAKER_MARKER,
    count_consecutive_sandbox_errors,
)

logger = logging.getLogger(__name__)

# 连续几次沙箱错误就熔断。默认 2：一次失败可能只是抖动，
# 连续两次基本可以确认"恢复也救不回来"，再试只是浪费用户时间。
THRESHOLD = 2


class SandboxCircuitBreakerMiddleware(AgentMiddleware):
    """连续沙箱错误超过阈值 → 熔断，跳转到 end。"""

    state_schema = AgentState

    def __init__(self, threshold: int = THRESHOLD) -> None:
        super().__init__()
        self.threshold = threshold

    @hook_config(can_jump_to=["end"])
    def before_model(
        self, state: AgentState, runtime: Runtime,
    ) -> dict[str, Any] | None:
        messages = state.get("messages", [])
        if not messages:
            return None

        count = count_consecutive_sandbox_errors(messages)
        if count <= self.threshold:
            return None

        logger.warning(
            "沙箱熔断: 连续 %d 次沙箱错误超过阈值 %d", count, self.threshold,
        )
        content = (
            f"{CIRCUIT_BREAKER_MARKER}: 连续 {count} 次沙箱操作失败，已触发熔断保护。"
            "请检查沙箱服务状态后重试，或联系管理员。"
        )
        # 标记消息承担两个作用：告知用户 + 让下一轮计数归零（避免同轮反复熔断）
        return {"jump_to": "end", "messages": [AIMessage(content=content)]}

    @hook_config(can_jump_to=["end"])
    async def abefore_model(
        self, state: AgentState, runtime: Runtime,
    ) -> dict[str, Any] | None:
        return self.before_model(state, runtime)
