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

## Loading the collection

The collection scripts write text, often with a header line before the JSON object (`collection_output` followed by `{...}`). `assess()` takes the parsed dict; `load_collection` turns the scripts' output into it, with the reader the command line uses:

```python
from pathlib import Path

from src.assessment import AssessmentRequest, CollectionLoadError, assess, load_collection

try:
    collection = load_collection(Path("docs/examples/wordpress/wordpress.zip"))
except CollectionLoadError as error:
    raise SystemExit(str(error))

result = assess(
    AssessmentRequest(database_name="wordpress", collection=collection, options={"run_through": "analysis"})
)
```

`load_collection(source)` accepts:

- a `str` or `bytes` (UTF-8): the output text. A `str` is always text, never a path;
- a `pathlib.Path` (or any path-like): a `.json` or `.txt` file with that text, or a `.zip` holding exactly one `.json` or `.txt` collection file, as the bundled samples under `docs/examples/` are shipped.

A header line before the object, a leading BOM and client chatter after it are ignored. The result is the collection dict (it has `tables` and `queries`), ready for `AssessmentRequest.collection`. `AssessmentRequest` itself still takes a dict.

Every problem raises `CollectionLoadError`, a `ValueError` subclass, with a message in your terms: no JSON object found, more than one JSON object (or, for a zip, more than one collection file), no collection file in the zip, not a valid zip, the JSON is invalid, not a collection (no `tables` or `queries` key), file not found, or not UTF-8. A test loads every bundled sample this way and runs `assess()` on it, so the example above runs as written.

## What the result is for

The result is for **engine placement**: which engine serves each statement, which statements move together, and which engine owns each table.

Placement alone is **not a data model**. Partition and sort keys, indexes and target tables come only from schema design (`run_through="schema_design"`), which always uses a model and has its own reviewer. A designed result carries them in the stable [design block](#the-design-block) (`designs`), copied from each engine's design; the full designs are in the `artifacts` block. The deterministic analysis does make first guesses about a data model, but they are inputs to that design, not results: they are never part of the stable surface, and a caller should not build on them.

## How far to run: `run_through`

Each value runs the phases before it too. From `"reality_check"` on, a later value's result keeps the stable fields an earlier one returns (see [Determinism](#determinism)).

| `run_through` | What it adds | Needs a model | Time and cost |
|---|---|---|---|
| `"analysis"` | A score per statement on every analysed engine; nothing placed (`label` `analysis_only`) | Never | Seconds (see below); no model spend |
| `"reality_check"` (default) | The final engine placement: each statement's engine, the groups' engines, each table's owner (`label` `recommendation`) | Only with `deterministic: false` (a model reviews the consolidations) | Seconds with `deterministic: true` (see below); with `false`, plus the model calls that review the consolidations and write a summary |
| `"schema_design"` | A schema design (the data model) for every engine the placement keeps: its target tables, keys and indexes in `designs`, the full design in `artifacts.schema_designs`; `summary.designed_engines` names them (`label` `designed`, or `recommendation` when no engine could be designed) | Always: send `deterministic: false` | Minutes, and model spend that grows with the statement count (see below) |
| `"synthesis"` | The report every deliverable renders, in `artifacts.synthesis_report`; `summary.tables` and `summary.engines[].reason` are the report's own lines (labelled as at `"schema_design"`) | Always: send `deterministic: false` | Schema design's, plus one executive-summary call |

`"schema_design"` and `"synthesis"` with `deterministic: true` (the default) are refused when the request is validated, with a message that says the phase needs a model, before anything runs. Model access is configured as for the pipeline's `--llm-mode bedrock` ([Other ways to run it](../other-ways-to-run-it.md)): schema design uses the schema-design model role (Claude Opus by default), the summaries the summary role.

**How long the deterministic phases take.** Measured on a laptop with nothing else running, three runs each, in a fresh process: the WordPress sample (107 statements) 0.8 seconds through `"analysis"` and 1.0 through `"reality_check"`; the TrailGear sample (95) 0.8 and 0.9; the Discourse sample (1,654) 6.3 to 6.4 and 9.1 to 9.2. A busy machine is slower: a run of Discourse next to the test suite took 11 to 15.5 seconds.

**What schema design costs.** It calls a model for every engine it designs, in parallel, and, for the engines that split a large workload, once per group of up to 20 statements, and a reviewer model reads the designs. So its time and spend grow with the number of statements and engines, not with the database's size. Measured on the WordPress sample (107 statements, three designed engines) on Amazon Bedrock in `us-east-1` with the default model roles: two runs through `"synthesis"` took 17 and 18 minutes (one through `"schema_design"` took 20), and the run with token accounting cost $12.49 at Bedrock list prices (40 model calls, 99% of the cost in schema design). A designed run records its token usage per phase and model in `_logs/model_usage.json` under the job (with `include_artifacts: true`, also `artifacts.model_usage`), and `uv run python scripts/usage_cost.py <model_usage.json>` prices it. Like the pipeline, `assess()` designs again each engine a post-schema reroute touches (see below), so a design that cannot serve a statement adds one more design call for the engine it left and the one it joined. Use `"analysis"` or `"reality_check"` (no model, seconds) for engine placement, and `"schema_design"` only when you need a data model.

```python
# Placement only, no model (seconds).
placed = assess({"database_name": "shop", "collection": raw, "options": {"run_through": "reality_check"}})

# Scores only, nothing placed.
scored = assess({"database_name": "shop", "collection": raw, "options": {"run_through": "analysis"}})

# The data model: a schema design per kept engine (needs a model).
designed = assess(
    {
        "database_name": "shop",
        "collection": raw,
        "options": {"run_through": "schema_design", "deterministic": False, "include_artifacts": True},
    }
)
for design in designed.designs:  # the stable design block
    if design.engine == "dynamodb":
        print([(t.name, t.partition_key.name) for t in design.tables])
dynamodb_design = designed.artifacts.schema_designs["dynamodb"]  # the full design: an internal contract

# The report too.
reported = assess(
    {
        "database_name": "shop",
        "collection": raw,
        "options": {"run_through": "synthesis", "deterministic": False, "include_artifacts": True},
    }
)
report = reported.artifacts.synthesis_report
```

Which engines get a schema design: the ones the effective assignment keeps, owners and the cache layer when it fronts an owner's reads, in the engine scope (`target_engines`). An engine the reality check consolidated away, or one outside the scope, gets none. A design that fails (an error from the designer or the model) fails the run (`AssessmentRunError`): no result is built from a partial set of designs. A design that is written but does not pass its checks (for example, it names a statement outside its engine's scope) is not a failure: as in the pipeline, it comes back as a normal design with `validation_passed: false` and the reasons in `validation_failures`, in `artifacts.schema_designs.<engine>`. Check `validation_passed` before you use a design.

`summary.designed_engines` lists the engines with a design. When the engine scope leaves no engine to design (for example `target_engines: ["elasticache"]`: a cache owns no statement, so every statement is unservable), no designer runs, `designed_engines` is empty, `summary.design_note` says so, and the result is labelled `recommendation`, not `designed`, because it has no data model. `summary.run_through_reached` still says how far the run got.

A designed result always carries the [design block](#the-design-block) (`designs`). The full designs and the report are only in `artifacts`, so ask for `include_artifacts: true` for them (or pass your own `store`, where they stay under `<database_name>/<job_id>/`; `artifacts.artifact_keys` names each key).

**Post-schema routing.** After the designs, `assess()` runs the pipeline's post-schema routing, the same implementation the local pipeline uses: a statement a design reports unsupported, or its reviewer flags, moves (with the statements it moves with) to its best-scoring remaining engine among the ones the placement keeps, in the engine scope, never a cache. The move is a new assignment version, so `summary.assignment_version` rises, the statement's `reason_code` is `post_schema_reroute`, and its Move (pass `post_schema_flag`) is in `artifacts.assignment.moves`. Only the engines a move touches (the one it left and the one it joined) are designed again on the new version; the others' designs are carried forward, and the result, its designs and `summary.designed_engines` are those of the new version. A statement with no remaining engine in the scope is not moved: it stays on the engine whose design flagged it, with its reason code, and the result marks it in `statements[].post_schema_terminal` and counts it in `summary.post_schema_terminal_count` (see [Post-schema terminal statements](#post-schema-terminal-statements)). A redesign that fails fails the run (`AssessmentRunError`), after a version restating the one before the move is written.

## The request

| Field | Meaning |
|---|---|
| `contract_version` | `"1.1"` (the default). A request written for `"1.0"` is read the same way |
| `database_name` | 1 to 128 ASCII letters, digits, underscores or hyphens. It names the run; it need not match the collection's own database name (see below) |
| `collection` | A raw collection: the unmodified output of the collection scripts. A collector output (contract 3.x) is accepted too, but every derived per-statement fact (tables, statement type, joins, tags) is read again from the statement text by the library's parser, and derived fields it carries are ignored |
| `options.run_through` | `"analysis"`, `"reality_check"` (default), `"schema_design"` or `"synthesis"` ([How far to run](#how-far-to-run-run_through)) |
| `options.target_engines` | Engine scope, as engine catalog ids; `null` means every engine the workload warrants. Unknown, legacy and planned ids, and an empty list, are refused |
| `options.deterministic` | `true` (default): no model is called. `false`: the reality check has a model review its consolidations, which needs model access. Must be `false` for `"schema_design"` and `"synthesis"`, which always use a model |
| `options.include_artifacts` | Include the internal contracts (see below) |
| `scope` | Informational: what a cut workload left out. Echoed back; no rule reads it. Statements in `scope.rewritten_query_ids` are marked `rewritten_by_caller` |

Unknown fields are refused, so a misspelt option never runs silently with a default.

`database_name` names the run (and, with your own `store`, its folder `<database_name>/<job_id>/`); it does not have to match the database name the collection records. When the collection records its database name, that name wins: table ids (`summary.tables[].table_id`, `groups[].tables`) keep it, so one collection gives the same result whatever name you pass. That holds too when `database_name` names another database the collection's statements belong to (SQL Server's collection records each statement's database): that database's statements are left out either way, and a warning is logged. When the collection records no database name, the table ids take the one database its statements name, if they name exactly one, and `database_name` otherwise. One case is refused as ambiguous: the collection records no database name and its statements belong to several databases, none of them `database_name`. `assess()` then raises `DatabaseNameMismatchError`, a `ValueError` subclass whose message names the databases, and builds no result. The error is the library's, not a field of the result, so it does not change `contract_version`.

## The stable surface

The result's stable surface follows semantic versioning (`contract_version`, separate from the internal contracts' versions). Contract `1.1` adds the OpenSearch entry of the design block (#1513), the one change since `1.0`; neither has been released yet. The rest of the fields below are part of `1.0`: the additions since its first draft (`label`, `summary.source_engine`, `assignment_version`, `engines[]`, `consolidations[]`, `tables[].reason`, `designed_engines`, `design_note`, `post_schema_terminal_count`, `statements[].query_type`, `join_count`, `matched_patterns` as a list of named patterns and `post_schema_terminal`, and the design block `designs`) did not bump it.

- `contract_version`, `label`, `library_version`, `ruleset_version` and `ruleset_hash`, `options` (as applied, engine ids normalised) and `scope` (echoed);
- `summary`: `run_through_reached`, `source_engine`, `assignment_version`, `statement_count`, `statements_per_engine`, `unservable_count`, `post_schema_terminal_count`, `unique_delta_threshold`, `relational_engine`, `engines[]`, `consolidations[]`, `tables[]` (per source table: `owner`, `owner_basis`, `also_read_by`, `cached_by`, `reason`), `designed_engines` and `design_note`;
- `statements[]`: per statement, `query_id`, `query_tags`, `query_type`, `join_count`, `tables`, `calls_per_second`, `rewritten_by_caller`, `engine`, `confidence`, `reason_code`, `consolidated_from`, `excluded`, `unservable_reason`, `fit`, `signals`, `matched_patterns`, `tied_engines`, `reason_text` and `post_schema_terminal`;
- `groups[]`: statements that move together, `kind` `"co_dependency"` (they join the same tables) or `"write_group"` (every write of one table: a table has one writer);
- `designs`: a designed result's data model per designed engine ([The design block](#the-design-block)); `null` unless `label` is `designed`.

Consumer contract tests keep this surface (`tests/contract/test_assess_consumer_contract.py`, on the fixtures in `tests/contract/fixtures/assess_consumer_fixtures.json`, each with the fields it guards), and a removed or renamed field, a changed type or a removed or changed enum value fails the build without a MAJOR bump of `contract_version` against the shape the last release shipped (`tests/contract/test_assess_released_contract.py`). A new enum value in the result (a new `reason_code` or `role`, for example) is a MINOR change: read enums with a default branch, since an exhaustive match on today's values can meet a new one in a MINOR release. Each release lists the routing changes since the previous one (CONTRIBUTING.md's Releases section).

Fields that come from a later phase are `null` (or empty) when `run_through` stops earlier: at `"analysis"`, nothing is placed, so `engine`, `confidence` and `reason_code` are `null`, `summary.assignment_version` is `null`, `summary.engines`, `summary.consolidations`, `summary.tables` and every statement's `matched_patterns` are empty, and every group's `engine` is `null`. Before `"schema_design"`, and when a model phase had no engine to design, `designs` is `null`.

### The label

`label` says what kind of result it is, so a routing-only result is never read as a data model:

| `label` | When | What it holds |
|---|---|---|
| `analysis_only` | `run_through_reached` is `"analysis"` | Scores per statement; nothing placed |
| `recommendation` | `run_through_reached` is `"reality_check"`, or a model phase that had no engine to design (`summary.designed_engines` empty) | Engine placement: where each statement runs, which engine owns each table. No data model |
| `designed` | `run_through_reached` is `"schema_design"` or `"synthesis"` and `summary.designed_engines` is not empty | Placement plus a schema design's data model for each engine in `summary.designed_engines`: the stable design block `designs`, and the full designs in `artifacts` (no compatibility promise) |

### The design block

`designs` is set only on a `designed` result (`null` otherwise): one entry per engine in `summary.designed_engines`, in that order, discriminated by `engine`. Every value is copied from that engine's schema design, never recomputed and never taken from the deterministic analysis; the full design (attributes, item collections, access patterns, sizing, trade-offs) stays in `artifacts.schema_designs.<engine>`. The [design block standard](assess-design-block.md) explains the choices and says what is guaranteed, with an example per engine.

Every entry has `engine` and `validation_passed`: `false` for a design that failed its own checks (the reasons are in `artifacts.schema_designs.<engine>.validation_failures`) or that records no result. Check it before you build on a design: a failed design is returned, not dropped. Then, per engine:

| `engine` | Fields |
|---|---|
| `dynamodb` | `tables[]`: `name`, `source_tables`, `partition_key` and `sort_key` (`{"name", "type"}`, `type` `S`, `N` or `B`; `sort_key` `null` for none), `gsis[]` (`name`, `partition_key[]` and `sort_key[]` as ordered key attributes, `projection` `ALL`, `KEYS_ONLY` or `INCLUDE`, `projected_attributes` for `INCLUDE`, `query_ids`) and `query_ids` (the statements the design serves from the table itself; those served from an index are on the index) |
| `aurora_mysql`, `aurora_postgresql` | `tables[]`: `name`, `primary_key` (columns) and `indexes` (the design's `CREATE INDEX` statements, verbatim); `ddl_artifact_path`, where the full generated DDL is in the result with `include_artifacts: true` (`artifacts.schema_designs.<engine>.generated_ddl`) |
| `documentdb` | `collections[]`: `name`, `source_tables`, `embedded[]` (per child table: `source_table`, `path` in the parent document, `strategy` `embed`, `reference` or `hybrid`), `referenced_collections`, `indexes[]` (`name`, `keys[]` as ordered `{"field", "direction"}`, `type` `compound`, `single`, `text`, `partial` or `unique`, `query_ids`) and `query_ids` |
| `elasticache` | `keys[]`: `pattern` (the key pattern), `data_type`, `ttl_seconds` (`null` for no expiry), `source_tables` and `query_ids` (what the key caches: the source tables and the statements whose reads it serves) |
| `opensearch` | `indexes[]`: `name`, `source_tables`, `fields[]` (its mapped fields in the design's order, `{"name", "type"}`, `type` the OpenSearch field type as the design names it), `aliases` and `query_ids` (the statements the design serves from the index); `data_streams[]`: `name`, `source_tables`, `fields[]` (its index template's mapped fields, with `@timestamp`) and `query_ids` |

The OpenSearch entry was added in contract `1.1` (#1513). From `1.1` on, every engine in `summary.designed_engines` has its entry, in that order, and the result model refuses one without it. A `1.0` result is not checked for this: it is read exactly as contract `1.0` read it, so it can list `opensearch` in `summary.designed_engines` with no `designs` entry, and still validates. A new engine in `designs`, or a new optional field in an entry, is a MINOR change: read it with a default branch, and validate a result against the schema of the `contract_version` it carries (an older schema file refuses an engine it does not know). Names (tables, keys, indexes) come from the design, which a model wrote from collection text: treat them as data and escape them when you render them.

### Sample design block

`designs` of a designed WordPress run (`run_through: "schema_design"`, no engine scope), trimmed to one or two entries per list:

```json
[
  {
    "validation_passed": true,
    "engine": "aurora_mysql",
    "tables": [
      {
        "name": "wp_actionscheduler_groups",
        "primary_key": [
          "group_id"
        ],
        "indexes": [
          "CREATE INDEX `slug` ON `wp_actionscheduler_groups` (`slug`);"
        ]
      }
    ],
    "ddl_artifact_path": "artifacts.schema_designs.aurora_mysql.generated_ddl"
  },
  {
    "validation_passed": true,
    "engine": "dynamodb",
    "tables": [
      {
        "name": "WpComments",
        "source_tables": [
          "wordpress.wp_comments"
        ],
        "partition_key": {
          "name": "comment_id",
          "type": "N"
        },
        "sort_key": null,
        "gsis": [
          {
            "name": "CommentsByPost",
            "partition_key": [
              {
                "name": "comment_post_id",
                "type": "N"
              }
            ],
            "sort_key": [
              {
                "name": "comment_approved",
                "type": "S"
              },
              {
                "name": "comment_date_gmt",
                "type": "S"
              }
            ],
            "projection": "KEYS_ONLY",
            "projected_attributes": [],
            "query_ids": [
              "8c66eaee9125d49703cd068cdba72c305ac38bcbb8e1dab7f399e224cb40a7d6"
            ]
          }
        ],
        "query_ids": [
          "7346ea7e6380af233fc40e0fa6427d3f17efe5fe63447f048542e8ff95f6a406",
          "de9a7ba3598eaa8b41839c2fce3889985e5636fc7489653be3b59378bf4779da"
        ]
      },
      {
        "name": "WpWcReservedStock",
        "source_tables": [
          "wordpress.wp_wc_reserved_stock"
        ],
        "partition_key": {
          "name": "order_id",
          "type": "N"
        },
        "sort_key": {
          "name": "product_id",
          "type": "N"
        },
        "gsis": [],
        "query_ids": [
          "77cd1ad742e2533743bbe7069a7a0a6a58076e5c1a9f9d057191504007e39d1d"
        ]
      }
    ]
  },
  {
    "validation_passed": true,
    "engine": "elasticache",
    "keys": [
      {
        "pattern": "wp_options:{option_name}",
        "data_type": "hash",
        "ttl_seconds": 3600,
        "source_tables": [
          "wordpress.wp_options"
        ],
        "query_ids": [
          "66c96f951dd01698438c1be2887ff2553664cb0b99c1a1b0108b18ca22dcb799",
          "e223aefb06f2d48303ad26d2ee9f8aa957286f08174a6e92a6e583544f8099f0"
        ]
      }
    ]
  }
]
```

### Summary additions

- **`source_engine`**: the source database engine, as the collection states it (`mysql`, `postgresql`, `sqlserver`, ...).
- **`assignment_version`**: the assignment version the result reflects. It rises when the reality check, a customer edit or the post-schema routing (from `"schema_design"` on) writes a new version.
- **`engines[]`**: one line per analysed engine, in analysis order: `engine`, `role`, `statement_count` (statements placed on it), `consolidated_into`, `unique_queries`, `avg_delta` and `reason`. `role` is `owner` (statements are placed on it), `cache_layer` (a cache in front of other engines' reads; it owns no statement), `consolidated` (the reality check moved its statements to other engines; `consolidated_into` is the one that took most of them, the first in the reality check's `consolidations` order on a tie) or `not_selected` (analysed, but no statement was placed on it). `unique_queries` and `avg_delta` are the reality check's unique-value figures for the engine (the statements it held whose score leads the best other engine by at least `unique_delta_threshold` points, and its mean lead), `null` for an engine the check did not assess. `reason` is the executive report's "why this engine" text for an owner or the cache, and the reality check's reason for a consolidated engine.
- **`consolidations[]`**: one line per reality-check consolidation: `from_engine`, `to_engine`, `query_count` and `reason`. Each moved statement names its `from_engine` in `statements[].consolidated_from`.
- **`tables[].reason`**: one sentence on why the owner is the owner: the writes it takes, or the share of the table's queries it serves (and how a tie was settled), and the engines that read a copy.

### Statement additions

- **`query_type`** and **`join_count`**: the statement's type (`SELECT`, `INSERT`, `UPDATE`, `DELETE`, `MERGE` or `OTHER`) and its number of joins, as the library's parser reads them.
- **`matched_patterns`**: the pattern catalog's patterns the statement matches on its assigned engine, as `{"id", "name"}` (for example `{"id": "key-value-lookup", "name": "Key-value lookup"}`). Only the assigned engine's: read another engine's fit from `fit`. Empty at `"analysis"` and for an unservable statement.
- **`post_schema_terminal`**: set when a schema design flagged the statement and the post-schema routing could not move it (see [Post-schema terminal statements](#post-schema-terminal-statements)); `null` otherwise, and always `null` before `"schema_design"`.
- **`query_tags`** are read from the statement text the collection holds, so which sources fill them depends on the collector. PostgreSQL (`pg_stat_statements` text) keeps every leading comment and every sqlcommenter trailing comment. SQL Server and Oracle collections keep block-comment tags (a `--` comment runs into the statement there, because the collectors replace newlines with spaces), which is what sqlcommenter writes. MySQL and MariaDB collections carry no tags at all: both collectors read `DIGEST_TEXT` from `performance_schema.events_statements_summary_by_digest`, and the server removes comments when it normalizes a digest, so a tag a customer prepends to a statement never reaches the collection. For a MySQL or MariaDB source, `query_tags` is always empty, and the library does not read `QUERY_SAMPLE_TEXT` (it holds literal values; [#1314](https://github.com/aws-samples/sample-aws-genai-db-modernizer/issues/1314)). The join key that works there is `query_id`, which for these sources is the statement's `DIGEST`: it hashes the normalized text, so it does not change with a comment. Keep your own map from the digest to the code that issues the statement (on MySQL 8.0, `SELECT STATEMENT_DIGEST('<your statement>')` returns the digest the server will record for it) and join `statements[].query_id` to that map.

How to read it:

- **The engine is decided once.** Read a statement's engine from `statements[].engine` and a table's owner from `summary.tables[].owner`. Do not re-decide engines from scores or table-level labels: ties are already settled, and `reason_code` says which rule decided. To word an engine rationale, use `summary.engines[].reason` and `summary.tables[].reason`.
- **`reason_code`** is one of a fixed list: `highest_score`, `tie_rule`, `co_dependency`, `write_group`, `override`, `fallback`, `consolidated`, `engine_floor`, `sweep`, `capability_flag`, `customer_choice`, `post_schema_reroute`, `unservable`. Routing's own rules are finer-grained; each maps onto one of these.
- **Unservable statements.** With `target_engines` set, a statement no engine in the scope can serve is never forced onto one: its `engine` is `null`, its `reason_code` is `unservable` and `unservable_reason` says why (and which engine outside the scope could serve it). It counts in `statement_count` and `unservable_count`, never in `statements_per_engine`.
- **`excluded`** lists the engines ruled out for a statement: a hard or capability gate, and the engine scope. From `"reality_check"` on it also lists the engines that cannot own the statement's writes (a cache or search engine for a write); routing still scored those, so they appear in `fit` too. An engine in both is scored but cannot take the statement.
- **`confidence`** is the score the statement was placed with, as the assignment stores it. Until [#1373](https://github.com/aws-samples/sample-aws-genai-db-modernizer/issues/1373) is fixed, a statement the reality check consolidated keeps the removed engine's score there; read `fit[engine]` for its score on the engine it is on.
- **`fit`** is the routing scorer's score for the statement on each engine it can be scored on: the same number routing compared. At `"analysis"` it is the score before anything is placed, so a capability that routing allows only once a table's writes are placed is not applied yet.
- **`reason_text`** and every `reason` (including `summary.engines[].reason`, `summary.consolidations[].reason` and `summary.tables[].reason`) are customer-facing sentences. Show them or word your own from them; do not parse them. They can quote collection text (table names, for example), so escape them for the format you render them in.
- **`ruleset_hash`** names the rules that produced the result. Scores compare only within one hash. Asking for an older ruleset is not supported: pin the library version to reproduce a result.

### Post-schema terminal statements

From `"schema_design"` on, a statement whose engine's design reports it unsupported, or whose reviewer flags it, is moved by the post-schema routing to its best remaining engine. When it cannot be moved, the router records it as terminal, and the result marks it:

```json
"post_schema_terminal": {
  "engine": "dynamodb",
  "flag_reason": "missing_feature",
  "cause": "no_kept_engine",
  "reason": "A design review found DynamoDB cannot serve this query (it needs a feature the engine lacks), and no other engine the plan keeps can take it, so it stays on DynamoDB."
}
```

- **`engine`**: the engine whose design flagged it. The statement **stays on that engine**: its `engine` is the same, and its `reason_code` is the one it had before the flag (never `unservable` or `post_schema_reroute`), so it still counts in `statements_per_engine`. That engine's design (its `designs` entry and `artifacts.schema_designs.<engine>`) is the one that flagged it. The pipeline's deliverables show it as application-layer work on that engine.
- **`flag_reason`**: the flag's reason category, one of `full_scan`, `client_side_join`, `missing_feature` (a design's unsupported pattern is always this one) or `other`. A new category is a MINOR change, so read it with a default branch.
- **`cause`**: why it was not moved. `no_kept_engine`: no other engine the placement keeps, in the engine scope, can take it (a cache never takes one); widening `target_engines` can give it one. `depth_limit`: the routing reached its last pass (it moves statements for at most two passes, then only marks what is still flagged).
- **`reason`**: a customer-facing sentence, the one the report states for the statement. Do not parse it.

`summary.post_schema_terminal_count` is the number of marked statements; it is `0` when nothing is terminal and before `"schema_design"`. A marker holds only while the statement is on the flagged engine in the result's assignment version. The marker reports what the routing did; it changes no placement.

## Determinism

- **`run_through: "analysis"`** (no model): the same collection, library version and engine scope give an identical result, byte for byte once serialized with sorted keys, in any process and under any `PYTHONHASHSEED`. The result carries no run timestamp; only the opt-in `artifacts` block records run times. A test runs every bundled sample in separate processes under different hash seeds and compares the results.
- **`run_through: "reality_check"`**: not guaranteed. Everything up to the reality check's model review is deterministic, but the review (`deterministic: false`) can differ between runs, and it can change consolidations. Every change it makes is recorded as a Move with `actor` `"model_flag"` in the assignment's `moves` (`artifacts.assignment.moves`, with `include_artifacts: true`), and the statement it moved has `reason_code` `capability_flag`, so a caller can see which parts came from the review.
- **`run_through: "schema_design"` and `"synthesis"`**: they always use a model, so they are not deterministic. The placement in their result is the reality check's (with the guarantee above: each change the review made is a `model_flag` Move) plus the post-schema routing's moves, each a Move with pass `post_schema_flag` and the statement's `reason_code` `post_schema_reroute`. When no design flags a statement, they change none of the stable fields `"reality_check"` returns for that placement: the statements, groups and summary are the same, except that `summary.engines[].reason` adds the schema design's sentence ("Schema design: ...") and, at `"synthesis"`, `summary.tables` can add a line for a table only a design maps. A moved statement changes what follows from it too (`summary.assignment_version`, `statements_per_engine`, the engines' and tables' lines). A test runs every phase on the WordPress sample with the model calls replaced and checks the first case; others move a flagged statement on the same sample and check the result at `"schema_design"` and `"synthesis"` (the report, the designs and the result are read at the new version). The designs in `artifacts.schema_designs` and the report's executive summary can differ between runs.
- **No stored results.** The library never stores or reuses a result: a repeat call is a new run.

## Artifacts: no compatibility promise

With `include_artifacts: true`, the result also carries `artifacts`: the pipeline's internal contracts, verbatim (`collector_output`, `triage`, `analyses`, `decision_traces`, and from `"reality_check"` on, `assignment` and `reality_check`; from `"schema_design"` on, `schema_designs`, each designed engine's schema output by engine id; at `"synthesis"`, `synthesis_report`; `model_usage`, the run's token usage by phase and model, when a model was called; and `artifact_keys`, the store key each design and the report were read from). Each keeps its own internal `contract_version`, and the block's `compatibility` field says it plainly: they may change in any release, as routing evolves. Use them for debugging and audit, not as an integration surface.

## JSON Schemas

The request and the result are published as JSON Schemas (draft 2020-12) in [`docs/contracts/schemas/`](schemas/README.md), for callers in other languages or callers that validate payloads outside Python:

- [`assess_request.v1.schema.json`](schemas/assess_request.v1.schema.json): `AssessmentRequest`;
- [`assess_result.v1.schema.json`](schemas/assess_result.v1.schema.json): `AssessmentResult`, as `model_dump_json` writes it.

`v1` is the contract's MAJOR version: the file covers every `contract_version` `1.x`, and each states the exact version it was rendered from in `x-contract-version`. The files are generated from the models (`uv run python scripts/render_contract_schemas.py --write`), never edited by hand, and lint fails when they are out of date. A schema describes the shape (including that `"schema_design"` and `"synthesis"` need `deterministic: false`); the library also refuses an unknown engine id, a `label` that does not match `summary.run_through_reached`, and, from contract `1.1` on, `designs` that are not one entry per engine of `summary.designed_engines`, in that order ([schemas/README.md](schemas/README.md)).

## Sample

A request for a SQL Server collection with two reads, scoped to Aurora PostgreSQL and DynamoDB, and the result it could give. The values are illustrative (shortened ids, a placeholder `ruleset_hash`); both payloads validate against the schemas above, and a test keeps them valid.

### Sample request

```json
{
  "contract_version": "1.1",
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
  "contract_version": "1.1",
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
    "post_schema_terminal_count": 0,
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
    ],
    "designed_engines": [],
    "design_note": null
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
      "reason_text": "highest fit for Aurora PostgreSQL",
      "post_schema_terminal": null
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
      "reason_text": "highest fit for DynamoDB",
      "post_schema_terminal": null
    }
  ],
  "groups": [],
  "designs": null,
  "artifacts": null
}
```
