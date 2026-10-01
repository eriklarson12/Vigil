"""Z-score detector math and the synthetic series generator (roadmap R7)."""

from datetime import UTC, datetime, timedelta

import pytest

from vigil.impact.anomaly import (
    METRICS,
    SPIKE_POINTS,
    detect,
    generate_series,
    synthesize_alert,
    zscores,
)
from vigil.impact.catalog import ServiceCatalog

END = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def _series(values: list[float]) -> list[tuple[datetime, float]]:
    """1-minute points ending at END."""
    n = len(values)
    return [(END - timedelta(minutes=n - 1 - i), v) for i, v in enumerate(values)]


def _calm(n: int = 200) -> list[float]:
    # Alternating 9/11: mean 10, sigma exactly 1.
    return [9.0 if i % 2 else 11.0 for i in range(n)]


def test_zscores_golden():
    assert zscores([9.0, 11.0], [10.0, 13.0, 7.0]) == pytest.approx([0.0, 3.0, -3.0], abs=1e-6)


def test_zscores_flat_baseline_is_zero():
    assert zscores([5.0] * 10, [5.0, 500.0]) == [0.0, 0.0]


def test_spike_detected_and_run_start_is_first_spiked_point():
    points = _series(_calm() + [20.0] * 5)
    detection = detect(points)
    assert detection is not None
    assert detection.run_start == points[-5][0]
    assert detection.z == pytest.approx(10.0, abs=1e-6)
    assert detection.mean == pytest.approx(10.0, abs=1e-6)


def test_calm_series_not_detected():
    assert detect(_series(_calm())) is None


def test_exactly_three_points_over_threshold_fires():
    assert detect(_series(_calm() + [13.5] * 3)) is not None


def test_two_points_over_threshold_does_not_fire():
    assert detect(_series(_calm() + [11.0, 13.5, 13.5])) is None


def test_point_at_threshold_does_not_fire():
    # z must exceed 3; exactly 3 sigma is not anomalous.
    assert detect(_series(_calm() + [13.0] * 3)) is None


def test_flat_series_never_fires():
    assert detect(_series([5.0] * 200 + [500.0] * 3)) is None


def test_short_baseline_does_not_fire():
    assert detect(_series(_calm(20) + [50.0] * 3)) is None


def test_synthesize_alert_shape():
    points = _series(_calm() + [20.0] * 3)
    alert = synthesize_alert("checkout", "error_rate", detect(points))
    assert alert["fingerprint"] == "anom-checkout-error_rate"
    assert alert["status"] == "firing"
    assert alert["startsAt"] == points[-3][0].isoformat()
    assert alert["labels"] == {
        "alertname": "AnomalyDetected",
        "service": "checkout",
        "metric": "error_rate",
        "severity": "warning",
        "detector": "vigil",
    }
    assert "10.0 sigma" in alert["annotations"]["summary"]


def test_generate_series_is_deterministic():
    a = generate_series("checkout", 1200, END, 180)
    assert a == generate_series("checkout", 1200, END, 180)
    assert a != generate_series("orders", 1200, END, 180)
    assert len(a) == 180 * len(METRICS)


def _metric(rows, metric: str) -> list[tuple[datetime, float]]:
    return [(ts, v) for _, m, ts, v in rows if m == metric]


@pytest.mark.parametrize("hours", range(0, 24, 3))
def test_generated_series_calm_vs_spike(hours):
    catalog = ServiceCatalog.load("services.yaml")
    end = END + timedelta(hours=hours)
    for name, cfg in catalog.services.items():
        calm = generate_series(name, cfg["baseline_rpm"], end, 180)
        for metric in METRICS:
            assert detect(_metric(calm, metric)) is None, (name, metric)
        spiked = _metric(generate_series(name, cfg["baseline_rpm"], end, 180, spike=True), "error_rate")
        detection = detect(spiked)
        assert detection is not None, name
        assert detection.run_start == spiked[-SPIKE_POINTS][0]
