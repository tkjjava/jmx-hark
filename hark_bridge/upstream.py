"""带凭据的上游调用。

所有出口都是 CallResult —— 错误是值，不是异常。
被闸门拒绝的账号不发请求，直接把原因返回。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx

from .endpoints import endpoint, render
from .session import DEFAULT_BASE, DEFAULT_TIMEOUT, probe_session
from .state import AccountState, after_probe, is_dispatchable
from .store import AccountStore

# 浏览器会话需要的请求头。缺失会被上游当成非页面来源拒绝。
BROWSER_HEADERS = {
    "origin": "https://hark.com",
    "referer": "https://hark.com/chat",
}


@dataclass(frozen=True)
class CallResult:
    ok: bool
    status: int | None = None
    data: Any = None
    verdict: str = ""
    note: str = ""
    endpoint: str = ""

    def to_log(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "status": self.status,
            "verdict": self.verdict,
            "endpoint": self.endpoint,
            "note": self.note,
            "data": self.data,
        }


def verdict_for(status: int | None) -> str:
    """状态码 → 可操作结论。

    Hark 前面有 Cloudflare：未授权路径由边缘直接 401/403，
    真正不存在的路径由源站返回 404。
    """
    if status is None:
        return "error"
    if status == 200:
        return "ok"
    if status in (401, 403):
        return "denied"
    if status == 404:
        return "not_found"
    if status == 429:
        return "throttled"
    return "error"


def _parse(response: httpx.Response) -> Any:
    ctype = (response.headers.get("content-type") or "").lower()
    if "json" in ctype:
        try:
            return response.json()
        except ValueError:
            return None
    text = response.text
    return text[:2000] if text else None


class NotDispatchable(RuntimeError):
    def __init__(self, account: str, state: AccountState, reason: str) -> None:
        super().__init__(f"账号 {account} 不可派单（{state.value}）：{reason}")
        self.account = account
        self.state = state
        self.reason = reason


async def acquire(
    store: AccountStore,
    account: str,
    *,
    base_url: str = DEFAULT_BASE,
    transport: httpx.AsyncBaseTransport | None = None,
    refresh_if_stale: bool = True,
) -> tuple[dict[str, str], Any]:
    """派单闸门：确认账号可用并返回会话 Cookie。

    探针过期时先复核一次。复核失败按结果落状态，然后抛 NotDispatchable。
    """
    record = store.get(account)
    if record is None:
        raise NotDispatchable(account, AccountState.UNVERIFIED, "账号不存在")

    now = int(time.time())
    stale = record.last_probe_ok_at is None or now - record.last_probe_ok_at > 15 * 60

    if stale and refresh_if_stale:
        cookies = store.cookies(account)
        if not cookies:
            store.mark_failure(
                account,
                state=AccountState.UNVERIFIED,
                error="库中无会话 Cookie",
                clear_credentials=False,
            )
            raise NotDispatchable(account, AccountState.UNVERIFIED, "无会话凭据")

        probe = await probe_session(
            cookies, base_url=base_url, transport=transport
        )
        if probe.valid:
            state = after_probe(
                valid=True, now=now, session_expires_at=probe.expires_at
            )
            store.save_session(
                account,
                cookies=dict(probe.cookies or cookies),
                session_id=probe.session_id,
                user_id=probe.user_id,
                session_expires_at=probe.expires_at,
                state=state,
                probe_status=probe.upstream_status,
                label=record.label,
            )
            record = store.get(account)
        else:
            state = (
                AccountState.SESSION_EXPIRED
                if probe.definitive
                else AccountState.UNVERIFIED
            )
            store.mark_failure(
                account,
                state=state,
                error=probe.note or "探针未通过",
                probe_status=probe.upstream_status,
                clear_credentials=False,
            )
            raise NotDispatchable(account, state, probe.note or "探针未通过")

    assert record is not None
    if not is_dispatchable(
        record.state,
        now=now,
        last_probe_ok_at=record.last_probe_ok_at,
        session_expires_at=record.session_expires_at,
    ):
        raise NotDispatchable(
            account,
            record.state,
            record.last_error or "闸门未通过",
        )

    cookies = store.cookies(account)
    if not cookies:
        raise NotDispatchable(account, record.state, "库中无会话 Cookie")
    return cookies, record


async def call(
    store: AccountStore,
    account: str,
    name: str,
    *,
    path_params: dict[str, object] | None = None,
    body: dict[str, object] | None = None,
    params: dict[str, str] | None = None,
    base_url: str = DEFAULT_BASE,
    timeout: httpx.Timeout = DEFAULT_TIMEOUT,
    transport: httpx.AsyncBaseTransport | None = None,
) -> CallResult:
    """取凭据 → 派单闸门 → 发请求 → 结构化返回。"""
    ep = endpoint(name)

    try:
        cookies, _record = await acquire(
            store, account, base_url=base_url, transport=transport
        )
    except NotDispatchable as exc:
        return CallResult(
            ok=False,
            verdict="not_dispatchable",
            note=str(exc),
            data={"state": exc.state.value, "reason": exc.reason},
            endpoint=f"{ep.method} {ep.path}",
        )

    try:
        path = render(ep.path, path_params or {})
    except ValueError as exc:
        return CallResult(
            ok=False, verdict="bad_request", note=str(exc), endpoint=ep.method
        )

    body = _normalize_body(name, body)
    headers = {"accept": "application/json", **BROWSER_HEADERS}

    async with httpx.AsyncClient(
        base_url=base_url,
        timeout=timeout,
        follow_redirects=False,
        transport=transport,
        cookies=cookies,
    ) as client:
        try:
            response = await client.request(
                ep.method, path, headers=headers, json=body, params=params
            )
        except httpx.HTTPError as exc:
            return CallResult(
                ok=False,
                verdict="error",
                note=f"传输失败: {type(exc).__name__}",
                endpoint=f"{ep.method} {path}",
            )

    verdict = verdict_for(response.status_code)
    ok = verdict == "ok"

    # 401/403 说明会话在服务端已不可用，落状态避免继续派单
    if response.status_code in (401, 403):
        store.mark_failure(
            account,
            state=AccountState.NEEDS_INTERACTIVE_VERIFICATION,
            error=f"上游 {response.status_code}",
            probe_status=response.status_code,
        )

    return CallResult(
        ok=ok,
        status=response.status_code,
        data=_parse(response),
        verdict=verdict,
        note="" if ok else f"HTTP {response.status_code} ({verdict})",
        endpoint=f"{ep.method} {path}",
    )


# POST /api/conversations 的真实入参是嵌套的：
# {"project": {"autoTitle": ...}}。直接传 {"autoTitle": ...} 会 400 invalid body，
# 只传 {"title": ...} 会报 autoTitle is required for projects。
_PROJECT_WRAPPED = {"create_conversation"}


def _normalize_body(
    name: str, body: dict[str, object] | None
) -> dict[str, object] | None:
    """把调用方给的扁平入参整理成上游要的形状。"""
    if body is None:
        return None
    if name in _PROJECT_WRAPPED and "project" not in body:
        inner = dict(body)
        if "autoTitle" not in inner and "title" in inner:
            inner["autoTitle"] = inner.get("title")
        return {"project": inner}
    return body
