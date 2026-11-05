"""上游端点表。

Hark 的私有 Web API 会变更，所以路径不写死在代码调用处。
默认值是按已知证据填的候选，运行时可用环境变量覆盖：

    HARK_PATH_SEND_MESSAGE=/api/messages/send
    HARK_PATH_SEND_MESSAGE_METHOD=POST

占位符用 {name}，由调用方传 path_params。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class Endpoint:
    method: str
    path: str


# 2026-11 活网校准记录：
#   /api/projects、/api/usage、/api/threads 在源站返回 404（路径不存在）
#   /api/conversations 存在 —— Hark 侧"项目"就是 conversation
#   POST /api/messages/send 存在，入参 {conversationId, message}，只返回 messageId
#   GET  /api/billing/summary 存在，额度在 meters.harkTokens
DEFAULT_ENDPOINTS: dict[str, Endpoint] = {
    "session_probe": Endpoint("GET", "/api/auth/get-session"),
    "list_conversations": Endpoint("GET", "/api/conversations"),
    "create_conversation": Endpoint("POST", "/api/conversations"),
    "send_message": Endpoint("POST", "/api/messages/send"),
    "history": Endpoint("GET", "/api/messages/history"),
    "search_messages": Endpoint("GET", "/api/search/messages"),
    "billing_summary": Endpoint("GET", "/api/billing/summary"),
}


def endpoint(name: str) -> Endpoint:
    """取端点定义，允许环境变量覆盖。"""
    try:
        default = DEFAULT_ENDPOINTS[name]
    except KeyError as exc:
        raise KeyError(f"未知端点: {name}") from exc

    prefix = "HARK_PATH_" + name.upper()
    path = os.environ.get(prefix, default.path)
    method = os.environ.get(prefix + "_METHOD", default.method).upper()
    return Endpoint(method, path)


def render(path: str, params: Mapping[str, object]) -> str:
    """替换 {placeholder}。缺参数就报错，不要发一条带 {x} 的请求出去。"""
    rendered = path
    for key, value in params.items():
        rendered = rendered.replace("{" + key + "}", str(value))
    if "{" in rendered and "}" in rendered:
        missing = rendered[rendered.index("{") : rendered.index("}") + 1]
        raise ValueError(f"路径缺少参数 {missing}: {path}")
    return rendered
