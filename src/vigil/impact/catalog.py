"""services.yaml loader + reverse dependency graph (spec §8)."""

import pathlib
from typing import Any

import yaml


class ServiceCatalog:
    def __init__(self, services: dict[str, dict[str, Any]], shared_globs: list[str]):
        self.services = services
        self.shared_globs = shared_globs
        # reverse edges: dependency -> set of dependents
        self._dependents: dict[str, set[str]] = {}
        for name, cfg in services.items():
            for dep in cfg.get("dependencies", []):
                self._dependents.setdefault(dep, set()).add(name)

    @classmethod
    def load(cls, path: str | pathlib.Path) -> "ServiceCatalog":
        data = yaml.safe_load(pathlib.Path(path).read_text(encoding="utf-8"))
        return cls(data.get("services", {}), data.get("shared_globs", []))

    def get(self, name: str | None) -> dict[str, Any] | None:
        if not name:
            return None
        cfg = self.services.get(name)
        return {"name": name, **cfg} if cfg else None

    def user_facing_dependents(self, name: str) -> list[str]:
        """BFS over reverse dependency edges -> downstream user-facing services."""
        seen: set[str] = set()
        frontier = [name]
        while frontier:
            current = frontier.pop()
            for dependent in self._dependents.get(current, ()):
                if dependent not in seen:
                    seen.add(dependent)
                    frontier.append(dependent)
        return sorted(s for s in seen if self.services.get(s, {}).get("user_facing"))

    def dependency_of_user_facing_count(self, name: str) -> int:
        return len(self.user_facing_dependents(name))
