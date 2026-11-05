"""自检工具：不导入任何会话，只验证"桥本身是否健康"。

用法：

    python -m hark_bridge.doctor

它回答四个问题：
  1. 加密密钥能用吗
  2. 数据库能读写吗
  3. 上游可达吗（只发无认证请求）
  4. 端点路径对得上吗

**不发任何带凭据的请求，不写入任何账号。**
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import time

import httpx

from . import __author__, __mark__, __version__
from .endpoints import DEFAULT_ENDPOINTS
from .session import DEFAULT_BASE


class CheckResult:
    def __init__(self, name: str) -> None:
        self.name = name
        self.ok = False
        self.detail = ""
        self.hint = ""

    def __str__(self) -> str:
        mark = "✓" if self.ok else "✗"
        line = f"  {mark} {self.name}"
        if self.detail:
            line += f" — {self.detail}"
        if not self.ok and self.hint:
            line += f"\n      提示：{self.hint}"
        return line


def check_key() -> CheckResult:
    result = CheckResult("加密密钥")
    raw = os.environ.get("HARK_BRIDGE_KEY", "").strip()
    if not raw:
        result.detail = "未设置 HARK_BRIDGE_KEY"
        result.hint = (
            'python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"'
        )
        return result

    try:
        from cryptography.fernet import Fernet, InvalidToken

        cipher = Fernet(raw.encode())
        probe = cipher.encrypt(b"probe")
        if cipher.decrypt(probe) != b"probe":
            result.detail = "加解密往返失败"
            return result
    except InvalidToken:
        result.detail = "密钥不可用"
        result.hint = "重新生成密钥并完整复制"
        return result
    except Exception as exc:
        result.detail = f"{type(exc).__name__}: {exc}"
        result.hint = "检查密钥是否为 Fernet 生成的 base64 字符串"
        return result

    result.ok = True
    result.detail = "加解密往返正常"
    return result


def check_store() -> CheckResult:
    result = CheckResult("数据库读写")
    try:
        from .state import AccountState
        from .store import AccountStore

        # 用临时库验证，绝不碰正式库
        with tempfile.TemporaryDirectory() as tmp:
            store = AccountStore(os.path.join(tmp, "probe.db"))
            store.save_session(
                "__probe__",
                cookies={"__Secure-hark.session_token": "probe-value"},
                session_id="probe",
                user_id="probe",
                session_expires_at=int(time.time()) + 3600,
                state=AccountState.SESSION_VALID,
                probe_status=200,
            )
            read_back = store.cookies("__probe__")
            with open(os.path.join(tmp, "probe.db"), "rb") as handle:
                raw = handle.read()
            store.close()

        if read_back.get("__Secure-hark.session_token") != "probe-value":
            result.detail = "写入后读回不一致"
            return result
        if b"probe-value" in raw:
            result.detail = "凭据以明文落盘"
            result.hint = "这是严重问题，请提 issue"
            return result
    except Exception as exc:
        result.detail = f"{type(exc).__name__}: {exc}"
        result.hint = "检查 data 目录写权限"
        return result

    result.ok = True
    result.detail = "写入、加密、读回均正常"
    return result


async def check_upstream(base_url: str) -> CheckResult:
    result = CheckResult("上游可达性")
    try:
        async with httpx.AsyncClient(
            base_url=base_url,
            timeout=15.0,
            follow_redirects=False,
        ) as client:
            # 只发无 Cookie 的 GET，不会泄露任何凭据
            response = await client.get(
                "/api/auth/get-session", headers={"accept": "application/json"}
            )
    except httpx.HTTPError as exc:
        result.detail = f"连接失败: {type(exc).__name__}"
        result.hint = "检查网络、代理、DNS"
        return result

    if response.status_code == 401:
        result.ok = True
        result.detail = "401 —— 端点存在且要求认证（符合预期）"
        return result
    if response.status_code == 200:
        result.ok = True
        result.detail = "200 —— 端点可达"
        return result
    if response.status_code == 404:
        result.detail = "404 —— 端点路径可能已变更"
        result.hint = "用 HARK_PATH_SESSION_PROBE 覆盖路径"
        return result
    if response.status_code in (403, 503):
        result.detail = f"{response.status_code} —— 可能被拦截"
        result.hint = "换网络环境重试；本项目不做指纹伪装"
        return result

    result.detail = f"HTTP {response.status_code}"
    return result


async def check_endpoints(base_url: str) -> CheckResult:
    """确认默认端点表里的路径在当前上游都还有响应（401 也算有响应）。"""
    result = CheckResult("端点路径")
    alive = 0
    dead: list[str] = []

    async with httpx.AsyncClient(
        base_url=base_url, timeout=15.0, follow_redirects=False
    ) as client:
        for name, ep in DEFAULT_ENDPOINTS.items():
            if ep.method != "GET":
                continue
            try:
                response = await client.get(
                    ep.path, headers={"accept": "application/json"}
                )
            except httpx.HTTPError:
                dead.append(f"{name}({ep.path}) 连接失败")
                continue
            # 401/403 说明路径存在，只是要认证
            if response.status_code in (200, 401, 403):
                alive += 1
            elif response.status_code == 404:
                dead.append(f"{name}({ep.path}) 404")
            else:
                alive += 1

    total = alive + len(dead)
    if dead:
        result.detail = f"{alive}/{total} 可达；失效：{', '.join(dead)}"
        result.hint = "用 HARK_PATH_* 环境变量覆盖失效路径"
        result.ok = alive > 0
        return result

    result.ok = True
    result.detail = f"{alive}/{total} 可达"
    return result


async def _run(base_url: str) -> int:
    print(f"hark-bridge {__version__}  ·  {__author__}  ·  {__mark__}")
    print(f"上游：{base_url}\n")

    results = [check_key(), check_store()]
    results.append(await check_upstream(base_url))

    # 上游不可达时跳过端点检查，避免重复报错
    if results[-1].ok:
        results.append(await check_endpoints(base_url))

    for item in results:
        print(item)

    failed = [item for item in results if not item.ok]
    print()
    if failed:
        print(f"{len(failed)} 项未通过")
        return 1
    print("全部通过")
    return 0


def main() -> int:
    base_url = os.environ.get("HARK_BASE", DEFAULT_BASE)
    return asyncio.run(_run(base_url))


if __name__ == "__main__":
    raise SystemExit(main())
