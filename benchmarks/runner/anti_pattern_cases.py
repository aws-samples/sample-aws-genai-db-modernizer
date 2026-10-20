"""Build the anti_pattern benchmark cases and their blind review sheets (#1084).

A case is one bundled sample and one catalog anti-pattern that can decide an
engine there. Its queries are chosen in two groups, and the sheet does not say
which is which:

- **decides**: queries the pipeline flags with the pattern whose final engine
  changes when the pattern's penalty is set to any of the basis points
  (``PENALTY_SWEEP``, 0 to 40, ADR-031 Decision 8 Amendment 6) in place of its
  catalog value, on every finding of the pattern, including those a redesign
  removes (which the scorer otherwise leaves unpenalised, #1231). Whether the
  pattern applies, whether a redesign removes it, and how much it costs, decides
  where these queries land.
- **decoys**: queries the pipeline does not flag with the pattern, matched to
  the deciding queries: for each one, ``DECOYS_PER_DECIDING`` unflagged
  queries of the same kind (read or write), of the same access-pattern class
  while the sample has any left, on the same tables first, then nearest in call
  rate. So a case has twice as many decoys as deciding queries, a key can say
  the pipeline missed the pattern, and no sheet column but the query itself
  tells the two groups apart.

The sheet hides what routing derived from the query (its access-pattern class,
the reason, the shape pointers), puts every row in one table and orders the
rows by a hash of the case id and query id, so neither a column nor a position
carries the pipeline's verdict.

A pattern with no deciding query on a sample makes no case there. The case's
``expected.json`` lists its ``queries`` and starts ``reviewed: false`` with an
empty ``expected``: the key is written by the maintainer from the sheet
(ADR-030 Amendments 1 and 2), never by this tool, and a reviewed case is never
rewritten (``--sheets-only`` included).

    uv run python -m benchmarks.runner.anti_pattern_cases --sample wordpress
    uv run python -m benchmarks.runner.anti_pattern_cases --sample wordpress --sheets-only

The first selects the cases and writes their files and sheets; ``--sheets-only``
regenerates the sheets for the committed cases' queries (when the collector's
facts change) without selecting again. Everything runs the deterministic
pipeline: no model, no AWS call.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import shutil
import sys
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from benchmarks.runner.review_sheet import Row, Section, _short_ids, build_rows, row_cells
from benchmarks.runner.stage_anti_pattern import pipeline_flags, read_analyses
from benchmarks.runner.stage_final_assignment import _JOB, run_pipeline_to_final_assignment
from src.shared.engine_names import ENGINE_DISPLAY_NAMES, PLACEHOLDER_ENGINES

STAGE = "anti_pattern"
STAGE_DIR = Path(__file__).resolve().parents[1] / "cases" / STAGE
REVIEW_DIR = STAGE_DIR / "review"
PENALTY_SWEEP = (0, 10, 20, 30, 40)
MAX_DECIDES = 12
DECOYS_PER_DECIDING = 2
SAMPLE_TAGS = {
    "wordpress": ["real", "mysql"],
    "discourse": ["real", "postgresql"],
    "trailgear": ["synthetic", "sqlserver"],
}

# What each penalised catalog anti-pattern means, in words that name no engine, so
# the sheet asks the question without hinting at an answer. One per catalog id with
# a penalty (``tests/unit/benchmarks/test_anti_pattern_cases.py`` checks coverage).
DEFINITIONS: dict[str, str] = {
    "acid-transactions": (
        "frequent UPDATE or DELETE statements (more than one call per second) on data that "
        "changes in place and needs transactional writes"
    ),
    "complex-aggregation": (
        "GROUP BY, HAVING, or an aggregate over rows no key value anchors, or over a join "
        "(one key's COUNT or SUM is not this pattern)"
    ),
    "correlated-subquery-joins": "a query with a correlated subquery, run once per outer row",
    "faceted-analytics": (
        "several groupings of one row set in one statement (GROUPING SETS, ROLLUP, CUBE)"
    ),
    "frequent-full-scan": (
        "a read run more often than once a minute that pins no column to a value, so no key "
        "value can address it and it reads every row"
    ),
    "full-text-search": (
        "full-text search with the source's text-search operators (tsvector/tsquery, "
        "MATCH ... AGAINST)"
    ),
    "fuzzy-search": "fuzzy or similarity search (trigram similarity, edit distance)",
    "graph-traversal-hierarchy": (
        "a self-referencing foreign key or a recursive query that walks a hierarchy"
    ),
    "heavy-cross-collection-joins": "a query joining three or more tables",
    "high-frequency-pk-lookup": (
        "a single-row read by primary key at very high frequency (more than 30 calls per second)"
    ),
    "high-volume-text-search": (
        "frequent text search (LIKE with a leading wildcard, ILIKE, text-search operators) on a "
        "table that is mostly text columns"
    ),
    "large-result-sets": "a query returning more than 10,000 rows",
    "multi-index-joins": "a query joining two or more tables",
    "no-relational-need": (
        "a query on a table with no foreign keys that takes part in no join: every query on it "
        "is single-table reads and writes"
    ),
    "ordered-scan": (
        "the first rows of a whole table in some order: ORDER BY with a row limit and no key value"
    ),
    "regex-search": "regular-expression matching (REGEXP, RLIKE, the ~ operator)",
    "simple-cache-read": (
        "a frequent read of small, rarely changing data (configuration, sessions, feature flags)"
    ),
    "substring-match": (
        "a LIKE whose pattern starts with a wildcard (LIKE '%x'), or a regular expression, that "
        "is not a word search on a text column: with no index built for substrings it reads "
        "every row"
    ),
    "single-access-pattern-table": (
        "a query on a table read by at most two distinct query shapes, all single-table, all "
        "by primary key or one column filter"
    ),
    "text_search": "text search (LIKE or ILIKE with wildcards, or text-search operators)",
    "unbounded-array-growth": "a list stored with its parent row that grows without limit",
    "unbounded-result-set": "a query returning more than 10,000 rows with no row limit",
    "wide-transaction": (
        "a write wider than one bounded transaction: more than 100 rows, or rows a range, "
        "join, subquery or a filter with no key value picks"
    ),
    "wildcard-search": "LIKE or ILIKE wildcard matching on a text column",
    "window-functions": "a window function (ROW_NUMBER, RANK, LAG, LEAD, running totals)",
}


@dataclass
class CaseSpec:
    case_id: str
    sample: str
    anti_pattern: str
    decides: list[str]
    decoys: list[str]

    @property
    def queries(self) -> list[str]:
        """Both groups, sorted by id: the key file's order says nothing about either."""
        return sorted({*self.decides, *self.decoys})


def penalised_patterns() -> list[str]:
    """Catalog anti-pattern ids that carry a penalty (aliases excluded)."""
    from src.shared.catalog.patterns import patterns

    return sorted(p.id for p in patterns() if p.penalty > 0)


def _final_engines(store, db: str) -> dict[str, str]:
    from benchmarks.runner.stage_final_assignment import final_assignment

    doc = final_assignment(store, db)
    return {qa["query_id"]: qa["assigned_engine"] for qa in doc.get("query_assignments", [])}


@contextlib.contextmanager
def penalty_set_to(pattern_id: str, points: int):
    """Temporarily give ``pattern_id`` (and its aliases) ``points`` in the scorer.

    Patches module state (``query_scoring.ANTI_PATTERN_PENALTIES``, and the
    catalog entry ``substring_match`` reads) and restores it on exit, so it is
    for one process running one sweep at a time, not thread-safe. A pattern the
    scorer has no penalty for raises: sweeping it would silently move nothing.

    The swept pattern's findings are all priced, the ones a redesign removes
    included (``query_scoring.fixable_keys`` is patched to leave them out, #1231):
    a case holds the queries the pattern's penalty can decide, and whether it
    should apply to each is the sheet's ``redesign`` question.
    """
    from benchmarks.runner.stage_anti_pattern import SUBSTRING_MATCH, pattern_keys
    from src.agents.referee import query_scoring, substring_match

    table = query_scoring.ANTI_PATTERN_PENALTIES
    keys = pattern_keys(pattern_id) if pattern_id != SUBSTRING_MATCH else {SUBSTRING_MATCH}
    saved = {k: table[k] for k in keys if k in table}
    if not saved and pattern_id != SUBSTRING_MATCH:
        raise KeyError(f"{pattern_id!r} has no entry in the scorer's anti-pattern penalties")
    real_pattern = substring_match._pattern
    original = query_scoring.fixable_keys

    def priced(findings):
        """The fixable findings, the swept pattern's left out (so they are priced)."""
        return frozenset(k for k in original(findings) if k[2] not in keys)

    try:
        for k in saved:
            table[k] = points
        query_scoring.fixable_keys = priced  # type: ignore[assignment]
        if pattern_id == SUBSTRING_MATCH:
            # the scan penalty reads the catalog entry, not the penalty table
            entry = real_pattern().model_copy(update={"penalty": points})
            substring_match._pattern = lambda: entry  # type: ignore[assignment]
        yield
    finally:
        table.update(saved)
        query_scoring.fixable_keys = original  # type: ignore[assignment]
        substring_match._pattern = real_pattern  # type: ignore[assignment]


def moved_by_sweep(artifact_root: Path, db: str, pattern_id: str, scratch: Path) -> set[str]:
    """Queries whose final engine changes at some point of ``PENALTY_SWEEP``.

    Reruns assignment and the reality check (the scorer applies the penalty;
    analysis does not) on a copy of the job without its assignment versions.
    """
    from src.agents.referee.assignment_handler import run_assignment_resolver
    from src.agents.referee.reality_check_handler import run_reality_check_handler
    from src.storage.local_store import LocalArtifactStore

    base = _final_engines(LocalArtifactStore(str(artifact_root)), db)
    moved: set[str] = set()
    for points in PENALTY_SWEEP:
        root = scratch / f"{pattern_id}-{points}"
        shutil.copytree(
            artifact_root / db / _JOB,
            root / db / _JOB,
            ignore=shutil.ignore_patterns("assignment", "reality-check"),
        )
        store = LocalArtifactStore(str(root))
        with penalty_set_to(pattern_id, points), contextlib.redirect_stdout(sys.stderr):
            run_assignment_resolver(_JOB, db, store)
            run_reality_check_handler(_JOB, db, store, llm_mode="none")
        swept = _final_engines(store, db)
        moved |= {q for q, e in base.items() if swept.get(q) != e}
        shutil.rmtree(root)
    return moved


def _is_write(q: Mapping) -> bool:
    return (q.get("query_type") or "").upper() in {"INSERT", "UPDATE", "DELETE", "MERGE", "UPSERT"}


def _calls(q: Mapping) -> float:
    v = q.get("calls_per_second")
    return float(v) if isinstance(v, (int, float)) else 0.0


def choose_decoys(
    queries: Iterable[Mapping],
    decides: list[str],
    flagged: set[str],
    classes: Mapping[str, str],
) -> list[str]:
    """Unflagged queries matched to the deciding ones, ``DECOYS_PER_DECIDING`` each.

    A decoy for deciding query ``d`` is not flagged, not a utility statement and
    of ``d``'s kind (read or write). Ranked: ``d``'s access-pattern class first,
    then ``d``'s tables, then the nearest in call rate (log scale), then the id;
    so a decoy of another class is taken only when ``d``'s class has no unused
    unflagged query left. A query is used once.
    """
    queries = list(queries)
    by_id = {q["query_id"]: q for q in queries}
    used: set[str] = set()
    out: list[str] = []
    for d in decides:
        dq = by_id[d]
        tables = set(dq.get("tables_accessed") or [])
        pool = [
            q
            for q in queries
            if q["query_id"] not in flagged
            and q["query_id"] not in used
            and classes.get(q["query_id"]) not in (None, "", "utility")
            and _is_write(q) == _is_write(dq)
        ]

        def rank(q: Mapping, tables=tables, dq=dq, d=d) -> tuple:
            gap = abs(math.log10(_calls(q) + 1e-4) - math.log10(_calls(dq) + 1e-4))
            return (
                classes.get(q["query_id"]) != classes.get(d),
                not tables & set(q.get("tables_accessed") or []),
                gap,
                q["query_id"],
            )

        for q in sorted(pool, key=rank)[:DECOYS_PER_DECIDING]:
            used.add(q["query_id"])
            out.append(q["query_id"])
    return out


def select_cases(sample: str, work_dir: Path) -> tuple[list[CaseSpec], dict]:
    """Every case of ``sample``: one per penalised pattern that decides an engine."""
    ctx = _pipeline_context(sample, work_dir)
    store, prefix = ctx["store"], ctx["prefix"]
    queries = ctx["collector"].get("queries", {}).get("query_patterns", [])
    calls = {q["query_id"]: _calls(q) for q in queries}
    classes = {q["query_id"]: q.get("class", "") for q in ctx["access"].get("queries", [])}
    analyses = read_analyses(store, prefix)
    artifact_root = Path(work_dir) / "artifacts"
    scratch = Path(work_dir) / "sweep"
    specs: list[CaseSpec] = []
    for pattern_id in penalised_patterns():
        flagged = pipeline_flags(analyses, pattern_id, ctx["collector"])
        if not flagged:
            continue
        moved = moved_by_sweep(artifact_root, sample, pattern_id, scratch)
        decides = sorted(flagged & moved, key=lambda q: (-calls.get(q, 0.0), q))[:MAX_DECIDES]
        if not decides:
            continue
        decoys = choose_decoys(queries, decides, flagged, classes)
        specs.append(CaseSpec(f"{sample}-{pattern_id}", sample, pattern_id, decides, decoys))
    return specs, ctx


# --- the sheet ----------------------------------------------------------------

# The final-assignment sheet's fact columns minus what routing derived from the
# query (access-pattern class, its reason, its pattern facts, the shape pointer):
# those restate the detectors' inputs, so they could tell flagged rows from decoys.
FACT_CELLS = (0, 1, 2, 3, 4, 5, 6, 7, 8, 11)  # indexes into review_sheet.row_cells
# The answer columns, in order (#1084; ``redesign`` and ``after redesign`` since #1231).
ANSWER_COLUMNS: tuple[str, ...] = (
    "shows",
    "redesign",
    "acceptable",
    "after redesign",
    "ideal",
    "rationale",
)
HEADER = (
    "| # | id | SQL | op | tables | calls/s | share | tier | groups | facts | "
    + " | ".join(ANSWER_COLUMNS)
    + " |"
)
DIVIDER = "|" + "---|" * (len(FACT_CELLS) + len(ANSWER_COLUMNS))
DEFAULT_LINE = "Default: " + " ; ".join(f"{c} =" for c in ANSWER_COLUMNS)


def row_order(case_id: str, query_ids: Iterable[str]) -> list[str]:
    """The sheet's row order: by a hash of the case id and the query id only."""
    return sorted(query_ids, key=lambda q: hashlib.sha256(f"{case_id}:{q}".encode()).hexdigest())


def case_rows(sections: list[Section], case_id: str, query_ids: Iterable[str]) -> list[Row]:
    """The case's rows from the sample's sheet, in ``row_order``, numbered from 1."""
    by_id = {r.query_id: r for s in sections for r in s.rows}
    rows = [by_id[q] for q in row_order(case_id, query_ids) if q in by_id]
    for n, r in enumerate(rows, start=1):
        r.number = n
        r.shape_of = None
    return rows


def render_case(case_id: str, sample: str, pattern_id: str, rows: list[Row]) -> str:
    ids = _short_ids([r.query_id for r in rows])
    engines = ", ".join(
        f"`{e}`" for e in ENGINE_DISPLAY_NAMES if e not in PLACEHOLDER_ENGINES and e != "aurora"
    )
    out = [f"# Anti-pattern review sheet: {case_id}", ""]
    out.append(
        f"Blind review sheet for the `{STAGE}` benchmark key of case `{case_id}` (#1084): "
        f"{len(rows)} queries of the `{sample}` sample (`docs/examples/{sample}/{sample}.zip`) "
        f"and one catalog anti-pattern, `{pattern_id}`. The pipeline flags some of these "
        "queries with it and not others; the sheet never says which, and never shows the "
        "engine the pipeline picked or any score. Rows are in an order that depends only on "
        "the case and query ids. Generated by "
        f"`uv run python -m benchmarks.runner.anti_pattern_cases --sample {sample}`; do not "
        "edit the facts columns by hand."
    )
    out.append("")
    out.append(f"**The anti-pattern, `{pattern_id}`:** {DEFINITIONS[pattern_id]}.")
    out.append("")
    out.append("## How to fill it in")
    out.append("")
    out.append(
        f"- `shows`: `yes` if the query shows `{pattern_id}` as defined above, `no` if it does "
        "not. Every answered row needs it."
    )
    out.append(
        "- `redesign` (on every row whose `shows` is `yes`): `intrinsic` if the query needs "
        "rows no key can narrow (a whole-table aggregate or export, a scan by design, a "
        "free-text filter no key can carry), so no redesign of the schema removes the "
        "pattern; `fixable` if the pattern comes from the source's design (no index on the "
        "filtered column, a filter a key or index could carry, a predicate on a stable "
        "attribute), so a redesign removes it."
    )
    out.append(
        "- `acceptable` (optional): every engine that would be a defensible home for the "
        f"query as it is written today, comma separated, using these ids: {engines}. Leave "
        "it empty to score the verdict only."
    )
    out.append(
        "- `after redesign` (optional): every engine that would be a defensible home for the "
        "query once the target schema is designed for it, the same ids, comma separated."
    )
    out.append(
        "- `ideal` (optional): the one best engine; it must be in `acceptable`, or in "
        "`after redesign` for a `fixable` row."
    )
    out.append("- `rationale`: a few words a later reviewer can check against the facts.")
    out.append(
        "- `=N`: same answer as row N. The **Default** line answers every row left empty. "
        "`?`: unsure, discuss first. `-`: leave out of the key."
    )
    out.append(
        "- When the sheet is done, the key goes into the case's `expected.json` "
        '(`{"<query_id>": {"shows": true, "redesign": "fixable", "acceptable": [...], '
        '"acceptable_after_redesign": [...], "ideal": "...", "rationale": "..."}}`) with '
        "`reviewed: true`, and the case's floor into the stage's `gate.json`."
    )
    out.append("")
    out.append(
        "Columns: `op` the statement type; `calls/s`, `share` and `tier` against all the "
        "sample's calls (tier A = the busiest queries making 90% of calls, B up to 99%, C the "
        "rest); `groups` marks co-dependency (`CDn`) and write groups (`W:table`); `facts` the "
        "collector flags (joins, aggregation, subquery, text search, average rows returned, "
        "median latency). The access-pattern class, its reason and its pattern facts are left "
        "out on purpose: they restate what the pipeline's detectors read."
    )
    out.append("")
    out.append("## Queries")
    out.append("")
    out.append("<!-- rows -->")
    out.append("")
    out.append(DEFAULT_LINE)
    out.append("")
    out.append(HEADER)
    out.append(DIVIDER)
    for r in rows:
        # collector flags only: the pattern facts are routing's reading of the query
        cells = row_cells(replace(r, facts=[]), ids)
        answers = [""] * len(ANSWER_COLUMNS)
        out.append("| " + " | ".join([cells[i] for i in FACT_CELLS] + answers) + " |")
    out.append("")
    return "\n".join(out)


def sheet_for(spec_queries: list[str], case_id: str, sample: str, pattern_id: str, ctx) -> str:
    v1 = ctx["store"].read_json(f"{ctx['prefix']}/assignment/v1/assignment.json")
    structure = {
        "co_dependency_groups": v1.get("co_dependency_groups") or [],
        "write_groups": v1.get("write_groups") or {},
    }
    sections = build_rows(ctx["collector"], ctx["access"], structure)
    return render_case(case_id, sample, pattern_id, case_rows(sections, case_id, spec_queries))


# --- writing the case files ---------------------------------------------------


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text()) if path.is_file() else {}


def write_cases(sample: str, specs: list[CaseSpec], ctx, stage_dir: Path = STAGE_DIR) -> list[str]:
    """Write the sample's cases, keys (unreviewed) and sheets; keep every reviewed case.

    Returns the ids written. An existing reviewed case of the sample is never
    rewritten or dropped, even when the selection no longer picks it.
    """
    index_path = stage_dir / "index.json"
    index = _read_json(index_path) or {"stage": STAGE, "cases": []}
    entries = index.get("cases", [])
    reviewed = {
        e["id"]
        for e in entries
        if _read_json(stage_dir / e["path"] / "expected.json").get("reviewed")
    }
    kept = [e for e in entries if e.get("sample") != sample or e["id"] in reviewed]
    for e in entries:  # drop an unreviewed case the selection no longer picks
        if e.get("sample") == sample and e["id"] not in reviewed:
            shutil.rmtree(stage_dir / e["path"], ignore_errors=True)
            (stage_dir / "review" / f"{e['id']}.md").unlink(missing_ok=True)
    written = []
    for spec in specs:
        if spec.case_id in reviewed:
            continue
        intent = (
            f"{sample}: whether queries show the `{spec.anti_pattern}` anti-pattern, on "
            "queries where it can decide the final engine and on matched queries the "
            "pipeline does not flag with it. The key is written with the maintainer from the blind review "
            f"sheet (review/{spec.case_id}.md), never from the pipeline output (#1084)."
        )
        kept.append(
            {
                "id": spec.case_id,
                "path": spec.case_id,
                "sample": sample,
                "anti_pattern": spec.anti_pattern,
                "intent": intent,
                "tags": [*SAMPLE_TAGS.get(sample, []), "anti-pattern", spec.anti_pattern],
            }
        )
        cdir = stage_dir / spec.case_id
        cdir.mkdir(parents=True, exist_ok=True)
        key = {
            "case_id": spec.case_id,
            "stage": STAGE,
            "anti_pattern": spec.anti_pattern,
            "intent": intent,
            "authored_by": "human",
            "reviewed": False,
            "queries": spec.queries,
            "expected": {},
        }
        (cdir / "expected.json").write_text(json.dumps(key, indent=2) + "\n")
        written.append(spec.case_id)
        write_sheet(spec.case_id, sample, spec.anti_pattern, spec.queries, ctx, stage_dir)
    order = list(SAMPLE_TAGS)
    kept.sort(key=lambda e: (order.index(e["sample"]) if e["sample"] in order else 99, e["id"]))
    index["cases"] = kept
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(json.dumps(index, indent=2) + "\n")
    return written


def write_sheet(case_id, sample, pattern_id, queries, ctx, stage_dir: Path = STAGE_DIR) -> Path:
    path = stage_dir / "review" / f"{case_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(sheet_for(queries, case_id, sample, pattern_id, ctx))
    return path


def refresh_sheets(sample: str, ctx, stage_dir: Path = STAGE_DIR) -> list[dict]:
    """Regenerate the sheets of the sample's committed unreviewed cases.

    A reviewed case's sheet is the record its key was written from: it is
    never rewritten.
    """
    out = []
    for e in _read_json(stage_dir / "index.json").get("cases", []):
        if e.get("sample") != sample:
            continue
        key = _read_json(stage_dir / e["path"] / "expected.json")
        if key.get("reviewed"):
            out.append({"case": e["id"], "sheet": "kept (reviewed)"})
            continue
        path = write_sheet(e["id"], sample, e["anti_pattern"], key["queries"], ctx, stage_dir)
        out.append({"case": e["id"], "sheet": str(path)})
    return out


def _pipeline_context(sample: str, work_dir: Path) -> dict:
    from benchmarks.runner.loader import Case

    case = Case(sample, work_dir, "", [], False, {}, {}, {}, {}, sample=sample)
    with contextlib.redirect_stdout(sys.stderr):
        store, prefix = run_pipeline_to_final_assignment(case, work_dir)
    return {
        "store": store,
        "prefix": prefix,
        "collector": store.read_json(f"{prefix}/collector/output.json"),
        "access": store.read_json(f"{prefix}/assignment/v1/access_patterns.json"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the anti_pattern benchmark cases")
    parser.add_argument("--sample", action="append", required=True, help="Bundled sample name")
    parser.add_argument(
        "--sheets-only",
        action="store_true",
        help="Regenerate the committed cases' sheets; select no cases",
    )
    args = parser.parse_args()
    for sample in args.sample:
        work_dir = Path(tempfile.mkdtemp(prefix=f"anti_pattern_{sample}_"))
        if args.sheets_only:
            for line in refresh_sheets(sample, _pipeline_context(sample, work_dir)):
                print(json.dumps(line))
            continue
        specs, ctx = select_cases(sample, work_dir)
        for case_id in write_cases(sample, specs, ctx):
            spec = next(s for s in specs if s.case_id == case_id)
            print(json.dumps({"case": case_id, "queries": len(spec.queries)}))


if __name__ == "__main__":
    main()
