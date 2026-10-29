"""订单写操作幂等单元测试（纯标准库：sqlite3 + sha256）。

覆盖三件容易出错的事：
    1. 指纹必须与字段顺序无关；
    2. 指纹必须剔除 orderNumber / orderTime 这类每次都会变的字段；
    3. 同 key 的重复写入必须"先到者为准"，过期记录必须被当作不存在。

运行：
    python -m unittest discover -s tests -v
"""

import sqlite3
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from mcp_server.idempotency import (  # noqa: E402
    IdempotencyStore,
    compute_fingerprint,
)


class TestComputeFingerprint(unittest.TestCase):

    def test_field_order_does_not_matter(self):
        a = compute_fingerprint({"supplierId": 1, "partId": 2, "quantity": 10})
        b = compute_fingerprint({"quantity": 10, "partId": 2, "supplierId": 1})
        self.assertEqual(a, b)

    def test_volatile_fields_are_ignored(self):
        payload = {"partId": 2, "quantity": 10}
        with_noise = {**payload, "orderNumber": "PO-20260513-0001", "orderTime": "2026-05-13T06:22:00"}
        self.assertEqual(compute_fingerprint(payload), compute_fingerprint(with_noise))

    def test_business_changes_change_fingerprint(self):
        base = {"partId": 2, "quantity": 10}
        self.assertNotEqual(compute_fingerprint(base), compute_fingerprint({**base, "quantity": 11}))

    def test_decimal_and_float_are_normalized(self):
        # 25.50 / 25.5 / Decimal("25.50") 必须是同一笔业务请求
        a = compute_fingerprint({"unitPrice": Decimal("25.50")})
        b = compute_fingerprint({"unitPrice": 25.5})
        c = compute_fingerprint({"unitPrice": 25.50})
        self.assertEqual(a, b)
        self.assertEqual(b, c)

    def test_strings_are_trimmed(self):
        self.assertEqual(
            compute_fingerprint({"remark": "紧急  "}),
            compute_fingerprint({"remark": "紧急"}),
        )

    def test_empty_payload_is_stable(self):
        self.assertEqual(compute_fingerprint({}), compute_fingerprint(None))


class TestIdempotencyStore(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmp.name) / "idem.sqlite3")
        self.store = IdempotencyStore(self.db_path, ttl_seconds=3600)

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def _backdate(self, key: str, created_at: str = "2020-01-01T00:00:00+00:00") -> None:
        """把某条记录的创建时间改成很久以前（模拟 TTL 过期）。

        注意 sqlite3 的连接上下文管理器只负责提交/回滚、不负责关闭连接，
        Windows 上未关闭的连接会锁住文件，导致 tearDown 删不掉临时目录。
        """
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "UPDATE idempotency_records SET created_at = ? WHERE key = ?",
                (created_at, key),
            )
            conn.commit()
        finally:
            conn.close()

    def _count_rows(self) -> int:
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute("SELECT COUNT(*) FROM idempotency_records").fetchone()[0]
        finally:
            conn.close()

    def test_put_then_get(self):
        self.store.put("k1", "fp1", {"orderId": 42})
        rec = self.store.get("k1")
        self.assertIsNotNone(rec)
        self.assertEqual(rec.response, {"orderId": 42})
        self.assertEqual(rec.fingerprint, "fp1")

    def test_unknown_key_returns_none(self):
        self.assertIsNone(self.store.get("nope"))
        self.assertIsNone(self.store.get(""))

    def test_first_response_wins(self):
        self.store.put("k1", "fp1", {"orderId": 1})
        self.store.put("k1", "fp1", {"orderId": 2})
        self.assertEqual(self.store.get("k1").response, {"orderId": 1})

    def test_expired_record_is_invisible_and_removed(self):
        self.store.put("k1", "fp1", {"orderId": 1})
        self._backdate("k1")           # 模拟 TTL 过期
        self.assertIsNone(self.store.get("k1"))
        self.assertEqual(self._count_rows(), 0, "过期记录应被顺手删除")

    def test_purge_expired(self):
        self.store.put("k1", "fp1", {"orderId": 1})
        self._backdate("k1")
        self.assertEqual(self.store.purge_expired(), 1)

    def test_survives_reopen(self):
        """同一文件重开（MCP Server 重启）后记录仍在，说明去重不依赖进程内存。"""
        self.store.put("k1", "fp1", {"orderId": 1})
        self.store.close()
        reopened = IdempotencyStore(self.db_path, ttl_seconds=3600)
        try:
            self.assertEqual(reopened.get("k1").response, {"orderId": 1})
        finally:
            reopened.close()


if __name__ == "__main__":
    unittest.main()
