"""启动冒烟测试：拉起真实服务，打一次 /healthz，然后关掉。

CI 用这个代替 shell 里的后台进程控制——跨平台，且失败时能看到
实际响应内容而不是只有一个退出码。

用法：

    python -m hark_bridge.smoke
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

PORT = int(os.environ.get("HARK_BRIDGE_PORT", "18787"))
BASE = f"http://127.0.0.1:{PORT}"
TIMEOUT_SECONDS = 40.0
INTERVAL = 0.5


def _get(path: str) -> dict:
    with urllib.request.urlopen(f"{BASE}{path}", timeout=5) as response:
        return json.loads(response.read().decode())


def wait_until_ready() -> bool:
    """轮询 /healthz 直到服务就绪或超时。"""
    deadline = time.monotonic() + TIMEOUT_SECONDS
    last_error = ""
    while time.monotonic() < deadline:
        try:
            payload = _get("/healthz")
            if payload.get("status") == "ok":
                return True
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            last_error = str(exc)
        time.sleep(INTERVAL)
    print(f"服务在 {TIMEOUT_SECONDS}s 内未就绪: {last_error}", file=sys.stderr)
    return False


def main() -> int:
    # 延迟导入，让 --help 之类的调用不必加载整个应用
    import uvicorn

    from .app import app

    config = uvicorn.Config(
        app, host="127.0.0.1", port=PORT, log_level="warning", lifespan="on"
    )
    server = uvicorn.Server(config)

    # 在后台线程里跑服务，主线程负责探测
    import threading

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    try:
        if not wait_until_ready():
            return 1

        health = _get("/healthz")
        print("healthz:", json.dumps(health, ensure_ascii=False))

        models = _get("/v1/models")
        count = len(models.get("data") or [])
        print(f"models: {count}")
        if count == 0:
            print("模型列表为空", file=sys.stderr)
            return 1

        print("冒烟测试通过")
        return 0
    finally:
        server.should_exit = True
        thread.join(timeout=10)


if __name__ == "__main__":
    raise SystemExit(main())
