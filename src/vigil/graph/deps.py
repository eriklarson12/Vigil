"""Shared dependency container wired at app startup (main.py) and injected
into graph nodes via closures."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from psycopg_pool import AsyncConnectionPool

from vigil.config import Settings
from vigil.impact.catalog import ServiceCatalog
from vigil.llm.client import LLMClient
from vigil.rag.embed import Embedder
from vigil.slack.sender import SlackSender

if TYPE_CHECKING:
    pass


@dataclass
class Deps:
    settings: Settings
    pool: AsyncConnectionPool
    catalog: ServiceCatalog
    llm: LLMClient
    embedder: Embedder
    slack: SlackSender
    runner: Any = None  # set by main.py after Runner construction ("Runner")
