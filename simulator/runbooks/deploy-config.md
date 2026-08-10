---
slug: deploy-config
title: Deploy & Config Runbook
service_tags: [checkout, orders, inventory]
---

# Deploy & Config Runbook

## CrashLoopBackOff after a config rollout

Pods crash-looping immediately after a config change means the service cannot parse or apply the
new configuration — the fix is almost always reverting the config, not the code.

1. Read the crash reason: `kubectl logs deploy/<service> --previous`. Parse errors like
   "invalid literal for int()" name the exact bad key.
2. Diff the config: `git log -p --follow config/<service>.yaml` — one-line typos (a stray unit
   suffix, wrong indentation, quoted number) are the classic culprits.
3. Revert the config commit and redeploy. Do not hand-edit in the cluster.
4. Verify pods pass readiness and the CrashLoopBackOff clears.

## Validating configuration

Every config/*.yaml has a schema in libs/config_schema. Run `python -m libs.config_schema.validate
config/` locally and in CI; a config rollout should be impossible without a green validation run.

## Rollback procedure

`kubectl rollout undo deploy/<service>` reverts the container image and mounted config together.
For config-only changes, reverting the git commit and letting CD redeploy is preferred so the
repository stays the source of truth.
