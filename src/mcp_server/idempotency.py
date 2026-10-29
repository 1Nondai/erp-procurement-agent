"""
MCP Server 侧写操作幂等存储。

背景：订单创建/更新属于外部副作用，而 Agent 的重试、用户重复点击审批、
SSE 断线重连都会让同一个业务请求被提交多次 —— 结果就是重复下单。
Java 后端目前没有幂等键机制，因此在 MCP 工具层加一道
"请求指纹 → 首次响应" 的缓存来兜住：

  - 指纹 = 业务字段规范化 JSON 的 sha256，**刻意排除** orderNumber / orderTime
    这类每次调用都会重新生成的字段；否则同一笔业务请求每次算出的指纹都不同，
    去重会完全失效（这一点是最容易踩的坑）；
  - 命中且未过期 → 直接返回首次响应，并标记 idempotent_replay=True，
    让 Agent 知道"这单没有重复下，返回的是之前那次的结果"。

存储选型：SQLite（标准库 sqlite3）。理由是单进程 MCP Server、数据量小、
不想为一个计数表引入额外运维组件。并发用一把 threading.Lock 串行化"查-写"，
避免同一指纹并发双写。

多副本部署说明：SQLite 是进程内文件库，多副本 MCP Server 之间不共享。
若将来横向扩副本，请把这里换成 PostgreSQL 表 + (key) 唯一索引，
用 INSERT ... ON CONFLICT DO NOTHING 抢占，语义与现在一致。
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

logger = logging.getLogger(__name__)

# 参与指纹计算的字段需要排除的键（每次调用都会变，或与业务语义无关）
DEFAULT_IGNORED_KEYS: tuple[str, ...] = (
    "orderNumber",      # 由工具侧按时间+随机数生成，重试时必然不同
    "orderTime",        # 默认填充为当前时间
    "requestId",        # 调用链路的追踪 ID
    "timestamp",
)


@dataclass
class IdempotencyRecord:
    """一条幂等记录：某个业务请求指纹在 TTL 内对应的首次响应。"""
    key: str
    fingerprint: str
    response: Any
    created_at: str


def compute_fingerprint(payload: Dict[str, Any], ignore_keys: Iterable[str] = DEFAULT_IGNORED_KEYS) -> str:
    """计算业务请求指纹。

    规则：剔除易变字段 → 按 key 排序序列化（保证字段顺序不影响结果）
    → 递归归一化数值与日期 → sha256。

    为什么必须排序并归一化：dict 的插入顺序、Decimal 与 float 的差异、
    date 与 ISO 字符串的差异，都会让"同一笔业务请求"算出不同指纹。
    """
    ignore = set(ignore_keys)
    normalized = _normalize({k: v for k, v in (payload or {}).items() if k not in ignore})
    # default=str 是最后一道防线：将来出现未知类型也不该让指纹计算整体失败
    # （指纹算不出来 = 幂等失效，比"多算一次请求"严重得多）
    blob = json.dumps(
        normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _normalize(value: Any) -> Any:
    """把请求体递归归一化成可稳定序列化的形态（数值、日期、容器统一处理）。"""
    # bool 必须先判：它是 int 的子类，否则 True 会被当成数字
    if isinstance(value, bool):
        return value
    # Decimal 是订单金额的原始类型（unitPrice/totalAmount 都是 BigDecimal），
    # 必须显式处理：它既不是 int/float，也没有 isoformat，落到 json.dumps 会直接抛错。
    # 统一成固定精度的字符串，顺带解决 25.50 与 25.5 被判成两个指纹的问题。
    if isinstance(value, (Decimal, int, float)):
        return f"num:{float(value):.6f}"
    if isinstance(value, str):
        return value.strip()
    if hasattr(value, "isoformat"):          # date / datetime
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _normalize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(v) for v in value]
    return value


class IdempotencyStore:
    """基于 SQLite 的幂等记录存储（进程内单例使用）。"""

    def __init__(self, db_path: str, ttl_seconds: int = 24 * 3600) -> None:
        self.db_path = db_path
        self.ttl_seconds = max(int(ttl_seconds), 0)
        # 占位（claim）的有效期：远小于 ttl_seconds。仅用于兜住"执行者进程崩溃后
        # 占位永远不会被 put/release 释放"的情况，到点后允许重新抢占。
        self.claim_ttl_seconds = 600
        # sqlite3 连接默认绑定创建线程；MCP 工具可能在不同线程执行，因此
        # 关闭线程检查并用一把锁保证串行访问（写入量极小，锁竞争可忽略）
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None
        self._ensure_schema()

    # ------------------------- 基础设施 -------------------------

    def _ensure_schema(self) -> None:
        db_file = Path(self.db_path)
        db_file.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_file), check_same_thread=False)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS idempotency_records (
                key         TEXT PRIMARY KEY,
                fingerprint TEXT NOT NULL,
                response    TEXT NOT NULL,
                created_at  TEXT NOT NULL
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_idem_created ON idempotency_records(created_at)"
        )
        # "处理中"占位表：解决两个同指纹并发请求都 miss、各下一单的问题。
        # 为什么单独建表而不是给 records 加列/加状态：
        #   1) records 的语义是"最终响应"，response 列 NOT NULL，占位时没有响应可写；
        #   2) 单独一张表可以直接用 PRIMARY KEY 冲突做原子抢占（INSERT OR IGNORE
        #      + rowcount 判断），不需要额外全局锁；
        #   3) 升级零风险：只是多建一张 IF NOT EXISTS 的表，老库无需 ALTER 迁移。
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS idempotency_pending (
                key         TEXT PRIMARY KEY,
                fingerprint TEXT NOT NULL,
                claimed_at  TEXT NOT NULL
            )
            """
        )
        self._conn.commit()
        logger.info("IdempotencyStore 就绪: %s (ttl=%ss)", self.db_path, self.ttl_seconds)

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    # ------------------------- 读写 -------------------------

    def get(self, key: str) -> Optional[IdempotencyRecord]:
        """按键取记录；已过 TTL 的视为不存在（同时顺手删除）。"""
        if not key:
            return None
        with self._lock:
            if self._conn is None:
                return None
            row = self._conn.execute(
                "SELECT key, fingerprint, response, created_at FROM idempotency_records WHERE key = ?",
                (key,),
            ).fetchone()
            if row is None:
                return None
            created_at = datetime.fromisoformat(row[3])
            if self._is_expired(created_at):
                self._conn.execute("DELETE FROM idempotency_records WHERE key = ?", (key,))
                self._conn.commit()
                return None
            return IdempotencyRecord(
                key=row[0],
                fingerprint=row[1],
                response=json.loads(row[2]),
                created_at=row[3],
            )

    def put(self, key: str, fingerprint: str, response: Any) -> None:
        """写入首次响应（同 key 已存在则不覆盖：先到者为准，保证"首次响应"语义）。"""
        if not key:
            return
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            if self._conn is None:
                return
            self._conn.execute(
                """
                INSERT OR IGNORE INTO idempotency_records (key, fingerprint, response, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (key, fingerprint, json.dumps(response, ensure_ascii=False, default=str), now),
            )
            # 结果落地后占位就没用了：留着会拦住 TTL 内的合法重试
            self._conn.execute("DELETE FROM idempotency_pending WHERE key = ?", (key,))
            self._conn.commit()

    # ------------------------- 并发占位 -------------------------

    def claim(self, key: str, fingerprint: str) -> bool:
        """抢占一个请求的执行权，返回是否抢到。

        为什么需要这个"占位"：
            原流程是 get() → 打后端 → put()。两个同指纹请求并发进来时，
            两边 get() 都是空、都去打后端，结果是**各下一单**——幂等形同虚设。
            claim() 先往 idempotency_pending 插一条 pending 记录，
            靠表上的 PRIMARY KEY 冲突做原子抢占：并发的第二个 INSERT 会失败
            （INSERT OR IGNORE + rowcount），于是它知道"已经有人在跑了"，
            直接返回"处理中"，而不是再打一次后端。

        约定：
            抢到（True）→ 本次调用负责真正执行，成功后必须 put() 写结果，
                          失败必须 release() 释放占位，否则同指纹请求要等到
                          占位过期（claim_ttl_seconds）才能重试。
            抢不到（False）→ 已有同指纹请求正在处理中；或该请求其实已经执行完成
                          （首次响应已落库），调用方应重新 get() 读取结果。

        Args:
            key: 幂等键（与 get/put 用的是同一个）。
            fingerprint: 请求指纹，便于排障时看出占位属于哪次请求。
        """
        if not key:
            return False
        now_dt = datetime.now(timezone.utc)
        stale_before = (now_dt - timedelta(seconds=self.claim_ttl_seconds)).isoformat()
        with self._lock:
            if self._conn is None:
                # 存储不可用时不要拦住业务：退化为"无并发保护"由调用方继续执行
                return True
            # 清掉崩溃残留的过期占位，避免一个 key 被永久锁死
            self._conn.execute(
                "DELETE FROM idempotency_pending WHERE key = ? AND claimed_at < ?",
                (key, stale_before),
            )
            # 已执行完成（首次响应已落库）就不要再抢占：否则调用方 get 与 claim
            # 之间的时间窗会让它再打一次后端，重复下单。返回 False 让调用方
            # 重新读一次结果（见 order_tools._claim_failed_response）。
            done = self._conn.execute(
                "SELECT 1 FROM idempotency_records WHERE key = ?", (key,)
            ).fetchone()
            if done is not None:
                self._conn.commit()
                return False
            cur = self._conn.execute(
                """
                INSERT OR IGNORE INTO idempotency_pending (key, fingerprint, claimed_at)
                VALUES (?, ?, ?)
                """,
                (key, fingerprint, now_dt.isoformat()),
            )
            self._conn.commit()
            return (cur.rowcount or 0) == 1

    def release(self, key: str) -> None:
        """释放占位（执行失败时调用，让调用方可以立即重试）。"""
        if not key:
            return
        with self._lock:
            if self._conn is None:
                return
            self._conn.execute("DELETE FROM idempotency_pending WHERE key = ?", (key,))
            self._conn.commit()

    def purge_expired(self) -> int:
        """清理过期记录，返回删除条数（可由定时任务调用，避免文件无限增长）。"""
        with self._lock:
            if self._conn is None:
                return 0
            cutoff = (datetime.now(timezone.utc) - timedelta(seconds=self.ttl_seconds)).isoformat()
            cur = self._conn.execute(
                "DELETE FROM idempotency_records WHERE created_at < ?", (cutoff,)
            )
            deleted = cur.rowcount or 0
            # 占位用更短的 TTL 清理（正常流程会在 put/release 时删掉）
            claim_cutoff = (
                datetime.now(timezone.utc) - timedelta(seconds=self.claim_ttl_seconds)
            ).isoformat()
            self._conn.execute(
                "DELETE FROM idempotency_pending WHERE claimed_at < ?", (claim_cutoff,)
            )
            self._conn.commit()
            return deleted

    def _is_expired(self, created_at: datetime) -> bool:
        if self.ttl_seconds <= 0:
            return False                      # ttl<=0 视为不过期（永久去重）
        if created_at.tzinfo is None:         # 兼容历史数据的无时区时间
            created_at = created_at.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - created_at > timedelta(seconds=self.ttl_seconds)
