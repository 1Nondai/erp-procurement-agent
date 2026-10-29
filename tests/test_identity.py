"""身份解析单元测试（纯标准库，不依赖 LangChain / PG，可直接在 CI 跑）。

运行：
    python -m unittest discover -s tests -v
"""

import sys
import unittest
from pathlib import Path

# 让 tests/ 能 import src/ 下的包，不依赖安装
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from api_view.identity import Identity, resolve_identity, sanitize_user_id  # noqa: E402


class TestSanitizeUserId(unittest.TestCase):
    """user_id 白名单校验：这是路径穿越的唯一防线。"""

    def test_accepts_normal_ids(self):
        for raw in ("zhangsan", "li.si-01_A", "U12345"):
            with self.subTest(raw=raw):
                self.assertEqual(sanitize_user_id(raw), raw)

    def test_strips_whitespace(self):
        self.assertEqual(sanitize_user_id("  zhangsan  "), "zhangsan")

    def test_rejects_path_traversal(self):
        for raw in ("../../etc/passwd", "a/../b", "..", "a..b"):
            with self.subTest(raw=raw):
                self.assertIsNone(sanitize_user_id(raw))

    def test_rejects_separators_and_specials(self):
        for raw in ("a/b", "a\\b", "user:1", "user 1", "user?x=1", "张三"):
            with self.subTest(raw=raw):
                self.assertIsNone(sanitize_user_id(raw))

    def test_rejects_empty_and_overlong(self):
        self.assertIsNone(sanitize_user_id(None))
        self.assertIsNone(sanitize_user_id(""))
        self.assertIsNone(sanitize_user_id("   "))
        self.assertIsNone(sanitize_user_id("a" * 65))
        self.assertEqual(sanitize_user_id("a" * 64), "a" * 64)


class TestResolveIdentity(unittest.TestCase):

    def test_header_identity(self):
        ident = resolve_identity("zhangsan", "张三")
        self.assertIsInstance(ident, Identity)
        self.assertEqual(ident.user_id, "zhangsan")
        self.assertEqual(ident.username, "张三")
        self.assertTrue(ident.from_request)

    def test_username_defaults_to_user_id(self):
        self.assertEqual(resolve_identity("zhangsan").username, "zhangsan")

    def test_username_truncated_to_64(self):
        ident = resolve_identity("zhangsan", "名" * 100)
        self.assertEqual(len(ident.username), 64)

    def test_missing_header_falls_back_and_marks_not_from_request(self):
        ident = resolve_identity(None)
        self.assertFalse(ident.from_request)
        self.assertTrue(ident.user_id)          # 兜底值必须非空
        self.assertEqual(ident.username, ident.username.strip())

    def test_illegal_header_falls_back(self):
        ident = resolve_identity("../../etc/passwd")
        self.assertFalse(ident.from_request)
        self.assertNotIn("..", ident.user_id)


if __name__ == "__main__":
    unittest.main()
