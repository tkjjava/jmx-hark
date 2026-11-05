"""池化调度与 OpenAI 形状转换。

一次补全的完整链路：
  挑号 → 确保有会话 → 建会话 → 发消息 → 轮询取正文 → 组装 OpenAI 响应

失败时换下一个账号重试；巳开始吐字后不再换号（避免客户端收到两份回答）。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx

from . import reader
from .session import DEFAULT_BASE
from .store import AccountStore
from .upstream import call

# 模型别名表。Hark 服务端自己选真实模型（Opus/Sonnet/Haiku），
# 客户端传任何常见模型名都映射到同一个上游，方便现有客户端直接接入。
MODEL_ALIASES = frozenset(
    {
        "hark-1",
        "hark",
        "gpt-4o",
        "gpt-4o-mini",
        "gpt-4",
        "gpt-3.5-turbo",
        "claude-3-5-sonnet",
        "claude-sonnet-4",
        "claude-opus-4",
        "claude-3-opus",
        "claude-3-haiku",
    }
)
DEFAULT_MODEL = "hark-1"


@dataclass
class DispatchResult:
    ok: bool
    account: str = ""
    conversation_id: str = ""
    message_id: str | None = None
    cookies: dict[str, str] | None = None
    verdict: str = ""
    note: str = ""


class NoAccountAvailable(RuntimeError):
    pass


def _round_robin(accounts: list, cursor: dict[str, int], key: str) -> list:
    """从游标位置开始轮转，保证请求分散到不同账号。"""
    if not accounts:
        return []
    start = cursor.get(key, 0) % len(accounts)
    cursor[key] = start + 1
    return accounts[start:] + accounts[:start]


_cursor: dict[str, int] = {}


async def dispatch(
    store: AccountStore,
    prompt: str,
    *,
    base_url: str = DEFAULT_BASE,
    transport: httpx.AsyncBaseTransport | None = None,
    max_attempts: int = 3,
) -> DispatchResult:
    """挑一个可用账号发消息。失败自动换号，最多试 max_attempts 次。"""
    candidates = store.dispatchable()
    if not candidates:
        raise NoAccountAvailable("账号池里没有可派单的账号（需先导入会话）")

    ordered = _round_robin(candidates, _cursor, "dispatch")
    last_error = ""

    for record in ordered[:max(1, max_attempts)]:
        account = record.account

        created = await call(
            store,
            account,
            "create_conversation",
            body={"autoTitle": prompt[:60]},
            base_url=base_url,
            transport=transport,
        )
        if not created.ok:
            last_error = f"{account}: 建会话失败 {created.verdict} {created.note}"
            continue

        conversation_id = _extract_conversation_id(created.data)
        if not conversation_id:
            last_error = f"{account}: 建会话响应无 conversationId"
            continue

        sent = await call(
            store,
            account,
            "send_message",
            body={"conversationId": conversation_id, "message": prompt},
            params={"cid": conversation_id},
            base_url=base_url,
            transport=transport,
        )
        if not sent.ok:
            last_error = f"{account}: 发消息失败 {sent.verdict} {sent.note}"
            continue

        return DispatchResult(
            ok=True,
            account=account,
            conversation_id=conversation_id,
            message_id=_extract_message_id(sent.data),
            cookies=store.cookies(account),
            verdict="ok",
        )

    return DispatchResult(ok=False, verdict="all_accounts_failed", note=last_error)


def _extract_conversation_id(data: object) -> str:
    if not isinstance(data, dict):
        return ""
    for key in ("conversationId", "id", "conversation_id"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    inner = data.get("conversation") or data.get("project")
    if isinstance(inner, dict):
        for key in ("id", "conversationId"):
            value = inner.get(key)
            if isinstance(value, str) and value:
                return value
    return ""


def _extract_message_id(data: object) -> str | None:
    if not isinstance(data, dict):
        return None
    for key in ("messageId", "id", "message_id"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    inner = data.get("message")
    if isinstance(inner, dict):
        value = inner.get("id")
        if isinstance(value, str):
            return value
    return None


async def collect_text(
    result: DispatchResult,
    *,
    reply_mode: str = "history",
    timeout: float = 120.0,
    base_url: str = DEFAULT_BASE,
    transport: httpx.AsyncBaseTransport | None = None,
) -> str:
    """一次性收全文（非流式）。"""
    if not result.ok or not result.cookies:
        return ""
    text = ""
    if reply_mode in ("history", "auto"):
        async for event in reader.poll_reply(
            result.cookies,
            result.conversation_id,
            result.message_id,
            time.time(),
            timeout=timeout,
            base_url=base_url,
            transport=transport,
        ):
            text = event["text"]
    if not text and reply_mode in ("search", "auto", "history"):
        text = (await reader.read_reply_search(
            result.cookies,
            result.conversation_id,
            base_url=base_url,
            transport=transport,
        )) or ""
    return text


async def stream_text(
    result: DispatchResult,
    *,
    reply_mode: str = "history",
    timeout: float = 120.0,
    base_url: str = DEFAULT_BASE,
    transport: httpx.AsyncBaseTransport | None = None,
) -> AsyncIterator[str]:
    """流式产出增量文本。

    上游本身是轮询累积，这里把新增部分按小块吐出形成打字机效果。
    """
    if not result.ok or not result.cookies:
        return

    emitted = 0
    if reply_mode in ("history", "auto"):
        async for event in reader.poll_reply(
            result.cookies,
            result.conversation_id,
            result.message_id,
            time.time(),
            timeout=timeout,
            base_url=base_url,
            transport=transport,
        ):
            text = event["text"]
            while emitted < len(text):
                chunk = text[emitted: emitted + 24]
                emitted += len(chunk)
                yield chunk
                await asyncio.sleep(0.02)

    if emitted == 0 and reply_mode in ("search", "auto", "history"):
        fallback = await reader.read_reply_search(
            result.cookies,
            result.conversation_id,
            base_url=base_url,
            transport=transport,
        )
        if fallback:
            for index in range(0, len(fallback), 24):
                yield fallback[index: index + 24]
                await asyncio.sleep(0.02)
