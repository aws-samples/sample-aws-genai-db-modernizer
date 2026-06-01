# ADR-014: CI/CD Pipeline Architecture and Ephemeral Environments

> Note: This ADR documents CI/CD architecture decisions. Implementation details were sanitized before open-source release — the original pipeline was internal.

**Status:** Accepted
**Date:** 2026-02-16
**Decision Makers:** Database Modernizer Engineering Team

## Context

The Database Modernizer project uses CloudFormation for infrastructure and a CI/CD pipeline for automation. We needed a CI/CD strategy that:

1. Prevents broken infrastructure from reaching production
2. Lets developers validate their changes before merging
3. Keeps the feedback loop short
4. Doesn't require manual coordination between team members

The initial pipeline auto-deployed to production on every main merge with no gate, which was risky.

## Decision

We adopted a two-track pipeline with ephemeral environments and a promotion gate.

### Pipeline Architecture

**Feature branches** (`feat/*`, `feature/*`):

```
security → lint → test → deploy-feature (auto) → integration-test → [cleanup on delete]
```

**Main branch** (default):

```
security → lint → test → deploy-dev (auto) → integration-test → deploy-prod (manual approval)
```

### Key Design Choices

**1. Ephemeral stacks for feature branches**

Every feature branch automatically deploys its own CloudFormation stack (`modernizer-{branch-name}`). This proves that the infrastructure can be deployed from scratch, catching template errors, missing parameters, and resource conflicts before merge.

Stacks are cleaned up automatically when the branch is deleted (after MR merge) and have a 1-week TTL safety net via a pipeline-enforced auto-cleanup rule.

**2. Integration tests run against deployed stacks**

Integration tests in `tests/integration/` use boto3 to validate the live stack — checking status, outputs, and resource health. The same test code runs on both feature branches (against the ephemeral stack) and main (against the dev stack).

The `STACK_NAME` environment variable tells the tests which stack to target.

**3. Dev stack as a promotion gate**

On main, changes deploy to `modernizer-dev` first. Integration tests run against dev. Only after tests pass does the production deploy become available — and it requires manual approval via a pipeline promotion gate.

This ensures no one accidentally pushes broken infrastructure to production.

**4. Feature deploys are automatic, not manual**

We chose automatic deployment for feature branches because:

- Feature branches are short-lived by convention
- Developers need to know their changes work before the MR review
- Manual triggers add friction and are often forgotten
- The cost of ephemeral stacks is bounded by the 1-week auto-cleanup rule

### Stack Naming Convention

| Environment | Stack Name | Lifecycle |
|---|---|---|
| Feature | `modernizer-{sanitized-branch}` | Ephemeral, auto-cleanup |
| Dev | `modernizer-dev` | Persistent, updated on main merge |
| Production | `modernizer-prod` | Persistent, manual approval |

### Security Scanning Stages

All branches run the same security pipeline before any deployment:

- Gitleaks (secret detection)
- Semgrep (SAST for `src/` and `infrastructure/docker/`)
- cfn-nag (CloudFormation security)
- Checkov (CloudFormation and Dockerfile security)
- Bandit (Python security)

## Consequences

**Positive:**

- Developers get deployment validation on every push to a feature branch
- Production is protected by a two-stage gate (dev deploy + manual approval)
- Infrastructure-as-code changes are tested the same way as application code
- Same integration test code runs everywhere — no environment-specific test logic

**Negative:**

- Feature branch deploys cost money (NAT gateways, VPC resources)
- CloudFormation deploys take time (~5-10 min), slowing the feature branch pipeline
- Parallel feature branches may hit AWS resource limits in the same account

**Mitigations:**

- 1-week auto-cleanup limits cost exposure
- Branch deletion triggers immediate stack cleanup
- Resource limits can be addressed with AWS service quota increases or a dedicated CI account

## Alternatives Considered

**1. Test against dev stack from feature branches (no ephemeral deploy)**

Rejected because feature branches adding new resources would have tests that fail against the dev stack where those resources don't exist yet.

**2. Manual trigger for feature deploys**

Rejected because developers forget to trigger it, and the whole point is catching issues before merge.

**3. Separate AWS accounts per environment**

Deferred. Worth revisiting when the team grows, but adds complexity for a small team.

## Related Documents

- [ADR-012: CloudFormation over CDK](ADR-012-cloudformation-over-cdk.md)
- [ADR-013: Core Infrastructure Cost Optimization](ADR-013-core-infrastructure-cost-optimization.md)
- [Release Management Guide](../../RELEASE_MANAGEMENT.md)
