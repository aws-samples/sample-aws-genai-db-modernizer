# Job artifacts: contracts and internal files

Every JSON file a run writes under `artifacts/<db>/<job>/` is one of three kinds (ADR-008 Decision 1, amendment of 2026-10-20, #1192). The lists live in `src/shared/contract_versioning.py`; this page mirrors them, and `tests/unit/docs/test_contract_artifacts_doc.py` keeps it in step. `tests/contract/test_declared_artifacts.py` fails on a written JSON file in none of them.

Each pattern matches the end of a path at a `/` boundary.

## Registered contracts

Versioned (`contract_version`, MAJOR.MINOR), written through their model and checked on every read. Callers may rely on these.

| Path pattern | Contract |
|---|---|
| `collector/output\.json` | `CollectorOutputContract` (`src.contracts.collector_output`) |
| `uploads/collector-output\.json` | `CollectorOutputContract` (`src.contracts.collector_output`) |
| `referee-triage/triage\.json` | `TriageOutputContract` (`src.contracts.triage_output`) |
| `analysis-[^/]+/analysis\.json` | `AnalysisOutputContract` (`src.contracts.analysis_output`) |
| `analysis-dynamodb/decision-trace\.json` | `DynamoDBDecisionTrace` (`src.contracts.dynamodb_decision_trace`) |
| `analysis-documentdb/decision-trace\.json` | `DocumentDBDecisionTrace` (`src.contracts.documentdb_decision_trace`) |
| `assignment/v\d+/assignment\.json` | `Assignment` (`src.contracts.assignment_models`) |
| `assignment/v\d+/validation\.json` | `ValidationResult` (`src.contracts.assignment_models`) |
| `reality-check/output\.json` | `RealityCheckOutputContract` (`src.contracts.reality_check_output`) |
| `synthesis/(?:v\d+/)?report\.json` | `SynthesisOutputContract` (`src.contracts.synthesis_output`) |
| `referee-synthesis/report\.json` | `SynthesisOutputContract` (`src.contracts.synthesis_output`) |
| `deliverables/assessment-data\.json` | `SynthesisOutputContract` (`src.contracts.synthesis_output`) |
| `schema-[^/]+/v\d+/groups_manifest\.json` | `SchemaDesignGroupsManifest` (`src.contracts.schema_design_input`) |
| `schema-[^/]+/v\d+/version_meta\.json` | `SchemaVersionMeta` (`src.contracts.schema_revision_models`) |
| `schema-[^/]+/v\d+/revision_request\.json` | `SchemaRevisionRequest` (`src.contracts.schema_revision_models`) |
| `schema-[^/]+/confirmed\.json` | `SchemaConfirmation` (`src.contracts.schema_revision_models`) |
| `[^/]+/questions\.json` | `AgentQuestions` (`src.contracts.agent_interaction_models`) |
| `[^/]+/answers\.json` | `AgentAnswers` (`src.contracts.agent_interaction_models`) |
| `\.meta/[^_/][^/]*\.json` | `PhaseProgression` (`src.contracts.phase_models`) |
| `post-schema-router/(?:pass-\d+/)?router_output\.json` | `RouterOutput` (`src.contracts.post_schema_router_output`) |
| `load-test-[^/]+/v\d+/infrastructure\.json` | `InfrastructureManifest` (`src.contracts.load_test_models`) |
| `load-test-[^/]+/v\d+/seed-manifest\.json` | `SeedManifest` (`src.agents.load_test.models`) |
| `load-test-[^/]+/v\d+/results/summary\.json` | `LoadTestOutput` (`src.contracts.load_test_models`) |
| `load-test-[^/]+/v\d+/results/(?!summary\.json)[^/]+\.json` | `PatternResult` (`src.contracts.load_test_models`) |
| `schema-{engine}/(?:v\d+/)?schema_output\.json` | the engine's `*ModelOutputContract` (`src.contracts.<engine>_model_output`), for every catalog engine that has one |

## Model I/O

A model session's own files: requests, inputs, answers and group drafts. Never checked on read; the writer that turns an answer into a contract validates it and writes the contract's own version.

| Path pattern |
|---|
| `llm_responses/[^/]+\.json` |
| `llm_requests/[^/]+\.json` |
| `llm_input\.json` |
| `llm_response\.json` |
| `partial_output\.json` |
| `schema-[^/]+/v\d+/schema_draft_group_\d+\.json` |
| `schema-[^/]+/v\d+/input_group_\d+\.json` |

## Internal artifacts

Not contracts: callers may not rely on their shape. A pattern starting with `^` is pinned to a path relative to the artifact root (`<db>/<job>/...`); `{engines}` stands for every catalog engine id. A file other phases or callers read back across runs is typed in its own issue and then moves to the registered contracts.

| Path pattern | Why it is not a contract |
|---|---|
| `collector/sql_shadow\.json` | parser comparison (sqlglot facade vs regex table references, #557): a diagnostic rewritten every collect, read by no phase |
| `analysis-aurora_mysql/decision-trace\.json` | an analysis decision trace not typed yet (UNTYPED_TRACE_ENGINES) |
| `analysis-aurora_postgresql/decision-trace\.json` | an analysis decision trace not typed yet (UNTYPED_TRACE_ENGINES) |
| `analysis-elasticache/decision-trace\.json` | an analysis decision trace not typed yet (UNTYPED_TRACE_ENGINES) |
| `analysis-opensearch/decision-trace\.json` | an analysis decision trace not typed yet (UNTYPED_TRACE_ENGINES) |
| `analysis-[^/]+/er-diagram\.json` | the ER diagram's Mermaid text in a JSON wrapper, rebuilt from the analysis |
| `analysis-[^/]+/partial\.json` | external-mode analysis held for the model's advisor pass, replaced at finalize |
| `assignment/v\d+/access_patterns\.json` | each query's access-pattern class and scores as routing scored them (#728, #936): an audit record written with its assignment version |
| `decisions/assignment_(?:review|approval)\.json` | recorded approvals (the assignment review and approval): typed by #1257 |
| `reality-check/awaiting_llm\.json` | external-mode marker that a reality-check preview waits for the model, read by the assignment lineage and the local API's job status: typed by #1267 |
| `assignment/review/pending_hitl\.json` | AWS Transform transport state: the pending review task to resume |
| `schema-[^/]+/(?:v\d+/)?design_trace\.json` | the schema design trace (model calls, merge notes, PE review): typed by #1258 |
| `schema-[^/]+/v\d+/design_trace_group_\d+\.json` | one DynamoDB group's design trace, merged into design_trace.json: typed by #1258 |
| `schema-[^/]+/v\d+/merge_record\.json` | the last --merge of the DynamoDB group drafts (draft hashes, errors) |
| `schema-[^/]+/v\d+/changelog\.json` | a schema revision's changelog, shown with the revision |
| `schema-[^/]+/v\d+/revision_context\.json` | a schema revision's context for the designer, rewritten per revision |
| `post-schema-router/external_run\.json` | external-mode post-schema routing pass record (depth, engines to design again) |
| `load-test-[^/]+/v\d+/config\.json` | the load test's configuration as run |
| `load-test-[^/]+/v\d+/result\.json` | the load test runner's raw result |
| `load-test-[^/]+/v\d+/k6_diagnostics\.json` | the load test runner's diagnostics |
| `^[^/]+/[^/]+/_meta\.json` | the local API runner's own job record (start time, request input), read only by the local API and its stored-input redaction, never by a phase |
| `^[^/]+/[^/]+/_status\.json` | the local API runner's job status marker (cancelled, failed) |
| `^[^/]+/[^/]+/(?:collector|referee-triage|referee-synthesis|assignment-resolver|reality-check|schema-design|schema-split|schema-merge|load-test|{engines})/error\.json` | a container agent's failure record, under its agent type |
| `_logs/prose_style_rejections-v\d+\.json` | a log of the synthesis prose a style check rejected, per assignment version |
| `uploads/input_key\.json` | AWS Transform transport state: the resolved input location |
| `_pending/[^/]+/upload\.json` | AWS Transform transport state: a pending upload task, before the job folder exists |
| `\.meta/_stored-input-redaction-v\d+\.json` | the stored-input redaction marker (not a job's progression) |
