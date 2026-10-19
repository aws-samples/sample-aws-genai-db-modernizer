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

## The gate

The benchmark is the routing-quality check: the e2e-llm rubric judge grades
the deliverables, not the engine choice (#952). `--gate` checks the stage's
`gate.json` and exits 1 when a floor is broken:

```bash
uv run python -m benchmarks.runner.run --stage assignment --gate
```

`benchmarks/cases/assignment/gate.json` holds the floors: the aggregate
acceptable-accuracy and one per reviewed case. A gated case that is missing,
unreviewed or errored fails the gate too, so a case can never pass by
dropping out. A case with no floor is reported, not gated. Both keys are required, and the case floors cannot be empty. `--gate` checks
every gated case, so it does not combine with `--tag`.

`tests/unit/benchmarks/test_gate.py` runs the gate on the committed cases in
the unit suite, and adds a deliberate routing regression (an engine
preference on top of the shared scorer) to show it fails. Every reviewed
case must have a floor; raise a floor only together with the change that
earns it.

## The final_assignment stage (#1074)

The `assignment` stage feeds the resolver gold triage and analysis inputs and scores its first pass. The deliverables show something later: the assignment the reality check leaves behind. The `final_assignment` stage scores that one. It runs the deterministic pipeline on a whole bundled sample (`docs/examples/<sample>/<sample>.zip`): collect, triage, every selected engine's analysis, assignment and the reality check, with no model and no AWS call, then reads the newest assignment version (the reality check's when it consolidated anything, else v1).

```bash
uv run python -m benchmarks.runner.run --stage final_assignment
```

Its cases (`benchmarks/cases/final_assignment/`) are the wordpress, discourse and trailgear samples. An index entry names its sample instead of shipping collection, triage and analysis files:

```json
{ "id": "discourse", "path": "discourse", "sample": "discourse", "intent": "...", "tags": ["real"] }
```

The key format, the scoring and the gate rules are the assignment stage's. Every key starts `reviewed: false` with an empty `expected`, so the stage skips it: an unreviewed case is never scored and never gates. The stage has no `gate.json` until a key is reviewed; `tests/unit/benchmarks/test_gate.py` checks, for every stage, that the floors in `gate.json` name exactly the reviewed cases. `--run-unreviewed` still runs the unreviewed cases, to show the stage works on them (the report gives how many queries each assigned), but they stay `skipped`, out of the aggregate and the gate. The three samples take about 30 seconds together.

### Writing a sample's key

The keys are written with the maintainer from review, never from the current routing output (copying it would anchor the key on the behaviour the benchmark exists to check). `benchmarks/runner/review_sheet.py` writes one blind review sheet per sample:

```bash
uv run python -m benchmarks.runner.review_sheet --sample wordpress --sample discourse --sample trailgear
```

The sheets (`benchmarks/cases/final_assignment/review/<sample>.md`) list every query with the facts routing reads (access-pattern class and reason, tables, statement type, call rate and share, co-dependency and write groups, pattern facts) and empty `acceptable`, `ideal` and `rationale` columns. They never show the engine the pipeline picked or any score. Rows are grouped by co-dependency group and table (a table's writes first), busiest section first, with a call-share tier and a "same shape as row N" pointer per row; each sheet's guide gives the review order. Once a sheet is filled in, its key goes into `expected.json` with `reviewed: true`, and its floor into the stage's `gate.json`.

`tests/unit/benchmarks/test_review_sheet.py` checks that the committed sheets show no engine outside the customer's own SQL and cover every query of the sample; regenerate them with the command above when the collector's query list changes.

## The anti_pattern stage (#1084)

The assignment keys show few anti-patterns, and none decides an answer, so the benchmark cannot tell a right penalty from a wrong one (#968). The `anti_pattern` stage adds cases where one does. A case is one bundled sample and one penalised catalog anti-pattern (`src/catalog/patterns/<id>.toml`); its key says, per query, whether the query shows that anti-pattern and, optionally, which engines are acceptable for it:

```json
{ "<query_id>": { "shows": true, "acceptable": ["aurora_mysql"], "ideal": "aurora_mysql", "rationale": "..." } }
```

```bash
uv run python -m benchmarks.runner.run --stage anti_pattern
```

The stage runs the `final_assignment` chain once per sample and returns, per case query, the pipeline's verdict (some engine's analysis reports the pattern for it, in `anti_patterns_detected` or `patterns_detected`; for `substring-match`, the collector-based detector) and its final engine. A query is correct when the verdicts agree and, if the key gives an acceptable set, the engine is in it; the report adds `verdict_accuracy` and `engine_accuracy` per case. The gate rules are the other stages': every key starts `reviewed: false` with an empty `expected`, so the stage skips it and has no `gate.json` until a key is reviewed. A key entry without a boolean `shows` errors its case.

### How the cases are chosen

`benchmarks/runner/anti_pattern_cases.py` builds them. For each penalised pattern the pipeline reports on a sample, it reruns assignment and the reality check with the pattern's penalty at each basis point (0, 10, 20, 30, 40; ADR-031 Decision 8 Amendment 6). Flagged queries whose final engine moves at some point are the ones the pattern decides (up to 12, busiest first); a pattern that decides none makes no case. Each deciding query gets two decoys: unflagged queries of its kind (read or write) and, while the sample has any left, of its access-pattern class, on its tables first, then nearest in call rate. So every case has twice as many decoys as deciding queries. `expected.json` lists both groups as one `queries` list sorted by id.

```bash
uv run python -m benchmarks.runner.anti_pattern_cases --sample wordpress --sample discourse --sample trailgear
uv run python -m benchmarks.runner.anti_pattern_cases --sample wordpress --sheets-only
```

The first selects the cases again (it rewrites unreviewed cases and drops the ones no longer selected, and never touches a reviewed one); `--sheets-only` regenerates the sheets of the unreviewed committed cases when the collector's facts change (a reviewed case's sheet is the record its key came from, and is kept).

### Writing a case's key

Each case has a blind sheet, `benchmarks/cases/anti_pattern/review/<case>.md`: the pattern's definition in words that name no engine, then one table with a row per case query: the SQL, statement type, tables, call rate, share and tier, groups and collector flags, and empty `shows`, `acceptable`, `ideal` and `rationale` columns. It leaves out what routing derived from the query (access-pattern class, its reason, pattern facts, shape pointers), since those restate the detectors' inputs, and orders the rows by a hash of the case id and query id. It never says which rows the pipeline flagged and never shows an engine or a score. The reviewer answers `shows` (`yes`/`no`) on every row they keep, and `acceptable` where the engine choice should turn on the answer; the `=N`, Default, `?` and `-` conventions apply. The filled sheet becomes the case's `expected` (with `reviewed: true`) and a floor in `benchmarks/cases/anti_pattern/gate.json`. Then the sweep in #1084 (each basis's points over 0 to 40) can be rerun against the keys.

`tests/unit/benchmarks/test_anti_pattern_cases.py` checks that the committed sheets carry no engine, score, pattern id or answer and cover exactly their case's queries, that no row position and no low-cardinality column (statement type, tier, group membership, collector flags) has a value only flagged rows show, and that the committed membership follows the sweep rule; `tests/unit/benchmarks/test_stage_anti_pattern.py` scores a reviewed fixture case on the real pipeline (test-only keys, never committed) and checks that unreviewed cases never score or gate.

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
