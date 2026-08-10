---
slug: checkout-service
title: Checkout Service Runbook
service_tags: [checkout, payments]
---

# Checkout Service Runbook

## High error rate after a deploy

If checkout 5xx error rate spikes shortly after a deploy (HighErrorRate alert), the fastest safe
move is a rollback — do not debug forward while revenue is impacted.

1. Confirm the correlation: compare the error-rate inflection with the deploy timestamp
   (`kubectl rollout history deploy/checkout`).
2. Roll back: `kubectl rollout undo deploy/checkout`. Rollback completes in ~90 seconds.
3. Verify the 5xx rate returns to baseline (<0.5%) on the checkout dashboard.
4. Announce the rollback in #incidents and link the suspect commit for follow-up.

Common culprits: handler refactors that skip payment intent state validation, gateway client
changes, and serializer changes on the pay endpoint.

## Payment intent failures

Errors mentioning "payment intent state invalid" mean an intent reached the gateway in a state the
gateway rejects (not REQUIRES_CONFIRMATION). Check whether validation was removed or bypassed in a
recent change to services/checkout/handlers. Intents stuck in a bad state can be requeued with
`python -m checkout.tools.requeue_intents --since 1h` after the fix ships.

## Dashboards and logs

- Error rate + latency: Grafana → Checkout Overview.
- Structured logs: `kubectl logs deploy/checkout | jq 'select(.level=="error")'`.
- Gateway-side failures appear in the payments provider dashboard with matching intent ids.
