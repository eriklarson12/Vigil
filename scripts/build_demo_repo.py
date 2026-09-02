"""Build the public `vigil-demo-shop` repo from the commit fixtures (roadmap R2).

Vigil's Slack brief links every culprit as `https://{repo}/commit/{sha}`, but the shas in
`tests/fixtures/github/*.json` are invented, so the link 404s. This replays those fixtures
into a real git history: same paths, same messages, same add/delete counts, same relative
ages. `record_fixtures.py` then reads the pushed repo back through the live GitHub client and
rewrites the fixtures with the real shas, which is what makes the links resolve.

Two properties the scorer depends on, both exact by construction:

- **add/delete counts** feed `f_size`. A commit deleting D lines needs the file to already
  hold D, so a first pass simulates the whole timeline to size each file's starting body
  before a single commit is written.
- **relative ages** feed `f_time`. Commit dates are set from `hours_before_alert` against a
  per-scenario anchor, so the recorder recovers the original values by inverting the same
  arithmetic.

Scenario anchors are spaced 96 hours apart, newest last. That clears the 48-hour
`commit_lookback_hours` window plus the widest fixture span (40h), so a live fetch anchored
on one scenario can never pull in a neighbour's commits.

    uv run python scripts/build_demo_repo.py --out /tmp/vigil-demo-shop
"""

import argparse
import json
import pathlib
import subprocess
import sys
from datetime import UTC, datetime, timedelta

ROOT = pathlib.Path(__file__).parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "github"

# 48h lookback + 40h widest fixture span, rounded up to whole days.
ANCHOR_SPACING_HOURS = 96
# bad_deploy is the scenario the one-time GITHUB_MODE=live check fires, so it owns the
# newest window; the rest are alphabetical for a reproducible build.
NEWEST_SCENARIO = "bad_deploy"

AUTHOR_DOMAIN = "vigil-demo.invalid"

# Files with no natural body of their own get one anyway: a diff that deletes from a file
# holding exactly the deleted number of lines reads as a truncation, not an edit.
NATURAL_SIZE = 24


def _scenario_order() -> list[str]:
    stems = sorted(p.stem for p in FIXTURES.glob("*.json"))
    return [s for s in stems if s != NEWEST_SCENARIO] + [NEWEST_SCENARIO]


def _load(scenario: str) -> dict:
    return json.loads((FIXTURES / f"{scenario}.json").read_text(encoding="utf-8"))


def _timeline(order: list[str], newest_anchor: datetime) -> tuple[dict[str, datetime], list[dict]]:
    """Place every fixture commit on one linear history. Returns (anchors, events)."""
    anchors, events = {}, []
    for index, scenario in enumerate(order):
        offset = (len(order) - 1 - index) * ANCHOR_SPACING_HOURS
        anchor = newest_anchor - timedelta(hours=offset)
        anchors[scenario] = anchor
        for commit in _load(scenario)["commits"]:
            events.append(
                {
                    "at": anchor - timedelta(hours=commit["hours_before_alert"]),
                    "scenario": scenario,
                    "commit": commit,
                }
            )
    events.sort(key=lambda e: e["at"])
    return anchors, events


# --------------------------------------------------------------------------- content


def _patch_lines(patch: str, sign: str) -> list[str]:
    """The real hand-written diff lines from a fixture's `patch`, minus their marker.

    Using these as the leading added/removed lines puts the actual planted bug at the top
    of the diff an interviewer opens, rather than burying it under generated filler.
    """
    out = []
    for line in patch.splitlines():
        if line.startswith(sign * 3):
            continue
        if line.startswith(sign) and len(line) > 1:
            out.append(line[1:])
    return out


def _filler(path: str, index: int) -> str:
    """Generated body line. `index` comes from one global counter and is never reused:
    git counts a line appearing on both sides of a hunk as context, so a repeat would
    silently cost the commit an addition and a deletion."""
    suffix = pathlib.PurePath(path).suffix
    stem = pathlib.PurePath(path).stem.replace("-", "_")
    if suffix == ".py":
        templates = [
            f"    {stem}_span = tracer.start_span('{stem}.step_{index}')",
            f"    result_{index} = await backend.fetch(key=f'{stem}:{index}')",
            f"    if result_{index} is None:  # cache miss on the {stem} path",
            f"        metrics.increment('{stem}.miss', tags={{'shard': {index}}})",
            f"    log.debug('{stem}.step', step={index}, elapsed=timer.elapsed_ms())",
        ]
        return templates[index % len(templates)]
    if suffix in (".yaml", ".yml"):
        return f"  {stem}_option_{index}: {index * 7 % 97}"
    if suffix == ".json":
        return f'  "{stem}_key_{index}": {index * 3 % 89},'
    if suffix == ".sql":
        return f"-- {stem} statement {index}"
    if suffix == ".md":
        return f"- {stem.replace('_', ' ').title()} note {index}: see the runbook for details."
    if suffix == ".css":
        return f".{stem}-row-{index} {{ padding: {index % 8}px; }}"
    if suffix == ".toml":
        return f'{stem}_option_{index} = "{index}"'
    if suffix == ".lock":
        return f'  {{ name = "pkg-{index:03d}", version = "1.{index}.0" }},'
    return f"{stem} entry {index}"


class Filler:
    """One monotonic counter for the whole build, so no generated line is ever written twice."""

    def __init__(self) -> None:
        self._next = 0

    def lines(self, path: str, count: int, lead: list[str]) -> list[str]:
        """`lead` first, then filler, to exactly `count` lines."""
        out = list(lead[:count])
        while len(out) < count:
            out.append(_filler(path, self._next))
            self._next += 1
        return out


# --------------------------------------------------------------------------- passes


def _plan_seeds(events: list[dict]) -> dict[str, int]:
    """Pass 1: the starting line count each file needs so no commit over-deletes.

    A file whose first touch deletes nothing is created by that commit and gets no seed.
    """
    seeds: dict[str, int] = {}
    length: dict[str, int] = {}
    for event in events:
        for entry in event["commit"]["files"]:
            path, adds, dels = entry["path"], entry.get("additions", 0), entry.get("deletions", 0)
            if path not in length:
                if dels == 0:
                    length[path] = 0
                    continue
                seeds[path] = max(dels, NATURAL_SIZE)
                length[path] = seeds[path]
            elif length[path] < dels:
                seeds[path] = seeds.get(path, 0) + (dels - length[path])
                length[path] = dels
            length[path] = length[path] - dels + adds
    return seeds


def _seed_bodies(events: list[dict], seeds: dict[str, int], filler: Filler) -> dict[str, list[str]]:
    """Seed each file with the `-` lines of the first commit that deletes from it, padded.

    That is what makes the planted diff show real removed code instead of filler.
    """
    leads: dict[str, list[str]] = {}
    for event in events:
        patch = event["commit"].get("patch", "")
        for entry in event["commit"]["files"]:
            path = entry["path"]
            if path in seeds and path not in leads:
                leads[path] = _patch_lines(patch, "-")
    return {path: filler.lines(path, size, leads.get(path, [])) for path, size in seeds.items()}


def _apply(
    current: list[str], path: str, adds: int, dels: int, lead: list[str], filler: Filler
) -> list[str]:
    """Replace the first `dels` lines with `adds` new ones — exactly that many of each."""
    if len(current) < dels:
        raise AssertionError(f"{path}: {dels} deletions against {len(current)} lines")
    # A fixture patch can show the same line on both sides (bad_deploy re-adds the
    # `store.get` call it removes). Git pairs those as context, so writing both would
    # cost the commit one addition and one deletion against the fixture's counts.
    removed = set(current[:dels])
    return filler.lines(path, adds, [ln for ln in lead if ln not in removed]) + current[dels:]


# --------------------------------------------------------------------------- git


def _git(repo: pathlib.Path, *args: str, env: dict | None = None) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    return result.stdout.strip()


def _commit(repo: pathlib.Path, message: str, author: str, at: datetime) -> str:
    import os

    stamp = at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S+0000")
    env = {
        **os.environ,
        "GIT_AUTHOR_DATE": stamp,
        "GIT_COMMITTER_DATE": stamp,
        "GIT_AUTHOR_NAME": author,
        "GIT_AUTHOR_EMAIL": f"{author}@{AUTHOR_DOMAIN}",
        "GIT_COMMITTER_NAME": author,
        "GIT_COMMITTER_EMAIL": f"{author}@{AUTHOR_DOMAIN}",
    }
    _git(repo, "add", "-A", env=env)
    _git(repo, "commit", "-m", message, env=env)
    return _git(repo, "rev-parse", "HEAD", env=env)


SKELETON = {
    "README.md": [
        "# vigil-demo-shop",
        "",
        "A small e-commerce system used as the subject of Vigil's incident demos.",
        "Every commit here is planted: the history reproduces the scenarios in",
        "`simulator/scenarios/` so Vigil's deterministic scorer has a real repo to rank.",
        "",
        "| Service | Tier | Path |",
        "|---|---|---|",
        "| checkout | 0 | `services/checkout/` |",
        "| auth | 0 | `services/auth/` |",
        "| payments-db | 0 | `db/` |",
        "| orders | 1 | `services/orders/` |",
        "| inventory | 1 | `services/inventory/` |",
        "",
        "Not a real shop. Do not deploy it.",
    ],
}


def _write_skeleton(repo: pathlib.Path, bodies: dict[str, list[str]]) -> None:
    for path, lines in {**SKELETON, **bodies}.items():
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build(out: pathlib.Path, newest_anchor: datetime) -> dict:
    order = _scenario_order()
    anchors, events = _timeline(order, newest_anchor)
    seeds = _plan_seeds(events)
    filler = Filler()
    bodies = _seed_bodies(events, seeds, filler)

    if out.exists():
        raise SystemExit(f"{out} already exists — remove it or pick another --out")
    out.mkdir(parents=True)
    _git(out, "init", "-b", "main")

    _write_skeleton(out, bodies)
    _commit(out, "chore: import the demo shop skeleton", "vigil", events[0]["at"] - timedelta(hours=6))

    contents = {path: list(lines) for path, lines in bodies.items()}
    manifest = {
        scenario: {
            "anchor": anchors[scenario].astimezone(UTC).isoformat(),
            "culprit": None,
            "shas": {},
        }
        for scenario in order
    }

    for event in events:
        commit, scenario = event["commit"], event["scenario"]
        message = commit["message"]
        # The revert's message names the sha it reverts, which REVERTED_SHA_RE parses for
        # the +0.6 bonus. That commit is already built, so substitute its real sha.
        for old, new in manifest[scenario]["shas"].items():
            message = message.replace(old, new)

        leads = _patch_lines(commit.get("patch", ""), "+")
        for entry in commit["files"]:
            path = entry["path"]
            adds, dels = entry.get("additions", 0), entry.get("deletions", 0)
            contents[path] = _apply(contents.get(path, []), path, adds, dels, leads, filler)
            target = out / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("\n".join(contents[path]) + "\n", encoding="utf-8")

        sha = _commit(out, message, commit.get("author", "dev"), event["at"])
        manifest[scenario]["shas"][commit["sha"]] = sha

    for scenario in order:
        culprit = _load(scenario).get("culprit")
        if culprit:
            manifest[scenario]["culprit"] = manifest[scenario]["shas"][culprit]
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=pathlib.Path, help="path to create the repo at")
    parser.add_argument(
        "--anchor",
        default="now",
        help="alert time of the newest scenario, ISO 8601 or 'now' (default: now)",
    )
    parser.add_argument(
        "--manifest",
        type=pathlib.Path,
        default=ROOT / "build" / "manifest.json",
        help="where to write the scenario -> real sha map",
    )
    args = parser.parse_args()

    anchor = (
        datetime.now(UTC) if args.anchor == "now" else datetime.fromisoformat(args.anchor)
    )
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=UTC)

    manifest = build(args.out, anchor)
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    print(f"built {args.out} — {sum(len(m['shas']) for m in manifest.values())} commits")
    for scenario, entry in manifest.items():
        culprit = entry["culprit"]
        print(f"  {scenario:24} {entry['anchor']}  culprit {culprit[:10] if culprit else '(none)'}")
    print(f"manifest: {args.manifest}")


if __name__ == "__main__":
    sys.exit(main())
