"""Golden tests for the deterministic pre-scorer (spec §16).

Expected values are HAND-COMPUTED from the formula in tasks/todo.md §6.2.
If you change weights or feature rules, update the spec and these constants
in the same change.
"""

import math
from datetime import timedelta

import pytest

from tests.conftest import ALERT_TIME, ROOT, load_scenario
from vigil.commits.github import load_fixture_commits
from vigil.commits.scoring import glob_to_regex, score_commits
from vigil.config import get_settings

CHECKOUT_GLOBS = ["services/checkout/**", "libs/payments/**"]
SHARED_GLOBS = ["libs/**", "deploy/**", ".github/**", "config/**"]


def _commit(sha, message, hours_before, files):
    return {
        "sha": sha,
        "message": message,
        "author": "dev",
        "committed_at": ALERT_TIME - timedelta(hours=hours_before),
        "files": files,
    }


class TestGoldenValues:
    def test_full_path_match_with_fix_message(self):
        # f_time=exp(-1)=0.367879, f_path=1, f_risk=0, f_size=log10(100)/3=0.666667,
        # f_msg=0.3 (\bfix\b), f_deploy=0
        # raw = .30*.367879 + .25 + .10*.666667 + .10*.3 = 0.457030
        commits = [
            _commit("aaa", "fix payment bug", 6,
                    [{"path": "services/checkout/api.py", "additions": 99, "deletions": 0}])
        ]
        [result] = score_commits(
            commits, service="checkout", path_globs=CHECKOUT_GLOBS, shared_globs=SHARED_GLOBS,
            deploys=[], starts_at=ALERT_TIME,
        )
        assert result["score"] == pytest.approx(0.457030, abs=1e-5)
        assert result["feature_scores"]["f_time"] == pytest.approx(math.exp(-1), abs=1e-5)
        assert result["feature_scores"]["f_path"] == 1.0
        assert result["feature_scores"]["f_msg"] == pytest.approx(0.3)
        assert not result["gated"]

    def test_relevance_gate_applies(self):
        # unrelated path, no deploy: raw = .30*exp(-1/6) + .10*log10(11)/3 = 0.288658 -> x0.3
        commits = [_commit("bbb", "update readme", 1,
                           [{"path": "docs/readme.md", "additions": 10, "deletions": 0}])]
        [result] = score_commits(
            commits, service="checkout", path_globs=CHECKOUT_GLOBS, shared_globs=SHARED_GLOBS,
            deploys=[], starts_at=ALERT_TIME,
        )
        assert result["gated"]
        assert result["score"] == pytest.approx(0.086597, abs=1e-5)

    def test_shared_glob_counts_half_and_ungates(self):
        commits = [_commit("ccc", "tweak values", 1,
                           [{"path": "config/checkout.yaml", "additions": 1, "deletions": 1}])]
        [result] = score_commits(
            commits, service="checkout", path_globs=CHECKOUT_GLOBS, shared_globs=SHARED_GLOBS,
            deploys=[], starts_at=ALERT_TIME,
        )
        assert result["feature_scores"]["f_path"] == pytest.approx(0.5)
        assert not result["gated"]

    def test_deploy_correlation_tiers(self):
        deploys = [
            {"service": "checkout", "commit_shas": ["near"],
             "finished_at": ALERT_TIME - timedelta(minutes=20)},
            {"service": "checkout", "commit_shas": ["far"],
             "finished_at": ALERT_TIME - timedelta(hours=3)},
            {"service": "checkout", "commit_shas": ["ancient"],
             "finished_at": ALERT_TIME - timedelta(hours=6)},
        ]
        commits = [
            _commit("near", "a", 1, [{"path": "x.py", "additions": 1, "deletions": 0}]),
            _commit("far", "b", 4, [{"path": "x.py", "additions": 1, "deletions": 0}]),
            _commit("ancient", "c", 7, [{"path": "x.py", "additions": 1, "deletions": 0}]),
        ]
        results = {
            r["sha"]: r["feature_scores"]["f_deploy"]
            for r in score_commits(
                commits, service="checkout", path_globs=CHECKOUT_GLOBS, shared_globs=SHARED_GLOBS,
                deploys=deploys, starts_at=ALERT_TIME,
            )
        }
        assert results == {"near": 1.0, "far": 0.5, "ancient": 0.0}

    def test_revert_shifts_signal_to_reverted_commit(self):
        commits = [
            _commit("aaaa1111", "tune retry limits", 5,
                    [{"path": "services/checkout/retry.py", "additions": 10, "deletions": 2}]),
            _commit("rrrr9999", 'Revert "tune retry limits"\n\nThis reverts commit aaaa1111.', 1,
                    [{"path": "services/checkout/retry.py", "additions": 2, "deletions": 10}]),
        ]
        results = {r["sha"]: r["feature_scores"]["f_msg"] for r in score_commits(
            commits, service="checkout", path_globs=CHECKOUT_GLOBS, shared_globs=SHARED_GLOBS,
            deploys=[], starts_at=ALERT_TIME,
        )}
        assert results["rrrr9999"] == 0.0          # the revert itself carries no signal
        assert results["aaaa1111"] == pytest.approx(0.6)  # the reverted commit gets the bonus


CULPRITS = {
    "bad_deploy": ("checkout", "a1b2c3d4e5"),
    "db_migration_lock": ("orders", "b2c3d4e5f6"),
    "memory_leak": ("inventory", "c3d4e5f6a7"),
    "config_typo": ("checkout", "d4e5f6a7b8"),
    "dependency_bump": ("orders", "e5f6a7b8c9"),
    "partial_revert": ("checkout", "9a8b7c6d5e"),
    "shared_db_saturation": ("payments-db", "b7c8d9e0f1"),
    "auth_key_rotation": ("auth", "e1f2a3b4c5"),
    "hotfix_regression": ("orders", "f6a7b8c9d0"),
}
# Scenarios with no single planted culprit: cert_expiry has no candidate above the
# floor at all, ambiguous_latency has three that the scorer cannot separate.
NO_SINGLE_CULPRIT = {"cert_expiry", "ambiguous_latency"}


def _score_scenario(name: str, service: str, catalog):
    settings = get_settings()
    commits = load_fixture_commits(settings, name, ALERT_TIME)
    scenario = load_scenario(name)
    deploys = [
        {
            "service": d["service"],
            "commit_shas": d["commit_shas"],
            "finished_at": ALERT_TIME - timedelta(minutes=d["minutes_before_alert"]),
        }
        for d in scenario.get("deploys", [])
    ]
    cfg = catalog.get(service)
    return score_commits(
        commits, service=service, path_globs=cfg["path_globs"], shared_globs=catalog.shared_globs,
        deploys=deploys, starts_at=ALERT_TIME,
    )


@pytest.mark.parametrize("name", sorted(CULPRITS))
def test_planted_culprit_ranks_first(name, catalog):
    service, culprit = CULPRITS[name]
    scores = _score_scenario(name, service, catalog)
    assert scores[0]["sha"] == culprit, f"{name}: expected {culprit} first, got {scores[:2]}"
    assert scores[0]["score"] >= 0.15  # survives the LLM-gate floor


def test_cert_expiry_has_no_candidate_above_floor(catalog):
    scores = _score_scenario("cert_expiry", "checkout", catalog)
    assert all(s["score"] < 0.15 for s in scores), scores[:2]


def test_partial_revert_ranks_the_reverted_commit_above_its_revert(catalog):
    """The revert is the newest touch of the failing file; the commit it reverts wins."""
    scores = {s["sha"]: s for s in _score_scenario("partial_revert", "checkout", catalog)}
    reverted, revert = scores["9a8b7c6d5e"], scores["4f3e2d1c0b"]
    assert reverted["score"] > revert["score"]
    assert revert["feature_scores"]["f_msg"] == 0.0
    assert reverted["feature_scores"]["f_msg"] >= 0.6


def test_ambiguous_latency_has_three_inseparable_candidates(catalog):
    """The scenario's point: scoring hands the LLM real candidates it cannot rank apart."""
    scores = _score_scenario("ambiguous_latency", "checkout", catalog)
    above_floor = [s for s in scores if s["score"] >= 0.15]
    assert len(above_floor) == 3
    assert above_floor[0]["score"] - above_floor[1]["score"] < 0.02


def test_culprit_map_covers_every_fixture():
    """R2 regenerates these fixtures from a real repo; a scenario added or dropped
    there must not silently escape the rank-1 assertions above."""
    stems = {p.stem for p in (ROOT / "tests" / "fixtures" / "github").glob("*.json")}
    assert stems == set(CULPRITS) | NO_SINGLE_CULPRIT


def test_glob_double_star():
    assert glob_to_regex("services/checkout/**").match("services/checkout/handlers/payment.py")
    assert glob_to_regex("**/migrations/**").match("services/orders/migrations/0042.sql")
    assert not glob_to_regex("services/checkout/**").match("services/orders/x.py")
    assert glob_to_regex("config/**/*.json").match("config/app.json")
    assert glob_to_regex("config/**/*.json").match("config/nested/app.json")
