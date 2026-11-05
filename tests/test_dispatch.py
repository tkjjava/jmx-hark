"""调度层优化测试：账号冷却、连接池复用、429 处理。"""

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
def clear_cooldown():
    """每个测试都从干净的调度状态开始。

    轮转游标是模块级的，不重置会让测试之间互相影响。
    """
    pool._cooldown.clear()
    pool._cursor.clear()
    yield
    pool._cooldown.clear()
    pool._cursor.clear()


def _add(store: AccountStore, name: str) -> None:
    store.save_session(
        name,
        cookies={"__Secure-hark.session_token": f"tok-{name}"},
        session_id=f"s-{name}",
        user_id=f"u-{name}",
        session_expires_at=int(time.time()) + 86400,
        state=AccountState.SESSION_VALID,
        probe_status=200,
    )


class TestCooldown:
    def test_bench_and_check(self):
        assert not pool._is_benched("a")
        pool._bench("a")
        assert pool._is_benched("a")
        assert "a" in pool.benched_accounts()

    def test_expired_cooldown_clears(self, monkeypatch):
        monkeypatch.setattr(pool, "COOLDOWN_SECONDS", 0.05)
        pool._bench("a")
        assert pool._is_benched("a")
        time.sleep(0.1)
        assert not pool._is_benched("a")

    def test_benched_accounts_excludes_expired(self, monkeypatch):
        monkeypatch.setattr(pool, "COOLDOWN_SECONDS", 0.05)
        pool._bench("a")
        time.sleep(0.1)
        assert pool.benched_accounts() == []


class TestDispatchWithCooldown:
    @pytest.mark.asyncio
    async def test_failed_account_is_benched(self, store):
        """建会话失败的账号应进冷却，后续请求不再选它。"""
        _add(store, "bad")

        def handler(request):
            return httpx.Response(500)

        transport = httpx.MockTransport(handler)
        result = await pool.dispatch(store, "你好", transport=transport)

        assert not result.ok
        assert pool._is_benched("bad")

    @pytest.mark.asyncio
    async def test_benched_account_skipped_on_next_dispatch(self, store):
        """冷却中的账号不参与下一轮挑选。"""
        _add(store, "a")
        pool._bench("a")

        with pytest.raises(pool.NoAccountAvailable) as exc:
            await pool.dispatch(store, "你好")
        assert "冷却" in str(exc.value)

    @pytest.mark.asyncio
    async def test_success_clears_cooldown(self, store):
        _add(store, "good")
        pool._bench("good")

        def handler(request):
            if request.url.path == "/api/conversations":
                return httpx.Response(200, json={"conversationId": "c1"})
            return httpx.Response(200, json={"messageId": "m1"})

        # 预热：手动清掉冷却，模拟冷却已到期后重新被选中
        pool._cooldown.clear()

        transport = httpx.MockTransport(handler)
        result = await pool.dispatch(store, "你好", transport=transport)
        assert result.ok
        # 成功后仍不该在冷却表里
        assert not pool._is_benched("good")

    @pytest.mark.asyncio
    async def test_falls_through_to_next_account(self, store):
        """第一个号失败时换第二个。"""
        _add(store, "a")
        _add(store, "b")

        failed: list[str] = []

        def handler(request):
            cookie = request.headers.get("cookie", "")
            if request.url.path == "/api/conversations":
                # 只有 a 号会失败
                if "__Secure-hark.session_token=tok-a" in cookie:
                    failed.append("a")
                    return httpx.Response(500)
                return httpx.Response(200, json={"conversationId": "c1"})
            return httpx.Response(200, json={"messageId": "m1"})

        transport = httpx.MockTransport(handler)
        result = await pool.dispatch(store, "你好", transport=transport)

        assert result.ok
        assert result.account == "b"
        assert failed == ["a"], "a 号应该被尝试过并失败"
        assert pool._is_benched("a")
        assert not pool._is_benched("b")


class TestRateLimit:
    @pytest.mark.asyncio
    async def test_429_marks_rate_limited_not_blocked(self, store):
        """429 是暂时的，不能当成终态隔离账号。"""
        _add(store, "a")

        def handler(request):
            return httpx.Response(429)

        transport = httpx.MockTransport(handler)
        result = await upstream.call(
            store, "a", "list_conversations", transport=transport
        )

        assert not result.ok
        assert result.verdict == "throttled"
        record = store.get("a")
        assert record.state is AccountState.RATE_LIMITED

    @pytest.mark.asyncio
    async def test_403_marks_terminal(self, store):
        """403 是终态，两者不能混。"""
        _add(store, "a")

        def handler(request):
            return httpx.Response(403)

        transport = httpx.MockTransport(handler)
        await upstream.call(store, "a", "list_conversations", transport=transport)

        record = store.get("a")
        assert record.state is AccountState.NEEDS_INTERACTIVE_VERIFICATION


class TestConnectionPool:
    @pytest.mark.asyncio
    async def test_custom_transport_not_shared(self):
        """注入 transport 时必须独立客户端，否则测试会串数据。"""
        assert not upstream._is_shared("https://x.invalid", object())  # type: ignore[arg-type]
        assert upstream._is_shared("https://x.invalid", None)

    @pytest.mark.asyncio
    async def test_shared_client_reused(self):
        """同一 base_url 应复用同一个客户端实例。"""
        await upstream.aclose()
        first = upstream._client_for(
            "https://x.invalid", httpx.Timeout(5.0), None
        )
        second = upstream._client_for(
            "https://x.invalid", httpx.Timeout(5.0), None
        )
        assert first is second
        await upstream.aclose()

    @pytest.mark.asyncio
    async def test_aclose_resets(self):
        upstream._client_for("https://x.invalid", httpx.Timeout(5.0), None)
        await upstream.aclose()
        assert upstream._shared_client is None
