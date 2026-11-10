"""
Langfuse 可观测性接入层（LLM/Agent 全链路追踪）。

设计目标（生产环境约束）：
1. **零内部依赖**：本模块只依赖标准库 + 可选 ``langfuse`` SDK，
   不 import 项目的 agent/api 模块，可独立导入、独立测试。
2. **失败降级**：未安装 SDK、未配置密钥、显式关闭、初始化异常——
   任何一条命中，``get_callback_handler()`` 返回 ``None``，
   调用方拿到 ``None`` 就照常跑，业务零影响。
3. **单例复用**：客户端与 callback handler 全局只建一次，
   避免每个请求重建连接/线程池。
4. **trace 属性走 config.metadata**：不使用 ``propagate_attributes``
   这类依赖 contextvar 的上下文管理器，避免在 async generator（SSE）
   里跨越 yield 时上下文丢失。langfuse 的 LangChain handler 会直接读取
   ``config["metadata"]`` 中的 ``langfuse_user_id`` / ``langfuse_session_id``
   / ``langfuse_trace_name`` / ``langfuse_tags`` 四个键。

对应环境变量见 ``LangfuseSettings``；示例见项目根 ``.env.example``。
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ============================================================
# 环境变量解析
# ============================================================

_TRUTHY = {"1", "true", "yes", "on", "y", "t"}
_FALSY = {"0", "false", "no", "off", "n", "f"}

# 惰性缓存：None = 未尝试；False = 导入失败（脱离项目使用）
_env_utils_mod: Any = None


def _env_utils_module() -> Any:
    """惰性导入项目统一的环境变量入口 ``agent.env_utils``。

    项目约定：所有敏感配置只在 ``env_utils`` 读取，不散落 os.getenv。
    本模块优先遵守该约定；当它被脱离项目单独使用（脚本 / 测试 / 其他服务）时，
    导入失败会自动回落到 os.getenv。
    """
    global _env_utils_mod
    if _env_utils_mod is False:
        return None
    if _env_utils_mod is None:
        try:
            from agent import env_utils as _mod

            _env_utils_mod = _mod
        except Exception:
            _env_utils_mod = False
            return None
    return _env_utils_mod


def _env_str(name: str, default: str = "") -> str:
    value = ""
    module = _env_utils_module()
    if module is not None:
        attr = getattr(module, name, None)
        if isinstance(attr, str) and attr.strip():
            value = attr.strip()
    if not value:
        raw = os.getenv(name)
        if isinstance(raw, str) and raw.strip():
            value = raw.strip()
    return value or default


def _env_bool(name: str, default: bool) -> bool:
    raw = _env_str(name).lower()
    if raw in _TRUTHY:
        return True
    if raw in _FALSY:
        return False
    return default


def _env_float(name: str, default: float) -> float:
    try:
        raw = _env_str(name)
        return float(raw) if raw else default
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        raw = _env_str(name)
        return int(raw) if raw else default
    except (TypeError, ValueError):
        return default


# ============================================================
# 配置
# ============================================================


@dataclass(frozen=True)
class LangfuseSettings:
    """Langfuse 接入配置，全部来自环境变量。

    ``enabled`` 的默认值取决于密钥是否存在：配了密钥就默认开启，
    这样只想关掉追踪时显式写 ``LANGFUSE_ENABLED=false`` 即可。
    """

    public_key: str
    secret_key: str
    base_url: str
    enabled: bool
    sample_rate: float
    environment: str
    release: str
    flush_at: int
    flush_interval: float
    timeout: float
    debug: bool
    mask_enabled: bool
    auth_check: bool

    @property
    def has_credentials(self) -> bool:
        return bool(self.public_key and self.secret_key)

    @classmethod
    def from_env(cls) -> "LangfuseSettings":
        public_key = _env_str("LANGFUSE_PUBLIC_KEY")
        secret_key = _env_str("LANGFUSE_SECRET_KEY")
        # 兼容旧变量名 LANGFUSE_HOST（v2 时代），新变量优先
        base_url = _env_str("LANGFUSE_BASE_URL") or _env_str(
            "LANGFUSE_HOST", "https://cloud.langfuse.com"
        )
        sample_rate = _env_float("LANGFUSE_SAMPLE_RATE", 1.0)
        # 采样率非法时退化为全量采集，避免静默丢数据
        if not 0.0 <= sample_rate <= 1.0:
            sample_rate = 1.0

        return cls(
            public_key=public_key,
            secret_key=secret_key,
            base_url=base_url.rstrip("/"),
            enabled=_env_bool("LANGFUSE_ENABLED", bool(public_key and secret_key)),
            sample_rate=sample_rate,
            environment=_env_str("LANGFUSE_ENVIRONMENT")
            or _env_str("APP_ENV", "development"),
            release=_env_str("LANGFUSE_RELEASE") or _env_str("APP_VERSION", "dev"),
            flush_at=_env_int("LANGFUSE_FLUSH_AT", 512),
            flush_interval=_env_float("LANGFUSE_FLUSH_INTERVAL", 5.0),
            timeout=_env_float("LANGFUSE_TIMEOUT", 10.0),
            debug=_env_bool("LANGFUSE_DEBUG", False),
            mask_enabled=_env_bool("LANGFUSE_MASK_ENABLED", True),
            auth_check=_env_bool("LANGFUSE_AUTH_CHECK", False),
        )


# ============================================================
# 敏感数据脱敏
# ============================================================
# 说明：Langfuse 的 MaskFunction 是 keyword-only 的 ``data`` 参数，
# 返回值必须可 JSON 序列化（见 langfuse.types.MaskFunction）。
# 因此这里对 data 做受限深拷贝 + 正则替换，而不是只处理 str。

_MASK_RULES = (
    # OpenAI / DeepSeek / 智谱风格的密钥
    (re.compile(r"\b(sk|pk)-(?:lf-)?[A-Za-z0-9_\-]{12,}"), r"\1-***MASKED***"),
    # HTTP 头里的 Bearer token
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9\-._~+/]{12,}=*"), r"\1***MASKED***"),
    # MongoDB / Redis 等连接串中的口令
    # 贪婪匹配到「最后一个 @」，才能覆盖口令里本身含 @ 的情况
    # （如 root:p@ssw0rd@host:27017）
    (
        re.compile(
            r"(?i)\b(mongodb(?:\+srv)?|redis|mysql|postgres(?:ql)?)://([^:@/\s]+):([^\s\"']*)@"
        ),
        r"\1://\2:***MASKED***@",
    ),
    # 中国大陆手机号
    (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "***MASKED_PHONE***"),
    # 邮箱
    (
        re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"),
        "***MASKED_EMAIL***",
    ),
)

_MAX_MASK_DEPTH = 6
_MAX_MASK_ITEMS = 200
_MAX_MASK_STR = 20_000


def _mask_text(text: str) -> str:
    if len(text) > _MAX_MASK_STR:
        text = text[:_MAX_MASK_STR]
    for pattern, repl in _MASK_RULES:
        text = pattern.sub(repl, text)
    return text


def _mask_value(value: Any, depth: int = 0) -> Any:
    if depth > _MAX_MASK_DEPTH:
        return "***TRUNCATED***"
    if isinstance(value, str):
        return _mask_text(value)
    if isinstance(value, dict):
        return {
            k: _mask_value(v, depth + 1)
            for k, v in list(value.items())[:_MAX_MASK_ITEMS]
        }
    if isinstance(value, (list, tuple, set)):
        return [_mask_value(v, depth + 1) for v in list(value)[:_MAX_MASK_ITEMS]]
    return value


def mask_data(*, data: Any = None, **_kwargs: Any) -> Any:
    """Langfuse MaskFunction 实现：对 trace 载荷做敏感信息脱敏。"""
    try:
        return _mask_value(data)
    except Exception:  # 脱敏失败绝不阻断上报链路
        logger.warning("[langfuse] 脱敏失败，已跳过本条载荷", exc_info=True)
        return "***MASK_ERROR***"


# ============================================================
# 单例客户端 / handler
# ============================================================

_client: Any = None
_handler: Any = None
_initialized: bool = False
_init_error: Optional[str] = None
_sdk_available: Optional[bool] = None
# 显式 shutdown 后置位，禁止被后续调用「复活」出新的客户端/上报线程
_shutdown_latched: bool = False


def _sdk_installed() -> bool:
    global _sdk_available
    if _sdk_available is None:
        try:
            import langfuse  # noqa: F401

            _sdk_available = True
        except Exception:
            _sdk_available = False
    return _sdk_available


_dotenv_loaded: bool = False


def _ensure_env_loaded() -> None:
    """确保 .env 已加载。

    项目主链路由 ``agent.env_utils`` 负责 load_dotenv，但本模块设计为零依赖，
    可能被单独导入（如脚本、测试、其他服务）。这里做一次兜底加载，
    且**不使用 override**，保证真实环境变量优先级最高。
    """
    global _dotenv_loaded
    if _dotenv_loaded:
        return
    _dotenv_loaded = True
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except Exception:
        # 没有 python-dotenv 或没有 .env 都属正常情况
        pass


def init_tracing(*, force: bool = False) -> bool:
    """初始化 Langfuse 客户端与 LangChain callback handler。

    幂等：重复调用直接返回上次结果。任何异常都被吞掉并记录，
    返回 ``False`` 表示追踪未启用，调用方无需做任何特判。

    Returns:
        True 表示追踪可用，False 表示已降级。
    """
    global _client, _handler, _initialized, _init_error, _shutdown_latched

    if _shutdown_latched and not force:
        # 已显式关闭：不再自动重建，避免进程退出阶段仍向上游推送
        return False

    if _initialized and not force:
        return _handler is not None

    if force:
        _shutdown_latched = False

    _initialized = True
    _init_error = None
    _client = None
    _handler = None

    _ensure_env_loaded()
    settings = LangfuseSettings.from_env()

    if not settings.enabled:
        logger.info("[langfuse] 追踪已关闭（LANGFUSE_ENABLED=false）")
        return False

    if not settings.has_credentials:
        _init_error = "缺少 LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY"
        logger.warning("[langfuse] %s，追踪已降级", _init_error)
        return False

    if not _sdk_installed():
        _init_error = "未安装 langfuse SDK"
        logger.warning("[langfuse] 未安装 langfuse SDK，追踪已降级（pip install langfuse）")
        return False

    try:
        from langfuse import Langfuse

        client_kwargs: Dict[str, Any] = {
            "public_key": settings.public_key,
            "secret_key": settings.secret_key,
            "base_url": settings.base_url,
            "tracing_enabled": True,
            "sample_rate": settings.sample_rate,
            "environment": settings.environment,
            "release": settings.release,
            "flush_at": settings.flush_at,
            "flush_interval": settings.flush_interval,
            "timeout": settings.timeout,
            "debug": settings.debug,
        }
        if settings.mask_enabled:
            client_kwargs["mask"] = mask_data

        _client = Langfuse(**client_kwargs)
    except Exception as exc:  # 初始化失败 → 降级
        _init_error = f"客户端初始化失败: {exc}"
        logger.warning("[langfuse] %s，追踪已降级", _init_error, exc_info=True)
        _client = None
        return False

    try:
        from langfuse.langchain import CallbackHandler

        # v4/v5 的 CallbackHandler 只接受 public_key / trace_context，
        # user_id、session_id 等 trace 属性通过 config.metadata 传入。
        _handler = CallbackHandler(public_key=settings.public_key)
    except Exception as exc:
        _init_error = f"CallbackHandler 创建失败: {exc}"
        logger.warning("[langfuse] %s，追踪已降级", _init_error, exc_info=True)
        _handler = None
        return False

    if settings.auth_check:
        try:
            if not _client.auth_check():
                _init_error = "鉴权失败（请检查密钥与 base_url）"
                logger.warning("[langfuse] %s", _init_error)
                _handler = None
                return False
        except Exception as exc:
            # 网络抖动不应阻断应用启动
            logger.warning("[langfuse] auth_check 调用失败: %s", exc)

    logger.info(
        "[langfuse] 追踪已启用 base_url=%s env=%s sample_rate=%s mask=%s",
        settings.base_url,
        settings.environment,
        settings.sample_rate,
        settings.mask_enabled,
    )
    return True


def get_callback_handler() -> Optional[Any]:
    """返回全局 callback handler；未启用/已降级时返回 None。"""
    if not _initialized:
        init_tracing()
    return _handler


# ============================================================
# LangGraph / LangChain config 注入
# ============================================================


def _build_tags(stage: str, environment: str) -> List[str]:
    tags = ["erp-openclaw", f"stage:{stage}"]
    if environment:
        tags.append(f"env:{environment}")
    return tags


def instrument_config(
    config: Dict[str, Any],
    *,
    user_id: str,
    thread_id: str,
    stage: str = "chat",
    extra_metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """把 Langfuse callback 与 trace 属性挂到 LangGraph 的 RunnableConfig 上。

    - 幂等：已有该 handler 时不重复追加。
    - 只做浅拷贝，不改动调用方传入的 config 对象（除 configurable 引用复用）。
    - 追踪未启用时原样返回 config，调用方无需判断。

    Args:
        config: ``agent_loader.create_config()`` 产出的 RunnableConfig。
        user_id: 业务用户标识，写入 trace.user_id。
        thread_id: 会话标识，写入 trace.session_id（Langfuse 用它串多轮对话）。
        stage: 运行阶段标签，如 ``chat`` / ``resume``。
        extra_metadata: 额外写入 trace.metadata 的键值（如模型名、前端版本）。

    Returns:
        dict: 注入了 callbacks / metadata 的 config（或原 config）。
    """
    handler = get_callback_handler()
    if handler is None:
        return config

    try:
        settings = LangfuseSettings.from_env()

        new_config: Dict[str, Any] = dict(config)

        callbacks = list(new_config.get("callbacks") or [])
        if handler not in callbacks:
            callbacks.insert(0, handler)
        new_config["callbacks"] = callbacks

        metadata: Dict[str, Any] = dict(new_config.get("metadata") or {})
        # 这四个键由 langfuse 的 LangChain handler 直接消费
        metadata.setdefault("langfuse_user_id", str(user_id))
        metadata.setdefault("langfuse_session_id", str(thread_id))
        metadata.setdefault("langfuse_trace_name", f"erp-{stage}")
        metadata.setdefault(
            "langfuse_tags", _build_tags(stage, settings.environment)
        )
        # 业务侧附加信息，便于在 Langfuse UI 里过滤/分组
        metadata["erp_stage"] = stage
        metadata["erp_user_id"] = str(user_id)
        metadata["erp_thread_id"] = str(thread_id)
        if extra_metadata:
            metadata.update(extra_metadata)
        new_config["metadata"] = metadata

        new_config.setdefault("run_name", f"erp-{stage}")
        return new_config
    except Exception:
        # 追踪注入失败不能影响对话主流程
        logger.warning("[langfuse] config 注入失败，本次不追踪", exc_info=True)
        return config


# ============================================================
# 生命周期
# ============================================================


def flush_tracing() -> None:
    """强制把缓冲区内的 trace 推到服务端（长任务/关键节点后调用）。"""
    if _client is None:
        return
    try:
        _client.flush()
    except Exception:
        logger.warning("[langfuse] flush 失败", exc_info=True)


def shutdown_tracing() -> None:
    """应用退出时调用：flush 并释放 OTel tracer provider。"""
    global _client, _handler, _initialized, _shutdown_latched

    _shutdown_latched = True

    if _client is not None:
        try:
            _client.flush()
        except Exception:
            logger.warning("[langfuse] 退出前 flush 失败", exc_info=True)
        try:
            _client.shutdown()
        except Exception:
            logger.warning("[langfuse] 关闭客户端失败", exc_info=True)

    _client = None
    _handler = None
    _initialized = False


def tracing_status() -> Dict[str, Any]:
    """返回追踪状态快照，用于 /health 与排查。不触发网络请求。"""
    settings = LangfuseSettings.from_env()
    return {
        "enabled": _handler is not None,
        "sdk_installed": bool(_sdk_installed()),
        "initialized": _initialized,
        "base_url": settings.base_url if settings.has_credentials else None,
        "environment": settings.environment,
        "sample_rate": settings.sample_rate,
        "mask_enabled": settings.mask_enabled,
        "last_error": _init_error,
    }
