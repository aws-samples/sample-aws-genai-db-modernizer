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

To see every feature run on a bundled sample, try the [library demo](../examples/library/README.md): `uv run python docs/examples/library/demo_assess.py`.

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

**How long the deterministic phases take.** Measured on a laptop with nothing else running, three runs each, in a fresh process: the WordPress sample (107 statements) 0.8 seconds through `"analysis"` and 1.0 through `"reality_check"`; the TrailGear sample (95) 0.8 and 0.9; the Discourse sample (1,654) 6.3 to 6.4 and 9.1 to 9.2. A busy machine is slower: a run of Discourse next to the test suite took 11 to 15.5 seconds. Contract `1.2`'s per-statement fit breakdown adds about 5% through `"reality_check"` and under 2% through `"analysis"` (Discourse, measured side by side with the code before it on another machine: 9.8 to 9.9 seconds before, 10.2 to 10.4 after, and 7.1 to 7.3 seconds through `"analysis"` either way).

**What schema design costs.** It calls a model for every engine it designs, in parallel, and, for the engines that split a large workload, once per group of up to 20 statements, and a reviewer model reads the designs. So its time and spend grow with the number of statements and engines, not with the database's size. Measured on the WordPress sample (107 statements, three designed engines) on Amazon Bedrock in `us-east-1` with the default model roles: two runs through `"synthesis"` took 17 and 18 minutes (one through `"schema_design"` took 20), and the run with token accounting cost $12.49 at Bedrock list prices (40 model calls, 99% of the cost in schema design). Two runs of the [library demo](../examples/library/README.md) took 18 minutes and $13.51, and 34 minutes and $22.14 for one with a post-schema reroute (two engines designed again). A designed run records its token usage per phase and model in `_logs/model_usage.json` under the job (with `include_artifacts: true`, also `artifacts.model_usage`), and `uv run python scripts/usage_cost.py <model_usage.json>` prices it. Like the pipeline, `assess()` designs again each engine a post-schema reroute touches (see below), so a design that cannot serve a statement adds one more design call for the engine it left and the one it joined. Use `"analysis"` or `"reality_check"` (no model, seconds) for engine placement, and `"schema_design"` only when you need a data model.

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

**Progress output.** `assess()` writes nothing to stdout. While it runs, the phases' progress lines go to the `src.assessment.progress` logger at INFO, each record with the run's `job_id` as `record.job_id` (`logging.basicConfig(level=logging.INFO)` shows them).

## The request

| Field | Meaning |
|---|---|
| `contract_version` | The contract version you build on: `"1.0"`, `"1.1"`, `"1.2"` or `"1.3"`; the result is written for the same version ([The result's version](#the-results-version)). Leave it out and you get the latest (`"1.3"` in this release). **Pin it in every request**, so a library upgrade never changes the shape you validate |
| `database_name` | 1 to 128 ASCII letters, digits, underscores or hyphens. It names the run; it need not match the collection's own database name (see below) |
| `collection` | A raw collection: the unmodified output of the collection scripts, or a cut's `collection` ([Cutting a result per service](#cutting-a-result-per-service)). A collector output (any version the library reads) is accepted too, but every derived per-statement fact (tables, statement type, joins, tags) is read again from the statement text by the library's parser, and derived fields it carries are ignored |
| `options.run_through` | `"analysis"`, `"reality_check"` (default), `"schema_design"` or `"synthesis"` ([How far to run](#how-far-to-run-run_through)) |
| `options.target_engines` | Engine scope, as engine catalog ids; `null` means every engine the workload warrants. Unknown, legacy and planned ids, and an empty list, are refused |
| `options.deterministic` | `true` (default): no model is called. `false`: the reality check has a model review its consolidations, which needs model access. Must be `false` for `"schema_design"` and `"synthesis"`, which always use a model |
| `options.include_artifacts` | Include the internal contracts (see below) |
| `scope` | Informational: what a cut workload left out (`tables`, `derived_from`, `rewritten_query_ids`, `dropped_foreign_keys` and, since `1.3`, `unplaced_statements`). Echoed back; no rule reads it. Statements in `scope.rewritten_query_ids` are marked `rewritten_by_caller`. Since `1.3`, `cut_collection` writes the whole scope for a cut, with each `dropped_foreign_keys` entry as `{"constraint_name", "table_id", "referenced_table_id"}`; any other entry is echoed as given |

Unknown fields are refused, so a misspelt option never runs silently with a default.

`database_name` names the run (and, with your own `store`, its folder `<database_name>/<job_id>/`); it does not have to match the database name the collection records. When the collection records its database name, that name wins: table ids (`summary.tables[].table_id`, `groups[].tables`) keep it, so one collection gives the same result whatever name you pass. That holds too when `database_name` names another database the collection's statements belong to (SQL Server's collection records each statement's database): that database's statements are left out either way, and a warning is logged. When the collection records no database name, the table ids take the one database its statements name, if they name exactly one, and `database_name` otherwise. One case is refused as ambiguous: the collection records no database name and its statements belong to several databases, none of them `database_name`. `assess()` then raises `DatabaseNameMismatchError`, a `ValueError` subclass whose message names the databases, and builds no result. The error is the library's, not a field of the result, so it does not change `contract_version`.

## Cutting a result per service

Since `1.3`, `cut_collection` cuts a result down to some of its tables, so you can assess each piece again: call `assess()` once on the whole database, then cut the result per service and assess each cut.

```python
from src.assessment import assess, cut_collection

whole = assess({"contract_version": "1.3", "database_name": "shop", "collection": raw_collection})
cut = cut_collection(
    whole,
    table_ids=["shop.SalesOrder", "shop.SalesOrderLine"],
    parts=[  # optional: a statement that crosses the cut, rewritten for this service
        {"from_query_id": "0x41A1D5EE8655B412",
         "query_text": "SELECT l.[ProductID], l.[Quantity] FROM [SalesOrderLine] l "
                       "JOIN [SalesOrder] o ON o.[SalesOrderID] = l.[SalesOrderID] WHERE o.[CustomerID] = @c"},
    ],
)
orders = assess({
    "contract_version": "1.3",
    "database_name": "orders",
    "collection": cut.collection,
    "scope": cut.scope,
    "options": {"run_through": "reality_check"},
})
```

`cut_collection(result, *, table_ids, parts=())` returns a frozen `CollectionCut`:

| Field | Meaning |
|---|---|
| `table_ids` | The tables (and views) kept, sorted |
| `query_ids` | The statements kept whole and the parts' ids, in the collection's order (a part stands where its statement stood) |
| `scope` | A `WorkloadScope` to pass as the next request's `scope`: `tables` (the tables kept), `rewritten_query_ids` (the parts' ids), `dropped_foreign_keys` (each foreign key between a kept table and a dropped one, either way round: `constraint_name`, `table_id`, the table that holds the key, and `referenced_table_id`), `unplaced_statements` (the statements the cut cannot place, see below) and `derived_from` (carried from an earlier cut) |
| `collection` | Pass it as the next request's `collection`. It is opaque, like a token: do not read or edit it, and read the data from the cut's result instead (`summary.table_facts[]`, `statements[]`) |

Cut the result `assess()` returned: it keeps the collection it read. A result read back from JSON can be cut only when it was assessed with `include_artifacts: true`. The function signature and the four fields are stable and versioned like the result. **Every later release accepts a cut's `collection` written by an earlier one**, even after the library's internal format changes; a test feeds a saved cut back in, and that saved cut is never regenerated (a release that cuts differently adds its own saved cut next to it).

The same result and arguments always give the same cut. Two runs of the same collection give cuts with the same `table_ids`, `query_ids`, `scope` and statements and tables in `collection`; the `collection` also carries the run it was cut from (its job id and the time it read the collection), so those two values differ between runs.

**What a cut keeps.** The tables in `table_ids`, and every statement whose tables all lie among them, whole. `table_ids` can also name a view the collection lists (by the id the statements name it with, for example `vw_ActiveProduct`): a statement over it is then kept, and a part can read it. A view counts as the base tables it reads (its `referenced_tables` when the collection gives them, else its definition as the library's parser reads it in the source dialect), so a view is refused unless every one of its base tables is in `table_ids` too (`ViewOutsideCutError`): keeping `vw_ActiveProduct`, which reads `Product` and `Brand`, in an orders cut would pull the catalog tables back in. A statement that touches a kept table and a dropped one is a *crossing statement*: it is left out, unless you give a part for it. A statement with no kept table is left out. The schema's views and procedures and the database-wide metrics are kept as collected; a trigger on a dropped table is left out. The foreign keys between a kept and a dropped table are left out of the cut and listed in `scope.dropped_foreign_keys`.

**Statements a cut cannot place.** A statement that reads no table of the collection belongs to no service's tables: a procedure call (`EXEC usp_PlaceOrder ...`), a sequence read (`NEXT VALUE FOR ...`), a function call, a session command, or a read of tables the collection does not hold (system catalogs, an unlisted materialized view). No cut keeps one, and every cut lists each in `scope.unplaced_statements` (since `1.3`) as `{"query_id", "reason"}`: `reason` is `no_tables_read` or `tables_not_collected`. Nothing is left out silently. To assess one with a service, give a part for it that names the service's tables (for a procedure, the statement its body runs); it is then placed like any part and is no longer listed.

**Parts.** A part is your rewrite of a crossing (or unplaced) statement over kept tables only: `{"from_query_id": ..., "query_text": ...}` (or a `StatementPart`). A cut takes one part per statement. The library builds each part's `query_id` from your input:

```text
part-<h>-<from_query_id>
```

`<h>` is the first 16 lowercase hexadecimal characters of the SHA-256 of the compact JSON array `[from_query_id, [the cut's table_ids, sorted]]` (UTF-8, no spaces). For example, `0x41A1D5EE8655B412` cut to the trailgear orders tables (`trailgear.OrderStatusHistory`, `trailgear.Payment`, `trailgear.SalesOrder`, `trailgear.SalesOrderLine`, `trailgear.Shipment`) is `part-7855a261de974f29-0x41A1D5EE8655B412`. The id depends only on the statement and the cut's tables, never on the part's text or on what the parser reads in it, so a later release's parser never moves it: the same statement cut to the same tables always gets the same id. No collection script writes an id of this form: every digest they write ([Statement ids](#statement-ids-query_id)) and the SHA-256 fallback is hexadecimal, `0x` and hexadecimal, a signed integer, `fms_` and digits, or an Oracle `SQL_ID`, and none starts with `part-`. A raw collection's `digest` is taken as given, so the guard is the cut itself: it refuses a part whose id is already a statement of the collection.

A part inherits every execution statistic of the statement it was cut from (frequency, calls, timings, rows): the database measured the statement as a whole, and splitting its calls or latency would invent numbers. So **a part's cost can be overstated, never understated**. A part of a crossing statement reads only tables its statement reads, a view counting as its base tables, so those statistics never move onto a table the statement does not read. A part of an unplaced statement is the exception: its statement reads no table, so the part's tables are yours, and it carries the statement's statistics onto them. Its text goes through the same parser and literal redaction as the collected statements ([#1625](https://github.com/aws-samples/sample-aws-genai-db-modernizer/issues/1625)), so routing reads its tables, joins and access pattern the same way.

**The dialect.** A part is in the source database's dialect. It must parse, as exactly one statement, with the library's parser in that dialect (sqlglot, which stops at the first error). Then the constructs below are refused, read on its SQL outside strings and comments. This is a fixed list of other dialects' constructs, not a full dialect check: a construct not listed is left to the parser.

| Source | Refused |
|---|---|
| SQL Server | MySQL backticks, `$1` parameters (a money literal with a fraction, `$19.99`, is kept), `::` casts (a CLR method such as `geography::Point(...)` and a securable class such as `SCHEMA::sales` are kept), `ILIKE`, `LIMIT`, `ROWNUM`, `RETURNING`, `ON CONFLICT`, `\|\|`, `NOW()`, `IFNULL()`, `NVL()` |
| PostgreSQL | backticks, `[...]` names, `TOP`, `@name` parameters, `ROWNUM`, `IFNULL()`, `NVL()`, `ISNULL()`, `GETDATE()` |
| MySQL | `[...]` names, `TOP`, `$1` parameters, `::` casts, `ILIKE`, `ROWNUM`, `RETURNING`, `ON CONFLICT`, `NVL()`, `GETDATE()` |
| MariaDB | as MySQL, except `RETURNING` and `NVL()`, which MariaDB has |
| Oracle | backticks, `[...]` names, `TOP`, `@name` parameters, `$1` parameters, `::` casts, `ILIKE`, `LIMIT`, `ON CONFLICT`, `NOW()`, `IFNULL()`, `ISNULL()`, `GETDATE()` |

SQL Server's `"..."` names are kept: T-SQL reads them as names under `QUOTED_IDENTIFIER ON`, the default. Another source (Db2, for one) is checked by its parser only.

**Refusals.** `cut_collection` never drops a part silently: it raises a `CutError` (a `ValueError` subclass) whose subclass says why:

| Error | When |
|---|---|
| `InvalidTableIdsError` | `table_ids` is not a list of table ids (a string, `None`, an item that is not a non-empty string) |
| `EmptyCutError`, `UnknownTableError` | `table_ids` is empty, or names a table or view the collection does not hold |
| `ViewOutsideCutError` | `table_ids` names a view that reads a table outside `table_ids` (or whose definition names no table the parser can read) |
| `InvalidPartError` | `parts` is not a list, or a part is not `{"from_query_id", "query_text"}` |
| `UnknownStatementError` | `from_query_id` is no statement of the collection (a `NotACrossingStatementError`) |
| `PartOfKeptStatementError` | `from_query_id` is kept whole by the cut: a statement is kept whole or cut into a part, never both |
| `NotACrossingStatementError` | `from_query_id` takes no part in the cut: none of its tables is kept, and it is not unplaced |
| `DuplicatePartError` | Two parts name the same statement |
| `PartDialectError` | The text uses a construct the table above refuses (a `PartParseError`) |
| `PartParseError` | The parser does not read the text as one statement over tables of the collection (for example two statements, or text that is cut off) |
| `PartTableOutsideCutError` | The part names a table outside `table_ids` |
| `PartTableNotInStatementError` | A part of a crossing statement names a kept table its statement never reads (a view counting as its base tables) |
| `PartIdCollisionError` | A part's id is already a statement's id in the collection |
| `CutSourceError` | The result holds no collection to cut (read from JSON without `artifacts`) |

**The cut's result.** The cut is assessed like any collection, at any `run_through`. Its statements are the cut's `query_ids`; a part's is marked `rewritten_by_caller` (it is in `scope.rewritten_query_ids`) and, since `1.3`, carries `from_query_id`, the id of the statement it was cut from (read from its id; `null` for every other statement). A request pinned to `1.2` or earlier gets no `from_query_id`, and its echoed `scope` has no `unplaced_statements`. A row count the original collection did not know stays `null` in the cut's result, as in the whole result, and so does each foreign key average that needs it. A statement with no execution count is the one exception: the collector output records a call rate, so in a cut's result it shows one call a day (`1/86400`), where the whole result shows `null`.

**A cut of a cut.** Cut a cut's result again for a narrower service. The new cut carries the earlier one forward: a part it keeps whole stays in `rewritten_query_ids` (so it stays `rewritten_by_caller`, and keeps its `from_query_id`), the earlier `dropped_foreign_keys` that still touch a kept table and the earlier `unplaced_statements` stay listed, and `derived_from` is kept. A part of an earlier part names that part in `from_query_id`.

## The stable surface

The result's stable surface follows semantic versioning (`contract_version`, separate from the internal contracts' versions). Contract `1.1` added the OpenSearch entry of the design block (#1513), contract `1.2` the fields listed in [From 1.1 to 1.2](#from-11-to-12) (#1603), and contract `1.3` the fields and null rules listed in [From 1.2 to 1.3](#from-12-to-13) (#1632, #1633): foreign key statistics, `row_count_basis`, `parse_status`, `null` for values the collection does not have, and `cut_collection`. The rest of the fields below are part of `1.0`: the additions since its first draft (`label`, `summary.source_engine`, `assignment_version`, `engines[]`, `consolidations[]`, `tables[].reason`, `designed_engines`, `design_note`, `post_schema_terminal_count`, `statements[].query_type`, `join_count`, `matched_patterns` as a list of named patterns and `post_schema_terminal`, and the design block `designs`) did not bump it.

- `contract_version`, `label`, `library_version`, `ruleset_version` and `ruleset_hash`, `options` (as applied, engine ids normalised) and `scope` (echoed);
- `summary`: `run_through_reached`, `source_engine`, `assignment_version`, `statement_count`, `statements_per_engine`, `unservable_count`, `post_schema_terminal_count`, `unique_delta_threshold`, `relational_engine`, `engines[]` (with `unique_query_ids` since `1.2`), `consolidations[]` (with `action` and `queries_retained` since `1.2`), `tables[]` (per source table: `owner`, `owner_basis`, `also_read_by`, `cached_by`, `reason`), `designed_engines`, `design_note` and, since `1.2`, `table_facts[]` (with `row_count_basis` and `foreign_keys[].avg_children_per_parent` and `basis` since `1.3`);
- `statements[]`: per statement, `query_id`, `query_tags`, `query_type`, `join_count`, `tables`, `calls_per_second`, `rewritten_by_caller`, `engine`, `confidence`, `reason_code`, `consolidated_from`, `excluded`, `unservable_reason`, `fit`, `signals`, `matched_patterns`, `tied_engines`, `reason_text` and `post_schema_terminal`, since `1.2` `query_text`, `matched_anti_patterns` and `fit_breakdown`, and since `1.3` `parse_status` and `from_query_id`;
- `groups[]`: statements that move together, `kind` `"co_dependency"` (they join the same tables) or `"write_group"` (every write of one table: a table has one writer);
- `designs`: a designed result's data model per designed engine ([The design block](#the-design-block)); `null` unless `label` is `designed`.

Consumer contract tests keep this surface (`tests/contract/test_assess_consumer_contract.py`, on the fixtures in `tests/contract/fixtures/assess_consumer_fixtures.json`, each with the fields it guards), and a removed or renamed field, a changed type or a removed or changed enum value fails the build without a MAJOR bump of `contract_version` against the shape the last release shipped (`tests/contract/test_assess_released_contract.py`). A new enum value in the result (a new `reason_code` or `role`, for example) is a MINOR change: read enums with a default branch, since an exhaustive match on today's values can meet a new one in a MINOR release. Each release lists the routing changes since the previous one (CONTRIBUTING.md's Releases section).

Fields that come from a later phase are `null` (or empty) when `run_through` stops earlier: at `"analysis"`, nothing is placed, so `engine`, `confidence` and `reason_code` are `null`, `summary.assignment_version` is `null`, `summary.engines`, `summary.consolidations`, `summary.tables` and every statement's `matched_patterns` are empty, and every group's `engine` is `null`. Before `"schema_design"`, and when a model phase had no engine to design, `designs` is `null`. `summary.table_facts` and each statement's `query_text`, `matched_anti_patterns` and `fit_breakdown` are filled from `"analysis"` on.

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
| `dynamodb` | `tables[]`: `name`, `source_tables`, `partition_key` and `sort_key` (`{"name", "type"}`, `type` `S`, `N` or `B`; `sort_key` `null` for none), `gsis[]` (`name`, `partition_key[]` and `sort_key[]` as ordered key attributes, `projection` `ALL`, `KEYS_ONLY` or `INCLUDE`, `projected_attributes` for `INCLUDE`, `query_ids`) and `query_ids` (the statements the design serves from the table itself; those served from an index are on the index); since `1.2`, `aggregate_pattern` (how the design shares the table among its source tables: `identifying_relationship`, `item_collection` or `separate`; `null` when the design does not say) and `entities[]` (an item collection's entity types: `source_table`, `pk_template` and `sk_template`, the key value templates, for example `USER#{id}` and `ORDER#{order_id}`; empty for any other table) |
| `aurora_mysql`, `aurora_postgresql` | `tables[]`: `name`, `primary_key` (columns) and `indexes` (the design's `CREATE INDEX` statements, verbatim); `ddl_artifact_path`, where the full generated DDL is in the result with `include_artifacts: true` (`artifacts.schema_designs.<engine>.generated_ddl`) |
| `documentdb` | `collections[]`: `name`, `source_tables`, `embedded[]` (per child table: `source_table`, `path` in the parent document, `strategy` `embed`, `reference` or `hybrid`), `referenced_collections`, `indexes[]` (`name`, `keys[]` as ordered `{"field", "direction"}`, `type` `compound`, `single`, `text`, `partial` or `unique`, `query_ids`) and `query_ids` |
| `elasticache` | `keys[]`: `pattern` (the key pattern), `data_type`, `ttl_seconds` (`null` for no expiry), `source_tables` and `query_ids` (what the key caches: the source tables and the statements whose reads it serves) |
| `opensearch` | `indexes[]`: `name`, `source_tables`, `fields[]` (its mapped fields in the design's order, `{"name", "type"}`, `type` the OpenSearch field type as the design names it), `aliases` and `query_ids` (the statements the design serves from the index); `data_streams[]`: `name`, `source_tables`, `fields[]` (its index template's mapped fields, with `@timestamp`) and `query_ids` |

The OpenSearch entry was added in contract `1.1` (#1513). From `1.1` on, every engine in `summary.designed_engines` has its entry, in that order, and the result model refuses one without it. A `1.0` result is not checked for this: it is read exactly as contract `1.0` read it, so it can list `opensearch` in `summary.designed_engines` with no `designs` entry, and still validates. A new engine in `designs`, or a new optional field in an entry, is a MINOR change: read it with a default branch, and validate a result against the schema of the `contract_version` it carries (an older schema file refuses an engine it does not know). Names (tables, keys, indexes) come from the design, which a model wrote from collection text: treat them as data and escape them when you render them.

### Sample design block

`designs` of a designed WordPress run (`run_through: "schema_design"`, no engine scope), trimmed to one or two entries per list. The run predates contract `1.2`, so its DynamoDB tables do not show `aggregate_pattern` and `entities`; the [design block standard](assess-design-block.md#dynamodb) has an item collection with them.

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
- **`engines[]`**: one line per analysed engine, in analysis order: `engine`, `role`, `statement_count` (statements placed on it), `consolidated_into`, `unique_queries`, `avg_delta` and `reason`. `role` is `owner` (statements are placed on it), `cache_layer` (a cache in front of other engines' reads; it owns no statement), `consolidated` (the reality check moved its statements to other engines; `consolidated_into` is the one that took most of them, the first in the reality check's `consolidations` order on a tie) or `not_selected` (analysed, but no statement was placed on it). `unique_queries` and `avg_delta` are the reality check's unique-value figures for the engine (the statements it held whose score leads the best other engine by at least `unique_delta_threshold` points, and its mean lead), `null` for an engine the check did not assess. Since `1.2`, `unique_query_ids` names the statements `unique_queries` counts, by `query_id`, in the reality check's order: the library's own first-pass unique-value set for the engine (so `unique_queries` is its length), `null` when `unique_queries` is. `reason` is the executive report's "why this engine" text for an owner or the cache, and the reality check's reason for a consolidated engine.
- **`consolidations[]`**: one line per reality-check consolidation: `from_engine`, `to_engine`, `query_count` and `reason`, and since `1.2` `action` (`full`: every statement the engine held moved; `partial`: some stayed) and `queries_retained` (a partial consolidation's statements that stayed on `from_engine`, by `query_id`; empty for a full one). Each moved statement names its `from_engine` in `statements[].consolidated_from`.
- **`tables[].reason`**: one sentence on why the owner is the owner: the writes it takes, or the share of the table's queries it serves (and how a tie was settled), and the engines that read a copy.

### Statement additions

- **`query_type`** and **`join_count`**: the statement's type (`SELECT`, `INSERT`, `UPDATE`, `DELETE`, `MERGE` or `OTHER`) and its number of joins, as the library's parser reads them.
- **`matched_patterns`**: the pattern catalog's patterns the statement matches on its assigned engine, as `{"id", "name"}` (for example `{"id": "key-value-lookup", "name": "Key-value lookup"}`). Only the assigned engine's: read another engine's fit from `fit`. Empty at `"analysis"` and for an unservable statement.
- **`query_text`** (since `1.2`): the statement text the library's parser read. When rows sharing a digest merged into one statement (see [Statement ids](#statement-ids-query_id)), it is the costliest variant's text (the highest total time). It is collection text with its literal values redacted ([#1625](https://github.com/aws-samples/sample-aws-genai-db-modernizer/issues/1625)): MySQL and MariaDB (`DIGEST_TEXT`) and PostgreSQL (`pg_stat_statements`) normalise it themselves, and on every source the library replaces each string, number, date and binary literal left in it with the engine's own kind of placeholder (`?`, Oracle's `:n`, PostgreSQL's `$n`) before it stores or parses the text. What the parser reads as structure stays: row limits and offsets (`TOP (10)`, `FETCH FIRST 1 ROWS ONLY`), lock waits (`FOR UPDATE WAIT 5`), sample sizes, `NTILE` counts, `ORDER BY` positions and type sizes (a kept number has at most nine digits, so a card number is never one), JSON paths, date parts and formats, and the shape of a `LIKE` or regular-expression pattern (`'%jane@example.com%'` becomes `'%?%'`, `'Jane%'` becomes `'x%'`); a prefixed string keeps its form (`N'?'`). The redaction keeps what routing reads, so a statement is placed as it was before it (tests hold the bundled samples, and SQL Server and Oracle collections with literals, to that). Comments are kept, less any e-mail address and run of nine or more digits in them; a comment that is not a sqlcommenter tag also has every run between two quotes written `?`. Escape the text when you render it.
- **`matched_anti_patterns`** (since `1.2`): the penalised anti-patterns found on the statement against any analysed engine, once each, in catalog id order, as `{"id", "name", "intrinsic"}` (for example `{"id": "frequent-full-scan", "name": "Frequent full scan", "intrinsic": false}`). `intrinsic` follows [#1231](https://github.com/aws-samples/sample-aws-genai-db-modernizer/issues/1231): `true` means it costs the engines it is found against points; `false` means it is fixable, because a redesign of the target removes it, so it costs nothing. Which engines it costs, and how much, is in `fit_breakdown[engine].penalties`.
- **`fit_breakdown`** (since `1.2`): per engine, how the scorer reached `fit` ([The fit breakdown](#the-fit-breakdown)).
- **`from_query_id`** (since `1.3`): on a statement part of a cut, the `query_id` of the statement it was cut from ([Cutting a result per service](#cutting-a-result-per-service)); `null` for every other statement.
- **`post_schema_terminal`**: set when a schema design flagged the statement and the post-schema routing could not move it (see [Post-schema terminal statements](#post-schema-terminal-statements)); `null` otherwise, and always `null` before `"schema_design"`.
- **`query_tags`** are read from the statement text the collection holds, so which sources fill them depends on the collector. A tag is redacted as the comment it came from ([#1625](https://github.com/aws-samples/sample-aws-genai-db-modernizer/issues/1625)): a sqlcommenter tag (a comment that is only comma-separated `key='value'` pairs) keeps its keys and values (they name code: `controller='cart'`), less any e-mail address or run of nine or more digits; any other tag also has every run between two quotes written `?` (`don't 'Jane'` becomes `don'?'?'`). Free words are kept, so a tag can still name a person in plain words: keep personal data out of the comments your application writes. PostgreSQL (`pg_stat_statements` text) keeps every leading comment and every sqlcommenter trailing comment. SQL Server and Oracle collections keep block-comment tags (a `--` comment runs into the statement there, because the collectors replace newlines with spaces), which is what sqlcommenter writes. MySQL and MariaDB collections carry no tags at all: both collectors read `DIGEST_TEXT` from `performance_schema.events_statements_summary_by_digest`, and the server removes comments when it normalizes a digest, so a tag a customer prepends to a statement never reaches the collection. For a MySQL or MariaDB source, `query_tags` is always empty, and the library does not read `QUERY_SAMPLE_TEXT` (it holds literal values; [#1314](https://github.com/aws-samples/sample-aws-genai-db-modernizer/issues/1314)). The join key that works there is `query_id`, which for these sources is the statement's `DIGEST`: it hashes the normalized text, so it does not change with a comment. Keep your own map from the digest to the code that issues the statement (on MySQL 8.0, `SELECT STATEMENT_DIGEST('<your statement>')` returns the digest the server will record for it) and join `statements[].query_id` to that map.

How to read it:

- **The engine is decided once.** Read a statement's engine from `statements[].engine` and a table's owner from `summary.tables[].owner`. Do not re-decide engines from scores or table-level labels: ties are already settled, and `reason_code` says which rule decided. To word an engine rationale, use `summary.engines[].reason` and `summary.tables[].reason`.
- **`reason_code`** is one of a fixed list: `highest_score`, `tie_rule`, `co_dependency`, `write_group`, `override`, `fallback`, `consolidated`, `engine_floor`, `sweep`, `capability_flag`, `customer_choice`, `post_schema_reroute`, `unservable`. Routing's own rules are finer-grained; each maps onto one of these.
- **Unservable statements.** With `target_engines` set, a statement no engine in the scope can serve is never forced onto one: its `engine` is `null`, its `reason_code` is `unservable` and `unservable_reason` says why (and which engine outside the scope could serve it). It counts in `statement_count` and `unservable_count`, never in `statements_per_engine`.
- **`excluded`** lists the engines ruled out for a statement: a hard or capability gate, and the engine scope. From `"reality_check"` on it also lists the engines that cannot own the statement's writes (a cache or search engine for a write); routing still scored those, so they appear in `fit` too. An engine in both is scored but cannot take the statement.
- **`confidence`** is the score the statement was placed with, as the assignment stores it. Until [#1373](https://github.com/aws-samples/sample-aws-genai-db-modernizer/issues/1373) is fixed, a statement the reality check consolidated keeps the removed engine's score there; read `fit[engine]` for its score on the engine it is on.
- **`fit`** is the routing scorer's score for the statement on each engine it can be scored on: the same number routing compared. At `"analysis"` it is the score before anything is placed, so a capability that routing allows only once a table's writes are placed is not applied yet. `fit_breakdown` shows how each score was reached ([The fit breakdown](#the-fit-breakdown)).
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

### The fit breakdown

`statements[].fit_breakdown` says, per engine, how the routing scorer reached the statement's `fit` on that engine. The scorer writes it while it scores (`score_query_for_engine`, the same call and the same arithmetic routing compares), so it explains `fit`. It never decides the engine: read that from `engine` and `reason_code`. It has one entry per engine the scorer saw for the statement. At `"analysis"`, that is every analysed engine. From `"reality_check"` on, it is every engine on the statement's candidate ladder (the scores `fit` is read from). Every engine in `fit` has an entry with the same `fit`, and an engine with no score has an entry with only its `gate`. From contract `1.2` on, the result model refuses a result where this does not hold.

| Field | Meaning |
|---|---|
| `gate` | `{"kind", "reason"}`, set only when the engine is gated: it then has no score, and every other field is `null` or empty. `kind` is, for example, `hard_exclusion`, `capability`, `model_flag` (a model's flag the reality check accepted) or `write_gate` (an engine that cannot own the statement's write, on a ladder rung routing recorded with no score); `reason` is a customer-facing sentence |
| `method` | The rule the base score followed: `per_table_weighted_mean` (a statement with an access-pattern class); `pattern_only_no_class_score` (a pattern-only engine, such as OpenSearch, whose catalog gives the statement's class no score: the base is 0); or, for a statement with no access-pattern class (`utility` or `unparsed`), `unclassified_own_tables` (its own rated tables' confidence), `unclassified_all_tables` (every rated table's, when it has none of its own) or `unclassified_pattern_tables` (on a pattern-only engine, the tables of the patterns that matched it) |
| `pattern` | `{"score", "source", "class"}`: the statement's own pattern dimension on the engine (`source` `"statement"`), the score of its access-pattern `class`. When a redesign scores higher and routing relies on it (#1231), `class` is the redesigned class. `null` under an `unclassified_*` method |
| `cost` | `{"score", "source"}`: the statement's own cost dimension on the engine (`source` `"statement"`). `null` under an `unclassified_*` method |
| `tables[]` | Each of the statement's tables the engine's analysis rated: `table_id`, `complexity` and `performance` (the table's own dimensions in that analysis), `weights` (the weight of each dimension for the table: `pattern`, `complexity`, `performance`, `cost`) and `score`. Under an `unclassified_*` method, `score` is the table's confidence and the other three are `null` |
| `base` | The base score: the mean of `tables[].score` |
| `penalties[]` | Each anti-pattern finding on the statement for the engine: `id`, `name`, `points` (what it costs the engine: the catalog's penalty, after its capability scoping, when `intrinsic`; 0 when fixable) and `intrinsic` |
| `fit` | The score: the same number as `fit[engine]` |

How the numbers fit together (a test recomputes it this way for every statement and engine of the three bundled samples, before and after placement):

- under `per_table_weighted_mean`, a table's `score` is `floor(pattern.score × weights.pattern + complexity × weights.complexity + performance × weights.performance + cost.score × weights.cost)`, summed in that order in double precision;
- `base` is `floor(mean(tables[].score))`, and 0 when `tables` is empty;
- `fit` is `max(0, base − p)`, where `p` is the largest `points` in `penalties` (0 when there is none). Penalties do not add up: the largest one applies.

A gate that routing lifts does not show. A capability waiver, or the guarantee that a homogeneous source's own Aurora engine can serve every statement, lets routing score an engine the plain gates would exclude. That engine has a score, and its entry is the arithmetic past the gate. The ownership gate (an engine that is not a system of record cannot take a write) keeps the score too: such an engine has a normal entry and is listed in `excluded`. The `gate` kinds, the `method` values and the dimension names (`pattern`, `cost`, the keys of `weights`) are open lists: read them with a default branch.

The table-level analysis ratings are not exposed on their own. A statement's `fit` takes the complexity and performance dimensions from its own tables (`tables[]` above), but its pattern and cost dimensions are its own, so a table's combined analysis score shows pattern and cost values routing replaced. For a per-table view on an engine, read the statements that touch the table (`statements[].tables`): their `fit[engine]` and, in each `fit_breakdown[engine].tables[]` entry for the table, its `complexity` and `performance`.

### Statement ids (`query_id`)

A statement's `query_id` is the `digest` of its row in the collection. A row with no `digest` gets the first 16 hexadecimal characters of the SHA-256 of its text (UTF-8), as the collection holds it: the text before its literals are redacted (see `query_text`), so the redaction changes no `query_id`. Each collection script writes the digest its engine computes for a query shape:

| Source engine | `digest`, and so `query_id` |
|---|---|
| MySQL, MariaDB | `DIGEST` from `performance_schema.events_statements_summary_by_digest` (64 hexadecimal characters on MySQL 8.0) |
| PostgreSQL | `queryid` from `pg_stat_statements`, as text (a signed 64-bit integer, for example `-3141592653589793238`) |
| SQL Server | `query_hash` as hexadecimal, as `CONVERT(VARCHAR(40), query_hash, 1)` writes it: `0x` and 16 hexadecimal digits, for example `0x1A2B3C4D5E6F7081` |
| Oracle | `fms_` and the `FORCE_MATCHING_SIGNATURE`, or the `SQL_ID` when the signature is 0 |

Rows that share a digest are one statement. Their counters are summed, and `query_text` is the costliest variant's text. On an Oracle source, rows whose texts differ only in their literals are one statement too, even with different digests (a collection made without `FORCE_MATCHING_SIGNATURE`), and it takes the digest of its costliest variant. Statements are listed in order of first appearance. A collector output passed as the collection keeps the `query_id`s it carries. A test per source engine pins this rule.

### Table facts

`summary.table_facts[]` (since `1.2`) has one entry per table in the collection, in `table_id` order, from `"analysis"` on: `table_id`, `row_count`, `size_mb`, `columns[]` (`name`, `data_type` as the source engine names it, `nullable`; in the table's column order), `primary_key` (its columns, in order; empty for none) and `foreign_keys[]`, one entry per constraint: `constraint_name`, `columns`, `referenced_table` and `referenced_columns` (paired with `columns`). `referenced_table` is the referenced table's `table_id` in this result: the table of that name in the referencing table's schema, else the only collected table of that name. It is `null` when the collection holds no such table, or when several schemas hold one and none is the referencing table's. The collectors keep only the referenced table's bare name, not its schema, so a foreign key to another schema's table can resolve to the wrong one: when both `sales.Customers` and `hr.Customers` exist, a key from `sales.Orders` to `hr.Customers` resolves to `sales.Customers`, the referencing table's own schema.

The ids are the library's own, the ones `statements[].tables`, `summary.tables[].table_id` and `groups[].tables` use, so you can join on them. The facts are what the library read from the collection, never a model's.

#### Foreign key statistics

Since `1.3`, each `foreign_keys[]` entry also carries two statistics about its two tables, filled from `"analysis"` on, with no model call. **They are statistics, not a design decision: an average cannot show whether a child set is bounded. For any data model, the recommended path is `run_through: "schema_design"`.**

| Field | Meaning |
|---|---|
| `avg_children_per_parent` | The table's row count divided by the referenced table's row count, unrounded: how many rows of this table there are per row of `referenced_table`, on average. `null` when `referenced_table` is `null`, when either table's `row_count` is `null`, or when the referenced table has 0 rows |
| `basis` | Where the counts come from: each table's row count from the source catalogue, as the collection script read it (MySQL and MariaDB `information_schema.tables.table_rows`, PostgreSQL `pg_class.reltuples`, SQL Server `sys.partitions.rows`, Oracle `NUM_ROWS`). `"catalogue_exact"` when both tables' `row_count_basis` is `"exact"`, `"catalogue_estimate"` otherwise. `null` when `avg_children_per_parent` is `null`. A new value is a MINOR change |

- The values belong to one foreign key constraint. A composite key has one entry with one average, and a second foreign key to the same table has its own entry, with the same average as the first, since both divide the same two counts.
- A self-referencing key (a table that references itself) divides the table's count by itself: `1.0` for a table with rows.
- A count is unknown when `table_facts[].row_count` is `null` (since `1.3`: the collection has no count for the table). The average is then `null`, never computed from a stand-in. It is `0.0` only when both counts are known and the table has no rows.
- There is no maximum or distribution: no collection script counts the rows per parent, and the library does not read table rows. An average of 3 fits both a table where every parent has 3 rows and one where one parent has thousands.

Why a separate list, and not new fields on `summary.tables[]`: `summary.tables[]` is the ownership view. It is empty at `"analysis"`, and it lists only the tables the placement (or, at `"synthesis"`, the report) gives an owner line. Facts on it would either change what it holds at `"analysis"` or leave out the tables no statement touches. `summary.table_facts[]` covers every collected table at every phase, and `summary.tables[]` keeps its meaning.

### From 1.1 to 1.2

Contract `1.2` (#1603) is a MINOR version. It only adds fields, each exposing a result the library already computes:

- `statements[].query_text`, `matched_anti_patterns[]` and `fit_breakdown`;
- `summary.table_facts[]`;
- `summary.consolidations[].action` and `queries_retained`;
- `summary.engines[].unique_query_ids`;
- on a DynamoDB design entry, `tables[].aggregate_pattern` and `entities[]`.

`statements[].query_tags` is unchanged: it has been in the result since `1.0`. No field was renamed, removed, retyped or given a new meaning. The published schema lists the new fields as optional, so a `1.0` or `1.1` result still validates against it, although the library always writes them. A result written for `1.0` or `1.1` loads with the library's models, is not checked by the `1.2` rule on `fit_breakdown`, and is written back as it was read, without the new fields. To read the new fields, check `contract_version` first, or treat a missing field as not stated.

`ruleset_hash` changes in this release, although no score and no engine placement changes: the hash covers the scorer's source code, and the scorer now also writes `fit_breakdown`. For this change only, a `1.2` result's `fit` compares with the previous release's `1.1` result for the same collection, although the two hashes differ.

### From 1.2 to 1.3

Contract `1.3` (#1632, #1633) is a MINOR version. It adds optional fields: on each `summary.table_facts[].foreign_keys[]` entry, `avg_children_per_parent` and `basis` ([Foreign key statistics](#foreign-key-statistics)); on each `summary.table_facts[]` entry, `row_count_basis` (`"exact"` where the catalogue keeps an exact count, MySQL-family MyISAM and Aria; `"estimate"` for PostgreSQL `reltuples`, Oracle `NUM_ROWS`, InnoDB `TABLE_ROWS` and SQL Server `sys.partitions.rows`; `null` when `row_count` is `null`, or when the collection does not say how its counts were recorded); and on each statement, `parse_status`: `"parsed"` when the library's SQL parser read the statement, `"fallback"` when it could not, so its `query_type`, `join_count` and `tables` are a pattern-based estimate (`null` for a statement with no text). They are statistics from the collection, not a design decision: an average cannot show whether a child set is bounded, and the recommended path for any data model stays `run_through: "schema_design"`.

It also adds:

- `cut_collection` and its `CollectionCut` ([Cutting a result per service](#cutting-a-result-per-service)): a cut's `collection` is a new kind of request `collection`, and the request's `scope.dropped_foreign_keys` (a list of objects since `1.0`) gets the entry shape `cut_collection` writes, `{"constraint_name", "table_id", "referenced_table_id"}`; any other entry is still echoed as given;
- the optional `scope.unplaced_statements` (`{"query_id", "reason"}`), where a cut lists the statements it cannot place; a result written for `1.2` or earlier does not echo it;
- the optional `statements[].from_query_id`, set on a cut's statement parts.

Unknown is `null` from `1.3`: where the collection gives no value, the fields below are `null` in a `1.3` result, where `1.2` wrote the collector's default for it. A `1.2` result keeps the `1.2` value, so in `1.2` these defaults can mean unknown:

| Field | Unknown in the collection | `1.2` value | `1.3` value |
|---|---|---|---|
| `summary.table_facts[].row_count` | no count for the table (the collector output's `row_count` is `null`) | `0` | `null` |
| `summary.table_facts[].size_mb` | no size for the table | `0.0` | `null` |
| `summary.table_facts[].columns[].nullable` | no `is_nullable`, `NULL`, or a value no collection script writes, in a raw collection | `true` (none given), `false` (the others) | `null` |
| `statements[].calls_per_second` | no `execution_count`, or `NULL`, in a raw collection | `1/86400` (one call a day) | `null` |
| `statements[].calls_per_second` | an `execution_count` of `0` (a real zero) | `1/86400` | `0.0` |
| `statements[].query_text` | the row has no text | `""` | `null` |

A known value is the same in both, so in `1.3` a `row_count` of `0` is an empty table. `nullable` and `calls_per_second` are `null` only for a raw collection: a collector output passed as the collection records no unknown for them, so it carries the collector's values (`true` or `false`, one call a day). `row_count` and `size_mb` follow the collector output's own `null`. The routing still reads the parser's figure, so a statement with no execution count, or a count of 0, is scored as one call a day. This change moves no score or engine placement.

Some values keep their meaning, and can still stand for "not known":

- `statements[].calls_per_second` is the execution count over a 24-hour window: the collection scripts read cumulative counters, and the library divides by one day.
- An empty `primary_key` or `foreign_keys` list means the table has none, or that the collection did not collect them.
- `columns[].data_type` is `""` when the collection gives no type.
- On a design entry, `ttl_seconds: null` means no expiry, or that the design did not state one; a missing `validation_passed` reads as `false`.

No field was renamed, removed or retyped. The published schema lists the new fields as optional, so a `1.0`, `1.1` or `1.2` result still validates against it. A result written for `1.2` loads with the library's models and is written back as it was read, without the new fields. A request pinned to `1.0`, `1.1` or `1.2` gets the same result, byte for byte, as before this version for the same collection.

### The result's version

The result follows the request's `contract_version`. A request for `"1.1"` gets a `1.1` result: the library writes it as `1.1` wrote it, without any field `1.2` added, and it validates against the `1.1` schema file. A request for `"1.0"` gets a `1.0` result, without the `1.2` fields and without the design entries `1.1` added (OpenSearch). A request for `"1.2"` gets a `1.2` result, without the fields `1.3` added. A request for `"1.3"`, or one that names no version, gets a `1.3` result. As Python objects, the models still hold every field: the version decides what is written as JSON (`model_dump_json`, `model_dump`) and which rules a result is checked by.

Pin `contract_version` in every request. A request that leaves it out gets the latest version, and each version's schema file refuses fields it does not know, so after an upgrade a newer result fails a validator built on an older schema file.

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

`v1` is the contract's MAJOR version: the file covers every `contract_version` `1.x`, and each states the exact version it was rendered from in `x-contract-version`. The files are generated from the models (`uv run python scripts/render_contract_schemas.py --write`), never edited by hand, and lint fails when they are out of date. A schema describes the shape (including that `"schema_design"` and `"synthesis"` need `deterministic: false`); the library also refuses an unknown engine id, a `label` that does not match `summary.run_through_reached`, from contract `1.1` on, `designs` that are not one entry per engine of `summary.designed_engines`, in that order, and, from contract `1.2` on, a `fit_breakdown` that does not have an entry with the same `fit` for every engine in `fit` ([schemas/README.md](schemas/README.md)). The fields contracts `1.2` and `1.3` added are optional in the schema, so a result of an earlier `1.x` validates against it.

## Sample

A request for a SQL Server collection with two reads, scoped to Aurora PostgreSQL and DynamoDB, and the result it could give. The values are illustrative (shortened ids, a placeholder `ruleset_hash`); both payloads validate against the schemas above, and a test keeps them valid.

### Sample request

```json
{
  "contract_version": "1.3",
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
  "contract_version": "1.3",
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
        "reason": "84% mean fit across 1 query (2 rated tables), pinned by multi-table joins (1 of 1).",
        "unique_query_ids": [
          "Q01"
        ]
      },
      {
        "engine": "dynamodb",
        "role": "owner",
        "statement_count": 1,
        "consolidated_into": null,
        "unique_queries": 0,
        "avg_delta": 4.0,
        "reason": "81% mean fit across 1 query (1 rated table), led by key-value lookups (1 of 1).",
        "unique_query_ids": []
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
    "design_note": null,
    "table_facts": [
      {
        "table_id": "sales.Carts",
        "row_count": 120000,
        "row_count_basis": "estimate",
        "size_mb": 48.5,
        "columns": [
          {
            "name": "CartID",
            "data_type": "int",
            "nullable": false
          },
          {
            "name": "CustomerID",
            "data_type": "int",
            "nullable": false
          },
          {
            "name": "UpdatedAt",
            "data_type": "datetime2",
            "nullable": true
          }
        ],
        "primary_key": [
          "CartID"
        ],
        "foreign_keys": []
      },
      {
        "table_id": "sales.OrderLines",
        "row_count": 2400000,
        "row_count_basis": "estimate",
        "size_mb": 310.0,
        "columns": [
          {
            "name": "OrderID",
            "data_type": "int",
            "nullable": false
          },
          {
            "name": "LineNo",
            "data_type": "smallint",
            "nullable": false
          },
          {
            "name": "Sku",
            "data_type": "nvarchar(40)",
            "nullable": false
          },
          {
            "name": "Quantity",
            "data_type": "int",
            "nullable": false
          }
        ],
        "primary_key": [
          "OrderID",
          "LineNo"
        ],
        "foreign_keys": [
          {
            "constraint_name": "FK_OrderLines_Orders",
            "columns": [
              "OrderID"
            ],
            "referenced_table": "sales.Orders",
            "referenced_columns": [
              "OrderID"
            ],
            "avg_children_per_parent": 4.0,
            "basis": "catalogue_estimate"
          }
        ]
      },
      {
        "table_id": "sales.Orders",
        "row_count": 600000,
        "row_count_basis": "estimate",
        "size_mb": 95.25,
        "columns": [
          {
            "name": "OrderID",
            "data_type": "int",
            "nullable": false
          },
          {
            "name": "CustomerID",
            "data_type": "int",
            "nullable": false
          },
          {
            "name": "PlacedAt",
            "data_type": "datetime2",
            "nullable": false
          }
        ],
        "primary_key": [
          "OrderID"
        ],
        "foreign_keys": []
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
      "reason_text": "highest fit for Aurora PostgreSQL",
      "post_schema_terminal": null,
      "query_text": "SELECT o.OrderID, l.Sku, l.Quantity FROM sales.Orders o JOIN sales.OrderLines l ON l.OrderID = o.OrderID WHERE o.OrderID = @OrderID",
      "parse_status": "parsed",
      "matched_anti_patterns": [
        {
          "id": "frequent-full-scan",
          "name": "Frequent full scan",
          "intrinsic": false
        },
        {
          "id": "large-result-sets",
          "name": "Large result sets",
          "intrinsic": true
        }
      ],
      "fit_breakdown": {
        "aurora_postgresql": {
          "gate": null,
          "method": "per_table_weighted_mean",
          "pattern": {
            "score": 85,
            "source": "statement",
            "class": "key_scoped"
          },
          "cost": {
            "score": 90,
            "source": "statement"
          },
          "tables": [
            {
              "table_id": "sales.Orders",
              "complexity": 90,
              "performance": 75,
              "weights": {
                "pattern": 0.4,
                "complexity": 0.3,
                "performance": 0.2,
                "cost": 0.1
              },
              "score": 85
            },
            {
              "table_id": "sales.OrderLines",
              "complexity": 85,
              "performance": 75,
              "weights": {
                "pattern": 0.4,
                "complexity": 0.3,
                "performance": 0.2,
                "cost": 0.1
              },
              "score": 83
            }
          ],
          "base": 84,
          "penalties": [
            {
              "id": "frequent-full-scan",
              "name": "Frequent full scan",
              "points": 0,
              "intrinsic": false
            }
          ],
          "fit": 84
        },
        "dynamodb": {
          "gate": null,
          "method": "per_table_weighted_mean",
          "pattern": {
            "score": 75,
            "source": "statement",
            "class": "key_scoped"
          },
          "cost": {
            "score": 80,
            "source": "statement"
          },
          "tables": [
            {
              "table_id": "sales.Orders",
              "complexity": 80,
              "performance": 70,
              "weights": {
                "pattern": 0.4,
                "complexity": 0.3,
                "performance": 0.2,
                "cost": 0.1
              },
              "score": 76
            },
            {
              "table_id": "sales.OrderLines",
              "complexity": 80,
              "performance": 70,
              "weights": {
                "pattern": 0.4,
                "complexity": 0.3,
                "performance": 0.2,
                "cost": 0.1
              },
              "score": 76
            }
          ],
          "base": 76,
          "penalties": [
            {
              "id": "large-result-sets",
              "name": "Large result sets",
              "points": 10,
              "intrinsic": true
            }
          ],
          "fit": 66
        }
      }
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
      "post_schema_terminal": null,
      "query_text": "/* endpoint: CartController.Get */ SELECT CartID, CustomerID, UpdatedAt FROM sales.Carts WHERE CartID = @CartID",
      "parse_status": "parsed",
      "matched_anti_patterns": [],
      "fit_breakdown": {
        "aurora_postgresql": {
          "gate": null,
          "method": "per_table_weighted_mean",
          "pattern": {
            "score": 80,
            "source": "statement",
            "class": "key_lookup"
          },
          "cost": {
            "score": 85,
            "source": "statement"
          },
          "tables": [
            {
              "table_id": "sales.Carts",
              "complexity": 80,
              "performance": 65,
              "weights": {
                "pattern": 0.4,
                "complexity": 0.3,
                "performance": 0.2,
                "cost": 0.1
              },
              "score": 77
            }
          ],
          "base": 77,
          "penalties": [],
          "fit": 77
        },
        "dynamodb": {
          "gate": null,
          "method": "per_table_weighted_mean",
          "pattern": {
            "score": 100,
            "source": "statement",
            "class": "key_lookup"
          },
          "cost": {
            "score": 70,
            "source": "statement"
          },
          "tables": [
            {
              "table_id": "sales.Carts",
              "complexity": 75,
              "performance": 60,
              "weights": {
                "pattern": 0.4,
                "complexity": 0.3,
                "performance": 0.2,
                "cost": 0.1
              },
              "score": 81
            }
          ],
          "base": 81,
          "penalties": [],
          "fit": 81
        }
      }
    }
  ],
  "groups": [],
  "designs": null,
  "artifacts": null
}
```
