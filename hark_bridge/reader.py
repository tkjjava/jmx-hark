"""回复读取器。

Hark 的 POST /api/messages/send 只返回 messageId，正文要另找。
三种读取方式：

  history —— 轮询 /api/messages/history，按 triggeredByMessageId 精确对上（默认）
  search  —— 用 /api/search/messages 捞（部分账号未建索引，读不到）
  none    —— 只投递不等回复（异步模式，返回 messageId）

轮询节奏：前 10 秒 0.6s 一次，之后放慢到 1.5s。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Mapping

import httpx

from .endpoints import endpoint
from .session import DEFAULT_BASE
from .upstream import BROWSER_HEADERS

FIRST_PHASE_SECONDS = 10.0
FAST_INTERVAL = 0.6
SLOW_INTERVAL = 1.5
MAX_IDLE_ROUNDS = 30


async def fetch_history_once(
    cookies: Mapping[str, str],
    conversation_id: str,
    *,
    base_url: str = DEFAULT_BASE,
    version: str = "2",
    timeout: float = 20.0,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict | None:
    """单次拉取会话历史，返回 messages 字典或 None。

    version 可传空串表示不带 v 参数——不同部署形态不一致，
    调用方可以按需降级重试。
    """
    path = endpoint("history").path
    params: dict[str, str] = {"conversationId": conversation_id}
    if version:
        params["v"] = version

    async with httpx.AsyncClient(
        base_url=base_url,
        timeout=timeout,
        transport=transport,
        follow_redirects=False,
        cookies=dict(cookies),
    ) as client:
        try:
            response = await client.get(
                path,
                params=params,
                headers={"accept": "application/json", **BROWSER_HEADERS},
            )
        except httpx.HTTPError:
            return None
        if response.status_code != 200:
            return None
        try:
            payload = response.json()
        except ValueError:
            return None

    messages = payload.get("messages")
    if isinstance(messages, dict):
        return messages
    if isinstance(messages, list):
        # 有些形态直接给数组，转成以 id 为键的字典
        return {
            str(item.get("id", index)): item
            for index, item in enumerate(messages)
            if isinstance(item, dict)
        }
    return None


def pick_reply(
    messages: Mapping[str, object],
    sent_message_id: str | None,
    since_ts: float,
) -> tuple[str | None, bool]:
    """从 messages 里挑本轮回复。返回 (正文, 是否已停止流式)。

    首选按 triggeredByMessageId 精确匹配；匹配不到再用时间戳兜底取最新一条。
    """
    matched: dict | None = None
    fallback: dict | None = None

    for message in messages.values():
        if not isinstance(message, dict):
            continue
        if message.get("role") != "assistant":
            continue
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            continue

        triggered = message.get("triggeredByMessageId")
        if sent_message_id and triggered == sent_message_id:
            matched = message
            break

        timestamp = message.get("timestamp")
        if (
            isinstance(timestamp, (int, float))
            and timestamp / 1000.0 >= since_ts - 1
            and (fallback is None or timestamp > (fallback.get("timestamp") or 0))
        ):
            fallback = message

    target = matched or fallback
    if target is None:
        return None, False
    text = str(target.get("content") or "").strip()
    return (text or None), target.get("isStreaming") is not True


async def poll_reply(
    cookies: Mapping[str, str],
    conversation_id: str,
    sent_message_id: str | None,
    since_ts: float,
    *,
    timeout: float = 120.0,
    base_url: str = DEFAULT_BASE,
    transport: httpx.AsyncBaseTransport | None = None,
) -> AsyncIterator[dict[str, str]]:
    """轮询回复正文，产出 {"text": 累计, "delta": 新增}。

    先查后睡，首字延迟更低。每一轮都把当前累积文本抛出来，
    调用方按 delta 推送即可做流式输出。
    """
    deadline = time.monotonic() + timeout
    last = ""
    sleep_for = FAST_INTERVAL
    idle_rounds = 0
    version = "2"
    version_fallbacks = 2

    while time.monotonic() < deadline:
        messages = await fetch_history_once(
            cookies,
            conversation_id,
            base_url=base_url,
            version=version,
            transport=transport,
        )
        if messages is None:
            # v=2 不通时降级为不带版本号，只试有限次，别把上游打爆
            if version_fallbacks > 0:
                version = "" if version else "2"
                version_fallbacks -= 1
                continue
            idle_rounds += 1
            if idle_rounds > MAX_IDLE_ROUNDS:
                return
            await asyncio.sleep(sleep_for)
            continue

        text, finished = pick_reply(messages, sent_message_id, since_ts)
        if text and text != last:
            delta = text[len(last) :] if text.startswith(last) else text
            last = text
            yield {"text": text, "delta": delta}
            if finished:
                return
        elif text and finished:
            return

        await asyncio.sleep(sleep_for)
        sleep_for = (
            FAST_INTERVAL
            if (time.monotonic() - since_ts) < FIRST_PHASE_SECONDS
            else SLOW_INTERVAL
        )


async def read_reply_search(
    cookies: Mapping[str, str],
    conversation_id: str,
    *,
    base_url: str = DEFAULT_BASE,
    transport: httpx.AsyncBaseTransport | None = None,
) -> str | None:
    """兜底：搜索索引里捞。索引没建时返回 None。"""
    path = endpoint("search_messages").path
    async with httpx.AsyncClient(
        base_url=base_url,
        timeout=25.0,
        transport=transport,
        follow_redirects=False,
        cookies=dict(cookies),
    ) as client:
        try:
            response = await client.get(
                path,
                params={"conversationId": conversation_id, "limit": 10},
                headers={"accept": "application/json", **BROWSER_HEADERS},
            )
        except httpx.HTTPError:
            return None
    if response.status_code != 200:
        return None
    try:
        payload = response.json()
    except ValueError:
        return None

    parts: list[str] = []
    for hit in payload.get("hits") or []:
        if not isinstance(hit, dict):
            continue
        text = hit.get("text") or hit.get("content")
        if isinstance(text, str) and text.strip():
            parts.append(text.strip())
    return "\n".join(parts) or None
