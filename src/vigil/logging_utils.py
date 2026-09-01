"""structlog secret redaction (spec §14, docs/conventions.md "Logging").

Three passes over every record: mask values under secret-looking key names, scrub the
configured secret values wherever they appear inside strings, then rewrite passwords out
of connection strings. The last pass exists because `database_url` matches no secret key
name and its value cannot go in the scrub set — the local default password is `vigil`,
the same token as the service and the database name.

Every pattern is compiled at import and the secret tuple is built once per process, so
the per-record cost is a dict walk plus a few str.replace calls.
"""

import re
from functools import lru_cache
from typing import Any

import structlog

from vigil.config import get_settings

MASK = "***"
MAX_DEPTH = 8
MIN_SECRET_LEN = 8

SECRET_KEY_RE = re.compile(r"token|secret|password|authorization|api_key|webhook_url", re.IGNORECASE)
DSN_PASSWORD_RE = re.compile(r"(?P<head>://[^@/\s:]+:)[^@/\s]+(?P<tail>@)")

SECRET_FIELDS = (
    "gemini_api_key",
    "slack_webhook_url",
    "slack_bot_token",
    "slack_signing_secret",
    "alertmanager_webhook_token",
    "resume_token",
    "github_token",
)


@lru_cache
def _secret_values() -> tuple[str, ...]:
    """Settings are not loaded at import time, so build on the first log line.

    Values under MIN_SECRET_LEN are dropped: an unset ("") or stub credential must not
    scrub ordinary words out of every message.
    """
    try:
        settings = get_settings()
    except Exception:  # a config failure must never take down every log line
        return ()
    values = {getattr(settings, field, "") for field in SECRET_FIELDS}
    return tuple(v for v in values if isinstance(v, str) and len(v) >= MIN_SECRET_LEN)


def _scrub(text: str) -> str:
    for secret in _secret_values():
        text = text.replace(secret, MASK)
    return DSN_PASSWORD_RE.sub(rf"\g<head>{MASK}\g<tail>", text)


def _walk(value: Any, depth: int) -> Any:
    if depth > MAX_DEPTH:
        return value
    if isinstance(value, str):
        return _scrub(value)
    if isinstance(value, dict):
        return {
            key: MASK if isinstance(key, str) and SECRET_KEY_RE.search(key) else _walk(val, depth + 1)
            for key, val in value.items()
        }
    if isinstance(value, list | tuple):
        return [_walk(item, depth + 1) for item in value]
    return value


def redact(logger: Any, method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    return _walk(event_dict, 0)


def configure_logging() -> None:
    structlog.configure(
        processors=[
            redact,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ]
    )
