"""Every deliverable shows the same figures as report.json (#573).

AGENTS.md "Deliverables agree": the engineering report and the executive report
render one synthesis ``report.json`` (the UI reads the same file; the decision
and interactive analysis HTML reports were retired in #987). Reviews kept finding figures
that differed between them, or that were impossible: a card cut at 90
characters dropped a figure, a merged wave counted its tables twice ("400
tables migrate" out of 311 analyzed), wave headlines and gate counts did not
reconcile. Per-renderer unit tests cannot catch drift *between* renderers, so
this module renders every deliverable for both bundled samples (the cached
``run`` fixture, so the pipeline is not run again), reads the key figures back
out of each (``tests/e2e/deliverable_figures.py``) and compares every one with
``report.json``:

- per-engine query counts and table counts;
- per wave: tables served, tables moving, tables kept in sync;
- monthly cost per engine and in total;
- risk counts by severity;
- the tied-query count;
- the engines the reality check removed;
- the queries that move to a new engine (#949);
- the Migration map heading's tables that migrate, are kept in sync or stay, and
  each migration target's Scope cell split, whose kept-in-sync counts must agree
  with the wave plan's (#1169);
- the queries and the distinct query patterns they make (#1146), in the UI too
  (``tests/e2e/test_ui.py``).

The same checks also run on a wordpress job with a schema design for every kept
engine (``designed_run``, #949): the deterministic samples never run schema
design, and the designed figures disagreed there.

It also checks the invariants: no total exceeds the tables or queries
analyzed, wave counts reconcile with their table groups, and every engine a
deliverable names in an engine slot is in the final architecture.

Deterministic-run only: the expected per-engine table counts and the known
mismatches below are specific to the no-LLM samples.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

import pytest

from src.agents.referee.table_assignments import retained_engine_for
from src.report.formatting import price_list_text
from src.shared.catalog.prices import estimates_price_basis
from src.shared.engine_names import display_engine
from src.shared.report_facts import compute_report_facts, wave_one_moved_by_later_waves
from src.tools.analysis.relational_gaps import detect_relational_gaps
from tests.e2e.deliverable_figures import (
    SEVERITIES,
    Figures,
    engineering_figures,
    expected_figures,
    final_architecture,
    invariant_violations,
    md_plain,
    migrating,
    norm,
    pdf_figures,
    pdf_page_texts,
    report_invariant_violations,
)
from tests.e2e.pipeline import PipelineResult

pytestmark = pytest.mark.deterministic

# Mismatches this test finds that are real bugs too large to fix here, each
# tracked by its own issue. Keyed by (sample, deliverable, figure key), so only
# that one figure in that one deliverable is excused: any other mismatch still
# fails, and an entry that no longer reproduces fails too, so it is removed
# once its issue is fixed.
KNOWN: dict[tuple[str, str, str], str] = {}


def _report_json(run: PipelineResult) -> dict[str, Any]:
    """The report.json the deliverables were rendered from."""
    report: dict[str, Any] = json.loads(run.report_json().read_text())
    return report


def _waves(report: dict[str, Any]) -> list[dict[str, Any]]:
    return [w for w in report.get("migration_waves") or [] if isinstance(w, dict)]


def _whole_schema_required(exp: dict[str, Any]) -> set[str]:
    """Wave 1 states its tables and views apart whenever it carries views (#578)."""
    return {
        k
        for k in exp
        if (k.endswith(":views") and exp[k])
        or (k.endswith(":tables_only") and exp.get(k.replace("tables_only", "views")))
    }


def _common_required(report: dict[str, Any], exp: dict[str, Any]) -> set[str]:
    req = {"risks:total", *(f"risks:{s}" for s in SEVERITIES)} if exp["risks:total"] else set()
    req |= {k for k in exp if k.startswith("cached_reads:")}
    req |= {"removed"} if exp["removed"] else set()
    req |= {"removal_savings"} if "removal_savings" in exp else set()
    # #730: every deliverable states the close calls (and the settled ties).
    req |= {"close_calls", "settled_ties"} & set(exp)
    # #729: and the queries on a fallback engine.
    req |= {"fallback_queries"} & set(exp)
    # #949: the queries that move to a new engine.
    req |= {"queries_moved"} & set(exp)
    # #1146: and the distinct query patterns those queries make.
    req |= {"query_patterns"} & set(exp)
    return req


def _pdf_required(report: dict[str, Any], exp: dict[str, Any]) -> set[str]:
    """The executive report shows totals, not the close calls (#1020): those stay in
    the engineering report."""
    req = _common_required(report, exp) - {k for k in exp if k.startswith("cached_reads:")}
    req -= {"close_calls", "settled_ties"}
    req |= {"cost:total", "tables_migrate", "tied", "engine_count"}
    req |= {"choices", "auto_decided", "auto_decided_queries"} & set(exp)
    req |= {f"cost:{e}" for e in final_architecture(report)}
    # #1169: each migration target's Scope cell says how many of its tables migrate.
    req |= {k for k in exp if k.startswith(("tables:", "migrate:"))}
    req |= {f"synced:{k.split(':', 1)[1]}" for k in exp if k.startswith("migrate:")}
    return req


def _engineering_required(report: dict[str, Any], exp: dict[str, Any]) -> set[str]:
    req = (
        _common_required(report, exp)
        | _whole_schema_required(exp)
        | {"cost:total", "engine_count", "tied", "tables_migrate"}
        | ({"choices", "auto_decided", "auto_decided_queries"} & set(exp))
    )
    req |= {f"queries:{e}" for e in final_architecture(report) if e.startswith("aurora")}
    req |= {k for k in exp if k.startswith(("tables:", "migrate:"))}
    req |= {f"synced:{k.split(':', 1)[1]}" for k in exp if k.startswith("migrate:")}
    # #1169: the Migration map heading's split and the moving tables it has no row for.
    req |= {k for k in exp if k.startswith("map:")}
    for w in _waves(report):
        req.add(f"wave{w['wave']}:queries")
        if w.get("table_count"):
            req.add(f"wave{w['wave']}:served")
        if migrating(w) and w.get("table_owners"):
            req.add(f"wave{w['wave']}:synced")
    return req


def _problems(
    report: dict[str, Any],
    figs: Figures,
    required: Callable[[dict[str, Any], dict[str, Any]], set[str]],
) -> list[tuple[str, str]]:
    """``(figure key, message)`` for every figure ``figs`` shows that differs
    from report.json, every required figure it does not show, every engine it
    names that is not in the final architecture, and every invariant broken."""
    exp = expected_figures(report)
    out: list[tuple[str, str]] = []
    for key, observations in figs.values.items():
        for value, where in observations:
            if key.endswith(":unparsed"):
                out.append((key, f"{figs.deliverable} {where}: could not read {value!r}"))
            elif key in exp and value != exp[key]:
                shown = sorted(value) if isinstance(value, frozenset) else value
                want = sorted(exp[key]) if isinstance(exp[key], frozenset) else exp[key]
                out.append(
                    (
                        key,
                        f"{figs.deliverable} {where}: {key} is {shown!r}, report.json says {want!r}",
                    )
                )
    for key in sorted(required(report, exp) - set(figs.values)):
        out.append((key, f"{figs.deliverable}: shows no {key} (report.json: {exp.get(key)!r})"))
    final = final_architecture(report)
    for engine, wheres in sorted(figs.engines.items()):
        if engine not in final:
            out.append(
                (
                    f"engine:{engine}",
                    f"{figs.deliverable} names {engine} in {sorted(wheres)}, not in the final "
                    f"architecture {sorted(final)}",
                )
            )
    out += invariant_violations(report, figs.values, figs.deliverable)
    return out


def _settle(db: str, deliverable: str, problems: list[tuple[str, str]]) -> None:
    """Fail on any problem not in KNOWN; fail on a KNOWN entry that no longer
    reproduces; xfail naming the issues when only known problems remain."""
    known = {k[2]: v for k, v in KNOWN.items() if k[0] == db and k[1] == deliverable}
    unknown = [msg for key, msg in problems if key not in known]
    assert not unknown, "\n".join(unknown)
    seen = {key for key, _ in problems}
    stale = sorted(f"{key} ({issue})" for key, issue in known.items() if key not in seen)
    assert not stale, f"known mismatches no longer reproduce, remove them from KNOWN: {stale}"
    if known:
        details = "; ".join(msg for _, msg in problems)
        pytest.xfail(f"known bugs {', '.join(sorted(set(known.values())))}: {details}")


def test_report_json_is_consistent(run: PipelineResult) -> None:
    """report.json itself satisfies every invariant the deliverables are held to."""
    report = _report_json(run)
    exp = expected_figures(report)
    as_observed = {k: [(v, "report.json")] for k, v in exp.items()}
    problems = report_invariant_violations(report)
    problems += invariant_violations(report, as_observed, "report.json")
    _settle(run.db, "report.json", problems)


def test_report_json_facts_are_the_facts_layer_output(run: PipelineResult) -> None:
    """#615: report.json carries the facts block, and it is exactly what the
    facts layer computes from the report's own fields -- the figures every
    deliverable reads are the ones synthesis computed, once."""
    report = _report_json(run)
    stored = report.get("facts")
    assert isinstance(stored, dict), "report.json has no facts block"
    recomputed = compute_report_facts(
        {k: v for k, v in report.items() if k != "facts"},
        relational_gaps=_relational_gaps_from_the_job(run, report),
        # #1196: synthesis passes the price dates the estimates recorded, as it does the gaps.
        price_list=_price_list_from_the_job(run, report),
    )
    assert stored == recomputed


def _relational_gaps_from_the_job(run: PipelineResult, report: dict[str, Any]) -> Any:
    """#471: the gap check reads the collection, not the report, so recompute it from
    the job's own collector output and assignment, as synthesis does."""
    job = run.job_dir()
    collector = json.loads((job / "collector" / "output.json").read_text())
    version = (report.get("assignment_summary") or {}).get("version")
    assignment = json.loads((job / "assignment" / f"v{version}" / "assignment.json").read_text())
    target = retained_engine_for(
        collector,
        assignment.get("query_assignments") or [],
        assignment.get("aurora_engine_choice"),
    )
    return detect_relational_gaps(collector, target)


def _price_list_from_the_job(run: PipelineResult, report: dict[str, Any]) -> Any:
    """#1196: the price table each TCO engine's analysis estimate recorded."""
    bases = {}
    for row in (report.get("tco_analysis") or {}).get("cost_breakdown") or []:
        engine = row["database"]
        analysis = json.loads((run.job_dir() / f"analysis-{engine}" / "analysis.json").read_text())
        bases[engine] = analysis["cost_estimate"]["cost_components"].get("price_list")
    return estimates_price_basis(bases)


def test_every_deliverable_states_the_price_date(run: PipelineResult) -> None:
    """#1196: report.json states the price table each TCO figure was priced from, the one
    its analysis estimate recorded, and the PDF and engineering reports both print it."""
    report = _report_json(run)
    basis = report["facts"]["price_list"]
    assert basis == _price_list_from_the_job(run, report)
    assert set(basis["engine_dates"]) == {
        row["database"] for row in report["tco_analysis"]["cost_breakdown"]
    }
    assert basis["retrieved_on"] and basis["undated_engines"] == []
    line = norm(f"List prices: {price_list_text(report['facts'])}")
    texts = {
        "engineering report": norm(md_plain(run.deliverable("engineering-report.md").read_text())),
        "PDF": " ".join(pdf_page_texts(str(run.deliverable("executive-report.pdf")))),
    }
    for deliverable, text in texts.items():
        assert line in text, (run.db, deliverable)


def _rc_titles(report: dict[str, Any]) -> list[str]:
    """The reality-check decisions that still apply, by title (#595)."""
    return [
        str(d["title"]) for d in (report.get("facts") or {}).get("reality_check_decisions") or []
    ]


def test_every_deliverable_shows_the_reality_check_decisions(run: PipelineResult) -> None:
    """#595: the reality check's own trade-offs (consolidations, kept engines,
    recommended patterns) appear in every deliverable that shows trade-offs or
    engine decisions, not only in the engineering report."""
    report = _report_json(run)
    titles = _rc_titles(report)
    assert titles, "the sample's reality check made no decision to show"
    texts = {
        "engineering report": md_plain(run.deliverable("engineering-report.md").read_text()),
        "PDF": " ".join(pdf_page_texts(str(run.deliverable("executive-report.pdf")))),
    }
    for deliverable, text in texts.items():
        if deliverable == "PDF":
            # The PDF's fonts lack the arrow, so it reads "to".
            shown_titles = [t.replace(" → ", " to ") for t in titles]
        else:
            shown_titles = titles
        missing = [t for t in shown_titles if norm(t) not in text]
        assert not missing, f"{deliverable} does not show {missing}"


def test_pdf_report_figures(run: PipelineResult) -> None:
    report = _report_json(run)
    text = " ".join(pdf_page_texts(str(run.deliverable("executive-report.pdf"))))
    _settle(run.db, "PDF", _problems(report, pdf_figures(text), _pdf_required))


def test_engineering_report_figures(run: PipelineResult) -> None:
    report = _report_json(run)
    figs = engineering_figures(run.deliverable("engineering-report.md").read_text())
    _settle(run.db, "engineering report", _problems(report, figs, _engineering_required))


def test_the_trace_appendix_agrees_with_the_unresolved_ties(run: PipelineResult) -> None:
    """#339 review: the appendix says "unresolved" only for ties still open, so
    its "(N still open)" is the "Unresolved engine ties (N)" count (since #1002
    discourse has 33 open ties and trailgear none: its 4 first-assignment ties all
    clear in the reality check), and every listed tie that was settled reads "settled tie"."""
    report = _report_json(run)
    trace = (report.get("facts") or {}).get("query_trace") or {}
    if not trace.get("listed_query_count"):
        pytest.skip("this sample lists no query trace")
    md = run.deliverable("engineering-report.md").read_text()
    appendix = md[md.index("## Appendix: per-query routing decisions") :]
    open_ties = int((report.get("facts") or {}).get("tied_query_count") or 0)
    assert trace["open_tie_query_count"] == open_ties
    m = re.search(r"Unresolved engine ties \((\d+) quer", md) or re.search(
        r"Pending engine choices \(\d+ tables?, (\d+) quer", md
    )
    assert (int(m.group(1)) if m else 0) == open_ties
    if trace["ever_tied_query_count"]:
        assert f"{trace['ever_tied_query_count']} tied at some step ({open_ties} still open)" in (
            appendix
        )
    rows = [r for r in trace["listed_queries"] if r["in_full"]]
    open_in_full = sum(1 for r in rows if r["open_tie"])
    # #1108: an entry is one query or a group of queries whose trace reads the
    # same; a group's heading carries its open-tie count ("26 unresolved ties"),
    # a single query's its "; unresolved tie" (#1170). They add up either way.
    groups = trace.get("trace_groups") or []
    assert sum(int(g["open_tie_count"]) for g in groups) == open_in_full
    labelled = 0
    for heading in re.findall(r"^### .*$", appendix, flags=re.MULTILINE):
        if re.match(r"### \d+ quer", heading):
            n = re.search(r"\b(\d+) unresolved ties?\b", heading)
            labelled += int(n.group(1)) if n else 0
        else:
            labelled += int(heading.endswith("; unresolved tie") or "; unresolved tie, " in heading)
    assert labelled == open_in_full
    assert "left in an unresolved tie" not in md


@pytest.fixture(params=["designed_run", "designed_partial_run"])
def designed_job(request: pytest.FixtureRequest) -> PipelineResult:
    """The designed wordpress job, and its partial-map variant (#1176): a map that
    misses moving tables and maps one DynamoDB does not serve."""
    job: PipelineResult = request.getfixturevalue(request.param)
    return job


def test_designed_report_json_is_consistent(designed_job: PipelineResult) -> None:
    """#949: the same invariants on a job whose engines all have a schema design."""
    report = _report_json(designed_job)
    exp = expected_figures(report)
    as_observed = {k: [(v, "report.json")] for k, v in exp.items()}
    problems = report_invariant_violations(report)
    problems += invariant_violations(report, as_observed, "report.json")
    _settle("wordpress-designed", "report.json", problems)


def test_designed_pdf_report_figures(designed_job: PipelineResult) -> None:
    """#949: with a schema design, the PDF's headline figures still agree with
    report.json: the retained core is not a migration target ("107 of 107 queries
    move"), and "N tables migrate" is the waves' figure, not the design map's."""
    report = _report_json(designed_job)
    text = " ".join(pdf_page_texts(str(designed_job.deliverable("executive-report.pdf"))))
    _settle("wordpress-designed", "PDF", _problems(report, pdf_figures(text), _pdf_required))


def test_designed_engineering_report_figures(designed_job: PipelineResult) -> None:
    report = _report_json(designed_job)
    figs = engineering_figures(designed_job.deliverable("engineering-report.md").read_text())
    _settle(
        "wordpress-designed",
        "engineering report",
        _problems(report, figs, _engineering_required),
    )


NOT_OPTIMIZED = "Wave 1 is not yet optimized for heterogeneous migrations"


def test_a_heterogeneous_wave_1_says_it_is_not_yet_optimized(run: PipelineResult) -> None:
    """#1120: on a cross-engine source (trailgear), wave 1 states the facts layer's note
    and lists every detected relational gap, in the engineering report and in the
    executive report (by feature, no table names); a homogeneous sample shows neither."""
    report = _report_json(run)
    facts = report.get("facts") or {}
    wave1 = next(w for w in _waves(report) if w.get("homogeneity") is not None)
    note = next(w for w in facts["waves"] if w["wave"] == wave1["wave"])["heterogeneous_note"]
    md = run.deliverable("engineering-report.md").read_text()
    start = md.index(f"### Wave {wave1['wave']}:")
    md_wave1 = md_plain(md[start : md.index("\n### ", start + 1)])
    pdf = " ".join(pdf_page_texts(str(run.deliverable("executive-report.pdf"))))
    m = re.search(rf"WAVE {wave1['wave']} (.*?)(?=WAVE \d+ |$)", pdf)
    assert m, "the executive report shows no wave 1 card"
    pdf_wave1 = m.group(1)
    if wave1.get("homogeneity") != "cross_engine":
        assert note is None
        assert NOT_OPTIMIZED not in md and NOT_OPTIMIZED not in pdf
        return
    assert note and note.startswith(NOT_OPTIMIZED)
    assert norm(note) in md_wave1
    assert norm(note) in pdf_wave1
    row = next(w for w in facts["waves"] if w["wave"] == wave1["wave"])
    assert row["lists_relational_gaps"], "the gap check targets wave 1's engine"
    gaps = (facts.get("relational_gaps") or {}).get("gaps") or []
    assert gaps, "the heterogeneous sample finds relational gaps"
    for gap in gaps:
        assert norm(gap["feature"]) in md_wave1, gap["rule_id"]
        assert norm(gap["feature"]) in pdf_wave1, gap["rule_id"]


def test_designed_wave_1_states_what_later_waves_move(designed_job: PipelineResult) -> None:
    """#1284: wave 1's map sentence counts the migration map's rows as mapped, and
    states apart the facts layer's figure for what later waves move off Aurora, so a
    table mapped to Aurora itself is never said to move; the engineering report and
    the PDF show the same sentence."""
    report = _report_json(designed_job)
    wave1 = _waves(report)[0]
    moved = report["facts"]["waves"][0]["moved_by_later_waves"]
    total = int(wave1["table_count"])
    assert moved == wave_one_moved_by_later_waves(_waves(report))
    target = display_engine(wave1["engines"][0])
    sentence = f"later waves move {moved} of the {total} off {target}"
    assert sentence in wave1["rationale"]
    assert "in a later wave" not in wave1["rationale"]
    texts = {
        "engineering report": md_plain(
            designed_job.deliverable("engineering-report.md").read_text()
        ),
        "PDF": " ".join(pdf_page_texts(str(designed_job.deliverable("executive-report.pdf")))),
    }
    for deliverable, text in texts.items():
        assert sentence in norm(text), f"{deliverable} does not show {sentence!r}"
        assert "mapped to a target engine in a later wave" not in norm(text), deliverable
