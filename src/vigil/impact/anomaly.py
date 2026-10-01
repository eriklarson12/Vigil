"""Proactive anomaly detection on synthetic metrics (roadmap R7).

Deterministic z-score over a trailing baseline. No LLM, no I/O: the runner
fetches points and ingests the alert this module synthesizes.
"""

import hashlib
import math
import random
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta

METRICS = ("rpm", "error_rate")
Z_THRESHOLD = 3.0
CONSECUTIVE = 3
BASELINE_WINDOW = timedelta(hours=3)
# The spec said 5 min; that left 10 of the 15 spiked points in the baseline and
# inflated sigma until the injected spike scored z ~3.3. 15 min keeps the run out.
EXCLUDE_RECENT = timedelta(minutes=15)
MIN_BASELINE_POINTS = 30
SPIKE_FACTOR = 8.0
SPIKE_POINTS = 15
BASE_ERROR_RATE = 0.002


@dataclass(frozen=True)
class Detection:
    run_start: datetime
    z: float
    value: float
    mean: float


def zscores(baseline: list[float], points: list[float]) -> list[float]:
    if len(baseline) < 2:
        return [0.0] * len(points)
    mean = statistics.fmean(baseline)
    sigma = statistics.pstdev(baseline, mean)
    if sigma == 0:
        return [0.0] * len(points)
    return [(p - mean) / sigma for p in points]


def detect(points: list[tuple[datetime, float]]) -> Detection | None:
    """Fire when the last CONSECUTIVE points all exceed Z_THRESHOLD.

    `points` is sorted by ts. The baseline is the BASELINE_WINDOW ending
    EXCLUDE_RECENT before the newest point. `run_start` walks back to the
    first point of the anomalous run, so it stays put across ticks and the
    alert's (fingerprint, starts_at) dedups.
    """
    if len(points) < CONSECUTIVE:
        return None
    latest = points[-1][0]
    lo, hi = latest - EXCLUDE_RECENT - BASELINE_WINDOW, latest - EXCLUDE_RECENT
    baseline = [v for ts, v in points if lo <= ts < hi]
    if len(baseline) < MIN_BASELINE_POINTS:
        return None
    z = zscores(baseline, [v for _, v in points])
    if not all(score > Z_THRESHOLD for score in z[-CONSECUTIVE:]):
        return None
    start = len(points) - CONSECUTIVE
    while start > 0 and z[start - 1] > Z_THRESHOLD:
        start -= 1
    return Detection(
        run_start=points[start][0],
        z=z[-1],
        value=points[-1][1],
        mean=statistics.fmean(baseline),
    )


def synthesize_alert(service: str, metric: str, detection: Detection) -> dict:
    """One Alertmanager v4 alert, shaped like a webhook alert so ingest treats it the same."""
    return {
        "status": "firing",
        "labels": {
            "alertname": "AnomalyDetected",
            "service": service,
            "metric": metric,
            "severity": "warning",
            "detector": "vigil",
        },
        "annotations": {
            "summary": f"{metric} on {service} is {detection.z:.1f} sigma above its 3h baseline",
            "description": (
                f"{metric} reached {detection.value:.4g} against a baseline mean of "
                f"{detection.mean:.4g}; {CONSECUTIVE}+ consecutive points over z={Z_THRESHOLD:g}."
            ),
        },
        "startsAt": detection.run_start.isoformat(),
        "endsAt": "0001-01-01T00:00:00Z",
        "fingerprint": f"anom-{service}-{metric}",
    }


def _seed(service: str) -> int:
    # hash() is salted per process; a digest keeps the series reproducible.
    return int.from_bytes(hashlib.sha256(service.encode()).digest()[:8], "big")


def generate_series(
    service: str, baseline_rpm: float, end: datetime, minutes: int, spike: bool = False
) -> list[tuple[str, str, datetime, float]]:
    """Plausible 1-minute rpm and error_rate series ending at `end` (rows for metric_points)."""
    rng = random.Random(_seed(service))
    end = end.replace(second=0, microsecond=0)
    rows: list[tuple[str, str, datetime, float]] = []
    for i in range(minutes):
        ts = end - timedelta(minutes=minutes - 1 - i)
        day_phase = 2 * math.pi * (ts.hour * 60 + ts.minute) / 1440
        rpm = baseline_rpm * (1 + 0.1 * math.sin(day_phase)) + rng.gauss(0, 0.03 * baseline_rpm)
        error_rate = max(0.0, rng.gauss(BASE_ERROR_RATE, BASE_ERROR_RATE * 0.1))
        if spike and i >= minutes - SPIKE_POINTS:
            error_rate *= SPIKE_FACTOR
        rows.append((service, "rpm", ts, max(0.0, rpm)))
        rows.append((service, "error_rate", ts, error_rate))
    return rows
