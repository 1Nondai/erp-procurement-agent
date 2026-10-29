"""
请求身份解析（P0）。

目标：把"当前用户是谁"从硬编码常量变成**每个请求携带的信息**。

设计要点：
  1. 身份来源为 HTTP 请求头 `X-User-Id` / `X-Username`，由前端 api/identity.js 统一注入；
  2. **必须做白名单校验**：user_id 会拼进 `/memories/{user_id}/preferences.md` 这类路径，
     若允许 `../`、`/`、`:` 等字符，就可以越出目录去读写别人的记忆（路径穿越）。
     因此只放行 `[A-Za-z0-9_.-]{1,64}`；
  3. 未携带身份时回退到 config.DEFAULT_USER_ID，并打 WARNING ——
     兜底必须是"看得见的降级"，而不是静默冒充某个真实用户；
  4. 本模块只依赖标准库 + agent.config，便于单测。

出现位置：api_view/api/chat.py、api_view/api/history.py 的端点函数
通过 FastAPI 的 Header 参数取值后调用 resolve_identity()。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# user_id 白名单：字母、数字、下划线、点、短横线，长度 1-64
# 注意：点号是允许的（形如 zhangsan.dev），但下面还会显式拒绝 ".." 组合。
_USER_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")

_USERNAME_MAX_LEN = 64


@dataclass(frozen=True)
class Identity:
    """一个请求的调用者身份。"""
    user_id: str                 # 已校验的合法 user_id（必然非空）
    username: str                # 展示名，缺省时等于 user_id
    from_request: bool           # True=来自请求头；False=客户端未提供，用的兜底值


def sanitize_user_id(raw: Optional[str]) -> Optional[str]:
    """校验并规范化 user_id；不合法返回 None。

    拒绝规则：
      - 空值 / 超长 / 含非法字符；
      - 含 ".."（路径穿越的经典写法，即便字符集本身允许点号）。
    """
    if raw is None:
        return None
    candidate = str(raw).strip()
    if not candidate:
        return None
    if ".." in candidate:
        # 单独拦住，避免 "a/../b" 之类在后续拼接路径时越权
        return None
    if not _USER_ID_PATTERN.match(candidate):
        return None
    return candidate


def resolve_identity(
    x_user_id: Optional[str] = None,
    x_username: Optional[str] = None,
) -> Identity:
    """把请求头解析成 Identity（带校验与兜底）。

    Args:
        x_user_id: 请求头 X-User-Id。
        x_username: 请求头 X-Username（可空，缺省用 user_id 代替）。

    Returns:
        Identity 实例。user_id 一定合法：要么来自请求头，要么是默认用户。
    """
    # 惰性导入：便于单测本模块时不拉起 agent.config 的重依赖
    try:
        from agent.config import DEFAULT_USER_ID, DEFAULT_USERNAME
    except Exception:  # pragma: no cover
        DEFAULT_USER_ID, DEFAULT_USERNAME = "dev_default", "开发默认用户"

    valid = sanitize_user_id(x_user_id)
    if valid is None:
        if x_user_id:
            # 提供了但非法：明确告警，避免"以为在用某个身份、其实走了兜底"
            logger.warning(
                "收到非法 X-User-Id=%r（只允许 [A-Za-z0-9_.-]{1,64}，且不得含 '..'），已回退默认用户",
                x_user_id,
            )
        else:
            logger.warning("请求未携带 X-User-Id，已使用默认用户 %s", DEFAULT_USER_ID)
        username = DEFAULT_USERNAME
        return Identity(user_id=DEFAULT_USER_ID, username=username, from_request=False)

    username = str(x_username).strip() if x_username else ""
    if not username:
        username = valid
    elif len(username) > _USERNAME_MAX_LEN:
        username = username[:_USERNAME_MAX_LEN]

    return Identity(user_id=valid, username=username, from_request=True)
