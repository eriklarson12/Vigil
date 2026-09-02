"""Test env is set BEFORE any vigil import: fixture/fake/mock modes everywhere.
No live LLM calls in tests, ever (docs/conventions.md)."""

import asyncio
import os
import pathlib
import sys

if sys.platform == "win32":
    # psycopg async cannot run on the ProactorEventLoop (Windows default)
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

ROOT = pathlib.Path(__file__).parent.parent

os.environ["GITHUB_MODE"] = "fixture"
os.environ["LLM_MODE"] = "fake"
os.environ["EMBEDDINGS_MODE"] = "fake"
os.environ["SLACK_MODE"] = "mock"
# Pin the credentials too, not just the modes: Settings() falls back to the
# developer's .env, which on a configured box holds a live Slack bot token and
# the production webhook tokens. Tests must never inherit either.
os.environ["SLACK_BOT_TOKEN"] = ""
os.environ["SLACK_WEBHOOK_URL"] = ""
os.environ["GEMINI_API_KEY"] = ""
os.environ["ALERTMANAGER_WEBHOOK_TOKEN"] = "dev-token"
os.environ["RESUME_TOKEN"] = "dev-token"
os.environ["GITHUB_FIXTURES_DIR"] = str(ROOT / "tests" / "fixtures" / "github")
os.environ["LLM_FIXTURES_DIR"] = str(ROOT / "tests" / "fixtures" / "llm")
os.environ["SERVICES_FILE"] = str(ROOT / "services.yaml")
os.environ.setdefault("DATABASE_URL", "postgresql://vigil:vigil@localhost:5433/vigil")

import json  # noqa: E402
from datetime import UTC, datetime  # noqa: E402

import pytest  # noqa: E402

from vigil.impact.catalog import ServiceCatalog  # noqa: E402

SCENARIOS_DIR = ROOT / "simulator" / "scenarios"
COMMITS_DIR = ROOT / "tests" / "fixtures" / "github"
ALERT_TIME = datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC)


@pytest.fixture(scope="session")
def catalog() -> ServiceCatalog:
    return ServiceCatalog.load(ROOT / "services.yaml")


def load_scenario(name: str) -> dict:
    return json.loads((SCENARIOS_DIR / f"{name}.json").read_text(encoding="utf-8"))


def load_commit_fixture(name: str) -> dict:
    return json.loads((COMMITS_DIR / f"{name}.json").read_text(encoding="utf-8"))


def planted_culprit(name: str) -> str | None:
    """The sha the scenario plants, read from the fixture that owns it.

    Ground truth lives in the fixture rather than in a constant here so that R2's rewrite to
    the demo repo's real shas needs no hand edits in any test.
    """
    return load_commit_fixture(name)["culprit"]


@pytest.fixture(scope="session")
def alert_time() -> datetime:
    return ALERT_TIME
