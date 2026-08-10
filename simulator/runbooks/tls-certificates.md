---
slug: tls-certificates
title: TLS Certificate Runbook
service_tags: [checkout]
---

# TLS Certificate Runbook

## Expired certificate on ingress

TLSHandshakeErrors with client messages like "certificate has expired" mean the ingress cert
lapsed — this is an infrastructure expiry, not a code regression; recent commits are usually
irrelevant.

1. Confirm: `openssl s_client -connect checkout.example.com:443 -servername checkout.example.com \
   </dev/null 2>/dev/null | openssl x509 -noout -dates`.
2. cert-manager path: check the Certificate resource — `kubectl describe certificate checkout-tls`.
   A stuck renewal usually means the ACME challenge is failing; delete the CertificateRequest to
   force a retry.
3. Manual path: renew with the CA, update the TLS secret
   (`kubectl create secret tls checkout-tls --cert=... --key=... --dry-run=client -o yaml | kubectl apply -f -`),
   and restart the ingress controller.
4. Verify handshakes succeed and error rate drops to zero.

## Renewal automation

Certificates renew at 2/3 of lifetime via cert-manager. If a manual cert exists anywhere, add it
to the cert inventory with an expiry alert at 21 days.

## Monitoring

The blackbox exporter probes every public endpoint daily and exports
probe_ssl_earliest_cert_expiry; alert when under 21 days.
