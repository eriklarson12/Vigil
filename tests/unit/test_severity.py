from datetime import timedelta

import pytest

from tests.conftest import ALERT_TIME
from vigil.impact.severity import classify

NOW = ALERT_TIME + timedelta(minutes=10)


def _sev(catalog, service, label):
    return classify(
        catalog=catalog, service_name=service, alert_severity_label=label,
        starts_at=ALERT_TIME, now=NOW,
    )


@pytest.mark.parametrize(
    ("service", "label", "expected"),
    [
        ("checkout", "critical", "SEV1"),   # rule 1: tier0 + user-facing + critical
        ("payments-db", "critical", "SEV1"),  # rule 2: dependency of >=2 user-facing
        ("inventory", "critical", "SEV1"),  # rule 2: checkout+orders depend on it
        ("checkout", "warning", "SEV2"),    # rule 3: tier0 + warning
        ("orders", "critical", "SEV2"),     # rule 3: tier1 user-facing (rule 2 count=0)
        ("orders", "warning", "SEV2"),
        ("auth", "critical", "SEV3"),       # rule 4: non-user-facing critical, 1 dependent
        ("auth", "warning", "SEV2"),        # rule 3: tier0 + warning
        ("inventory", "warning", "SEV4"),   # tier1, not user-facing, not critical -> default
        (None, "critical", "SEV4"),         # unknown service
        ("nonexistent", "critical", "SEV4"),
    ],
)
def test_rubric(catalog, service, label, expected):
    assert _sev(catalog, service, label)["severity"] == expected


def test_unknown_service_flagged(catalog):
    result = _sev(catalog, "nonexistent", "critical")
    assert result["unknown_service"] is True
    assert result["est_requests_affected"] is None


def test_quantitative_estimate(catalog):
    result = _sev(catalog, "checkout", "critical")
    assert result["minutes_open"] == 10
    assert result["est_requests_affected"] == 1200 * 10


def test_blast_radius_bfs(catalog):
    result = _sev(catalog, "payments-db", "critical")
    # checkout & orders are user-facing dependents (directly and via inventory)
    assert result["blast_radius"] == ["checkout", "orders"]
