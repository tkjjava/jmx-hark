"""核心逻辑测试。不需要真实上游，全部用假数据。"""

from __future__ import annotations

import time

import pytest

from hark_bridge import pool, reader, session
from hark_bridge.state import (
    AccountState,
    after_probe,
    after_session_expiry_check,
    is_dispatchable,
)

# ---- Cookie 识别 ------------------------------------------------------


def test_session_cookie_prefix_variants():
    assert session.is_session_cookie_name("__Secure-hark.session_token")
    assert session.is_session_cookie_name("hark.session_token")
    # multiSession 插件下发的变体
    assert session.is_session_cookie_name("__Secure-hark.session_token_multi-abc")
    # 分块 cookie
    assert session.is_session_cookie_name("__Secure-hark.session_token.0")


def test_preference_cookie_is_not_credential():
    """偏好 Cookie 不是凭据——这是必须防止的误判。"""
    assert not session.is_session_cookie_name("theme")
    assert not session.is_session_cookie_name("__Secure-hark.region")
    assert not session.is_session_cookie_name("__Secure-hark.access")


def test_filter_keeps_only_session_tokens():
    raw = {
        "__Secure-hark.session_token": "abc",
        "theme": "dark",
        "__Secure-hark.region": "us",
    }
    filtered = session.filter_session_cookies(raw)
    assert filtered == {"__Secure-hark.session_token": "abc"}
    assert session.has_session_cookie(filtered)
    assert not session.has_session_cookie({"theme": "dark"})


# ---- 状态机 -----------------------------------------------------------


def test_dispatchable_only_for_fresh_valid_session():
    now = int(time.time())
    assert is_dispatchable(
        AccountState.SESSION_VALID,
        now=now,
        last_probe_ok_at=now - 60,
        session_expires_at=now + 86400,
    )


def test_stale_probe_blocks_dispatch():
    now = int(time.time())
    assert not is_dispatchable(
        AccountState.SESSION_VALID,
        now=now,
        last_probe_ok_at=now - (16 * 60),  # 超过 15 分钟
        session_expires_at=now + 86400,
    )


def test_expired_session_blocks_dispatch():
    now = int(time.time())
    assert not is_dispatchable(
        AccountState.SESSION_VALID,
        now=now,
        last_probe_ok_at=now - 60,
        session_expires_at=now - 1,
    )


@pytest.mark.parametrize(
    "state",
    [
        AccountState.BLOCKED,
        AccountState.NEEDS_INTERACTIVE_VERIFICATION,
        AccountState.CREDENTIALS_REJECTED,
        AccountState.UNVERIFIED,
        AccountState.SESSION_EXPIRED,
    ],
)
def test_terminal_and_dead_states_never_dispatch(state):
    now = int(time.time())
    assert not is_dispatchable(
        state, now=now, last_probe_ok_at=now, session_expires_at=now + 86400
    )


def test_expiring_still_dispatchable():
    """快过期但仍有效——应该能派单，只是要提醒重新导入。"""
    now = int(time.time())
    assert is_dispatchable(
        AccountState.SESSION_EXPIRING,
        now=now,
        last_probe_ok_at=now - 60,
        session_expires_at=now + 3600,
    )


def test_after_probe_marks_expiring_within_warn_window():
    now = int(time.time())
    assert after_probe(valid=True, now=now, session_expires_at=now + 7200) is (
        AccountState.SESSION_EXPIRING
    )
    assert after_probe(valid=True, now=now, session_expires_at=now + (7 * 86400)) is (
        AccountState.SESSION_VALID
    )
    assert after_probe(valid=False, now=now, session_expires_at=None) is (
        AccountState.UNVERIFIED
    )


def test_expiry_check_downgrades():
    now = int(time.time())
    assert (
        after_session_expiry_check(
            AccountState.SESSION_VALID, now=now, session_expires_at=now - 1
        )
        is AccountState.SESSION_EXPIRED
    )
    # 非派单状态不应被改动
    assert (
        after_session_expiry_check(
            AccountState.BLOCKED, now=now, session_expires_at=now - 1
        )
        is AccountState.BLOCKED
    )


# ---- 回复挑选 ---------------------------------------------------------


def test_pick_reply_by_triggered_message_id():
    messages = {
        "m1": {"role": "user", "content": "问题", "timestamp": 1000},
        "m2": {
            "role": "assistant",
            "content": "回答",
            "triggeredByMessageId": "sent-1",
            "timestamp": 2000,
            "isStreaming": False,
        },
    }
    text, finished = reader.pick_reply(messages, "sent-1", 0.0)
    assert text == "回答"
    assert finished is True


def test_pick_reply_detects_streaming_incomplete():
    messages = {
        "m1": {
            "role": "assistant",
            "content": "正在生成",
            "triggeredByMessageId": "sent-1",
            "timestamp": 2000,
            "isStreaming": True,
        }
    }
    text, finished = reader.pick_reply(messages, "sent-1", 0.0)
    assert text == "正在生成"
    assert finished is False


def test_pick_reply_timestamp_fallback():
    """没有 triggeredByMessageId 时用时间戳兜底。"""
    messages = {
        "m1": {"role": "assistant", "content": "旧回答", "timestamp": 1000},
        "m2": {"role": "assistant", "content": "新回答", "timestamp": 9000},
    }
    text, _ = reader.pick_reply(messages, None, 8.5)
    assert text == "新回答"


def test_pick_reply_ignores_user_messages():
    messages = {"m1": {"role": "user", "content": "只有用户消息", "timestamp": 9000}}
    text, finished = reader.pick_reply(messages, None, 0.0)
    assert text is None
    assert finished is False


def test_pick_reply_ignores_empty_content():
    messages = {"m1": {"role": "assistant", "content": "   ", "timestamp": 9000}}
    text, _ = reader.pick_reply(messages, None, 0.0)
    assert text is None


# ---- 响应字段提取 -----------------------------------------------------


def test_extract_conversation_id_variants():
    assert pool._extract_conversation_id({"conversationId": "c1"}) == "c1"
    assert pool._extract_conversation_id({"id": "c2"}) == "c2"
    assert pool._extract_conversation_id({"conversation": {"id": "c3"}}) == "c3"
    assert pool._extract_conversation_id({"project": {"id": "c4"}}) == "c4"
    assert pool._extract_conversation_id({}) == ""
    assert pool._extract_conversation_id("not a dict") == ""


def test_extract_message_id_variants():
    assert pool._extract_message_id({"messageId": "m1"}) == "m1"
    assert pool._extract_message_id({"message": {"id": "m2"}}) == "m2"
    assert pool._extract_message_id({}) is None


# ---- 模型别名 ---------------------------------------------------------


def test_model_aliases_include_default():
    assert pool.DEFAULT_MODEL in pool.MODEL_ALIASES
    assert "gpt-4o" in pool.MODEL_ALIASES


# ---- Cookie 解析 ------------------------------------------------------


def test_parse_cookie_header_form():
    from hark_bridge.app import parse_cookie_input

    parsed = parse_cookie_input("__Secure-hark.session_token=abc; theme=dark")
    assert parsed["__Secure-hark.session_token"] == "abc"
    assert parsed["theme"] == "dark"


def test_parse_cookie_json_form():
    from hark_bridge.app import parse_cookie_input

    parsed = parse_cookie_input('{"__Secure-hark.session_token": "xyz"}')
    assert parsed == {"__Secure-hark.session_token": "xyz"}


def test_parse_cookie_line_form():
    from hark_bridge.app import parse_cookie_input

    parsed = parse_cookie_input("__Secure-hark.session_token=abc\ntheme=dark")
    assert parsed["__Secure-hark.session_token"] == "abc"
    assert parsed["theme"] == "dark"


def test_parse_cookie_empty():
    from hark_bridge.app import parse_cookie_input

    assert parse_cookie_input("") == {}
    assert parse_cookie_input("   ") == {}
