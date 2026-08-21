---
slug: auth-sessions
title: Auth & Session Runbook
service_tags: [auth, checkout]
---

# Auth & Session Runbook

## Token validation failures after a key rotation

TokenValidationFailures with verifier errors like "unknown key id" mean the signing key moved
before the published key set caught up. Sessions issued under the old key keep working, new ones
do not, so the failure rate climbs as old tokens expire.

1. Compare the active signing kid with the published set:
   `kubectl exec deploy/auth -- python -m auth.tools.keys --show` against
   `curl -s https://auth.example.com/.well-known/jwks.json | jq '.keys[].kid'`.
2. If the previous kid is missing from the JWKS, revert the rotation commit and let CD redeploy.
   Do not hand-edit the secret; the repository is the source of truth for the key set.
3. Verify recovery: `python -m auth.tools.verify --fresh-token` against a cold verifier, then watch
   the validation success rate return to baseline.
4. Check downstream: checkout depends on auth, so sign-in and session-refresh errors there should
   clear within one JWKS cache lifetime (10 minutes).

Rotation is a two-deploy operation. Publish the new key alongside the old one, wait out the cache
lifetime, then switch the signing key. Dropping the old key in the same change is the usual cause
of this alert.

## Session and cache issues

Auth caches verification keys and session lookups in-process on a 10 minute TTL. After any key or
config change, a stale cache can keep a subset of pods failing after the fix ships: confirm with
`kubectl logs deploy/auth | jq 'select(.event=="jwks_cache_refresh")'` and restart the lagging pods
rather than waiting the TTL out during an incident.

## Dashboards and logs

- Validation success rate and issuance volume: Grafana → Auth Overview.
- Rejection reasons: `kubectl logs deploy/auth | jq 'select(.event=="token_rejected") | .reason'`.
- Blast radius: checkout is the only user-facing dependent; its sign-in error panel is the fastest
  confirmation that auth recovered.
