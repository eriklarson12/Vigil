---
slug: inventory-service
title: Inventory Service Runbook
service_tags: [inventory]
---

# Inventory Service Runbook

## Memory growth and OOM restarts

HighMemory alerts with linear RSS growth and periodic OOMKilled events almost always mean an
unbounded in-process cache or a leaked collection.

1. Grab a heap profile: `kubectl exec deploy/inventory -- python -m memray attach --live $(pgrep -f inventory)`.
2. Identify the dominating type. SKU/lookup cache entries point at services/inventory/cache.py.
3. Mitigate now: restart pods off-peak (`kubectl rollout restart deploy/inventory`) to reset RSS.
4. Fix: bound the cache (LRU with maxsize) or revert the change that unbounded it.
5. Prevention: alert on memory growth rate at 70% so leaks surface before OOM.

## Cache tuning

The SKU cache should be an LRU capped at 10,000 entries (~80MB). Hit rate below 85% is a sizing
problem, not a reason to unbound it. Change maxsize via INVENTORY_CACHE_SIZE and redeploy.

## Restart error bursts

Each inventory restart drops in-flight requests from checkout and orders; expect a <30s burst of
downstream errors. If bursts exceed 60s, verify readiness probes gate traffic until the cache is
warmed.
