"""R9 live issue filing against a fake GitHub REST API (httpx.MockTransport)."""

import json

import httpx
import pytest

from vigil.graph.action_items import LABEL, file_live_issues, marker

REPO = "o/r"
BASE = f"/repos/{REPO}"
INCIDENT = "0b6f2a4e-1111-4000-8000-000000000001"


def _issues(*indices):
    return {
        i: {"title": f"t{i}", "body": f"b{i}\n{marker(INCIDENT, i)}", "marker": marker(INCIDENT, i)}
        for i in indices
    }


class FakeGitHub:
    def __init__(self, *, label_status=201, existing=(), fail_on_create=None):
        self.label_status = label_status
        self.existing = list(existing)
        self.fail_on_create = fail_on_create
        self.created: list[dict] = []
        self.label_posts = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if (method, path) == ("POST", f"{BASE}/labels"):
            self.label_posts += 1
            return httpx.Response(self.label_status, json={})
        if (method, path) == ("GET", f"{BASE}/issues"):
            assert request.url.params["labels"] == LABEL
            assert request.url.params["state"] == "all"
            return httpx.Response(200, json=self.existing)
        if (method, path) == ("POST", f"{BASE}/issues"):
            body = json.loads(request.content)
            if self.fail_on_create is not None and len(self.created) == self.fail_on_create:
                return httpx.Response(502, json={"message": "bad gateway"})
            self.created.append(body)
            return httpx.Response(201, json={"html_url": f"https://github.com/o/r/issues/{len(self.created)}"})
        raise AssertionError(f"unexpected {method} {path}")


async def _run(fake: FakeGitHub, issues) -> dict[int, str]:
    filed: dict[int, str] = {}

    async def on_filed(index, url):
        filed[index] = url

    transport = httpx.MockTransport(fake)
    async with httpx.AsyncClient(transport=transport, base_url="https://api.github.com") as gh:
        try:
            await file_live_issues(gh, owner_repo=REPO, issues=issues, on_filed=on_filed)
        finally:
            fake.filed = filed
    return filed


async def test_happy_path_files_one_labeled_issue_per_item():
    fake = FakeGitHub()
    filed = await _run(fake, _issues(0, 1))
    assert filed == {0: "https://github.com/o/r/issues/1", 1: "https://github.com/o/r/issues/2"}
    assert fake.label_posts == 1
    assert [c["labels"] for c in fake.created] == [[LABEL], [LABEL]]
    assert fake.created[0]["title"] == "t0"


async def test_existing_label_is_fine():
    fake = FakeGitHub(label_status=422)
    assert len(await _run(fake, _issues(0))) == 1


async def test_label_error_other_than_422_raises():
    with pytest.raises(httpx.HTTPStatusError):
        await _run(FakeGitHub(label_status=403), _issues(0))


async def test_marked_issue_is_adopted_not_filed_again():
    existing = [{"html_url": "https://github.com/o/r/issues/9", "body": f"old\n{marker(INCIDENT, 1)}"}]
    fake = FakeGitHub(existing=existing)
    filed = await _run(fake, _issues(0, 1))
    assert filed[1] == "https://github.com/o/r/issues/9"
    assert [c["title"] for c in fake.created] == ["t0"]


async def test_only_pending_indices_are_filed():
    fake = FakeGitHub()
    filed = await _run(fake, _issues(2))
    assert list(filed) == [2]
    assert len(fake.created) == 1


async def test_error_mid_way_keeps_what_was_filed():
    fake = FakeGitHub(fail_on_create=1)
    with pytest.raises(httpx.HTTPStatusError):
        await _run(fake, _issues(0, 1, 2))
    assert fake.filed == {0: "https://github.com/o/r/issues/1"}
