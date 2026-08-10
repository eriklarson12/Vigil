"""Deterministic severity classification (spec §8). The LLM never picks the SEV."""

from datetime import UTC, datetime
from typing import Any

from vigil.impact.catalog import ServiceCatalog


def classify(
    *,
    catalog: ServiceCatalog,
    service_name: str | None,
    alert_severity_label: str | None,
    starts_at: datetime,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    cfg = catalog.get(service_name)
    label = (alert_severity_label or "").lower()
    critical = label == "critical"
    warning = label == "warning"

    if cfg is None:
        severity, rule = "SEV4", "unknown-service"
        blast_radius: list[str] = []
        est_affected = None
        baseline = None
    else:
        tier = cfg.get("tier", 3)
        user_facing = bool(cfg.get("user_facing"))
        dep_count = catalog.dependency_of_user_facing_count(cfg["name"])
        blast_radius = catalog.user_facing_dependents(cfg["name"])
        baseline = cfg.get("baseline_rpm")

        if tier == 0 and user_facing and critical:
            severity, rule = "SEV1", "tier0-user-facing-critical"
        elif dep_count >= 2 and critical:
            severity, rule = "SEV1", "critical-shared-dependency"
        elif (tier <= 1 and user_facing) or (tier == 0 and warning):
            severity, rule = "SEV2", "tier1-user-facing-or-tier0-warning"
        elif tier == 2 or (not user_facing and critical):
            severity, rule = "SEV3", "tier2-or-internal-critical"
        else:
            severity, rule = "SEV4", "default"

        minutes_open = max(1, int((now - starts_at).total_seconds() // 60))
        est_affected = baseline * minutes_open if baseline else None

    return {
        "severity": severity,
        "rule_matched": rule,
        "unknown_service": cfg is None,
        "baseline_rpm": baseline,
        "minutes_open": max(1, int((now - starts_at).total_seconds() // 60)),
        "est_requests_affected": est_affected,
        "blast_radius": blast_radius,
    }
