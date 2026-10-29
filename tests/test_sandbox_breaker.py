"""沙箱熔断纯逻辑单测：连续错误计数 + 标记归零 + 误熔断防护。

为什么单独测这些纯函数（而不是测中间件）：
    熔断是"止损失效"的最后一道闸。算错一次要么该熔断时不熔断（死循环烧钱），
    要么正常对话被误熔断（用户莫名被踢出），两者在联调时都很难发现。
    而且这层逻辑零第三方依赖，能在 CI 的裸环境里跑。

用 stub 而不是 LangChain 消息对象：
    被测函数只按 `.type` / `.status` / `.content` 三个属性做鸭子类型判断，
    用一个小类就能构造任意场景，也顺带证明了"它不依赖具体消息类"。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agent.middlewares.sandbox_error_signals import (  # noqa: E402
    CIRCUIT_BREAKER_MARKER,
    count_consecutive_sandbox_errors,
    looks_like_sandbox_error,
)


class Msg:
    """最小消息替身：只带被测逻辑用到的三个属性。"""

    def __init__(self, type_, content="", status=None):
        self.type = type_
        self.content = content
        self.status = status


def tool_err(text="sandbox unreachable"):
    return Msg("tool", text, status="error")


def tool_ok(text="fine"):
    return Msg("tool", text, status="success")


class SandboxSignalTest(unittest.TestCase):
    def test_keyword_detection(self):
        self.assertTrue(looks_like_sandbox_error("SandboxBackendProxy: 沙箱不可达"))
        self.assertTrue(looks_like_sandbox_error("ConnectionError: refused"))
        self.assertTrue(looks_like_sandbox_error("命令执行超时 execute timeout"))
        self.assertFalse(looks_like_sandbox_error("订单金额超出预算"))

    def test_counts_consecutive_errors(self):
        msgs = [Msg("human", "查一下"), tool_err(), tool_err(), tool_err()]
        self.assertEqual(count_consecutive_sandbox_errors(msgs), 3)

    def test_stops_at_success(self):
        msgs = [Msg("human", "查一下"), tool_err(), tool_ok(), tool_err()]
        self.assertEqual(count_consecutive_sandbox_errors(msgs), 1)

    def test_breaker_marker_resets(self):
        msgs = [
            Msg("human", "查一下"),
            Msg("ai", CIRCUIT_BREAKER_MARKER + ": 已熔断"),
            tool_err(),
        ]
        self.assertEqual(count_consecutive_sandbox_errors(msgs), 0)

    def test_business_error_not_counted(self):
        msgs = [Msg("human", "下单"), Msg("tool", "库存不足", status="error")]
        self.assertEqual(count_consecutive_sandbox_errors(msgs), 0)

    def test_human_message_stops_scan(self):
        msgs = [tool_err(), Msg("human", "换个问题"), tool_err()]
        self.assertEqual(count_consecutive_sandbox_errors(msgs), 1)

    def test_empty(self):
        self.assertEqual(count_consecutive_sandbox_errors([]), 0)


if __name__ == "__main__":
    unittest.main()
