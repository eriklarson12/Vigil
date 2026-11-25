"""Scenario annotations MUST describe symptoms only (roadmap R16).

A real Alertmanager annotation is a template over a metric: value, threshold, duration,
route or pod. It cannot know which file changed or that a deploy went out. An annotation
that names either hands the ranking call and the retrieval query the answer, and every
number measured on top of it is inflated.
"""

import json
import pathlib
import re

import pytest

from tests.conftest import load_commit_fixture

ROOT = pathlib.Path(__file__).parent.parent.parent
SCENARIOS_DIR = ROOT / "simulator" / "scenarios"
SCENARIOS = sorted(p.stem for p in SCENARIOS_DIR.glob("*.json"))

# Prefix match, so `deploys`, `reverted` and `migrations` are caught too. `release` is not
# in the roadmap's list; it names a deploy just as plainly.
CAUSE_WORDS = ("deploy", "rollout", "revert", "upgrade", "migration", "release")
# Shorter stem pieces (`add`, `db`) are too common to ban; the whole stem is still banned.
MIN_PIECE = 4


def _annotation_text(alert: dict) -> str:
    return " ".join(str(v) for v in alert.get("annotations", {}).values())


def _culprit_identifiers(scenario: str, service: str) -> set[str]:
    """Paths, basenames, stems, and stem pieces of every file the culprit touches.

    The service name is exempt: `config/checkout.yaml` has the stem `checkout`, and an
    alert on checkout has to say so.
    """
    fixture = load_commit_fixture(scenario)
    culprit = fixture["culprit"]
    if culprit is None:
        return set()
    commit = next(c for c in fixture["commits"] if c["sha"] == culprit)
    found: set[str] = set()
    for f in commit["files"]:
        path = pathlib.PurePosixPath(f["path"])
        found |= {str(path), path.name, path.stem}
        found |= {p for p in re.split(r"[_\-.]", path.stem) if len(p) >= MIN_PIECE}
    return {i.lower() for i in found} - {service.lower()}


def _leaks(text: str, identifiers: set[str]) -> list[str]:
    hits = [
        i for i in sorted(identifiers)
        if re.search(rf"(?<!\w){re.escape(i)}(?!\w)", text, re.IGNORECASE)
    ]
    hits += [w for w in CAUSE_WORDS if re.search(rf"(?<!\w){w}", text, re.IGNORECASE)]
    return hits


def _load(scenario: str) -> dict:
    return json.loads((SCENARIOS_DIR / f"{scenario}.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_annotations_name_no_culprit_file_and_no_cause(scenario):
    alert = _load(scenario)["alert"]
    service = alert["labels"].get("service", "")
    leaks = _leaks(_annotation_text(alert), _culprit_identifiers(scenario, service))
    assert not leaks, f"{scenario}: annotations give away the diagnosis: {leaks}"


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_annotations_carry_a_summary_and_a_description(scenario):
    annotations = _load(scenario)["alert"]["annotations"]
    assert annotations.get("summary") and annotations.get("description")


@pytest.mark.parametrize(
    ("scenario", "leaked_text", "expected"),
    [
        # The pre-R16 annotations, which this test exists to forbid.
        ("config_typo", "while parsing db_pool_timeout from config/checkout.yaml", "config/checkout.yaml"),
        ("memory_leak", "Heap dominated by SKU lookup cache entries", "cache"),
        ("auth_key_rotation", "before the last auth rollout", "rollout"),
        ("db_migration_lock", "held by a CREATE INDEX statement", "index"),
    ],
)
def test_the_check_catches_the_old_annotations(scenario, leaked_text, expected):
    service = _load(scenario)["alert"]["labels"]["service"]
    assert expected in _leaks(leaked_text, _culprit_identifiers(scenario, service))
