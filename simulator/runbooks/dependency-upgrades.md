---
slug: dependency-upgrades
title: Dependency Upgrade Runbook
service_tags: [orders, checkout]
---

# Dependency Upgrade Runbook

## Latency regression after a dependency bump

If p95 latency degrades after a lockfile change (HighLatency alert), suspect the HTTP client or
connection pooling layers first — behavior changes there rarely break tests but change performance.

1. Identify the bump: `git log -p poetry.lock | head -100` around the deploy time.
2. Pin back the suspect package to the previous version and redeploy the affected service.
3. Compare p95 before/after the pin to confirm the regression source.
4. Check upstream changelogs for connection reuse, keep-alive, or timeout default changes.
5. Re-attempt the upgrade only with a load-test comparison and the changelog reviewed.

## Safe upgrade procedure

Dependency bumps ship alone — never mixed with feature changes — so rollback is a one-line pin.
Upgrades to HTTP clients, database drivers, and serializers require a before/after load test in
staging.

## Auditing current pins

`uv tree` (or `poetry show --tree`) lists the resolved versions; the deploy that introduced a
version appears in `git log -S '<package>' poetry.lock`.
