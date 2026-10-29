"""
沙箱注册表 —— user_id → sandbox_id 的持久化绑定（PostgreSQL 版）。

为什么单独一层：
    `sandbox_manager` 只负责"生命周期状态机"（认领预热池 / 缓存命中 / 重连 /
    重建 / 销毁），它不该关心 SQL。把表结构、DDL、UPSERT 全部收在这一层，
    将来要换成 Redis / etcd 做注册表，只改本文件，manager 一行不动。

为什么从 MongoDB 换成 PostgreSQL：
    原实现用 MongoDB 存 `sandbox_registry`（`user_id` 唯一索引）。但本项目
    其余持久化（LangGraph checkpoint、业务库 erp schema、会话展示消息）
    全在 PostgreSQL 上，多一套 MongoDB 意味着：部署多一个容器、备份多一条链路、
    运维多一种故障模式。注册表只有一个"键值 + 时间戳"的形状，PG 的
    `INSERT ... ON CONFLICT` 完全够用，而且是原子的。

一致性取舍：
    沙箱本身是"外部有状态资源"，数据库里的 sandbox_id 只是**索引**而不是真相。
    所以读到的 id 连不上时（沙箱被 docker 清掉、机器重启）不会报错，
    而是走"重建"路径并覆盖绑定 —— 宁可多建一个沙箱，也不让用户卡住。
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import Optional

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

logger = logging.getLogger(__name__)

# Windows 上 psycopg 的异步实现依赖 Selector 事件循环；
# ProactorEventLoop 下建池会以"连接池超时 30 秒"的形式失败（不是报错，是干等）。
if sys.platform == "win32":  # pragma: no cover - 平台相关
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        logger.debug("设置 WindowsSelectorEventLoopPolicy 失败（可能已设置）")

TABLE_DDL = """
CREATE SCHEMA IF NOT EXISTS ops;

CREATE TABLE IF NOT EXISTS ops.sandbox_registry (
    user_id    TEXT PRIMARY KEY,
    sandbox_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 反向查询：从沙箱反查归属用户（运维排查"这个沙箱是谁的"）
CREATE INDEX IF NOT EXISTS idx_sandbox_registry_sandbox
    ON ops.sandbox_registry (sandbox_id);
"""

_POOL: Optional[AsyncConnectionPool] = None
_READY = False


def dsn() -> str:
    """注册表所在库的连接串。

    优先 `SANDBOX_REGISTRY_DSN`（可把沙箱绑定单独放一个库），
    其次复用 `POSTGRES_URI`（默认，和 checkpoint 同库）。
    """
    value = os.environ.get("SANDBOX_REGISTRY_DSN") or os.environ.get("POSTGRES_URI")
    if not value:
        raise RuntimeError(
            "未配置 SANDBOX_REGISTRY_DSN 或 POSTGRES_URI，无法持久化沙箱绑定"
        )
    return value


async def init(connection_string: Optional[str] = None) -> None:
    """建立连接池并建表（幂等）。进程生命周期内只做一次。"""
    global _POOL, _READY
    if _POOL is None:
        _POOL = AsyncConnectionPool(
            connection_string or dsn(),
            min_size=1,
            max_size=4,
            open=True,
            kwargs={"row_factory": dict_row},
        )
    if not _READY:
        pool = _POOL
        if pool is None:  # 理论不可达，交给类型检查与显式报错
            raise RuntimeError("沙箱注册表连接池创建失败")
        async with pool.connection() as conn:
            await conn.execute(TABLE_DDL)
            await conn.commit()
        _READY = True
        logger.info("沙箱注册表已就绪（ops.sandbox_registry）")


def _pool() -> AsyncConnectionPool:
    if _POOL is None:
        raise RuntimeError("注册表未初始化，请先 await sandbox_registry.init()")
    return _POOL


async def bind(user_id: str, sandbox_id: str) -> None:
    """把用户绑定到沙箱（存在则覆盖）。"""
    async with _pool().connection() as conn:
        await conn.execute(
            """
            INSERT INTO ops.sandbox_registry (user_id, sandbox_id, updated_at)
            VALUES (%s, %s, now())
            ON CONFLICT (user_id) DO UPDATE
               SET sandbox_id = EXCLUDED.sandbox_id,
                   updated_at = now()
            """,
            (user_id, sandbox_id),
        )
        await conn.commit()


async def get_sandbox_id(user_id: str) -> Optional[str]:
    """查询用户当前绑定的沙箱 ID（没有则 None）。"""
    async with _pool().connection() as conn:
        row = await conn.execute(
            "SELECT sandbox_id FROM ops.sandbox_registry WHERE user_id = %s",
            (user_id,),
        )
        result = await row.fetchone()
        return result["sandbox_id"] if result else None


async def touch(user_id: str) -> None:
    """只更新心跳时间（重连成功时调用）。"""
    async with _pool().connection() as conn:
        await conn.execute(
            "UPDATE ops.sandbox_registry SET updated_at = now() WHERE user_id = %s",
            (user_id,),
        )
        await conn.commit()


async def unbind(user_id: str) -> None:
    """解除绑定（沙箱被销毁时调用）。"""
    async with _pool().connection() as conn:
        await conn.execute("DELETE FROM ops.sandbox_registry WHERE user_id = %s", (user_id,))
        await conn.commit()


async def list_bindings(limit: int = 100) -> list[dict]:
    """列出绑定关系（运维/自检用）。"""
    async with _pool().connection() as conn:
        row = await conn.execute(
            """
            SELECT user_id, sandbox_id, created_at, updated_at
              FROM ops.sandbox_registry
             ORDER BY updated_at DESC
             LIMIT %s
            """,
            (max(1, min(int(limit), 500)),),
        )
        return list(await row.fetchall())


async def close() -> None:
    """关闭连接池（应用退出时调用）。"""
    global _POOL, _READY
    if _POOL is not None:
        await _POOL.close()
        _POOL = None
        _READY = False
        logger.info("沙箱注册表连接池已关闭")
