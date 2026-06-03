# ADR-015: DNS Naming Convention for Multi-Environment Subdomains

> Note: Domain names in this ADR use example.com as a placeholder. Actual deployment domains were sanitized before open-source release.

**Status:** Accepted
**Date:** 2026-02-17
**Decision Makers:** Database Modernizer Assessment Engineering Team

## Context

The project uses a single ACM wildcard certificate (`*.modernizer.example.com`) for HTTPS across all environments. We needed a subdomain naming convention that works with this certificate for dev, prod, and ephemeral feature branch environments.

## Decision

Use single-level subdomains with hyphenated environment names instead of nested subdomains.

**Pattern:** `{service}-{environment}.modernizer.example.com`

**Examples:**

| Environment | URL |
|---|---|
| Dev API | `api-dev.modernizer.example.com` |
| Prod API | `api-prod.modernizer.example.com` |
| Feature API | `api-feat-base-infra.modernizer.example.com` |
| Dev UI | `app-dev.modernizer.example.com` |
| Prod UI | `app-prod.modernizer.example.com` |

Note: `api-{env}` and `app-{env}` resolve to the same ALB. Path-based routing directs `/api/*` to the API service and `/*` to the UI service.

## Alternatives Considered

**1. Nested subdomains: `api.prod.modernizer.example.com`**

Rejected. ACM wildcard certs only match one subdomain level. `*.modernizer...` covers `api.modernizer...` but not `api.prod.modernizer...`. Would require additional SANs (`*.prod.modernizer...`, `*.dev.modernizer...`) or multiple certs.

**2. Separate certs per environment with nested subdomains**

Rejected. Adds operational complexity — each new environment tier needs a new SAN or cert, plus DNS validation. Doesn't scale for ephemeral feature branches.

**3. Partial wildcard records: `*-dev.modernizer...`**

Not possible. Route 53 only supports full label wildcards (`*.domain`), not partial patterns.

## Consequences

**Positive:**

- Single wildcard cert covers all environments and services
- No cert changes needed when adding new services or environments
- Clean, predictable URLs
- Works with ephemeral feature branch environments

**Negative:**

- Service and environment are encoded in a single label (`api-prod`) rather than hierarchically (`api.prod`)
- Slightly less intuitive than nested subdomains

## Related Documents

- [ADR-014: CI/CD Pipeline and Ephemeral Environments](ADR-014-cicd-pipeline-and-ephemeral-environments.md)
