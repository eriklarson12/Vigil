"""R5 live revert mechanics against a fake GitHub REST API (httpx.MockTransport)."""

import json

import httpx
import pytest

from vigil.commits.rollback import RevertRefused, open_live_pr, revert_entries

REPO = "o/r"
BASE = f"/repos/{REPO}"
SHA = "c" * 40
PARENT = "p" * 40
HEAD = "h" * 40
PR = {"branch": "vigil/revert-ccccccc-i1", "title": 'Revert "feat: x"', "body": "b"}


def blob(sha, mode="100644"):
    return {"sha": sha, "mode": mode, "type": "blob"}


class FakeGitHub:
    def __init__(self, *, parents=(PARENT,), files=None, head_tree=None, parent_tree=None,
                 branch_exists=False, open_pulls=(), fail=None):
        self.parents = parents
        self.files = files if files is not None else [
            {"filename": "app.py", "status": "modified", "sha": "new-app"}
        ]
        self.head_tree = head_tree if head_tree is not None else {"app.py": blob("new-app")}
        self.parent_tree = parent_tree if parent_tree is not None else {"app.py": blob("old-app")}
        self.branch_exists = branch_exists
        self.open_pulls = list(open_pulls)
        self.fail = fail
        self.posts: dict[str, dict] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if self.fail and path.endswith(self.fail):
            return httpx.Response(502, json={"message": "bad gateway"})
        if method == "POST":
            body = json.loads(request.content)
            self.posts[path.removeprefix(BASE)] = body
            return httpx.Response(201, json={
                "/git/trees": {"sha": "new-tree"},
                "/git/commits": {"sha": "revert-commit"},
                "/git/refs": {"ref": body.get("ref")},
                "/pulls": {"html_url": "https://github.com/o/r/pull/7"},
            }[path.removeprefix(BASE)])
        routes = {
            f"{BASE}/git/ref/heads/{PR['branch']}": (
                (200, {"object": {"sha": "revert-commit"}}) if self.branch_exists else (404, {})
            ),
            f"{BASE}/pulls": (200, self.open_pulls),
            f"{BASE}/commits/{SHA}": (
                200, {"parents": [{"sha": p} for p in self.parents], "files": self.files}
            ),
            f"{BASE}/git/ref/heads/main": (200, {"object": {"sha": HEAD}}),
            f"{BASE}/git/commits/{HEAD}": (200, {"tree": {"sha": "head-tree"}}),
            f"{BASE}/git/commits/{PARENT}": (200, {"tree": {"sha": "parent-tree"}}),
            f"{BASE}/git/trees/head-tree": (200, self._tree(self.head_tree)),
            f"{BASE}/git/trees/parent-tree": (200, self._tree(self.parent_tree)),
        }
        status, body = routes[path]
        return httpx.Response(status, json=body)

    @staticmethod
    def _tree(entries):
        return {"truncated": False, "tree": [{"path": p, **e} for p, e in entries.items()]}


async def _run(fake: FakeGitHub) -> str:
    transport = httpx.MockTransport(fake)
    async with httpx.AsyncClient(transport=transport, base_url="https://api.github.com") as gh:
        return await open_live_pr(gh, owner_repo=REPO, sha=SHA, pr=PR, default_branch="main")


async def test_happy_path_builds_revert_on_head_and_opens_pr():
    fake = FakeGitHub()
    assert await _run(fake) == "https://github.com/o/r/pull/7"
    assert fake.posts["/git/trees"] == {
        "base_tree": "head-tree",
        "tree": [{"path": "app.py", "mode": "100644", "type": "blob", "sha": "old-app"}],
    }
    assert fake.posts["/git/commits"]["parents"] == [HEAD]
    assert f"This reverts commit {SHA}." in fake.posts["/git/commits"]["message"]
    assert fake.posts["/git/refs"] == {"ref": f"refs/heads/{PR['branch']}", "sha": "revert-commit"}
    assert fake.posts["/pulls"]["head"] == PR["branch"]
    assert fake.posts["/pulls"]["base"] == "main"


async def test_merge_commit_is_refused_before_any_write():
    fake = FakeGitHub(parents=(PARENT, "q" * 40))
    with pytest.raises(RevertRefused, match="merge commit"):
        await _run(fake)
    assert fake.posts == {}


async def test_file_changed_after_culprit_is_refused():
    fake = FakeGitHub(head_tree={"app.py": blob("even-newer-app")})
    with pytest.raises(RevertRefused, match="conflict: app.py"):
        await _run(fake)
    assert fake.posts == {}


async def test_existing_branch_with_open_pr_resumes_without_writing():
    fake = FakeGitHub(branch_exists=True, open_pulls=[{"html_url": "https://github.com/o/r/pull/3"}])
    assert await _run(fake) == "https://github.com/o/r/pull/3"
    assert fake.posts == {}


async def test_existing_branch_without_pr_only_opens_the_pr():
    fake = FakeGitHub(branch_exists=True)
    assert await _run(fake) == "https://github.com/o/r/pull/7"
    assert list(fake.posts) == ["/pulls"]


async def test_api_error_propagates():
    with pytest.raises(httpx.HTTPStatusError):
        await _run(FakeGitHub(fail="/git/trees"))


def test_revert_entries_cover_added_removed_and_renamed():
    files = [
        {"filename": "new.py", "status": "added", "sha": "n1"},
        {"filename": "gone.py", "status": "removed", "sha": "g0"},
        {"filename": "b.py", "status": "renamed", "sha": "b1", "previous_filename": "a.py"},
        {"filename": "run.sh", "status": "modified", "sha": "r1"},
    ]
    head = {"new.py": blob("n1"), "b.py": blob("b1"), "run.sh": blob("r1", "100755")}
    parent = {"gone.py": blob("g0"), "a.py": blob("a0"), "run.sh": blob("r0", "100755")}
    assert revert_entries(files, head, parent) == [
        {"path": "new.py", "mode": "100644", "type": "blob", "sha": None},
        {"path": "gone.py", "mode": "100644", "type": "blob", "sha": "g0"},
        {"path": "b.py", "mode": "100644", "type": "blob", "sha": None},
        {"path": "a.py", "mode": "100644", "type": "blob", "sha": "a0"},
        {"path": "run.sh", "mode": "100755", "type": "blob", "sha": "r0"},
    ]


@pytest.mark.parametrize(
    ("files", "head", "match"),
    [
        ([{"filename": "gone.py", "status": "removed", "sha": "g0"}], {"gone.py": blob("x")}, "re-added"),
        (
            [{"filename": "b.py", "status": "renamed", "sha": "b1", "previous_filename": "a.py"}],
            {"b.py": blob("b1"), "a.py": blob("x")},
            "re-created",
        ),
    ],
)
def test_revert_entries_refuse_later_changes(files, head, match):
    with pytest.raises(RevertRefused, match=match):
        revert_entries(files, head, {"gone.py": blob("g0"), "a.py": blob("a0")})
