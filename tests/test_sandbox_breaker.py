"""沙箱熔断逻辑单测：连续沙箱错误计数 + 熔断标记复位。

为什么值得测：熔断阈值判断是"止损失效"的最后一道闸，
它一旦算错（少算一次/多算一次），要么该熔断时不熔断（死循环烧钱），
要么正常对话被误熔断（用户莫名其妙被踢出）。两者都很难在联调时发现。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage  # noqa: E402

from agent.middlewares.sandbox_breaker import (  # noqa: E402
    CIRCUIT_BREAKER_MARKER,
    _count_consecutive_sandbox_errors,
    _looks_like_sandbox_error,
)


def _err_tool(text="sandbox unreachable"):
    return ToolMessage(content=text, tool_call_id="t1", status="error")


def _ok_tool(text="fine"):
    return ToolMessage(content=text, tool_call_id="t1", status="success")


class SandboxBreakerTest(unittest.TestCase):
    def test_keyword_detection(self):
        self.assertTrue(_looks_like_sandbox_error("SandboxBackendProxy: 沙箱不可达"))
        self.assertTrue(_looks_like_sandbox_error("ConnectionError: refused"))
        self.assertFalse(_looks_like_sandbox_error("订单金额超出预算"))

    def test_counts_consecutive_errors_from_tail(self):
        msgs = [HumanMessage(content="查一下"), _err_tool(), _err_tool(), _err_tool()]
        self.assertEqual(_count_consecutive_sandbox_errors(msgs), 3)

    def test_stops_at_success(self):
        msgs = [HumanMessage(content="查一下"), _err_tool(), _ok_tool(), _err_tool()]
        self.assertEqual(_count_consecutive_sandbox_errors(msgs), 1)

    def test_breaker_marker_resets_counter(self):
        msgs = [
            HumanMessage(content="查一下"),
            AIMessage(content=CIRCUIT_BREAKER_MARKER + ": 已熔断"),
            _err_tool(),
        ]
        self.assertEqual(_count_consecutive_sandbox_errors(msgs), 0)

    def test_human_message_stops_scan(self):
        msgs = [_err_tool(), HumanMessage(content="换一个问题"), _err_tool()]
        self.assertEqual(_count_consecutive_sandbox_errors(msgs), 1)

    def test_non_sandbox_error_not_counted(self):
        msgs = [HumanMessage(content="下单"), ToolMessage(content="库存不足", tool_call_id="t", status="error")]
        self.assertEqual(_count_consecutive_sandbox_errors(msgs), 0)


if __name__ == "__main__":
    unittest.main()
