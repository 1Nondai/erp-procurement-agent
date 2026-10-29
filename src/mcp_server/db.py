"""
PostgreSQL 访问层（MCP Server 侧）。

为什么单独一层：
    MCP 工具的职责是"把业务能力描述清楚并做参数校验"，不该关心 SQL 长什么样。
    把连接池与查询收口到这里后，将来若要接回真实 ERP 的 REST 接口，
    只需要换掉 repository 的实现，工具契约（工具名/参数/返回结构）完全不动。

线程/协程模型：
    FastMCP 的 streamable-http 传输里，每次工具调用都在同一个事件循环上跑，
    因此池用 AsyncConnectionPool 并在 lifespan 里 open/close 各一次；
    不要在每个工具里新建连接——那是"每次查询三次握手"的经典性能坑。

行格式：
    一律 dict_row（列名 → 值），工具层不用再按下标取数。
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Optional, Sequence

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

logger = logging.getLogger(__name__)

# ------------------------------------------------------------
# Windows 事件循环兼容（踩过的坑，写在这里省下后来人半天）
# ------------------------------------------------------------
# psycopg3 的异步连接依赖 selector 上的非阻塞 socket，**不能**跑在 Windows 默认的
# ProactorEventLoop 上。症状极易误判：日志只会刷
#   "Psycopg cannot use the 'ProactorEventLoop' ..."
# 紧接着是 "couldn't get a connection after 30.00 sec"（连接池超时），
# 看起来像数据库连不上，实际是事件循环类型不对。
# 必须在事件循环创建之前替换 policy，因此放在模块导入期执行。
def _ensure_compatible_loop() -> None:
    if sys.platform != "win32":
        return
    policy = asyncio.get_event_loop_policy()
    if isinstance(policy, asyncio.WindowsProactorEventLoopPolicy):  # type: ignore[attr-defined]
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())  # type: ignore[attr-defined]
        logger.debug("Windows: 事件循环策略已切换为 Selector（psycopg 异步要求）")


_ensure_compatible_loop()

DEFAULT_DB_URI = "postgresql://postgres:postgres@localhost:5432/langgraph_db"

# 进程级单例。MCP Server 是单进程服务，不需要跨进程共享。
_POOL: Optional[AsyncConnectionPool] = None


def db_uri() -> str:
    """业务库连接串。

    ERP_DB_URI 与 Agent 的 POSTGRES_URI 默认是同一个库（业务表在 erp schema 下，
    与 LangGraph 的 checkpoint 表用 schema 隔离），但允许分开配置：
    生产环境通常是"业务库只读账号 + 状态库独立账号"。
    """
    return os.environ.get("ERP_DB_URI") or os.environ.get("POSTGRES_URI") or DEFAULT_DB_URI


async def open_pool(min_size: int = 1, max_size: int = 8) -> AsyncConnectionPool:
    """创建并打开连接池（由 server_main 的 lifespan 调用一次）。"""
    global _POOL
    if _POOL is not None:
        return _POOL
    _POOL = AsyncConnectionPool(
        db_uri(),
        min_size=min_size,
        max_size=max_size,
        open=False,                 # 显式 await open()，让启动失败能在 lifespan 里暴露
        kwargs={"row_factory": dict_row},
    )
    await _POOL.open()
    # 启动即探活：连不上数据库要在启动时报错，而不是等第一个工具调用才 500
    async with _POOL.connection() as conn:
        await conn.execute("SELECT 1")
    logger.info("ERP 业务库连接池已就绪（min=%s max=%s）", min_size, max_size)
    return _POOL


async def close_pool() -> None:
    """关闭连接池（应用停止时调用）。"""
    global _POOL
    if _POOL is not None:
        await _POOL.close()
        _POOL = None
        logger.info("ERP 业务库连接池已关闭")


def get_pool() -> AsyncConnectionPool:
    if _POOL is None:
        raise RuntimeError("连接池尚未初始化：请确认 server_main 的 lifespan 已执行 open_pool()")
    return _POOL


async def fetch_all(sql: str, params: Sequence[Any] | None = None) -> list[dict]:
    """查询多行。"""
    async with get_pool().connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, params or ())
            return await cur.fetchall()


async def fetch_one(sql: str, params: Sequence[Any] | None = None) -> Optional[dict]:
    """查询单行，没有则返回 None。"""
    async with get_pool().connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, params or ())
            return await cur.fetchone()


async def execute(sql: str, params: Sequence[Any] | None = None) -> int:
    """执行写操作并提交，返回受影响行数。"""
    async with get_pool().connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, params or ())
            affected = cur.rowcount
        await conn.commit()
        return affected


@asynccontextmanager
async def transaction() -> AsyncIterator[Any]:
    """显式事务：一次下单 = 主表 + 明细，必须原子落库。

    用法：
        async with transaction() as conn:
            async with conn.cursor() as cur:
                await cur.execute(...)
    """
    async with get_pool().connection() as conn:
        try:
            yield conn
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise
