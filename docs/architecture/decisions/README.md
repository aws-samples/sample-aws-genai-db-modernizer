# Architecture decision records

Each file records one decision: the context, the choice and the consequences. ADRs stay in place and keep their numbers; a decision that was replaced, retired or never built says so in its status line and in a banner under its title, and this index groups them. Decision text is history and is not edited when a later ADR changes it.

Where a page and an ADR disagree, the code and the current pages (the [high-level design](../high-level-design.md), the [guides](../../guides/README.md)) win. The routing model lives in one place, [ADR-031](ADR-031-query-first-routing-and-engine-rule-catalog.md).

Two files are numbered ADR-028: the Aurora schema-design agents and the customer assignment-review gate. Links and prose name them by title ("ADR-028 (customer assignment review gate)"). They are not renumbered, because that would break every link to them.

## Active

| ADR | Title | Notes |
|---|---|---|
| [ADR-002](ADR-002-structured-output-and-validation.md) | Structured output with Pydantic models | |
| [ADR-008](ADR-008-contract-versioning.md) | Contract versioning strategy | Decision 4 amended: readers accept any MINOR of their MAJOR |
| [ADR-011](ADR-011-monorepo-structure.md) | Monorepo structure | |
| [ADR-012](ADR-012-cloudformation-over-cdk.md) | CloudFormation over CDK | Applies to the templates that remain under `infrastructure/cloudformation/` |
| [ADR-022](ADR-022-documentdb-cluster-sizing.md) | DocumentDB cluster sizing for load tests | Accepted; implemented in `src/agents/load_test/documentdb/sizing.py` |
| [ADR-023](ADR-023-context-graph-layer.md) | Context graph layer | Experimental; supersedes ADR-019 |
| [ADR-024](ADR-024-consolidate-deterministic-analysis-fleet.md) | Consolidate the deterministic analysis fleet | |
| [ADR-025](ADR-025-consolidate-deterministic-core-agent.md) | Consolidate the deterministic core into one agent | |
| [ADR-027](ADR-027-consolidate-schema-design-fleet.md) | Consolidate the schema-design fleet | |
| [ADR-028 (gate)](ADR-028-customer-assignment-review-gate.md) | Customer assignment-review gate before schema design | The one human gate |
| [ADR-029](ADR-029-assignment-reentry-and-feasibility-review.md) | Assignment re-entry and post-gate feasibility review | |
| [ADR-030](ADR-030-assessment-quality-benchmark.md) | Assessment quality benchmark | |
| [ADR-031](ADR-031-query-first-routing-and-engine-rule-catalog.md) | Query-first routing model and engine rule catalog | The routing model |
| [ADR-032](ADR-032-one-facts-layer-renderers-only-format.md) | One facts layer in synthesis; renderers only format | |

## Partly superseded

The decision still holds in part. The status line names what was replaced.

| ADR | Title | Replaced by |
|---|---|---|
| [ADR-004](ADR-004-rds-tools-and-aws-integration.md) | RDS tools and AWS integration | The tool names in Decisions 1 and 3 differ from the code |
| [ADR-006](ADR-006-analysis-agent-patterns.md) | Analysis agent architecture | ADR-024 and ADR-031 Decision 8 |
| [ADR-009](ADR-009-testing-infrastructure.md) | Testing infrastructure and strategies | The tiers now live in `ci/README.md` and AGENTS.md |
| [ADR-010](ADR-010-release-management.md) | Release management and version control | CONTRIBUTING.md "Releases": every tag is on `main`, no release or hotfix branches |
| [ADR-016](ADR-016-compute-and-orchestration-strategy.md) | Compute and orchestration strategy | The orchestration mechanism was retired with the hosted setup (#175); principle 4 and the triage and synthesis split stand. ADR-001 and ADR-007 name it as their successor |
| [ADR-017](ADR-017-analysis-agent-scoring-framework.md) | Analysis agent scoring framework | ADR-031 Decision 8 (catalogs) |
| [ADR-018](ADR-018-reality-check-and-human-gate.md) | Reality check and human approval gate | ADR-031 Decision 7 (scoring); ADR-028 (gate) |
| [ADR-020](ADR-020-load-testing-stage.md) | Load testing stage architecture | The ECS execution model; k6 now runs locally or in the `agent-load-test` image |
| [ADR-021](ADR-021-aurora-absorption-pass.md) | Aurora absorption pass in reality check | ADR-031 Decision 7 |
| [ADR-026](ADR-026-reality-check-in-assessment-core.md) | Fold reality check into the assessment core | ADR-028 (gate) Decision A; ADR-031 |
| [ADR-028 (Aurora)](ADR-028-aurora-relational-schema-design-agents.md) | Aurora PostgreSQL and MySQL schema-design agents | ADR-031 Decision 6 (heterogeneous sources) |

## Superseded, retired or not implemented

| ADR | Title | Status |
|---|---|---|
| [ADR-001](ADR-001-state-management-and-checkpoints.md) | Hybrid state management with checkpoints | Superseded by ADR-016 |
| [ADR-003](ADR-003-progress-reporting-architecture.md) | Progress reporting architecture | Retired with the hosted setup (#175): progress is polled from artifact presence |
| [ADR-005](ADR-005-mini-collectors-for-large-databases.md) | Mini-collectors for large databases | Not implemented |
| [ADR-007](ADR-007-referee-orchestration.md) | Referee agent orchestration | Superseded by ADR-016 |
| [ADR-013](ADR-013-core-infrastructure-cost-optimization.md) | Core infrastructure cost optimization | Deprecated: `core-infra.yaml` was removed with the hosted setup |
| [ADR-014](ADR-014-cicd-pipeline-and-ephemeral-environments.md) | CI/CD pipeline and ephemeral environments | Retired with the hosted setup (#175) |
| [ADR-015](ADR-015-dns-naming-convention.md) | DNS naming convention | Retired with the hosted setup (#175) |
| [ADR-019](ADR-019-query-journey-materialization.md) | Query journey materialization | Superseded by ADR-023 |
