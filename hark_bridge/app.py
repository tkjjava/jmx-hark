"""FastAPI 应用：OpenAI 兼容接口 + 会话导入授权台。"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from . import __author__, __mark__, __version__, pool, session, upstream
from .state import AccountState, after_probe
from .store import AccountStore

REPLY_TIMEOUT = float(os.environ.get("HARK_REPLY_TIMEOUT", "120"))
REPLY_MODE = os.environ.get("HARK_REPLY_MODE", "history").lower()


def allowed_keys() -> set[str]:
    raw = os.environ.get("HARK_BRIDGE_API_KEYS", "")
    return {item.strip() for item in raw.split(",") if item.strip()}


async def require_key(authorization: str | None = Header(default=None)) -> None:
    """客户端密钥校验。未配置密钥时放行（仅限本地使用）。"""
    keys = allowed_keys()
    if not keys:
        return
    token = ""
    if authorization:
        token = authorization.removeprefix("Bearer ").strip()
    if token not in keys:
        raise HTTPException(status_code=401, detail="无效的 API 密钥")


app = FastAPI(title="hark-bridge", version=__version__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """应用生命周期：退出时释放连接池与数据库句柄。"""
    yield
    await upstream.aclose()
    if _store is not None:
        _store.close()


app.router.lifespan_context = lifespan

_store: AccountStore | None = None


def store() -> AccountStore:
    global _store
    if _store is None:
        _store = AccountStore()
    return _store


# ---- OpenAI 兼容 ------------------------------------------------------


class ChatMessage(BaseModel):
    role: str
    content: str | list[dict] | None = None


class ChatRequest(BaseModel):
    model: str = pool.DEFAULT_MODEL
    messages: list[ChatMessage]
    stream: bool = False
    # 以下字段接受但不使用：Hark 没有对应能力
    temperature: float | None = None
    max_tokens: int | None = None
    top_p: float | None = None


def _prompt_of(messages: list[ChatMessage]) -> str:
    """把 OpenAI 消息数组压成一段文本。

    Hark 是单轮对话接口，没有 system/assistant 角色概念，
    因此把历史拼成带标记的纯文本。
    """
    parts: list[str] = []
    for message in messages:
        content = message.content
        if isinstance(content, list):
            content = "".join(
                str(item.get("text", ""))
                for item in content
                if isinstance(item, dict)
            )
        if not content:
            continue
        role = message.role
        if role == "system":
            parts.append(f"[系统指令] {content}")
        elif role == "assistant":
            parts.append(f"[助手] {content}")
        else:
            parts.append(content)
    return "\n\n".join(parts).strip()


@app.get("/v1/models")
async def list_models(_: None = Depends(require_key)) -> dict:
    return {
        "object": "list",
        "data": [
            {
                "id": model,
                "object": "model",
                "created": 0,
                "owned_by": "hark-bridge",
            }
            for model in sorted(pool.MODEL_ALIASES)
        ],
    }


@app.post("/v1/chat/completions")
async def chat_completions(
    request: ChatRequest, _: None = Depends(require_key)
):
    """OpenAI 兼容补全。流式走 SSE，非流式一次性返回。

    故意不标注返回类型：JSONResponse 与 StreamingResponse 的联合类型会
    被 FastAPI 当成 Pydantic 响应模型解析并报错。
    """
    prompt = _prompt_of(request.messages)
    if not prompt:
        raise HTTPException(status_code=400, detail="消息内容为空")

    try:
        result = await pool.dispatch(store(), prompt)
    except pool.NoAccountAvailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    if not result.ok:
        raise HTTPException(
            status_code=502,
            detail={
                "error": result.verdict,
                "note": result.note,
                "type": "upstream_error",
            },
        )

    completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())
    model = request.model or pool.DEFAULT_MODEL

    if not request.stream:
        try:
            text = await pool.collect_text(
                result, reply_mode=REPLY_MODE, timeout=REPLY_TIMEOUT
            )
        except asyncio.CancelledError:
            # 客户端断开：不要留下半截状态，直接向上抛
            raise
        except Exception as exc:
            # 超时或上游异常都应给客户端一个明确的 504，
            # 而不是让它等满整个连接超时后收到一个空响应
            raise HTTPException(
                status_code=504,
                detail={
                    "error": "upstream_timeout",
                    "note": f"{type(exc).__name__}: {exc}",
                    "type": "timeout",
                },
            ) from exc

        if not text:
            # 投递成功但没取到正文：明确告知，别伪装成空回答
            raise HTTPException(
                status_code=504,
                detail={
                    "error": "empty_reply",
                    "note": "已投递但未取到正文，可试 HARK_REPLY_MODE=search",
                    "type": "empty_reply",
                },
            )

        return JSONResponse(
            {
                "id": completion_id,
                "object": "chat.completion",
                "created": created,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": text},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": len(prompt) // 4,
                    "completion_tokens": len(text) // 4,
                    "total_tokens": (len(prompt) + len(text)) // 4,
                },
            }
        )

    async def event_stream() -> AsyncIterator[str]:
        def chunk(payload: dict) -> str:
            return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

        yield chunk(
            {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [
                    {"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}
                ],
            }
        )
        async for delta in pool.stream_text(
            result, reply_mode=REPLY_MODE, timeout=REPLY_TIMEOUT
        ):
            if not delta:
                continue
            yield chunk(
                {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [
                        {"index": 0, "delta": {"content": delta}, "finish_reason": None}
                    ],
                }
            )
        yield chunk(
            {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
        )
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
    )


# ---- 会话导入 ---------------------------------------------------------


class ImportRequest(BaseModel):
    account: str
    label: str = ""
    cookie: str = ""


def parse_cookie_input(raw: str) -> dict[str, str]:
    """接受三种输入形态：Cookie 头、JSON 对象、每行 name=value。"""
    text = raw.strip()
    if not text:
        return {}

    if text.startswith("{"):
        try:
            payload = json.loads(text)
        except ValueError:
            return {}
        if isinstance(payload, dict):
            return {str(k): str(v) for k, v in payload.items()}
        return {}

    cookies: dict[str, str] = {}
    for part in text.replace("\n", ";").split(";"):
        item = part.strip()
        if not item or "=" not in item:
            continue
        name, _, value = item.partition("=")
        name = name.strip()
        if name:
            cookies[name] = value.strip()
    return cookies


@app.post("/admin/import")
async def import_session(request: ImportRequest) -> dict:
    """校验并加密入库。不通过只记原因，不登记。"""
    cookies = parse_cookie_input(request.cookie)
    if not cookies:
        raise HTTPException(status_code=400, detail="未解析到任何 Cookie")

    filtered = session.filter_session_cookies(cookies)
    if not filtered:
        # 偏好 Cookie 不是凭证，直接拒，别把 theme=dark 当登录
        raise HTTPException(
            status_code=400,
            detail="粘贴内容里没有会话 Cookie（偏好 Cookie 不算凭据）",
        )

    probe = await session.probe_session(filtered)
    if not probe.valid:
        return {
            "ok": False,
            "note": probe.note,
            "upstream_status": probe.upstream_status,
            "definitive": probe.definitive,
        }

    now = int(time.time())
    state = after_probe(valid=True, now=now, session_expires_at=probe.expires_at)
    store().save_session(
        request.account,
        cookies=dict(probe.cookies or filtered),
        session_id=probe.session_id,
        user_id=probe.user_id,
        session_expires_at=probe.expires_at,
        state=state,
        probe_status=probe.upstream_status,
        label=request.label,
    )
    return {
        "ok": True,
        "account": request.account,
        "state": state.value,
        "session_expires_at": probe.expires_at,
        "rotated": probe.rotated,
    }


@app.get("/admin/accounts")
async def list_accounts(_: None = Depends(require_key)) -> dict:
    """脱敏状态快照，不含任何凭据。"""
    return {"accounts": store().redacted()}


@app.delete("/admin/accounts/{account}")
async def delete_account(account: str) -> dict:
    return {"deleted": store().delete(account)}


@app.post("/admin/accounts/{account}/probe")
async def reprobe(account: str) -> dict:
    """重新复核一个账号。"""
    cookies = store().cookies(account)
    if not cookies:
        raise HTTPException(status_code=404, detail="账号不存在或无凭据")
    probe = await session.probe_session(cookies)
    if probe.valid:
        now = int(time.time())
        state = after_probe(valid=True, now=now, session_expires_at=probe.expires_at)
        store().save_session(
            account,
            cookies=dict(probe.cookies or cookies),
            session_id=probe.session_id,
            user_id=probe.user_id,
            session_expires_at=probe.expires_at,
            state=state,
            probe_status=probe.upstream_status,
        )
        return {"ok": True, "state": state.value}
    store().mark_failure(
        account,
        state=AccountState.SESSION_EXPIRED if probe.definitive else AccountState.UNVERIFIED,
        error=probe.note,
        probe_status=probe.upstream_status,
    )
    return {"ok": False, "note": probe.note}


@app.get("/healthz")
async def healthz() -> dict:
    accounts = store().list_all()
    dispatchable = store().dispatchable()
    benched = pool.benched_accounts()
    return {
        "status": "ok",
        "version": __version__,
        "author": __author__,
        "mark": __mark__,
        "accounts_total": len(accounts),
        "accounts_dispatchable": len(dispatchable),
        "accounts_benched": len(benched),
    }


# ---- 授权台页面 -------------------------------------------------------

CONSOLE_HTML = """<!doctype html>
<html lang="zh-CN">
<meta charset="utf-8">
<title>hark-bridge 授权台</title>
<style>
 body{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;max-width:760px;
      margin:40px auto;padding:0 20px;line-height:1.6;color:#1a1a1a}
 h1{font-size:20px;font-weight:600}
 label{display:block;margin:14px 0 6px;font-size:13px;color:#555}
 input,textarea{width:100%;padding:8px 10px;border:1px solid #d0d0d0;
      border-radius:6px;font-size:14px;font-family:inherit;box-sizing:border-box}
 textarea{min-height:120px;font-family:ui-monospace,Consolas,monospace;font-size:12px}
 button{margin-top:16px;padding:9px 18px;background:#1a1a1a;color:#fff;
      border:0;border-radius:6px;font-size:14px;cursor:pointer}
 button:disabled{opacity:.5;cursor:default}
 pre{background:#f5f5f5;padding:12px;border-radius:6px;font-size:12px;
      overflow:auto;white-space:pre-wrap}
 .hint{font-size:12px;color:#777;margin-top:6px}
 .sign{margin-top:32px;padding-top:14px;border-top:1px solid #eee;
      font-size:12px;color:#999;text-align:center}
 .sign b{color:#666;font-weight:600}
</style>
<h1>hark-bridge 授权台</h1>
<p class="hint">在浏览器登录 Hark 后，从开发者工具复制 Cookie 粘贴到下方。
偏好 Cookie 不算凭据，必须包含 <code>__Secure-hark.session_token</code>。</p>

<label>账号标签</label>
<input id="account" placeholder="例如 me-main">
<label>显示名（可选）</label>
<input id="label" placeholder="例如 主号">
<label>Cookie</label>
<textarea id="cookie" placeholder="__Secure-hark.session_token=..."></textarea>
<button id="go">校验并入库</button>
<pre id="out">等待操作</pre>

<div class="sign">hark-bridge · <b>君莫笑</b><br><b>永恒之兰</b></div>

<script>
document.getElementById('go').onclick = async () => {
  const out = document.getElementById('out');
  out.textContent = '校验中…';
  const body = {
    account: document.getElementById('account').value.trim(),
    label: document.getElementById('label').value.trim(),
    cookie: document.getElementById('cookie').value
  };
  if (!body.account || !body.cookie) {
    out.textContent = '账号标签和 Cookie 都要填';
    return;
  }
  try {
    const response = await fetch('/admin/import', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body)
    });
    out.textContent = JSON.stringify(await response.json(), null, 2);
  } catch (error) {
    out.textContent = '请求失败: ' + error;
  }
};
</script>
</html>
"""


@app.get("/console", response_class=HTMLResponse)
async def console() -> str:
    return CONSOLE_HTML
