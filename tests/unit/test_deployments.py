"""GitHub Deployments API parsing and fixture replay (roadmap R6).

f_deploy is worth far more than its 0.10 weight: a non-zero value also lifts a commit
out of the 0.3x relevance gate (scoring.py), so a parse that silently drops a deployment
costs the scorer its strongest signal.
"""

from datetime import UTC, datetime, timedelta

import pytest

from tests.conftest import ALERT_TIME, load_scenario
from vigil.commits.github import (
    load_fixture_commits,
    load_fixture_deployments,
    parse_deployments,
)
from vigil.commits.scoring import score_commits
from vigil.config import get_settings

LOOKBACK = 48
SHA = "71cd87314d4ab16755b3b8f721372a5b1e1fdab1"
OTHER_SHA = "520191a2fa06d48da80b227c9b8767681437b698"

SCENARIOS = [
    "ambiguous_latency", "auth_key_rotation", "bad_deploy", "cert_expiry", "config_typo",
    "db_migration_lock", "dependency_bump", "hotfix_regression", "memory_leak",
    "partial_revert", "shared_db_saturation",
]


def _stamp(minutes_before: float) -> str:
    return (ALERT_TIME - timedelta(minutes=minutes_before)).isoformat().replace("+00:00", "Z")


# --------------------------------------------------------------------------- parse


def test_updated_at_wins_over_created_at():
    """created_at is deploy *start*; using it would over-age every deploy by its own
    duration and push a genuine 25-minute deploy out of the 45-minute 1.0 tier."""
    out = parse_deployments(
        [{"sha": SHA, "created_at": _stamp(90), "updated_at": _stamp(20)}], ALERT_TIME, LOOKBACK
    )
    assert out == [{"sha": SHA, "finished_at": ALERT_TIME - timedelta(minutes=20)}]


def test_created_at_is_the_fallback():
    out = parse_deployments([{"sha": SHA, "created_at": _stamp(20)}], ALERT_TIME, LOOKBACK)
    assert out[0]["finished_at"] == ALERT_TIME - timedelta(minutes=20)


def test_null_updated_at_falls_back_rather_than_crashing():
    out = parse_deployments(
        [{"sha": SHA, "created_at": _stamp(20), "updated_at": None}], ALERT_TIME, LOOKBACK
    )
    assert out[0]["finished_at"] == ALERT_TIME - timedelta(minutes=20)


@pytest.mark.parametrize("minutes_before", [LOOKBACK * 60 + 1, -30])
def test_out_of_window_deployments_are_dropped(minutes_before):
    """Older than the lookback, or stamped after the alert fired."""
    items = [{"sha": SHA, "updated_at": _stamp(minutes_before)}]
    assert parse_deployments(items, ALERT_TIME, LOOKBACK) == []


def test_window_boundaries_are_inclusive():
    items = [
        {"sha": SHA, "updated_at": _stamp(LOOKBACK * 60)},
        {"sha": OTHER_SHA, "updated_at": _stamp(0)},
    ]
    assert len(parse_deployments(items, ALERT_TIME, LOOKBACK)) == 2


@pytest.mark.parametrize("item", [{"updated_at": _stamp(20)}, {"sha": SHA}, {"sha": None}, {}])
def test_items_without_a_sha_or_a_timestamp_are_dropped(item):
    assert parse_deployments([item], ALERT_TIME, LOOKBACK) == []


def test_duplicate_sha_and_timestamp_collapses():
    item = {"sha": SHA, "updated_at": _stamp(20)}
    assert len(parse_deployments([item, dict(item)], ALERT_TIME, LOOKBACK)) == 1


def test_same_sha_deployed_twice_keeps_both():
    """A redeploy of the same sha is two correlations, not a duplicate."""
    items = [{"sha": SHA, "updated_at": _stamp(20)}, {"sha": SHA, "updated_at": _stamp(300)}]
    out = parse_deployments(items, ALERT_TIME, LOOKBACK)
    assert len(out) == 2
    assert out[0]["finished_at"] > out[1]["finished_at"]  # newest first


def test_deployment_state_and_task_are_not_filtered():
    """A failed deploy right before an alert is a correlation, not noise."""
    items = [{"sha": SHA, "updated_at": _stamp(20), "task": "deploy:migrations"}]
    assert len(parse_deployments(items, ALERT_TIME, LOOKBACK)) == 1


# --------------------------------------------------------------------------- fixture


def test_fixture_deployments_materialize_relative_to_starts_at():
    settings = get_settings()
    later = datetime(2027, 3, 4, 9, 0, 0, tzinfo=UTC)
    for anchor in (ALERT_TIME, later):
        out = load_fixture_deployments(settings, "bad_deploy", anchor)
        assert [d["sha"] for d in out] == [SHA, OTHER_SHA]
        assert all(d["finished_at"] == anchor - timedelta(minutes=20) for d in out)


@pytest.mark.parametrize("scenario", ["cert_expiry", "memory_leak", "ambiguous_latency"])
def test_scenarios_without_a_deploy_have_no_deployments(scenario):
    """The absent key is what keeps these three at f_deploy = 0."""
    assert load_fixture_deployments(get_settings(), scenario, ALERT_TIME) == []


def test_missing_fixture_yields_nothing():
    assert load_fixture_deployments(get_settings(), "no_such_scenario", ALERT_TIME) == []


# --------------------------------------------------------------------------- parity


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_fixture_path_scores_identically_to_the_simulator_path(scenario, catalog):
    """The roadmap's acceptance test: a fixture `deployments` entry must yield exactly
    the f_deploy the simulator-planted deploy_events row does.

    The fixture path drops the service name and the graph node recovers it by fanning the
    repo out across the catalog, so this also pins that the fan-out cannot move a score.
    """
    settings = get_settings()
    service = load_scenario(scenario)["alert"]["labels"]["service"]
    cfg = catalog.get(service)
    commits = load_fixture_commits(settings, scenario, ALERT_TIME)

    planted = [
        {
            "service": d["service"],
            "commit_shas": d["commit_shas"],
            "finished_at": ALERT_TIME - timedelta(minutes=d["minutes_before_alert"]),
        }
        for d in load_scenario(scenario).get("deploys", [])
    ]
    fanned = [
        {"service": name, "commit_shas": [d["sha"]], "finished_at": d["finished_at"]}
        for d in load_fixture_deployments(settings, scenario, ALERT_TIME)
        for name in catalog.services_for_repo(cfg["repo"])
    ]

    def features(deploys):
        scored = score_commits(
            commits, service=service, path_globs=cfg["path_globs"],
            shared_globs=catalog.shared_globs, deploys=deploys, starts_at=ALERT_TIME,
        )
        return {s["sha"]: s["feature_scores"]["f_deploy"] for s in scored}

    assert features(fanned) == features(planted)
