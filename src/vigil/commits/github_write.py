"""GitHub REST client for writes (R5 revert PRs, R9 action-item issues).

Uses GITHUB_WRITE_TOKEN, never the read-only GITHUB_TOKEN.
"""

from typing import Any

import httpx

from vigil.config import Settings


def write_client(settings: Settings) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url="https://api.github.com",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {settings.github_write_token}",
        },
        timeout=10.0,
    )


async def get_json(gh: httpx.AsyncClient, path: str, **params: Any) -> Any:
    resp = await gh.get(path, params=params or None)
    resp.raise_for_status()
    return resp.json()


async def post_json(gh: httpx.AsyncClient, path: str, body: dict[str, Any]) -> Any:
    resp = await gh.post(path, json=body)
    resp.raise_for_status()
    return resp.json()
