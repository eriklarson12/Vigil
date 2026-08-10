"""The LLM seam (spec §16). ALL generation goes through LLMClient — direct
google-genai usage anywhere else in the codebase is a defect.

GeminiClient: budget check -> primary model -> 10s backoff -> fallback model.
FakeLLMClient: replays recorded fixtures validated through the REAL Pydantic
schemas, so schema drift breaks tests loudly. Also used at runtime when no
GEMINI_API_KEY is configured (offline demos).
"""

import asyncio
import json
import pathlib
import re
from typing import Protocol, TypeVar

import structlog
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel

from vigil.config import Settings
from vigil.llm.budget import consume_call

log = structlog.get_logger()

T = TypeVar("T", bound=BaseModel)

RETRY_BACKOFF_SECONDS = 10.0


class LLMUnavailable(Exception):
    """All models/retries failed — callers degrade to deterministic output."""


class LLMClient(Protocol):
    async def generate_structured(self, system: str, user: str, schema: type[T], call_type: str) -> T: ...


class GeminiClient:
    def __init__(self, settings: Settings, pool: AsyncConnectionPool):
        from google import genai

        self._client = genai.Client(api_key=settings.gemini_api_key)
        self._settings = settings
        self._pool = pool

    async def generate_structured(self, system: str, user: str, schema: type[T], call_type: str) -> T:
        from google.genai import errors as genai_errors
        from google.genai import types

        await consume_call(self._pool, self._settings.llm_daily_budget)
        models = [self._settings.gemini_model, self._settings.gemini_fallback_model]
        last_error: Exception | None = None
        for attempt, model in enumerate(models):
            try:
                resp = await self._client.aio.models.generate_content(
                    model=model,
                    contents=user,
                    config=types.GenerateContentConfig(
                        system_instruction=system,
                        response_mime_type="application/json",
                        response_schema=schema,
                        temperature=0.2,
                    ),
                )
                result = schema.model_validate_json(resp.text)
                log.info("llm_call", call_type=call_type, model=model, attempt=attempt)
                return result
            except genai_errors.APIError as exc:
                last_error = exc
                log.warning("llm_error", call_type=call_type, model=model, code=exc.code, msg=str(exc))
                if exc.code in (429, 500, 503) and attempt < len(models) - 1:
                    await asyncio.sleep(RETRY_BACKOFF_SECONDS)
                    continue
                break
            except Exception as exc:  # noqa: BLE001 - schema validation, transport
                last_error = exc
                log.warning("llm_error", call_type=call_type, model=model, msg=str(exc))
                break
        raise LLMUnavailable(str(last_error)) from last_error


class FakeLLMClient:
    """Fixture replay keyed {call_type}.{scenario}.json -> {call_type}.default.json.

    The scenario is inferred by scanning the prompt for scenario tokens present
    in the fixture inventory (simulator alerts carry a `scenario` label that
    flows into every prompt). Demo/test mode only — documented in README.
    """

    def __init__(self, fixtures_dir: str | pathlib.Path):
        self._dir = pathlib.Path(fixtures_dir)

    def _scenarios_for(self, call_type: str) -> list[str]:
        pattern = re.compile(rf"^{re.escape(call_type)}\.(.+)\.json$")
        out = []
        if self._dir.exists():
            for p in self._dir.iterdir():
                m = pattern.match(p.name)
                if m and m.group(1) != "default":
                    out.append(m.group(1))
        return out

    async def generate_structured(self, system: str, user: str, schema: type[T], call_type: str) -> T:
        scenario = next((s for s in self._scenarios_for(call_type) if s in user or s in system), "default")
        path = self._dir / f"{call_type}.{scenario}.json"
        if not path.exists():
            path = self._dir / f"{call_type}.default.json"
        if not path.exists():
            raise LLMUnavailable(f"no LLM fixture for call_type={call_type}")
        data = json.loads(path.read_text(encoding="utf-8"))
        log.info("llm_call_fake", call_type=call_type, fixture=path.name)
        return schema.model_validate(data)


def get_llm_client(settings: Settings, pool: AsyncConnectionPool) -> LLMClient:
    mode = settings.llm_mode
    if mode == "auto":
        mode = "gemini" if settings.gemini_api_key else "fake"
    if mode == "gemini":
        return GeminiClient(settings, pool)
    log.info("llm_fake_mode", fixtures=settings.llm_fixtures_dir)
    return FakeLLMClient(settings.llm_fixtures_dir)
