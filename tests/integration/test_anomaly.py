"""Anomaly detection feeds the same ingest path as the webhook (roadmap R7).

Needs Postgres:  docker compose up -d db && uv run pytest -m integration
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from vigil.impact.anomaly import generate_series

pytestmark = pytest.mark.integration

TOKEN = {"Authorization": "Bearer dev-token"}


async def _write(deps, rows) -> None:
    async with deps.pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.executemany(
                "INSERT INTO metric_points (service, metric, ts, value) VALUES (%s, %s, %s, %s)"
                " ON CONFLICT DO NOTHING",
                rows,
            )


async def _seed(deps, spike: str | None = "checkout") -> None:
    now = datetime.now(UTC)
    for name, cfg in deps.catalog.services.items():
        await _write(deps, generate_series(name, cfg["baseline_rpm"], now, 180, spike=name == spike))


async def _scalar(deps, sql: str, *params) -> Any:
    async with deps.pool.connection() as conn:
        cur = await conn.execute(sql, params)
        return (await cur.fetchone())[0]


async def _close_all(deps) -> None:
    # An open checkout incident on the shared local db swallows the next demo alert (Rule 2).
    async with deps.pool.connection() as conn:
        await conn.execute("UPDATE incidents SET status = 'postmortem_done' WHERE status = 'open'")


async def test_spike_opens_incident_and_posts_brief_once(client, monkeypatch):
    c, deps = client
    monkeypatch.setattr(deps.settings, "anomaly_detection", "on")
    await _seed(deps)
    try:
        resp = await c.post("/internal/resume", headers=TOKEN)
        assert resp.status_code == 200
        assert resp.json()["anomalies_detected"] == 1

        alert_count = "SELECT count(*) FROM alerts WHERE alert_name = 'AnomalyDetected'"
        assert await _scalar(deps, alert_count) == 1
        assert await _scalar(
            deps,
            "SELECT count(*) FROM incidents WHERE service = 'checkout'"
            " AND title = 'AnomalyDetected on checkout' AND slack_message_ts IS NOT NULL",
        ) == 1
        assert await _scalar(
            deps, "SELECT fingerprint FROM alerts WHERE alert_name = 'AnomalyDetected'"
        ) == "anom-checkout-error_rate"

        second = await c.post("/internal/resume", headers=TOKEN)
        assert second.json()["anomalies_detected"] == 0
        assert await _scalar(deps, alert_count) == 1
        assert await _scalar(deps, "SELECT count(*) FROM incidents") == 1
    finally:
        await _close_all(deps)


async def test_detection_off_creates_nothing(client):
    c, deps = client
    await _seed(deps)
    resp = await c.post("/internal/resume", headers=TOKEN)
    assert resp.json()["anomalies_detected"] == 0
    assert await _scalar(deps, "SELECT count(*) FROM incidents") == 0


async def test_stale_spike_does_not_alert(client, monkeypatch):
    c, deps = client
    monkeypatch.setattr(deps.settings, "anomaly_detection", "on")
    cfg = deps.catalog.services["checkout"]
    old = datetime.now(UTC) - timedelta(hours=1)
    await _write(deps, generate_series("checkout", cfg["baseline_rpm"], old, 180, spike=True))
    resp = await c.post("/internal/resume", headers=TOKEN)
    assert resp.json()["anomalies_detected"] == 0


async def test_prune_drops_points_older_than_24h(client):
    _, deps = client
    now = datetime.now(UTC)
    await _write(deps, [("checkout", "rpm", now - timedelta(hours=25), 1.0),
                        ("checkout", "rpm", now - timedelta(hours=1), 2.0)])
    await deps.runner.prune()
    assert await _scalar(deps, "SELECT array_agg(value) FROM metric_points") == [2.0]


async def test_metrics_endpoint(client):
    c, deps = client
    await _seed(deps, spike=None)
    resp = await c.get("/api/metrics/checkout")
    assert resp.status_code == 200
    body = resp.json()
    assert body["service"] == "checkout"
    assert len(body["metrics"]["rpm"]) == 180
    assert len(body["metrics"]["error_rate"]) == 180
    assert (await c.get("/api/metrics/nope")).status_code == 404
