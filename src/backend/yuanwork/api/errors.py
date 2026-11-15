"""One shape for every error the API answers with.

An error response carries two things: a **code** the client can branch
on, and a **message** the user can read. It never carries the exception
text - that can hold a local path, an upstream endpoint, or a piece of
the user's own data, and it goes to the local log instead.

The codes are also what keeps "the model is down" apart from "the
documents do not answer this": a refusal says the evidence is not
enough, a failure says the model could not be reached. Mixing the two
would tell the user their question has no answer when the truth is that
the machine could not think.
"""

from __future__ import annotations

from typing import Any

#: The error codes the API answers with, and what the user is told.
ERROR_MESSAGES: dict[str, str] = {
    "INTERNAL_ERROR": "服务暂时不可用，请稍后重试",
    "MODEL_UNAVAILABLE": "模型暂时不可用，请稍后重试",
    "EVIDENCE_INSUFFICIENT": "知识库里没有足够的证据回答这个问题",
    "NOT_FOUND": "没有找到对应的内容",
    "INVALID_REQUEST": "请求的内容有问题",
    "NOT_INITIALIZED": "服务还没有准备好",
}


def error_payload(code: str, message: str | None = None) -> dict[str, Any]:
    """The body of an error response."""
    return {
        "error": message or ERROR_MESSAGES.get(code, ERROR_MESSAGES["INTERNAL_ERROR"]),
        "code": code,
    }


def classify_exception(exc: BaseException) -> str:
    """The code that fits an exception - never its text."""
    name = type(exc).__name__.lower()
    if "timeout" in name:
        return "MODEL_UNAVAILABLE"
    if "connect" in name or "ratelimit" in name or "apierror" in name:
        return "MODEL_UNAVAILABLE"
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return "MODEL_UNAVAILABLE"
    return "INTERNAL_ERROR"
