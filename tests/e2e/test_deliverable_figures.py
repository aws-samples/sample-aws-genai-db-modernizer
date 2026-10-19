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
- the queries that move to a new engine (#949).

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
from src.shared.report_facts import compute_report_facts
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
    return req


def _pdf_required(report: dict[str, Any], exp: dict[str, Any]) -> set[str]:
    """The executive report shows totals, not the close calls (#1020): those stay in
    the engineering report."""
    req = _common_required(report, exp) - {k for k in exp if k.startswith("cached_reads:")}
    req -= {"close_calls", "settled_ties"}
    req |= {"cost:total", "tables_migrate", "tied", "engine_count"}
    req |= {"choices", "auto_decided", "auto_decided_queries"} & set(exp)
    req |= {f"cost:{e}" for e in final_architecture(report)}
    return req


def _engineering_required(report: dict[str, Any], exp: dict[str, Any]) -> set[str]:
    req = (
        _common_required(report, exp)
        | _whole_schema_required(exp)
        | {"cost:total", "engine_count", "tied", "tables_migrate"}
        | ({"choices", "auto_decided", "auto_decided_queries"} & set(exp))
    )
    req |= {f"queries:{e}" for e in final_architecture(report) if e.startswith("aurora")}
    req |= {k for k in exp if k.startswith("tables:")}
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
    clear in the reality check), and every listed tie that was settled reads "was tied"."""
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
    assert appendix.count("(unresolved tie") + appendix.count(", unresolved tie") == sum(
        1 for r in rows if r["open_tie"]
    )
    assert "left in an unresolved tie" not in md


def test_designed_report_json_is_consistent(designed_run: PipelineResult) -> None:
    """#949: the same invariants on a job whose engines all have a schema design."""
    report = _report_json(designed_run)
    exp = expected_figures(report)
    as_observed = {k: [(v, "report.json")] for k, v in exp.items()}
    problems = report_invariant_violations(report)
    problems += invariant_violations(report, as_observed, "report.json")
    _settle("wordpress-designed", "report.json", problems)


def test_designed_pdf_report_figures(designed_run: PipelineResult) -> None:
    """#949: with a schema design, the PDF's headline figures still agree with
    report.json: the retained core is not a migration target ("107 of 107 queries
    move"), and "N tables migrate" is the waves' figure, not the design map's."""
    report = _report_json(designed_run)
    text = " ".join(pdf_page_texts(str(designed_run.deliverable("executive-report.pdf"))))
    _settle("wordpress-designed", "PDF", _problems(report, pdf_figures(text), _pdf_required))


def test_designed_engineering_report_figures(designed_run: PipelineResult) -> None:
    report = _report_json(designed_run)
    figs = engineering_figures(designed_run.deliverable("engineering-report.md").read_text())
    _settle(
        "wordpress-designed",
        "engineering report",
        _problems(report, figs, _engineering_required),
    )
