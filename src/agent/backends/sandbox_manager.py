"""
沙箱管理器 —— Per-User 沙箱生命周期 + 预热池 + 健康重建。

一句话：**同一用户的多条对话共用一个沙箱；启动时先造好一个空沙箱放在池里，
第一个用户进来直接认领，不用等那 10~20 秒的创建时间。**

为什么需要它（对比"每次请求建一个沙箱"）：
    1. OpenSandbox 创建沙箱要拉镜像、起容器、装依赖，耗时 10~20 秒；
       如果放在请求链路上，第一个用户必然看到一个转圈 20 秒的界面；
    2. 沙箱是有状态的（技能文件、分析中间产物、venv），同一用户的连续对话
       应该看到同一个文件系统，否则"刚才生成的分析结果"下一轮就没了；
    3. 沙箱会挂（docker 重启、容器被 OOM kill）。挂掉后必须能自动重建，
       而不是让用户看到 500。

五态生命周期（`ensure_sandbox_for_user`）：
    0. 预热池有货         → 直接认领，并把 id 写进注册表
    1. 进程内缓存命中      → `echo ok` 探活，失败转重建
    2. 注册表有 sandbox_id → 重连，探活失败转重建
    3. 什么都没有          → 新建
    （认领/新建之后，后台异步补一个预热沙箱，让下一个用户继续零等待）

与中间件的分工（重要，面试常被追问）：
    - 本模块：客观事实 —— 沙箱存不存在、能不能连上、绑定关系是什么；
    - SandboxHealthMiddleware：主动探活 → 挂了就调 `recreate_user_sandbox()`；
    - SandboxCircuitBreakerMiddleware：重建也救不回来 → 熔断，防止死循环。
    三者是"恢复"与"止损"的分工，不是重复实现。

持久化：`ops.sandbox_registry`（PostgreSQL），见 sandbox_registry.py。
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

from agent.backends import sandbox_registry
from agent.backends.sandbox_proxy import SandboxBackendProxy
from agent.config import SANDBOX_CONFIG

logger = logging.getLogger(__name__)

# 进程内沙箱缓存：user_id → 稳定代理句柄。
# 存代理而不是后端，是因为重建沙箱时要"换芯不换壳"——
# agent graph、中间件、工具闭包都持有这个代理，后端被热替换后它们无需感知。
SANDBOX_BACKENDS: dict[str, SandboxBackendProxy] = {}

# 预热池（当前实现是"一个备用"，够覆盖单机低并发；扩容思路见文档）
_warm_reserve: Optional[SandboxBackendProxy] = None
_warm_lock = asyncio.Lock()
# 预热开关：本地开发嫌慢可以 SANDBOX_WARM_POOL=0 关掉（首个用户会多等 10~20 秒）
_warm_enabled = os.environ.get("SANDBOX_WARM_POOL", "1").strip().lower() not in ("0", "false", "no")


# ============================================================
# 初始化 / 预热
# ============================================================


async def initialize(connection_string: Optional[str] = None) -> None:
    """初始化注册表（建池 + 建表）。幂等。"""
    await sandbox_registry.init(connection_string)
    logger.info("沙箱管理器已初始化")


async def pre_warm() -> None:
    """启动时预热一个沙箱。失败只告警，不阻塞启动。

    为什么不阻塞：沙箱服务没起来时，整个 Web 服务仍然应该能启动
    （只是聊天会失败），否则"沙箱挂了 = 服务起不来"，运维没法先看一眼日志。
    """
    global _warm_reserve
    if not _warm_enabled:
        logger.info("沙箱预热已关闭（SANDBOX_WARM_POOL=0），首请求时创建")
        return

    logger.info("正在预热沙箱（首次约 10~20 秒）...")
    try:
        from agent.backends.sandbox_setup import setup_sandbox

        backend = await asyncio.to_thread(setup_sandbox, SANDBOX_CONFIG)
        _warm_reserve = SandboxBackendProxy(backend)
        logger.info("预热沙箱就绪: %s", backend.id)
    except Exception:  # noqa: BLE001
        logger.warning("预热沙箱失败，将在第一个用户连接时创建", exc_info=True)
        _warm_reserve = None


def set_warm_enabled(enabled: bool) -> None:
    """运维开关：预热失败且不想再尝试时关掉（也用于单测）。"""
    global _warm_enabled
    _warm_enabled = bool(enabled)


# ============================================================
# 核心：取用户的沙箱
# ============================================================


async def ensure_sandbox_for_user(user_id: str) -> SandboxBackendProxy:
    """取得该用户可用的沙箱（五态生命周期，见模块 docstring）。"""
    # ---- 状态 1：进程内缓存 ----
    proxy = SANDBOX_BACKENDS.get(user_id)
    if proxy is not None:
        if await _is_alive(proxy):
            logger.debug("用户 %s 命中沙箱缓存: %s", user_id, proxy.id)
            return proxy
        logger.warning("用户 %s 的沙箱 %s 不可达，开始重建", user_id, proxy.id)
        return await _recreate_sandbox(user_id, proxy)

    # ---- 状态 0：认领预热沙箱 ----
    async with _warm_lock:
        if _warm_reserve is not None:
            proxy = _warm_reserve
            globals()["_warm_reserve"] = None
            SANDBOX_BACKENDS[user_id] = proxy
            await sandbox_registry.bind(user_id, proxy.id)
            logger.info("用户 %s 认领了预热沙箱: %s", user_id, proxy.id)
            asyncio.create_task(_replenish_warm())
            return proxy

    # ---- 状态 2：注册表重连 ----
    sandbox_id = await sandbox_registry.get_sandbox_id(user_id)
    if sandbox_id:
        logger.info("用户 %s 尝试重连沙箱: %s", user_id, sandbox_id)
        proxy = await _connect(sandbox_id)
        if proxy is not None and await _is_alive(proxy):
            SANDBOX_BACKENDS[user_id] = proxy
            await sandbox_registry.touch(user_id)
            return proxy
        logger.warning("沙箱 %s 不可达，改为新建", sandbox_id)

    # ---- 状态 3：新建 ----
    return await _create_sandbox_for_user(user_id)


# ============================================================
# 对中间件暴露的能力
# ============================================================


async def ping_user_sandbox(user_id: str) -> bool:
    """沙箱是否可用（供中间件/健康检查调用）。"""
    proxy = SANDBOX_BACKENDS.get(user_id)
    if proxy is None:
        return False
    return await _is_alive(proxy)


async def has_sandbox(user_id: str) -> bool:
    """进程内是否已有该用户的沙箱句柄（不代表可用）。"""
    return user_id in SANDBOX_BACKENDS


async def recreate_user_sandbox(user_id: str) -> SandboxBackendProxy:
    """重建沙箱（健康检查发现不可用时调用）。

    注意：这里不负责重新上传 AGENTS.md —— 那是调用方（中间件）的职责，
    因为"播种哪些文件"属于 Agent 装配，不属于沙箱管理。
    """
    return await _recreate_sandbox(user_id, SANDBOX_BACKENDS.get(user_id))


async def cleanup_user(user_id: str) -> None:
    """销毁某用户的沙箱并解绑（会话删除/用户登出时调用）。"""
    proxy = SANDBOX_BACKENDS.pop(user_id, None)
    if proxy is not None:
        await _delete_quietly(proxy.id)
    await sandbox_registry.unbind(user_id)
    logger.info("用户 %s 沙箱已清理", user_id)


async def shutdown() -> None:
    """清理所有沙箱（含预热池）并关闭注册表连接。"""
    global _warm_reserve
    logger.info("正在清理所有沙箱...")

    if _warm_reserve is not None:
        await _delete_quietly(_warm_reserve.id)
        _warm_reserve = None

    for user_id in list(SANDBOX_BACKENDS.keys()):
        await cleanup_user(user_id)

    await sandbox_registry.close()
    logger.info("沙箱管理器已关闭")


async def stats() -> dict:
    """自检/运维用：池状态快照（不探活，只报事实）。"""
    return {
        "warm_reserve": _warm_reserve.id if _warm_reserve is not None else None,
        "cached_users": {u: p.id for u, p in SANDBOX_BACKENDS.items()},
        "registry_bindings": len(await sandbox_registry.list_bindings(limit=500)),
    }


# ============================================================
# 内部实现
# ============================================================


async def _is_alive(proxy: SandboxBackendProxy) -> bool:
    """用 `echo ok` 探活。为什么不查 SDK 的状态接口：不同版本接口不稳定，
    而"能不能执行一条命令"才是我们真正关心的能力。"""
    try:
        await asyncio.to_thread(proxy.execute, "echo ok")
        return True
    except Exception:  # noqa: BLE001
        return False


async def _connect(sandbox_id: str) -> Optional[SandboxBackendProxy]:
    """按 id 连接已有沙箱（失败返回 None，由调用方决定是否新建）。

    复用 `setup_sandbox(config, sandbox_id=...)`：它内部就是
    `SandboxSync.connect(id)`，连不上会自行降级为新建并播种。
    """
    try:
        from agent.backends.sandbox_setup import setup_sandbox

        backend = await asyncio.to_thread(setup_sandbox, SANDBOX_CONFIG, sandbox_id)
        return SandboxBackendProxy(backend)
    except Exception:  # noqa: BLE001
        logger.warning("连接沙箱 %s 失败", sandbox_id, exc_info=True)
        return None


async def _create_sandbox_for_user(user_id: str) -> SandboxBackendProxy:
    """新建沙箱并绑定到用户。"""
    from agent.backends.sandbox_setup import setup_sandbox

    logger.info("为用户 %s 创建新沙箱...", user_id)
    backend = await asyncio.to_thread(setup_sandbox, SANDBOX_CONFIG)
    proxy = SandboxBackendProxy(backend)
    SANDBOX_BACKENDS[user_id] = proxy
    await sandbox_registry.bind(user_id, backend.id)
    logger.info("用户 %s 沙箱创建完成: %s", user_id, backend.id)
    return proxy


async def _recreate_sandbox(
    user_id: str, existing_proxy: Optional[SandboxBackendProxy]
) -> SandboxBackendProxy:
    """新建一个沙箱，并把旧代理"换芯"（旧沙箱尽力删除）。"""
    from agent.backends.sandbox_setup import setup_sandbox

    logger.info("为用户 %s 重建沙箱...", user_id)
    old_id = existing_proxy.id if existing_proxy is not None else await sandbox_registry.get_sandbox_id(user_id)

    backend = await asyncio.to_thread(setup_sandbox, SANDBOX_CONFIG)

    if existing_proxy is not None:
        existing_proxy.replace_backend(backend)      # 换芯：外部持有者无感知
        proxy = existing_proxy
    else:
        proxy = SandboxBackendProxy(backend)
        SANDBOX_BACKENDS[user_id] = proxy

    if old_id and old_id != backend.id:
        await _delete_quietly(old_id)

    await sandbox_registry.bind(user_id, backend.id)
    logger.info("用户 %s 沙箱重建完成: %s", user_id, backend.id)
    return proxy


async def _replenish_warm() -> None:
    """后台补充预热沙箱（被认领后异步触发）。"""
    global _warm_reserve
    if not _warm_enabled or _warm_reserve is not None:
        return
    try:
        from agent.backends.sandbox_setup import setup_sandbox

        backend = await asyncio.to_thread(setup_sandbox, SANDBOX_CONFIG)
        async with _warm_lock:
            if _warm_reserve is None:
                _warm_reserve = SandboxBackendProxy(backend)
                logger.info("后台补充预热沙箱就绪: %s", backend.id)
                return
        # 已有别的协程补上了，这个多出来的立刻释放，避免容器泄漏
        await _delete_quietly(backend.id)
    except Exception:  # noqa: BLE001
        logger.warning("后台补充预热沙箱失败", exc_info=True)


async def _delete_quietly(sandbox_id: str) -> None:
    """删除沙箱，失败只记录（旧沙箱残留不该影响主流程）。"""
    try:
        from opensandbox import SandboxSync

        await asyncio.to_thread(SandboxSync.delete, sandbox_id)
    except Exception:  # noqa: BLE001
        logger.warning("删除沙箱 %s 失败（可忽略）", sandbox_id, exc_info=True)
