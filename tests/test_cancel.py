"""取消、超时、空回复场景测试。"""

from __future__ import annotations

import time

import httpx
import pytest

from hark_bridge import pool, upstream
from hark_bridge.state import AccountState
from hark_bridge.store import AccountStore


@pytest.fixture
def bridge_key(monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("HARK_BRIDGE_KEY", Fernet.generate_key().decode())
    return None


@pytest.fixture
def store(bridge_key, tmp_path, monkeypatch):
    monkeypatch.setenv("HARK_BRIDGE_DB", str(tmp_path / "acc.db"))
    instance = AccountStore()
    yield instance
    instance.close()


@pytest.fixture(autouse=True)
def clear_state():
    pool._cooldown.clear()
    pool._cursor.clear()
    yield
    pool._cooldown.clear()
    pool._cursor.clear()


def _add(store, name):
    store.save_session(
        name,
        cookies={"__Secure-hark.session_token": f"tok-{name}"},
        session_id=f"s-{name}",
        user_id=f"u-{name}",
        session_expires_at=int(time.time()) + 86400,
        state=AccountState.SESSION_VALID,
        probe_status=200,
    )


class TestStreamTimeout:
    @pytest.mark.asyncio
    async def test_stream_stops_at_timeout(self, store, monkeypatch):
        """上游一直返回不完整文本时，必须在超时后停止轮询。"""
        _add(store, "a")

        call_count = {"n": 0}

        def handler(request):
            call_count["n"] += 1
            if request.url.path == "/api/conversations":
                return httpx.Response(200, json={"conversationId": "c1"})
            if request.url.path == "/api/messages/send":
                return httpx.Response(200, json={"messageId": "m1"})
            # history 一直返回 isStreaming=true，永远不"完成"
            return httpx.Response(
                200,
                json={
                    "messages": {
                        "m2": {
                            "role": "assistant",
                            "content": "永远写不完",
                            "triggeredByMessageId": "m1",
                            "timestamp": int(time.time() * 1000),
                            "isStreaming": True,
                        }
                    }
                },
            )

        transport = httpx.MockTransport(handler)
        result = await pool.dispatch(store, "你好", transport=transport)
        assert result.ok

        started = time.monotonic()
        chunks = []
        async for delta in pool.stream_text(result, timeout=0.15, transport=transport):
            chunks.append(delta)

        elapsed = time.monotonic() - started
        assert elapsed < 3.0, f"应在超时后停止，实际耗时 {elapsed:.1f}s"
        # 首次拿到全文就够，不该无限重复
        assert chunks


class TestUpstreamTimeout:
    @pytest.mark.asyncio
    async def test_transport_error_is_value_not_exception(self, store):
        """传输异常必须返回值，不能让异常穿透到 API 层。"""
        _add(store, "a")

        def handler(request):
            raise httpx.ConnectTimeout("模拟超时")

        transport = httpx.MockTransport(handler)
        result = await upstream.call(
            store, "a", "list_conversations", transport=transport
        )

        assert not result.ok
        assert result.verdict == "error"
        assert "传输失败" in result.note


class TestCookieHeader:
    def test_cookie_header_built_correctly(self):
        header = upstream._cookie_header(
            {"__Secure-hark.session_token": "abc", "theme": "dark"}
        )
        assert "__Secure-hark.session_token=abc" in header
        assert "theme=dark" in header
        assert header.count(";") == 1

    def test_cookie_header_empty(self):
        assert upstream._cookie_header({}) == ""
