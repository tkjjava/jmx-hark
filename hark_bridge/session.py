"""会话复核。

判定"已登录"的唯一依据是服务端 GET /api/auth/get-session 返回真实 session。
"hark.com 域下存在 Cookie"不算登录——偏好 Cookie（如 theme=dark）也会存在。

两个关键细节：
  1. **token 滚动续期**：Better Auth 在会话进入刷新窗口时随响应下发新的
     session token，旧 token 随即失效。丢掉这一枚等于放弃续期。
  2. **definitive 判定**：只有上游明确回答"没有这个会话"（200 + 结构化 JSON
     且 session 为空）才算确定失效。200 但返回 HTML（Cloudflare 拦截页、
     网关页）属于不确定，凭据必须保留——否则一次误判就把好账号清掉。
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx

DEFAULT_BASE = "https://hark.com"
SESSION_PATH = "/api/auth/get-session"

# Hark 把 Better Auth 的 cookiePrefix 改成了 hark，
# 会话 Cookie 名是 __Secure-hark.session_token，
# 另有 __Secure-hark.session_token_multi-<id>（multiSession 插件）。
DEFAULT_SESSION_COOKIE_PREFIXES = (
    "hark.session_token",
    "better-auth.session_token",
)

DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=20.0, write=10.0, pool=5.0)


def _bare_cookie_name(name: str) -> str:
    for prefix in ("__Secure-", "__Host-"):
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


def is_session_cookie_name(name: str) -> bool:
    """是不是会话 token 名。覆盖分块（.0/.1）与 multiSession（_multi-xxx）变体。"""
    bare = _bare_cookie_name(name)
    return any(bare.startswith(prefix) for prefix in DEFAULT_SESSION_COOKIE_PREFIXES)


def filter_session_cookies(cookies: Mapping[str, str]) -> dict[str, str]:
    """只保留会话 token，丢掉偏好 Cookie。"""
    return {name: value for name, value in cookies.items() if is_session_cookie_name(name)}


def has_session_cookie(cookies: Mapping[str, str]) -> bool:
    return any(is_session_cookie_name(name) for name in cookies)


def _parse_expiry(value: object) -> int | None:
    """Better Auth 的 expiresAt 通常是 ISO 字符串，也接受秒/毫秒时间戳。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > 1e11 else value
        return int(seconds)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp())


@dataclass(frozen=True)
class Probe:
    """一次 get-session 复核的结果。valid 为 False 时一律当未登录。"""

    valid: bool
    session_id: str | None = None
    user_id: str | None = None
    expires_at: int | None = None
    upstream_status: int | None = None
    # 只有上游结构化地否认会话时才为 True
    definitive: bool = False
    note: str = ""
    cookies: Mapping[str, str] = field(default_factory=dict)
    rotated: bool = False


def _safe_json(response: httpx.Response) -> dict | None:
    ctype = (response.headers.get("content-type") or "").lower()
    if "json" not in ctype:
        return None
    try:
        payload = response.json()
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _response_session_cookies(response: httpx.Response) -> dict[str, str]:
    """只取本次响应 Set-Cookie 下发的 session token，即服务端轮换后的那一枚。"""
    if not response.headers.get_list("set-cookie"):
        return {}
    jar = httpx.Cookies()
    try:
        jar.extract_cookies(response)
    except (TypeError, AttributeError):
        return {}
    found: dict[str, str] = {}
    for cookie in jar.jar:
        host = (cookie.domain or "").lstrip(".")
        if host and not host.endswith("hark.com"):
            continue
        if is_session_cookie_name(cookie.name):
            found[cookie.name] = cookie.value
    return found


async def probe_session(
    cookies: Mapping[str, str],
    *,
    base_url: str = DEFAULT_BASE,
    timeout: httpx.Timeout = DEFAULT_TIMEOUT,
    transport: httpx.AsyncBaseTransport | None = None,
    retries_on_non_json: int = 3,
) -> Probe:
    """用给定 Cookie 复核会话。这是判定"已登录"的唯一依据。"""
    if not cookies:
        return Probe(valid=False, note="无 session cookie")

    rotated: dict[str, str] = {}
    response: httpx.Response | None = None

    async with httpx.AsyncClient(
        base_url=base_url,
        timeout=timeout,
        follow_redirects=False,
        transport=transport,
        cookies=dict(cookies),
    ) as client:
        try:
            response = await client.get(
                SESSION_PATH, headers={"accept": "application/json"}
            )
        except httpx.HTTPError as exc:
            return Probe(valid=False, note=f"探针传输失败: {type(exc).__name__}")
        rotated = _response_session_cookies(response)

    merged = {**dict(cookies), **rotated}
    changed = bool(rotated) and any(
        cookies.get(name) != value for name, value in rotated.items()
    )

    if response.status_code != 200:
        return Probe(
            valid=False,
            upstream_status=response.status_code,
            note=f"探针返回 {response.status_code}",
        )

    payload = _safe_json(response)
    if payload is None:
        # 200 但拿到非 JSON：Cloudflare 拦截页 / 网关页。属于"不确定"，不是失效。
        for _ in range(retries_on_non_json):
            await asyncio.sleep(0.8)
            try:
                async with httpx.AsyncClient(
                    base_url=base_url,
                    timeout=timeout,
                    follow_redirects=False,
                    transport=transport,
                    cookies=dict(cookies),
                ) as retry_client:
                    response = await retry_client.get(
                        SESSION_PATH, headers={"accept": "application/json"}
                    )
            except httpx.HTTPError:
                continue
            rotated = _response_session_cookies(response) or rotated
            merged = {**dict(cookies), **rotated}
            changed = bool(rotated) and any(
                cookies.get(name) != value for name, value in rotated.items()
            )
            payload = _safe_json(response)
            if payload is not None:
                break
        if payload is None:
            return Probe(
                valid=False,
                upstream_status=response.status_code if response else None,
                note="探针响应非 JSON 对象（疑似拦截页）",
            )

    session = payload.get("session")
    if not isinstance(session, dict) or not session.get("id"):
        return Probe(
            valid=False,
            upstream_status=200,
            note="探针返回空 session",
            definitive=True,
        )

    user = payload.get("user")
    user_id = user.get("id") if isinstance(user, dict) else None
    return Probe(
        valid=True,
        session_id=session.get("id"),
        user_id=user_id if isinstance(user_id, str) else None,
        expires_at=_parse_expiry(session.get("expiresAt")),
        upstream_status=200,
        cookies=merged,
        rotated=changed,
    )
