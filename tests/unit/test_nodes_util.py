import pytest

from vigil.graph.nodes_util import NodeFailed, degrading


def _failing(times: int):
    calls = {"n": 0}

    async def node(state):
        calls["n"] += 1
        if calls["n"] <= times:
            raise RuntimeError("boom")
        return {"ok": True}

    return node, calls


async def test_degrades_into_errors_after_retries():
    node, calls = _failing(times=5)
    out = await degrading("n", retries=2)(node)({})
    assert out == {"errors": {"n": "RuntimeError: boom"}}
    assert calls["n"] == 3


async def test_retry_recovers_without_an_error():
    node, calls = _failing(times=1)
    assert await degrading("n", retries=2)(node)({}) == {"ok": True}
    assert calls["n"] == 2


async def test_raises_surfaces_node_failed_after_retries():
    node, calls = _failing(times=5)
    with pytest.raises(NodeFailed, match="n: RuntimeError: boom"):
        await degrading("n", retries=2, raises=True)(node)({})
    assert calls["n"] == 3
