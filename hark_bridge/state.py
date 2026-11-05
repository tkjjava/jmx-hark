"""账号状态机与派单闸门。

核心规则：
  * 只有 session_valid / session_expiring 且探针新鲜、会话未过期，才允许派单。
  * needs_interactive_verification / blocked / credentials_rejected 是终态，
    不自动重试，只能由本人重新导入会话后重置。

这条规则的作用是防止失效账号反复向上游发请求，放大风控观测。
"""

from __future__ import annotations

from enum import Enum

# 探针超过这个时间就不再信任，派单前必须重新复核
PROBE_TTL_SECONDS = 15 * 60

# 会话在这些秒数内到期时，提前标记为需要重新导入
EXPIRY_WARN_SECONDS = 48 * 3600


class AccountState(str, Enum):
    UNVERIFIED = "unverified"
    SESSION_VALID = "session_valid"
    SESSION_EXPIRING = "session_expiring"
    SESSION_EXPIRED = "session_expired"
    NEEDS_INTERACTIVE_VERIFICATION = "needs_interactive_verification"
    BLOCKED = "blocked"
    CREDENTIALS_REJECTED = "credentials_rejected"
    RATE_LIMITED = "rate_limited"


TERMINAL_STATES = frozenset(
    {
        AccountState.NEEDS_INTERACTIVE_VERIFICATION,
        AccountState.BLOCKED,
        AccountState.CREDENTIALS_REJECTED,
    }
)

# 需要人工动作才能继续的状态
HUMAN_STATES = frozenset(
    {
        AccountState.UNVERIFIED,
        AccountState.SESSION_EXPIRED,
        AccountState.NEEDS_INTERACTIVE_VERIFICATION,
        AccountState.BLOCKED,
        AccountState.CREDENTIALS_REJECTED,
    }
)

DISPATCHABLE_STATES = frozenset(
    {AccountState.SESSION_VALID, AccountState.SESSION_EXPIRING}
)


def is_dispatchable(
    state: AccountState,
    *,
    now: int,
    last_probe_ok_at: int | None,
    session_expires_at: int | None,
) -> bool:
    """派单闸门。任何一项不满足就拒绝，不做善意推测。

    SESSION_EXPIRING 仍可派单——它只是提前把人拉进重新导入队列的标记，
    真正拦工作的是会话到期时间。
    """
    if state not in DISPATCHABLE_STATES:
        return False
    if last_probe_ok_at is None:
        return False
    if now - last_probe_ok_at > PROBE_TTL_SECONDS:
        return False
    if session_expires_at is not None and session_expires_at <= now:
        return False
    return True


def needs_reauthorization(*, now: int, session_expires_at: int | None) -> bool:
    """是否该提前提醒重新导入。"""
    if session_expires_at is None:
        return False
    return session_expires_at - now <= EXPIRY_WARN_SECONDS


def after_probe(
    *, valid: bool, now: int, session_expires_at: int | None
) -> AccountState:
    """探针成功后判断是 valid 还是 expiring。"""
    if not valid:
        return AccountState.UNVERIFIED
    if needs_reauthorization(now=now, session_expires_at=session_expires_at):
        return AccountState.SESSION_EXPIRING
    return AccountState.SESSION_VALID


def after_session_expiry_check(
    state: AccountState, *, now: int, session_expires_at: int | None
) -> AccountState:
    """会话真的过期了就把 valid / expiring 降级为 expired。"""
    if state not in DISPATCHABLE_STATES:
        return state
    if session_expires_at is not None and session_expires_at <= now:
        return AccountState.SESSION_EXPIRED
    return state
