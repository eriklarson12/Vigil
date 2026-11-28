"""`scripts/backtest_reverts.py` turns reverts into labelled cases (roadmap R18).

Each test builds a small git repo with controlled commit times. The case extractor is
the part that can leak the answer: if a revert of the label stays among the
candidates, REVERT_BONUS names the label and the backtest measures nothing.
"""

import importlib.util
import os
import pathlib
import subprocess
from datetime import UTC, datetime, timedelta

import pytest

from vigil.commits.scoring import REVERTED_SHA_RE

ROOT = pathlib.Path(__file__).parent.parent.parent
_spec = importlib.util.spec_from_file_location("backtest", ROOT / "scripts" / "backtest_reverts.py")
backtest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(backtest)

T0 = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


class Repo:
    def __init__(self, path: pathlib.Path):
        self.path = path
        self._git("init", "-q", "-b", "main")
        self._git("config", "user.name", "t")
        self._git("config", "user.email", "t@example.com")
        self._git("config", "commit.gpgsign", "false")

    def _git(self, *args, env=None):
        return subprocess.run(
            ["git", "-C", str(self.path), *args], check=True, capture_output=True, text=True,
            env={**os.environ, **(env or {})},
        ).stdout.strip()

    def commit(self, hours: float, message: str, lines: int = 1, path: str = "src/app.py") -> str:
        """Commit `lines` new lines to `path`, `hours` after T0."""
        f = self.path / path
        f.parent.mkdir(parents=True, exist_ok=True)
        with f.open("a") as fh:
            fh.write("x\n" * lines)
        self._git("add", "-A")
        when = (T0 + timedelta(hours=hours)).isoformat()
        self._git("commit", "-q", "-m", message,
                  env={"GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when})
        return self._git("rev-parse", "HEAD")

    def revert_of(self, hours: float, sha: str, subject: str = "x") -> str:
        return self.commit(hours, f'Revert "{subject}"\n\nThis reverts commit {sha}.')


@pytest.fixture()
def repo(tmp_path):
    r = Repo(tmp_path)
    r.commit(0, "initial")  # puts history_start well before every window
    return r


def _cases(repo, **kw):
    return backtest.extract_cases(backtest.load_history(repo.path, None), **kw)


def test_a_revert_becomes_a_case_without_itself(repo):
    a = repo.commit(90, "add search")
    label = repo.commit(95, "speed up cache")
    c = repo.commit(97, "docs")
    revert = repo.revert_of(100, label)
    cases, skipped = _cases(repo)
    assert [(k["revert"], k["label"]) for k in cases] == [(revert, label)]
    assert cases[0]["candidates"] == [c, label, a]
    assert not skipped


def test_no_candidate_reverts_the_label(repo):
    repo.commit(90, "add search")
    label = repo.commit(95, "speed up cache")
    first = repo.revert_of(96, label)
    repo.commit(97, "reland the cache change")
    repo.revert_of(100, label)
    cases, _ = _cases(repo)
    for case in cases:
        reverted = []
        for sha in case["candidates"]:
            msg = repo._git("log", "-1", "--format=%B", sha)
            m = REVERTED_SHA_RE.search(msg)
            reverted += [m.group(1)] if m else []
        assert not any(case["label"].startswith(r) for r in reverted)
    assert first not in cases[0]["candidates"]


def test_a_label_older_than_the_window_is_skipped_and_counted(repo):
    label = repo.commit(50, "old change")
    repo.commit(99, "recent")
    repo.revert_of(100, label)
    cases, skipped = _cases(repo)
    assert cases == []
    assert skipped == {"label_outside_window": 1}


def test_a_window_reaching_before_history_is_skipped(repo):
    label = repo.commit(10, "change")
    repo.revert_of(12, label)
    assert _cases(repo) == ([], {"window_before_history": 1})


def test_a_label_past_the_commit_cap_is_skipped_and_counted(repo):
    label = repo.commit(90, "change")
    repo.commit(91, "a")
    repo.commit(92, "b")
    repo.revert_of(100, label)
    cases, skipped = _cases(repo, cap=2)
    assert cases == [] and skipped == {"label_beyond_cap": 1}


def test_a_revert_of_a_revert_is_not_an_incident(repo):
    repo.commit(90, "base")
    change = repo.commit(92, "change")
    first = repo.revert_of(94, change)
    repo.revert_of(96, first)
    cases, skipped = _cases(repo)
    assert [k["revert"] for k in cases] == [first]
    assert skipped == {"label_is_a_revert": 1}


def test_an_unknown_sha_and_a_lone_candidate_are_counted(repo):
    repo.commit(90, "a")
    repo.revert_of(100, "deadbeefdeadbeef")
    label = repo.commit(150, "lonely")
    repo.revert_of(151, label)
    cases, skipped = _cases(repo)
    assert cases == []
    assert skipped == {"label_not_on_branch": 1, "single_candidate": 1}


def test_short_shas_resolve(repo):
    repo.commit(90, "a")
    label = repo.commit(95, "b")
    repo.revert_of(100, label[:9])
    cases, _ = _cases(repo)
    assert cases[0]["label"] == label


def test_ranks_come_from_the_real_scorer_and_baselines(repo):
    big = repo.commit(90, "rewrite parser", lines=400)
    label = repo.commit(98, "hotfix: bump timeout", lines=1, path="config/app.yaml")
    newest = repo.commit(99, "docs", lines=5, path="README.md")
    repo.revert_of(100, label)
    history = backtest.load_history(repo.path, None)
    cases, _ = backtest.extract_cases(history)
    files = backtest.load_files(repo.path, set(cases[0]["candidates"]))
    assert files[big] == [{"path": "src/app.py", "additions": 400, "deletions": 0}]
    ranks = backtest.rank_case(cases[0], {c["sha"]: c for c in history}, files)
    assert ranks == {"scorer": 1, "most_recent": 2, "largest_diff": 3}
    assert newest in cases[0]["candidates"]


def test_summary_metrics():
    ranked = [
        {"n": 2, "ranks": {"scorer": 1, "most_recent": 2, "largest_diff": 1}},
        {"n": 4, "ranks": {"scorer": 4, "most_recent": 1, "largest_diff": 2}},
    ]
    s = backtest.summarize(ranked)
    assert s["scorer"]["top1"] == {"n": 1, "rate": 0.5}
    assert s["scorer"]["top3"] == {"n": 1, "rate": 0.5}
    assert s["scorer"]["mrr"] == pytest.approx((1 + 1 / 4) / 2, abs=1e-4)
    assert s["largest_diff"]["mrr"] == pytest.approx(0.75, abs=1e-4)
    # random over n candidates: top-1 1/n, top-3 min(3, n)/n, MRR H(n)/n
    assert s["random_expected"]["top1"] == pytest.approx((1 / 2 + 1 / 4) / 2, abs=1e-4)
    assert s["random_expected"]["top3"] == pytest.approx((1 + 3 / 4) / 2, abs=1e-4)
    h4 = 1 + 1 / 2 + 1 / 3 + 1 / 4
    assert s["random_expected"]["mrr"] == pytest.approx((1.5 / 2 + h4 / 4) / 2, abs=1e-4)


def test_a_github_revert_button_names_the_squash_merged_pr(repo):
    repo.commit(90, "add search (#11)")
    label = repo.commit(95, "speed up cache (#12)")
    revert = repo.commit(100, 'Revert "speed up cache (#12)" (#13)\n\nReverts acme/shop#12')
    cases, skipped = _cases(repo)
    assert [(k["revert"], k["label"]) for k in cases] == [(revert, label)]
    assert revert not in cases[0]["candidates"] and not skipped


def test_a_gerrit_revert_names_the_cl_in_the_reviewed_on_trailer(repo):
    repo.commit(90, "a\n\nReviewed-on: https://go-review.example.com/c/go/+/500")
    label = repo.commit(95, "b\n\nReviewed-on: https://go-review.example.com/c/go/+/501")
    repo.commit(100, 'Revert "b"\n\nThis reverts CL 501.\n\nReviewed-on: https://go-review.example.com/c/go/+/502')
    cases, _ = _cases(repo)
    assert [k["label"] for k in cases] == [label]


def test_a_revert_naming_nothing_is_counted(repo):
    repo.commit(90, "a")
    repo.commit(100, 'Revert "a"\n\nIt broke the build.')
    assert _cases(repo) == ([], {"no_reference": 1})


def test_macro_averages_each_repo_equally():
    one = backtest.summarize([{"n": 2, "ranks": {"scorer": 1, "most_recent": 1, "largest_diff": 1}}])
    many = backtest.summarize(
        [{"n": 2, "ranks": {"scorer": 2, "most_recent": 2, "largest_diff": 2}}] * 9
    )
    assert backtest.macro([one, many])["scorer"]["top1"] == 0.5
