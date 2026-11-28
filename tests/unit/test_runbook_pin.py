"""An alert's `runbook_url` pins its runbook first (roadmap R20).

Alertmanager rules carry the runbook link themselves. When its last path segment is a
known slug, retrieval has nothing to guess for that runbook; it fills the other slots.
"""

import pytest

from vigil.rag.retrieve import MAX_RUNBOOKS, TOP_CHUNKS, runbook_slug_from_url, select_chunks


def _row(chunk_id, runbook, final=0.03):
    return {
        "id": chunk_id, "runbook_id": f"rb-{runbook}", "runbook_slug": runbook,
        "runbook_title": runbook.title(), "heading_path": f"{runbook} > {chunk_id}",
        "content": f"content {chunk_id}", "final": final,
    }


def _pinned(chunk_id, runbook):
    row = _row(chunk_id, runbook)
    del row["final"]
    return row


ROWS = [_row("c1", "checkout"), _row("c2", "checkout"), _row("o1", "orders"),
        _row("t1", "tls"), _row("c3", "checkout")]


@pytest.mark.parametrize(
    ("url", "slug"),
    [
        ("https://runbooks.example.com/tls-certificates", "tls-certificates"),
        ("https://runbooks.example.com/tls-certificates/", "tls-certificates"),
        ("https://wiki.example.com/ops/tls-certificates?tab=1#renew", "tls-certificates"),
        ("tls-certificates", "tls-certificates"),
        ("https://runbooks.example.com/", None),
        ("", None),
        ("   ", None),
        (None, None),
        (42, None),
    ],
)
def test_slug_is_the_last_path_segment(url, slug):
    assert runbook_slug_from_url(url) == slug


def test_without_a_pin_the_caps_apply_to_retrieval_alone():
    picked = select_chunks(ROWS)
    assert [c["heading_path"] for c in picked] == [
        "checkout > c1", "checkout > c2", "orders > o1", "checkout > c3"
    ]
    assert {c["source"] for c in picked} == {"retrieval"}
    assert len({c["runbook_slug"] for c in picked}) <= MAX_RUNBOOKS


def test_the_pinned_chunk_leads_and_takes_a_runbook_slot():
    picked = select_chunks(ROWS, _pinned("t9", "tls"))
    assert picked[0] == {
        "runbook_slug": "tls", "runbook_title": "Tls", "heading_path": "tls > t9",
        "content": "content t9", "rrf_score": None, "source": "annotation",
    }
    # tls takes one of the two runbook slots, so orders is dropped.
    assert [c["heading_path"] for c in picked[1:]] == [
        "checkout > c1", "checkout > c2", "tls > t1"
    ]
    assert len(picked) == TOP_CHUNKS


def test_a_pinned_chunk_that_also_ranked_appears_once():
    picked = select_chunks(ROWS, _pinned("o1", "orders"))
    paths = [c["heading_path"] for c in picked]
    assert paths.count("orders > o1") == 1
    assert picked[0]["source"] == "annotation"
