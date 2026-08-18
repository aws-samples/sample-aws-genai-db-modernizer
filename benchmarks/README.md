# Assessment quality benchmark

Measures how well the assessment pipeline assigns queries to the right target
engine, scored against human-reviewed answer keys. v1 covers the assignment
stage only.

## What it measures

For every query in a case, the benchmark compares the engine the pipeline
actually assigned against a curated set of acceptable engines for that query.
A query scores as acceptable if the assigned engine is anywhere in that set,
and as ideal if it matches the single best engine a reviewer picked. The
report aggregates acceptable-accuracy and ideal-accuracy across all scored
cases.

This is not a test of "did the code crash." It is a test of whether the
assignment logic reaches the same engine choice a human reviewer would make
for a known workload.

## The assignment stage is deterministic

`AssignmentResolver.resolve()` is rule-based: it scores engines from triage
signals and analysis table recommendations, with no LLM or Bedrock call
involved. Identical inputs always produce identical output. That means the
benchmark runs once, needs no AWS credentials, and any drop in accuracy after
a code change is a real regression, not sampling noise.

Later stages (triage, schema design, synthesis) call Bedrock and will need
N runs with variance, on-demand execution, and throttle handling. The
harness's outcome categories (`scored` / `throttled` / `errored` / `skipped`)
already exist for that reason, even though the assignment stage only ever
produces `scored` or `skipped`.

## How to run it

```bash
uv run python -m benchmarks.runner.run --stage assignment
```

Add `--json` for a machine-readable report, or `--tag <tag>` to run a subset
of cases (tags are declared per case in `index.json`).

```bash
uv run python -m benchmarks.runner.run --stage assignment --tag kv
```

## Reading the output

```text
stage=assignment  complete=True
scored=2  throttled=0  errored=0  skipped=0
aggregate acceptable-accuracy: 100.0%
  kv-lookup-clean                scored     100.0%
  wordpress-trimmed              scored     100.0%
```

`complete=True` means every case produced a scorable result. If any case
throttled or errored, `complete` flips to `False` and a banner appears above
the report:

```text
INCOMPLETE RUN — 1/2 cases did not produce a scorable result (throttled/errored).
    Scores below cover 1 scored cases only.
    Re-run before trusting these numbers: some-case-id
```

Throttled and errored cases are quarantined out of the aggregate, never
counted as wrong answers. Skipped cases (`reviewed: false`) are excluded the
same way, so an unreviewed draft case can't drag down or inflate the score.
When you see that banner, re-run before drawing any conclusion from the
numbers.

## Case layout

Cases for the assignment stage live under `benchmarks/cases/assignment/`,
listed in `index.json`:

```text
benchmarks/cases/assignment/
  index.json
  kv-lookup-clean/
    collection.json        collector output: queries.query_patterns + database_schema.tables
    triage.json             gold triage output: selected_agents (+ optional signals)
    analysis/
      dynamodb.json         gold analysis output for one selected engine
      elasticache.json
    expected.json           the answer key
```

`index.json` is the manifest the loader reads:

```json
{
  "stage": "assignment",
  "cases": [
    { "id": "kv-lookup-clean", "path": "kv-lookup-clean", "intent": "...", "tags": ["clean", "kv"] }
  ]
}
```

## The expected.json contract

```json
{
  "case_id": "kv-lookup-clean",
  "stage": "assignment",
  "intent": "Human-readable description of the workload and why the labels are what they are.",
  "authored_by": "human",
  "reviewed": true,
  "expected": {
    "q_get_by_pk": {
      "acceptable": ["dynamodb", "elasticache"],
      "ideal": "dynamodb",
      "rationale": "PK point read, high RPS"
    }
  }
}
```

- `acceptable` is a list of engines that would all be a defensible assignment
  for that query. Score any of them as correct.
- `ideal` is optional and names the single best engine. It drives the
  secondary ideal-accuracy metric; it does not affect acceptable-accuracy.
- `reviewed` gates whether the case counts at all. A case with
  `reviewed: false` runs but is reported as `skipped` and excluded from the
  aggregate. Flip it to `true` only after a human has actually read the case
  and agrees with the labels.
- Only query ids present in `expected` get scored. A query the pipeline
  assigns that isn't in the key shows up under `unmatched` in the score
  result, never as a miss.

## The selected_agents requirement (read this before authoring a case)

The real assignment resolver only scores and assigns among engines that
appear in `triage.json`'s `selected_agents` list, and it only loads
`analysis/<engine>.json` for engines in that same list. If an engine is
missing from `selected_agents`, the resolver can never assign a query to it,
no matter what `expected.json` says.

Concretely: for every engine that appears anywhere in a case's
`expected.json` acceptable-sets, `triage.json` must list that engine in
`selected_agents`:

```json
{ "selected_agents": [{ "agent_type": "dynamodb" }, { "agent_type": "elasticache" }] }
```

and the case should provide a matching `analysis/<engine>.json` for each of
those engines (an empty-anti-pattern analysis is fine for a clean case,
for example `{"workload_analysis": {"anti_patterns_detected": []}}`).

Miss this and every query in the case silently falls back to the aurora
engine, and the case scores near zero. That is not a real quality result;
it is a malformed gold input. If a case scores unexpectedly low, check this
coverage before assuming you found a resolver bug.

## Authoring or reviewing a case

1. Pick a workload: either hand-author a small, clean, single-purpose
   example, or trim a real collector output down to a handful of
   representative queries.
2. Write `collection.json` in the collector output shape (see
   `tests/graph/conftest.py`'s `sample_collector_output` for the field
   template, and `src/agents/referee/assignment_resolver.py` for exactly
   which fields `resolve()` reads).
3. Write `triage.json` with `selected_agents` covering every engine your
   answer key will name, plus any `signals` you want to exercise.
4. Write one `analysis/<engine>.json` per selected engine.
5. Write `expected.json` with an acceptable-set (and optionally an ideal)
   per query, and a rationale a reviewer can check against.
6. Run the benchmark and confirm the case scores the way you expect before
   setting `reviewed: true`. If it doesn't, check the selected_agents
   coverage above first.
7. Add the case to `index.json`.

## What's in v1

Two cases ship with this benchmark:

- `kv-lookup-clean`: a hand-authored, single-table point-lookup workload
  (no joins, high calls-per-second) isolating the DynamoDB/ElastiCache
  decision.
- `wordpress-trimmed`: five representative queries trimmed from a real
  WordPress collector output (`docs/examples/wordpress/wordpress-collection.json`),
  covering high-frequency single-table lookups alongside a taxonomy join and
  a GROUP BY aggregation that belong on a relational engine.

Both currently score 100% acceptable-accuracy and 100% ideal-accuracy. That
reflects the resolver's actual behavior on these inputs, not a target baked
into the harness. Future code changes that regress assignment quality should
show up here.
