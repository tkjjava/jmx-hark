"""端到端测试：用 mock 上游验证完整链路。

不碰真实 hark.com，全部用 httpx MockTransport 拦截。
"""

from __future__ import annotations

import time

import httpx
import pytest

from hark_bridge import pool
from hark_bridge.state import AccountState
from hark_bridge.store import AccountStore


@pytest.fixture
def bridge_key(monkeypatch):
    from cryptography.fernet import Fernet

    key = Fernet.generate_key().decode()
    monkeypatch.setenv("HARK_BRIDGE_KEY", key)
    return key


@pytest.fixture
def store(bridge_key, tmp_path, monkeypatch):
    monkeypatch.setenv("HARK_BRIDGE_DB", str(tmp_path / "accounts.db"))
    instance = AccountStore()
    yield instance
    instance.close()


def _mock_transport(handler):
    return httpx.MockTransport(handler)


def test_save_and_read_roundtrip_encrypted(store, tmp_path):
    store.save_session(
        "acc-1",
        cookies={"__Secure-hark.session_token": "secret-value"},
        session_id="s1",
        user_id="u1",
        session_expires_at=int(time.time()) + 86400,
        state=AccountState.SESSION_VALID,
        probe_status=200,
        label="主号",
    )

    assert store.cookies("acc-1") == {"__Secure-hark.session_token": "secret-value"}

    # 凭据必须以密文落盘
    raw = (tmp_path / "accounts.db").read_bytes()
    assert b"secret-value" not in raw

    record = store.get("acc-1")
    assert record is not None
    assert record.label == "主号"
    assert record.state is AccountState.SESSION_VALID


def test_redacted_snapshot_has_no_credentials(store):
    store.save_session(
        "acc-1",
        cookies={"__Secure-hark.session_token": "secret-value"},
        session_id="s1",
        user_id="u1",
        session_expires_at=int(time.time()) + 86400,
        state=AccountState.SESSION_VALID,
        probe_status=200,
    )
    snapshot = store.redacted()
    assert len(snapshot) == 1
    assert "secret-value" not in str(snapshot)
    assert "cookies" not in snapshot[0]


def test_dispatchable_filters_dead_accounts(store):
    now = int(time.time())
    store.save_session(
        "good",
        cookies={"__Secure-hark.session_token": "a"},
        session_id="s1",
        user_id="u1",
        session_expires_at=now + 86400,
        state=AccountState.SESSION_VALID,
        probe_status=200,
    )
    store.save_session(
        "bad",
        cookies={"__Secure-hark.session_token": "b"},
        session_id="s2",
        user_id="u2",
        session_expires_at=now - 100,
        state=AccountState.SESSION_EXPIRED,
        probe_status=200,
    )
    names = [record.account for record in store.dispatchable()]
    assert names == ["good"]


@pytest.mark.asyncio
async def test_dispatch_full_chain_with_mock_upstream(store):
    """建会话 → 发消息 → 轮询取到回复。"""
    now = int(time.time())
    store.save_session(
        "acc-1",
        cookies={"__Secure-hark.session_token": "tok"},
        session_id="s1",
        user_id="u1",
        session_expires_at=now + 86400,
        state=AccountState.SESSION_VALID,
        probe_status=200,
    )

    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))

        if request.url.path == "/api/conversations" and request.method == "POST":
            # 校验嵌套入参形态
            body = request.read().decode()
            assert '"project"' in body
            return httpx.Response(200, json={"conversationId": "conv-1"})

        if request.url.path == "/api/messages/send":
            return httpx.Response(200, json={"messageId": "msg-1"})

        if request.url.path == "/api/messages/history":
            return httpx.Response(
                200,
                json={
                    "messages": {
                        "m2": {
                            "role": "assistant",
                            "content": "你好，我是回答。",
                            "triggeredByMessageId": "msg-1",
                            "timestamp": int(time.time() * 1000),
                            "isStreaming": False,
                        }
                    }
                },
            )

        return httpx.Response(404)

    transport = _mock_transport(handler)
    result = await pool.dispatch(store, "你好", transport=transport)

    assert result.ok
    assert result.conversation_id == "conv-1"
    assert result.message_id == "msg-1"

    text = await pool.collect_text(result, transport=transport, timeout=5)
    assert "回答" in text

    # 确认调用了三个必需端点
    paths = [path for _, path in calls]
    assert "/api/conversations" in paths
    assert "/api/messages/send" in paths
    assert "/api/messages/history" in paths


@pytest.mark.asyncio
async def test_dispatch_returns_error_when_no_accounts(store):
    with pytest.raises(pool.NoAccountAvailable):
        await pool.dispatch(store, "你好")


@pytest.mark.asyncio
async def test_upstream_403_marks_account_needs_verification(store, monkeypatch):
    """上游 403 必须落终态，不能留在可派单池里放大风控观测。"""
    from hark_bridge import upstream

    now = int(time.time())
    store.save_session(
        "acc-1",
        cookies={"__Secure-hark.session_token": "tok"},
        session_id="s1",
        user_id="u1",
        session_expires_at=now + 86400,
        state=AccountState.SESSION_VALID,
        probe_status=200,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": "forbidden"})

    result = await upstream.call(
        store, "acc-1", "list_conversations", transport=_mock_transport(handler)
    )
    assert not result.ok
    assert result.verdict == "denied"

    record = store.get("acc-1")
    assert record is not None
    assert record.state is AccountState.NEEDS_INTERACTIVE_VERIFICATION


@pytest.mark.asyncio
async def test_probe_rejects_preference_cookie_only(store):
    """只有偏好 Cookie 时必须判失败——这是最重要的回归测试。"""
    from hark_bridge import session

    def handler(request: httpx.Request) -> httpx.Response:
        # 上游会返回没有 session 的响应
        return httpx.Response(200, json={"session": None})

    probe = await session.probe_session(
        {"__Secure-hark.session_token": "tok"}, transport=_mock_transport(handler)
    )
    assert not probe.valid
    assert probe.definitive is True


@pytest.mark.asyncio
async def test_probe_keeps_credentials_when_response_is_html(store):
    """200 返回 HTML 是拦截页，不是会话失效——不能把账号清掉。"""
    from hark_bridge import session

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, text="<html>cloudflare</html>", headers={"content-type": "text/html"}
        )

    probe = await session.probe_session(
        {"__Secure-hark.session_token": "tok"},
        transport=_mock_transport(handler),
        retries_on_non_json=0,
    )
    assert not probe.valid
    # 关键：不是 definitive，凭据必须保留
    assert probe.definitive is False


@pytest.mark.asyncio
async def test_probe_accepts_valid_session(store):
    from hark_bridge import session

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "session": {
                    "id": "sess-1",
                    "expiresAt": "2030-01-01T00:00:00Z",
                },
                "user": {"id": "user-1"},
            },
        )

    probe = await session.probe_session(
        {"__Secure-hark.session_token": "tok"}, transport=_mock_transport(handler)
    )
    assert probe.valid
    assert probe.session_id == "sess-1"
    assert probe.user_id == "user-1"
    assert probe.expires_at is not None
