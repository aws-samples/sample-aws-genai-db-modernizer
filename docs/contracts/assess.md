# The library entry point: `assess()`

`src.assessment.assess(request) -> AssessmentResult` runs an assessment in one call and returns a typed result. It is the supported way to use the pipeline as a library: the result is a small, versioned surface, so a caller never reads artifacts by store path or imports internal names.

```python
from src.assessment import AssessmentRequest, assess

result = assess(
    AssessmentRequest(
        database_name="shop",
        collection=raw_collection,  # the collection scripts' output, as a dict
        options={"run_through": "reality_check", "target_engines": ["aurora_postgresql", "dynamodb"]},
    )
)
for statement in result.statements:
    print(statement.query_id, statement.engine, statement.reason_code)
```

## What the result is for

The result is for **engine placement**: which engine serves each statement, which statements move together, and which engine owns each table.

It is **not a data model**. Partition and sort keys, indexes and target tables come only from schema design (`run_through="schema_design"`), which always uses a model and has its own reviewer. The deterministic analysis does make first guesses about a data model, but they are inputs to that design, not results: they are never part of the stable surface, and a caller should not build on them. Schema design is not available through `assess()` in this release.

## The request

| Field | Meaning |
|---|---|
| `contract_version` | `"1.0"` |
| `database_name` | 1 to 128 ASCII letters, digits, underscores or hyphens |
| `collection` | A raw collection: the unmodified output of the collection scripts. A collector output (contract 3.x) is accepted too, but every derived per-statement fact (tables, statement type, joins, tags) is read again from the statement text by the library's parser, and derived fields it carries are ignored |
| `options.run_through` | `"analysis"` or `"reality_check"` (default). `"schema_design"` and `"synthesis"` are refused in this release |
| `options.target_engines` | Engine scope, as engine catalog ids; `null` means every engine the workload warrants. Unknown, legacy and planned ids, and an empty list, are refused |
| `options.deterministic` | `true` (default): no model is called. `false`: the reality check has a model review its consolidations, which needs model access |
| `options.include_artifacts` | Include the internal contracts (see below) |
| `scope` | Informational: what a cut workload left out. Echoed back; no rule reads it. Statements in `scope.rewritten_query_ids` are marked `rewritten_by_caller` |

Unknown fields are refused, so a misspelt option never runs silently with a default.

## The stable surface

The result's stable surface follows semantic versioning (`contract_version`, separate from the internal contracts' versions):

- `contract_version`, `library_version`, `ruleset_version` and `ruleset_hash`, `options` (as applied, engine ids normalised) and `scope` (echoed);
- `summary`: `run_through_reached`, `statement_count`, `statements_per_engine`, `unservable_count`, `unique_delta_threshold`, `relational_engine`, and `tables[]` (per source table: `owner`, `owner_basis`, `also_read_by`, `cached_by`);
- `statements[]`: per statement, `query_id`, `query_tags`, `tables`, `calls_per_second`, `rewritten_by_caller`, `engine`, `confidence`, `reason_code`, `consolidated_from`, `excluded`, `unservable_reason`, `fit`, `signals`, `matched_patterns`, `tied_engines` and `reason_text`;
- `groups[]`: statements that move together, `kind` `"co_dependency"` (they join the same tables) or `"write_group"` (every write of one table: a table has one writer).

Fields that come from a later phase are `null` (or empty) when `run_through` stops earlier: at `"analysis"`, nothing is placed, so `engine`, `confidence` and `reason_code` are `null`, `summary.tables` is empty and every group's `engine` is `null`.

How to read it:

- **The engine is decided once.** Read a statement's engine from `statements[].engine` and a table's owner from `summary.tables[].owner`. Do not re-decide engines from scores or table-level labels: ties are already settled, and `reason_code` says which rule decided.
- **`reason_code`** is one of a fixed list: `highest_score`, `tie_rule`, `co_dependency`, `write_group`, `override`, `fallback`, `consolidated`, `engine_floor`, `sweep`, `capability_flag`, `customer_choice`, `post_schema_reroute`, `unservable`. Routing's own rules are finer-grained; each maps onto one of these.
- **Unservable statements.** With `target_engines` set, a statement no engine in the scope can serve is never forced onto one: its `engine` is `null`, its `reason_code` is `unservable` and `unservable_reason` says why (and which engine outside the scope could serve it). It counts in `statement_count` and `unservable_count`, never in `statements_per_engine`.
- **`excluded`** lists the engines ruled out for a statement: a hard or capability gate, and the engine scope. From `"reality_check"` on it also lists the engines that cannot own the statement's writes (a cache or search engine for a write); routing still scored those, so they appear in `fit` too. An engine in both is scored but cannot take the statement.
- **`confidence`** is the score the statement was placed with, as the assignment stores it. Until [#1373](https://github.com/aws-samples/sample-aws-genai-db-modernizer/issues/1373) is fixed, a statement the reality check consolidated keeps the removed engine's score there; read `fit[engine]` for its score on the engine it is on.
- **`fit`** is the routing scorer's score for the statement on each engine it can be scored on: the same number routing compared. At `"analysis"` it is the score before anything is placed, so a capability that routing allows only once a table's writes are placed is not applied yet.
- **`reason_text`** and every `reason` are customer-facing sentences. Show them or word your own from them; do not parse them. They can quote collection text (table names, for example), so escape them for the format you render them in.
- **`ruleset_hash`** names the rules that produced the result. Scores compare only within one hash. Asking for an older ruleset is not supported: pin the library version to reproduce a result.

With `deterministic: true`, the same request on the same library version gives the same result.

## Artifacts: no compatibility promise

With `include_artifacts: true`, the result also carries `artifacts`: the pipeline's internal contracts, verbatim (`collector_output`, `triage`, `analyses`, `decision_traces`, and from `"reality_check"` on, `assignment` and `reality_check`). Each keeps its own internal `contract_version`, and the block's `compatibility` field says it plainly: they may change in any release, as routing evolves. Use them for debugging and audit, not as an integration surface.
