# PE-review routing flag fixture (#740)

A recorded DynamoDB PE review of the wordpress sample that flags one query as unservable on DynamoDB, so the post-schema reroute path can be exercised end to end without a model: the reviewer's `PEReviewResult.routing_flags`, the design trace, the post-schema router and the shared scorer, a new assignment version, the redesign, and "moved after approval" in every deliverable.

## The flagged query

`413c99e446984db35e1e4e8f3952ddb966d21a85a898e88235b61d26ea096e60`:

```sql
SELECT `option_name` , `option_value` FROM `wp_options` WHERE `autoload` = ?
```

It filters `wp_options` only on `autoload`, a low-cardinality column that is not part of any key, so DynamoDB serves it only with a scan, or with a GSI whose few partitions run hot. The recorded review (`wordpress_dynamodb_pe_review.json`) flags it with `reason_category: full_scan`, in the `routing_flags` shape the DynamoDB reviewer skill gives (`src/skills/dynamodb-pe-review.md`); nothing in the routing reads the flag's explanation. The skill keeps routing flags for patterns DynamoDB cannot serve at all, so a live reviewer may instead ask for a sparse GSI (`CHANGES_REQUESTED`); the recording is the flagging answer. Its query ids are the start of the sample's 64-character digest ids, which the replay expands (the full ids read as high-entropy strings to the secret scanner).

## Expected outcome

On the deterministic wordpress run (`--llm-mode none` up to the approved assignment, v2):

| Step | Expected |
|---|---|
| First assignment | DocumentDB (79 against DynamoDB's 78; a tie at 78 before #1197 billed DocumentDB reads at the source's buffer-cache hit ratio, bounded to 1% billed on wordpress) |
| Reality check (v2) | DocumentDB removed; the query is consolidated onto DynamoDB |
| DynamoDB schema design on v2 | 39 queries, 4 groups; every group's review returns the recording, so `schema-dynamodb/v2/design_trace.json` carries the flag four times (once per group) |
| Post-schema router | Moves the query once, to Aurora MySQL, score 57 against 78 on DynamoDB, as one `post_schema_flag` Move (rule `flag.capability_gap`, actor `model_flag`); no terminal query |
| Removed engine | DocumentDB scores 79 but stays out: a flag never brings back a removed engine |
| v3 | Status `rerouted_after_approval`; only this query changed engine |
| Redesign on v3 | Both engines the move touched (DynamoDB and Aurora MySQL) are designed again; DynamoDB's design no longer lists the query; no further reroute (no v4) |
| Deliverables | `report.json` `post_schema_reroutes` lists it (approved version 2); the engineering report and the executive report PDF have "Moved after approval (1 query)" with the override path |

The scores in this table are today's routing for the query (its entry in `tests/e2e/baselines/routing_snapshot.json`, wordpress `413c99e4…`); the test reads them from the run (the approved assignment's confidence and the router's candidate ladder), never from here. When a scoring change moves them, regenerate the routing snapshot as usual and update this table to match; the test needs no edit unless the query's final engine changes.

## Commands

Replay (deterministic, no model, no AWS):

```bash
uv run pytest tests/unit/orchestrator/test_pe_flag_fixture_wordpress.py -q
```

The test runs the real DynamoDB schema design path (`run_schema_design_auto`, the group split, each group's `SchemaDesignRunner` loop and the merge) with the two model calls replayed: the designer calls the input tool it was given and returns the committed wordpress DynamoDB design with one access pattern per query it read, and every group's reviewer returns this recorded review as it is (a live reviewer of one group may name another group's query too).

Live, in Bedrock mode (real model calls and cost; needs Bedrock access):

```bash
uv run python scripts/run_assessment.py --file wordpress-collection.json --all -y --llm-mode bedrock
```

`wordpress-collection.json` is the file inside `docs/examples/wordpress/wordpress.zip`. Then read `artifacts/wordpress/<job>/schema-dynamodb/v2/design_trace.json` (`routing_flags`, per group under `groups`), `post-schema-router/router_output.json` and `assignment/v3/assignment.json`. A live reviewer decides for itself whether to flag the query (a Bedrock run on wordpress produced no flags, #740); when it flags it, the outcome is the table above, as long as the live reality check removes DocumentDB too. The replay test is the reproducible evidence.

Headless (Claude Code, `/modernize`): no step reviews a design or runs the post-schema router today, so this flag has no headless path, and a designer's `unsupported_patterns` reach the deliverables as risks only. The maintainer decided headless runs the PE review and the post-schema reroute the same as Bedrock mode; that work is #1059, and this table is its expected outcome too.
