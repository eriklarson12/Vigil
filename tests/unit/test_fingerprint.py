from vigil.ingest.fingerprint import alert_fingerprint


def test_alertmanager_fingerprint_passthrough():
    assert alert_fingerprint({"fingerprint": "abc123", "labels": {"a": "1"}}) == "abc123"


def test_fallback_hash_is_deterministic_and_order_invariant():
    a = alert_fingerprint({"labels": {"alertname": "X", "service": "checkout"}})
    b = alert_fingerprint({"labels": {"service": "checkout", "alertname": "X"}})
    assert a == b
    assert len(a) == 16


def test_fallback_hash_differs_by_labels():
    a = alert_fingerprint({"labels": {"alertname": "X", "service": "checkout"}})
    b = alert_fingerprint({"labels": {"alertname": "X", "service": "orders"}})
    assert a != b
