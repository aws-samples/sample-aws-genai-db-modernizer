"""Every deliverable shows the same figures as report.json (#573).

AGENTS.md "Deliverables agree": the decision report, the engineering report,
the executive deck and its PDF, and the interactive analysis report (the UI
export) all render one synthesis ``report.json``. Reviews kept finding figures
that differed between them, or that were impossible: a deck card cut at 90
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
- the engines the reality check removed.

It also checks the invariants: no total exceeds the tables or queries
analyzed, wave counts reconcile with their table groups, and every engine a
deliverable names in an engine slot is in the final architecture.

Deterministic-run only: the expected per-engine table counts and the known
mismatches below are specific to the no-LLM samples.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Page

from src.shared.report_facts import compute_report_facts
from tests.e2e.deliverable_figures import (
    SEVERITIES,
    Figures,
    analysis_figures,
    decision_figures,
    deck_figures,
    deck_slide_texts,
    engineering_figures,
    expected_figures,
    final_architecture,
    invariant_violations,
    md_plain,
    migrating,
    norm,
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
    """The report.json the deliverables were rendered from (same directory)."""
    report: dict[str, Any] = json.loads((run.deliverable(".md").parent / "report.json").read_text())
    return report


def _open(page: Page, path: Path) -> None:
    page.route(
        "**/*", lambda r: r.continue_() if r.request.url.startswith("file://") else r.abort()
    )
    page.goto(path.as_uri())
    page.wait_for_load_state("networkidle")


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
    return req


def _decision_required(report: dict[str, Any], exp: dict[str, Any]) -> set[str]:
    req = (
        _common_required(report, exp)
        | _whole_schema_required(exp)
        | {"cost:total", "engine_count", "tables_migrate", "tied"}
    )
    req |= {f"cost:{e}" for e in final_architecture(report)}
    req |= {k for k in exp if k.startswith("tables:")}
    for w in _waves(report):
        req.add(f"wave{w['wave']}:queries")
        if w.get("table_count"):
            req.add(f"wave{w['wave']}:served")
        if migrating(w) and w.get("table_owners"):
            req |= {f"wave{w['wave']}:moving", f"wave{w['wave']}:synced"}
    return req


def _analysis_required(report: dict[str, Any], exp: dict[str, Any]) -> set[str]:
    req = (
        _common_required(report, exp) | _whole_schema_required(exp) | {"cost:total", "engine_count"}
    )
    req |= {f"cost:{e}" for e in final_architecture(report)}
    req |= {k for k in exp if k.startswith("queries:")}
    for w in _waves(report):
        req.add(f"wave{w['wave']}:queries")
        if w.get("table_count"):
            req.add(f"wave{w['wave']}:served")
        if migrating(w) and w.get("table_owners"):
            req |= {f"wave{w['wave']}:moving", f"wave{w['wave']}:synced"}
    return req


def _engineering_required(report: dict[str, Any], exp: dict[str, Any]) -> set[str]:
    req = (
        _common_required(report, exp)
        | _whole_schema_required(exp)
        | {"cost:total", "engine_count", "tied"}
    )
    req |= {f"queries:{e}" for e in final_architecture(report) if e.startswith("aurora")}
    for w in _waves(report):
        req.add(f"wave{w['wave']}:queries")
        if w.get("table_count"):
            req.add(f"wave{w['wave']}:served")
        if migrating(w) and w.get("table_owners"):
            req.add(f"wave{w['wave']}:synced")
    return req


def _deck_required(report: dict[str, Any], exp: dict[str, Any]) -> set[str]:
    req = _common_required(report, exp) | {"tables_migrate", "tied"}
    req |= {k for k in exp if k.startswith(("tables:", "queries:"))}
    for w in _waves(report):
        if migrating(w) and w.get("table_count"):
            req |= {f"wave{w['wave']}:moving", f"wave{w['wave']}:synced"}
            req |= {f"wave{w['wave']}:moving:{e}" for e in w.get("engines") or []}
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
    recomputed = compute_report_facts({k: v for k, v in report.items() if k != "facts"})
    assert stored == recomputed


def _rc_titles(report: dict[str, Any]) -> list[str]:
    """The reality-check decisions that still apply, by title (#595)."""
    return [
        str(d["title"]) for d in (report.get("facts") or {}).get("reality_check_decisions") or []
    ]


def test_every_deliverable_shows_the_reality_check_decisions(
    page: Page, run: PipelineResult
) -> None:
    """#595: the reality check's own trade-offs (consolidations, kept engines,
    recommended patterns) appear in every deliverable that shows trade-offs or
    engine decisions, not only in the engineering report."""
    report = _report_json(run)
    titles = _rc_titles(report)
    assert titles, "the sample's reality check made no decision to show"
    _open(page, run.html("decision-report"))
    shown = {
        int(i)
        for li in page.locator("[data-figure=reality-check-decisions] li[data-decision]").all()
        for i in [li.get_attribute("data-decision")]
        if i is not None
    }
    for li in page.locator("[data-figure=engines-not-used] li[data-decisions]").all():
        shown |= {int(i) for i in (li.get_attribute("data-decisions") or "").split()}
    expected = {
        int(d["index"]) for d in (report.get("facts") or {}).get("reality_check_decisions") or []
    }
    assert shown == expected, "decision report"
    texts = {
        "engineering report": md_plain(run.deliverable(".md").read_text()),
        "deck": " ".join(deck_slide_texts(str(run.deliverable("summary-executive-report.pptx")))),
        "PDF": " ".join(pdf_page_texts(str(run.deliverable("summary-executive-report.pdf")))),
    }
    _open(page, run.html("analysis-report"))
    texts["analysis report"] = norm(page.locator("#tradeoffs-container").text_content() or "")
    for deliverable, text in texts.items():
        if deliverable in ("deck", "PDF"):
            # The deck shows the first cards and names how many more the
            # engineering report lists, with "to" for the arrow its fonts lack.
            shown_titles = [t.replace(" → ", " to ") for t in titles[:5]]
        else:
            shown_titles = titles
        missing = [t for t in shown_titles if norm(t) not in text]
        assert not missing, f"{deliverable} does not show {missing}"


def test_decision_report_figures(page: Page, run: PipelineResult) -> None:
    report = _report_json(run)
    _open(page, run.html("decision-report"))
    _settle(
        run.db, "decision report", _problems(report, decision_figures(page), _decision_required)
    )


def test_analysis_report_figures(page: Page, run: PipelineResult) -> None:
    report = _report_json(run)
    _open(page, run.html("analysis-report"))
    _settle(
        run.db, "analysis report", _problems(report, analysis_figures(page), _analysis_required)
    )


def test_engineering_report_figures(run: PipelineResult) -> None:
    report = _report_json(run)
    figs = engineering_figures(run.deliverable(".md").read_text())
    _settle(run.db, "engineering report", _problems(report, figs, _engineering_required))


def test_deck_figures(run: PipelineResult) -> None:
    report = _report_json(run)
    slides = deck_slide_texts(str(run.deliverable("summary-executive-report.pptx")))
    _settle(run.db, "deck", _problems(report, deck_figures(slides, "deck"), _deck_required))


def test_pdf_figures(run: PipelineResult) -> None:
    """The PDF is drawn from the deck's shapes; its text must carry the same
    figures, so read it with the same extractor and compare with report.json."""
    report = _report_json(run)
    pages = pdf_page_texts(str(run.deliverable("summary-executive-report.pdf")))
    _settle(run.db, "PDF", _problems(report, deck_figures(pages, "PDF"), _deck_required))


def test_pdf_shows_what_the_deck_shows(run: PipelineResult) -> None:
    """Figure for figure, the PDF text carries what the deck's shapes carry."""
    deck = deck_figures(
        deck_slide_texts(str(run.deliverable("summary-executive-report.pptx"))), "deck"
    )
    pdf = deck_figures(pdf_page_texts(str(run.deliverable("summary-executive-report.pdf"))), "PDF")

    def values(f: Figures) -> dict[str, list[Any]]:
        return {k: [v for v, _ in obs] for k, obs in f.values.items()}

    assert values(pdf) == values(deck)
    assert set(pdf.engines) == set(deck.engines)
