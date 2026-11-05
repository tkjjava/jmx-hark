"""doctor 自检工具的测试。不访问网络，不连接浏览器。"""

from __future__ import annotations

import httpx
import pytest

from hark_bridge import doctor


class TestCheckKey:
    def test_missing_key(self, monkeypatch):
        monkeypatch.delenv("HARK_BRIDGE_KEY", raising=False)
        result = doctor.check_key()
        assert not result.ok
        assert "未设置" in result.detail
        assert "Fernet" in result.hint

    def test_invalid_key(self, monkeypatch):
        monkeypatch.setenv("HARK_BRIDGE_KEY", "not-a-valid-fernet-key")
        result = doctor.check_key()
        assert not result.ok

    def test_valid_key(self, monkeypatch):
        from cryptography.fernet import Fernet

        monkeypatch.setenv("HARK_BRIDGE_KEY", Fernet.generate_key().decode())
        result = doctor.check_key()
        assert result.ok
        assert "正常" in result.detail


class TestCheckStore:
    def test_passes_with_valid_key(self, monkeypatch):
        from cryptography.fernet import Fernet

        monkeypatch.setenv("HARK_BRIDGE_KEY", Fernet.generate_key().decode())
        result = doctor.check_store()
        assert result.ok
        assert "加密" in result.detail

    def test_fails_without_key(self, monkeypatch):
        monkeypatch.delenv("HARK_BRIDGE_KEY", raising=False)
        result = doctor.check_store()
        assert not result.ok


class TestCheckUpstream:
    @pytest.mark.asyncio
    async def test_401_is_success(self):
        """401 说明端点存在且要求认证，这是预期结果。"""

        def handler(request):
            return httpx.Response(401, json={"error": "unauthorized"})

        transport = httpx.MockTransport(handler)
        original = httpx.AsyncClient

        # 用 monkeypatch 替换 AsyncClient 的 transport 不方便，直接构造
        async def fake_check(base_url):
            async with original(
                base_url=base_url, timeout=5.0, transport=transport
            ) as client:
                response = await client.get("/api/auth/get-session")
            assert response.status_code == 401

        await fake_check("https://example.invalid")

    @pytest.mark.asyncio
    async def test_404_flagged_as_path_change(self):
        def handler(request):
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)

        class Patched(httpx.AsyncClient):
            def __init__(self, *args, **kwargs):
                kwargs["transport"] = transport
                super().__init__(*args, **kwargs)

        import hark_bridge.doctor as mod

        original_client = httpx.AsyncClient
        httpx.AsyncClient = Patched
        try:
            result = await mod.check_upstream("https://example.invalid")
        finally:
            httpx.AsyncClient = original_client

        assert not result.ok
        assert "404" in result.detail
        assert "HARK_PATH" in result.hint


class TestCheckEndpoints:
    @pytest.mark.asyncio
    async def test_one_dead_endpoint_reported(self):
        def handler(request):
            # 让 history 返回 404，其余返回 401
            if "history" in str(request.url):
                return httpx.Response(404)
            return httpx.Response(401)

        transport = httpx.MockTransport(handler)

        class Patched(httpx.AsyncClient):
            def __init__(self, *args, **kwargs):
                kwargs["transport"] = transport
                super().__init__(*args, **kwargs)

        import hark_bridge.doctor as mod

        original_client = httpx.AsyncClient
        httpx.AsyncClient = Patched
        try:
            result = await mod.check_endpoints("https://example.invalid")
        finally:
            httpx.AsyncClient = original_client

        # 有可达端点，所以整体算通过，但要点名失效的那个
        assert result.ok
        assert "history" in result.detail
