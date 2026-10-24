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

The result's stable surface follows semantic versioning (`contract_version`, separate from the internal contracts' versions). Contract `1.0` has not been released yet, so the fields below are all part of `1.0`: the additions since the first draft (`label`, `summary.source_engine`, `assignment_version`, `engines[]`, `consolidations[]`, `tables[].reason`, and `statements[].query_type`, `join_count` and `matched_patterns` as a list of named patterns) did not bump it.

- `contract_version`, `label`, `library_version`, `ruleset_version` and `ruleset_hash`, `options` (as applied, engine ids normalised) and `scope` (echoed);
- `summary`: `run_through_reached`, `source_engine`, `assignment_version`, `statement_count`, `statements_per_engine`, `unservable_count`, `unique_delta_threshold`, `relational_engine`, `engines[]`, `consolidations[]`, and `tables[]` (per source table: `owner`, `owner_basis`, `also_read_by`, `cached_by`, `reason`);
- `statements[]`: per statement, `query_id`, `query_tags`, `query_type`, `join_count`, `tables`, `calls_per_second`, `rewritten_by_caller`, `engine`, `confidence`, `reason_code`, `consolidated_from`, `excluded`, `unservable_reason`, `fit`, `signals`, `matched_patterns`, `tied_engines` and `reason_text`;
- `groups[]`: statements that move together, `kind` `"co_dependency"` (they join the same tables) or `"write_group"` (every write of one table: a table has one writer).

Fields that come from a later phase are `null` (or empty) when `run_through` stops earlier: at `"analysis"`, nothing is placed, so `engine`, `confidence` and `reason_code` are `null`, `summary.assignment_version` is `null`, `summary.engines`, `summary.consolidations`, `summary.tables` and every statement's `matched_patterns` are empty, and every group's `engine` is `null`.

### The label

`label` says what kind of result it is, so a routing-only result is never read as a data model:

| `label` | When | What it holds |
|---|---|---|
| `analysis_only` | `run_through_reached` is `"analysis"` | Scores per statement; nothing placed |
| `recommendation` | `run_through_reached` is `"reality_check"` | Engine placement: where each statement runs, which engine owns each table. No data model |
| `designed` | `run_through_reached` is `"schema_design"` or `"synthesis"` | Placement plus a schema design's data model. Not produced by this release |

### Summary additions

- **`source_engine`**: the source database engine, as the collection states it (`mysql`, `postgresql`, `sqlserver`, ...).
- **`assignment_version`**: the assignment version the result reflects. It rises when the reality check or a customer edit writes a new version.
- **`engines[]`**: one line per analysed engine, in analysis order: `engine`, `role`, `statement_count` (statements placed on it), `consolidated_into`, `unique_queries`, `avg_delta` and `reason`. `role` is `owner` (statements are placed on it), `cache_layer` (a cache in front of other engines' reads; it owns no statement), `consolidated` (the reality check moved its statements to other engines; `consolidated_into` is the one that took most of them, the first in the reality check's `consolidations` order on a tie) or `not_selected` (analysed, but no statement was placed on it). `unique_queries` and `avg_delta` are the reality check's unique-value figures for the engine (the statements it held whose score leads the best other engine by at least `unique_delta_threshold` points, and its mean lead), `null` for an engine the check did not assess. `reason` is the executive report's "why this engine" text for an owner or the cache, and the reality check's reason for a consolidated engine.
- **`consolidations[]`**: one line per reality-check consolidation: `from_engine`, `to_engine`, `query_count` and `reason`. Each moved statement names its `from_engine` in `statements[].consolidated_from`.
- **`tables[].reason`**: one sentence on why the owner is the owner: the writes it takes, or the share of the table's queries it serves (and how a tie was settled), and the engines that read a copy.

### Statement additions

- **`query_type`** and **`join_count`**: the statement's type (`SELECT`, `INSERT`, `UPDATE`, `DELETE`, `MERGE` or `OTHER`) and its number of joins, as the library's parser reads them.
- **`matched_patterns`**: the pattern catalog's patterns the statement matches on its assigned engine, as `{"id", "name"}` (for example `{"id": "key-value-lookup", "name": "Key-value lookup"}`). Only the assigned engine's: read another engine's fit from `fit`. Empty at `"analysis"` and for an unservable statement.

How to read it:

- **The engine is decided once.** Read a statement's engine from `statements[].engine` and a table's owner from `summary.tables[].owner`. Do not re-decide engines from scores or table-level labels: ties are already settled, and `reason_code` says which rule decided. To word an engine rationale, use `summary.engines[].reason` and `summary.tables[].reason`.
- **`reason_code`** is one of a fixed list: `highest_score`, `tie_rule`, `co_dependency`, `write_group`, `override`, `fallback`, `consolidated`, `engine_floor`, `sweep`, `capability_flag`, `customer_choice`, `post_schema_reroute`, `unservable`. Routing's own rules are finer-grained; each maps onto one of these.
- **Unservable statements.** With `target_engines` set, a statement no engine in the scope can serve is never forced onto one: its `engine` is `null`, its `reason_code` is `unservable` and `unservable_reason` says why (and which engine outside the scope could serve it). It counts in `statement_count` and `unservable_count`, never in `statements_per_engine`.
- **`excluded`** lists the engines ruled out for a statement: a hard or capability gate, and the engine scope. From `"reality_check"` on it also lists the engines that cannot own the statement's writes (a cache or search engine for a write); routing still scored those, so they appear in `fit` too. An engine in both is scored but cannot take the statement.
- **`confidence`** is the score the statement was placed with, as the assignment stores it. Until [#1373](https://github.com/aws-samples/sample-aws-genai-db-modernizer/issues/1373) is fixed, a statement the reality check consolidated keeps the removed engine's score there; read `fit[engine]` for its score on the engine it is on.
- **`fit`** is the routing scorer's score for the statement on each engine it can be scored on: the same number routing compared. At `"analysis"` it is the score before anything is placed, so a capability that routing allows only once a table's writes are placed is not applied yet.
- **`reason_text`** and every `reason` (including `summary.engines[].reason`, `summary.consolidations[].reason` and `summary.tables[].reason`) are customer-facing sentences. Show them or word your own from them; do not parse them. They can quote collection text (table names, for example), so escape them for the format you render them in.
- **`ruleset_hash`** names the rules that produced the result. Scores compare only within one hash. Asking for an older ruleset is not supported: pin the library version to reproduce a result.

With `deterministic: true`, the same request on the same library version gives the same result.

## Artifacts: no compatibility promise

With `include_artifacts: true`, the result also carries `artifacts`: the pipeline's internal contracts, verbatim (`collector_output`, `triage`, `analyses`, `decision_traces`, and from `"reality_check"` on, `assignment` and `reality_check`). Each keeps its own internal `contract_version`, and the block's `compatibility` field says it plainly: they may change in any release, as routing evolves. Use them for debugging and audit, not as an integration surface.

## JSON Schemas

The request and the result are published as JSON Schemas (draft 2020-12) in [`docs/contracts/schemas/`](schemas/README.md), for callers in other languages or callers that validate payloads outside Python:

- [`assess_request.v1.schema.json`](schemas/assess_request.v1.schema.json): `AssessmentRequest`;
- [`assess_result.v1.schema.json`](schemas/assess_result.v1.schema.json): `AssessmentResult`, as `model_dump_json` writes it.

`v1` is the contract's MAJOR version: the file covers every `contract_version` `1.x`, and each states the exact version it was rendered from in `x-contract-version`. The files are generated from the models (`uv run python scripts/render_contract_schemas.py --write`), never edited by hand, and lint fails when they are out of date. A schema describes the shape; the library also refuses an unknown engine id, a `run_through` this release does not run and a `label` that does not match `summary.run_through_reached` ([schemas/README.md](schemas/README.md)).

## Sample

A request for a SQL Server collection with two reads, scoped to Aurora PostgreSQL and DynamoDB, and the result it could give. The values are illustrative (shortened ids, a placeholder `ruleset_hash`); both payloads validate against the schemas above, and a test keeps them valid.

### Sample request

```json
{
  "contract_version": "1.0",
  "database_name": "shop",
  "collection": {
    "...": "the collection scripts' output"
  },
  "options": {
    "target_engines": [
      "aurora_postgresql",
      "dynamodb"
    ],
    "run_through": "reality_check",
    "deterministic": true,
    "include_artifacts": false
  },
  "scope": null
}
```

### Sample result

```json
{
  "contract_version": "1.0",
  "label": "recommendation",
  "library_version": "0.3.0",
  "ruleset_version": "0.3.0",
  "ruleset_hash": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
  "options": {
    "target_engines": [
      "aurora_postgresql",
      "dynamodb"
    ],
    "run_through": "reality_check",
    "deterministic": true,
    "include_artifacts": false
  },
  "scope": null,
  "summary": {
    "run_through_reached": "reality_check",
    "source_engine": "sqlserver",
    "assignment_version": 2,
    "statement_count": 2,
    "statements_per_engine": {
      "aurora_postgresql": 1,
      "dynamodb": 1
    },
    "unservable_count": 0,
    "unique_delta_threshold": 15,
    "relational_engine": {
      "engine": "aurora_postgresql",
      "reason": "The customer's engine scope includes Aurora PostgreSQL and no other Aurora engine, so it is the relational candidate"
    },
    "engines": [
      {
        "engine": "aurora_postgresql",
        "role": "owner",
        "statement_count": 1,
        "consolidated_into": null,
        "unique_queries": 1,
        "avg_delta": 18.0,
        "reason": "84% mean fit across 1 query (2 rated tables), pinned by multi-table joins (1 of 1)."
      },
      {
        "engine": "dynamodb",
        "role": "owner",
        "statement_count": 1,
        "consolidated_into": null,
        "unique_queries": 0,
        "avg_delta": 4.0,
        "reason": "81% mean fit across 1 query (1 rated table), led by key-value lookups (1 of 1)."
      }
    ],
    "consolidations": [],
    "tables": [
      {
        "table_id": "sales.Orders",
        "owner": "aurora_postgresql",
        "owner_basis": "most_queries",
        "also_read_by": [],
        "cached_by": null,
        "reason": "No statement writes it; Aurora PostgreSQL serves its only query."
      },
      {
        "table_id": "sales.OrderLines",
        "owner": "aurora_postgresql",
        "owner_basis": "most_queries",
        "also_read_by": [],
        "cached_by": null,
        "reason": "No statement writes it; Aurora PostgreSQL serves its only query."
      },
      {
        "table_id": "sales.Carts",
        "owner": "dynamodb",
        "owner_basis": "most_queries",
        "also_read_by": [],
        "cached_by": null,
        "reason": "No statement writes it; DynamoDB serves its only query."
      }
    ]
  },
  "statements": [
    {
      "query_id": "Q01",
      "query_tags": [],
      "query_type": "SELECT",
      "join_count": 1,
      "tables": [
        "sales.Orders",
        "sales.OrderLines"
      ],
      "calls_per_second": 12.5,
      "rewritten_by_caller": false,
      "engine": "aurora_postgresql",
      "confidence": 84,
      "reason_code": "highest_score",
      "consolidated_from": null,
      "excluded": [],
      "unservable_reason": null,
      "fit": {
        "aurora_postgresql": 84,
        "dynamodb": 66
      },
      "signals": [],
      "matched_patterns": [
        {
          "id": "simple-join",
          "name": "Simple join"
        }
      ],
      "tied_engines": [],
      "reason_text": "highest fit for Aurora PostgreSQL"
    },
    {
      "query_id": "Q02",
      "query_tags": [
        "endpoint: CartController.Get"
      ],
      "query_type": "SELECT",
      "join_count": 0,
      "tables": [
        "sales.Carts"
      ],
      "calls_per_second": 3.0,
      "rewritten_by_caller": false,
      "engine": "dynamodb",
      "confidence": 81,
      "reason_code": "highest_score",
      "consolidated_from": null,
      "excluded": [],
      "unservable_reason": null,
      "fit": {
        "aurora_postgresql": 77,
        "dynamodb": 81
      },
      "signals": [
        "key_value_lookups"
      ],
      "matched_patterns": [
        {
          "id": "key-value-lookup",
          "name": "Key-value lookup"
        }
      ],
      "tied_engines": [],
      "reason_text": "highest fit for DynamoDB"
    }
  ],
  "groups": [],
  "artifacts": null
}
```
