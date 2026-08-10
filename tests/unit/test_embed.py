import math

from vigil.rag.embed import FakeEmbedder, l2_normalize, vec_literal
from vigil.rag.retrieve import build_query_text


async def test_fake_embedder_deterministic_and_normalized():
    embedder = FakeEmbedder(768)
    [a1] = await embedder.embed(["checkout high error rate"])
    [a2] = await embedder.embed(["checkout high error rate"])
    assert a1 == a2
    assert math.isclose(sum(x * x for x in a1), 1.0, rel_tol=1e-9)


async def test_fake_embedder_related_texts_are_closer():
    embedder = FakeEmbedder(768)
    [a, b, c] = await embedder.embed([
        "checkout error rate deploy",
        "checkout error rate spike deploy rollback",
        "certificate expired tls handshake",
    ])
    cos_ab = sum(x * y for x, y in zip(a, b, strict=True))
    cos_ac = sum(x * y for x, y in zip(a, c, strict=True))
    assert cos_ab > cos_ac


def test_l2_normalize_zero_vector_safe():
    assert l2_normalize([0.0, 0.0]) == [0.0, 0.0]


def test_vec_literal_format():
    lit = vec_literal([0.5, -0.25])
    assert lit.startswith("[") and lit.endswith("]")
    assert "0.5000000" in lit


def test_query_text_from_alert():
    alert = {
        "labels": {"alertname": "HighErrorRate", "severity": "critical", "service": "checkout"},
        "annotations": {"summary": "errors up", "description": "5xx spike"},
    }
    q = build_query_text(alert)
    assert "HighErrorRate" in q and "checkout" in q and "5xx spike" in q
