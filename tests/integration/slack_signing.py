"""Slack v0 request signing for integration tests."""

import hashlib
import hmac
import time

SIGNING_SECRET = "test-signing-secret"  # pinned in tests/conftest.py


def sign_slack_request(body: str, secret: str = SIGNING_SECRET, timestamp: int | None = None) -> dict:
    ts = str(timestamp or int(time.time()))
    digest = hmac.new(secret.encode(), f"v0:{ts}:{body}".encode(), hashlib.sha256).hexdigest()
    return {
        "x-slack-request-timestamp": ts,
        "x-slack-signature": f"v0={digest}",
        "content-type": "application/x-www-form-urlencoded",
    }
