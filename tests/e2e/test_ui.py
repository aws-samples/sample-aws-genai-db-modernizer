"""The local UI lists the jobs and renders their results against the local API.

Route selection: the live app only ever links to these pages for a completed job
(see src/ui/src/AppRoutes.js and the pages that `href`/`navigate` to them). The
results page lives at `/analysis/results/:jobId`. Two older URLs redirect to it so
bookmarks keep working, each covered below: `/analysis/results-v2/:jobId` (the
page's name until its version suffix was dropped, #957) and `/analysis/report/:jobId`
(the earlier, orange-styled report, removed in #356). The job-monitoring page
(`/analysis/monitor/:jobId`) and the engine-analysis page were removed (#957); their
URLs fall through to the dashboard. `/analysis/patterns/:jobId` is exercised only with
its required `?target=` query param, since every real link to that route supplies one
(src/ui/src/pages/PatternAnalysis.js reads `target` from `useSearchParams` with no
default; a bare visit fetches `.../analysis/null` and 404s).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from src.report.deliverables import DELIVERABLES_DIR, FOLDER_FILENAMES
from tests.e2e.pipeline import PipelineResult
from tests.placeholders import placeholder_leaks

# The results page is a primary deliverable next to the executive report (#987); its
# browser checks (rendering, overflow, accessibility) live here. Restrict this file
# to Chromium only. NOTE for ci/e2e.sh: pytest-playwright implements only_browser via a
# runtime pytest.skip() in pytest_runtest_setup (not deselection), so if this suite
# is ever run with `--browser chromium --browser webkit` under `--fail-on-skip`, the
# webkit parametrizations of every test below will be skipped-then-failed. Run this
# file with a single `--browser chromium` invocation, separate from any multi-browser
# `--fail-on-skip` run.
pytestmark = pytest.mark.only_browser("chromium")

# Mirrors the ENGINE_LABELS map used by the UI's results page
# (src/ui/src/pages/AnalysisResults.js) so this
# test can assert the real, human-readable engine name rather than the raw contract id.
ENGINE_LABELS = {
    "dynamodb": "DynamoDB",
    "documentdb": "DocumentDB",
    "opensearch": "OpenSearch",
    "elasticache": "ElastiCache",
    "aurora_mysql": "Aurora MySQL",
    "aurora_postgresql": "Aurora PostgreSQL",
}


# How long a page may take to render its fetched data. A readiness wait returns the moment
# the content shows, so this is a ceiling for a slow runner and the large discourse
# sample (about 10 s cold on CI), never a delay; Playwright's 5 s default is too tight.
READY_MS = 60_000


def _watch(page: Page) -> dict[str, list[str]]:
    ev: dict[str, list[str]] = {"console": [], "pageerror": [], "failed": []}

    def on_console(msg: object) -> None:
        if getattr(msg, "type", None) == "error":
            ev["console"].append(msg.text)  # type: ignore[attr-defined]

    def on_pageerror(err: object) -> None:
        ev["pageerror"].append(str(err))

    def on_requestfailed(req: object) -> None:
        ev["failed"].append(req.url)  # type: ignore[attr-defined]

    def on_response(resp: object) -> None:
        if resp.status >= 400 and "/api/v1/" in resp.url:  # type: ignore[attr-defined]
            ev["failed"].append(f"{resp.status} {resp.url}")  # type: ignore[attr-defined]

    page.on("console", on_console)
    page.on("pageerror", on_pageerror)
    page.on("requestfailed", on_requestfailed)
    page.on("response", on_response)
    return ev


def _loaded(page: Page, r: PipelineResult) -> None:
    """Wait until the results page has rendered ``r``'s data (#1320).

    Network idle can fire before the fetch that fills the page has started or finished,
    leaving only "Loading assessment results...". The migration roadmap section and the
    source database name render only once the results are loaded (the loading state shows
    neither), and ``expect`` retries until they do.
    """
    page.locator("#migration-roadmap-section").wait_for(timeout=READY_MS)
    expect(page.get_by_text(r.db).first).to_be_visible(timeout=READY_MS)


def _top_engine_label(r: PipelineResult) -> str:
    """The highest-ranked target engine from the synthesis report, as the UI labels it.

    Reads the path off ``r.report`` (the "report" step), not ``r.steps["synthesis"]``:
    the latter is only populated by the deterministic pipeline, while "report" is
    populated in every mode, including a job wrapped by ``from_existing_job``.
    """
    report_path = r.artifact_root / r.report["report"]
    report = json.loads(report_path.read_text())
    top: str = report["ranking"][0]["target"]
    return ENGINE_LABELS.get(top, top)


def test_dashboard_lists_both_jobs(page: Page, ui: str, all_runs: list[PipelineResult]) -> None:
    ev = _watch(page)
    page.goto(f"{ui}/dashboard")
    for r in all_runs:
        expect(page.get_by_text(r.job_id).or_(page.get_by_text(r.db)).first).to_be_visible(
            timeout=READY_MS
        )
    text = page.inner_text("body")
    for r in all_runs:
        assert r.job_id in text or r.db in text, f"{r.db}/{r.job_id} not on dashboard"
    assert ev == {"console": [], "pageerror": [], "failed": []}


def test_results_page_shows_the_ranked_engines(
    page: Page, ui: str, all_runs: list[PipelineResult]
) -> None:
    for r in all_runs:
        ev = _watch(page)
        page.goto(f"{ui}/analysis/results/{r.job_id}")
        _loaded(page, r)
        text = page.inner_text("body")
        assert "undefined" not in text and "NaN" not in text, r.db
        assert r.db in text, (r.db, "source database name missing")
        assert _top_engine_label(r) in text, (
            r.db,
            "top-ranked engine missing",
            _top_engine_label(r),
        )
        assert "/mo" in text, (r.db, "no projected cost rendered")
        assert ev == {"console": [], "pageerror": [], "failed": []}, (r.db, ev)


def test_old_results_v2_route_redirects_to_results(
    page: Page, ui: str, all_runs: list[PipelineResult]
) -> None:
    """Old bookmarks/links to `/analysis/results-v2/:jobId` (#957 dropped the
    version suffix) must land on the `/analysis/results/:jobId` content."""
    for r in all_runs:
        ev = _watch(page)
        page.goto(f"{ui}/analysis/results-v2/{r.job_id}")
        expect(page).to_have_url(re.compile(rf"/analysis/results/{r.job_id}/?$"), timeout=READY_MS)
        _loaded(page, r)
        assert page.url.rstrip("/").endswith(f"/analysis/results/{r.job_id}"), (
            r.db,
            page.url,
        )
        text = page.inner_text("body")
        assert "undefined" not in text and "NaN" not in text, r.db
        assert r.db in text, (r.db, "source database name missing")
        assert _top_engine_label(r) in text, (
            r.db,
            "top-ranked engine missing",
            _top_engine_label(r),
        )
        assert ev == {"console": [], "pageerror": [], "failed": []}, (r.db, ev)


def test_assignments_page_recommends_the_ranked_engines(
    page: Page, ui: str, all_runs: list[PipelineResult]
) -> None:
    for r in all_runs:
        ev = _watch(page)
        page.goto(f"{ui}/analysis/assignments/{r.job_id}")
        expect(page.get_by_text(f"Your {r.db} workload").first).to_be_visible(timeout=READY_MS)
        text = page.inner_text("body")
        assert "undefined" not in text and "NaN" not in text, r.db
        assert f"Your {r.db} workload" in text, (r.db, "workload summary missing")
        assert _top_engine_label(r) in text, (
            r.db,
            "top-ranked engine missing",
            _top_engine_label(r),
        )
        assert ev == {"console": [], "pageerror": [], "failed": []}, (r.db, ev)


def test_legacy_report_route_redirects_to_results(
    page: Page, ui: str, all_runs: list[PipelineResult]
) -> None:
    """Old bookmarks/links to the retired `/analysis/report/:jobId` page (#356,
    the earlier, orange-styled report) must land on the `/analysis/results/:jobId`
    content instead of the now-removed page."""
    for r in all_runs:
        ev = _watch(page)
        page.goto(f"{ui}/analysis/report/{r.job_id}")
        expect(page).to_have_url(re.compile(rf"/analysis/results/{r.job_id}/?$"), timeout=READY_MS)
        _loaded(page, r)
        assert page.url.rstrip("/").endswith(f"/analysis/results/{r.job_id}"), (
            r.db,
            page.url,
        )
        text = page.inner_text("body")
        assert "undefined" not in text and "NaN" not in text, r.db
        assert r.db in text, (r.db, "source database name missing")
        assert _top_engine_label(r) in text, (
            r.db,
            "top-ranked engine missing",
            _top_engine_label(r),
        )
        assert ev == {"console": [], "pageerror": [], "failed": []}, (r.db, ev)


@pytest.mark.parametrize("route", ["analysis/monitor", "analysis/engine-analysis"])
def test_removed_job_pages_fall_through_to_the_dashboard(
    page: Page, ui: str, all_runs: list[PipelineResult], route: str
) -> None:
    """#957: the job-monitoring page (dead download buttons) and the engine-analysis
    page (pre-assignment figures that contradicted the results page) are gone; their
    URLs land on the dashboard instead of rendering either page."""
    r = all_runs[0]
    ev = _watch(page)
    page.goto(f"{ui}/{route}/{r.job_id}")
    expect(page).to_have_url(re.compile(r"/dashboard/?$"), timeout=READY_MS)
    expect(page.get_by_text(r.db).or_(page.get_by_text(r.job_id)).first).to_be_visible(
        timeout=READY_MS
    )
    assert page.url.rstrip("/").endswith("/dashboard"), (route, page.url)
    text = page.inner_text("body")
    assert "Engine analysis:" not in text, route
    assert "Download artifact" not in text and "Download logs" not in text, route
    assert ev == {"console": [], "pageerror": [], "failed": []}, (route, ev)


def test_pattern_analysis_page_renders_for_a_selected_engine(
    page: Page, ui: str, all_runs: list[PipelineResult]
) -> None:
    """Both samples route 'key value lookups' queries to dynamodb, so it is always
    a valid, non-empty ``target`` regardless of which sample is loaded."""
    for r in all_runs:
        ev = _watch(page)
        page.goto(f"{ui}/analysis/patterns/{r.job_id}?target=dynamodb")
        # "Key Value Lookups" is a pattern name the page draws only from the fetched pattern
        # analysis (the loading state shows "0 patterns detected" and "Loading patterns..."),
        # and both samples route key value lookups to dynamodb, so it marks the loaded page.
        # innerText, not textContent: a hidden SVG label carries the same words.
        expect(page.locator("body")).to_contain_text(
            "Key Value Lookups", use_inner_text=True, timeout=READY_MS
        )
        text = page.inner_text("body")
        assert "undefined" not in text and "NaN" not in text, r.db
        assert "patterns detected for dynamodb" in text, (r.db, "pattern analysis body missing")
        assert "Key Value Lookups" in text, (r.db, "expected signal missing")
        assert ev == {"console": [], "pageerror": [], "failed": []}, (r.db, ev)


def _moved_query_trace(r: PipelineResult) -> dict | None:
    """The full decision trace (report.json ``query_traces``) of the first query whose
    engine moved, or None when no query moved (a real run may move none)."""
    report = json.loads((r.artifact_root / r.report["report"]).read_text())
    listed = ((report.get("facts") or {}).get("query_trace") or {}).get("listed_queries") or []
    traces = {q["query_id"]: q for q in (report.get("query_traces") or {}).get("queries") or []}
    moved = [q["query_id"] for q in listed if q.get("moved") and q["query_id"] in traces]
    trace: dict | None = traces[moved[0]] if moved else None
    return trace


OPEN_TRACE = "Open the history and decision trace for query "


def test_a_querys_trace_opens_from_the_routing_table(
    page: Page, ui: str, all_runs: list[PipelineResult]
) -> None:
    """#822: the results page's query routing table opens a query's journey and its
    "Why this engine" trace from the keyboard, with no schema design in the run.

    On the deterministic wordpress sample (or an external job) it opens a moved
    query and checks the move; on a run where no query moved it opens the first
    query and checks the trace renders.
    """
    r = next((x for x in all_runs if x.db == "wordpress"), all_runs[0])
    if not r.external:
        assert not list(r.job_dir().glob("schema-*")), "the deterministic run has no schema design"
        assert _moved_query_trace(r) is not None, "the wordpress sample moves queries"
    trace = _moved_query_trace(r)

    ev = _watch(page)
    page.goto(f"{ui}/analysis/results/{r.job_id}")
    section = page.locator("#query-routing-section")
    expect(section).to_be_visible(timeout=READY_MS)
    # #1028: the section opens collapsed; its header expands it.
    assert not section.get_by_role("table", name="Query routing").is_visible()
    _expand_query_routing(page)
    section.get_by_role("table", name="Query routing").wait_for(timeout=READY_MS)
    if not r.external:  # wordpress lists 107 queries: more than one page of 25
        section.get_by_role("button", name="Next page").wait_for(timeout=READY_MS)
        section.get_by_role("button", name="Previous page").wait_for(timeout=READY_MS)
    if trace is not None:
        qid = trace["query_id"]
        section.get_by_role("button", name="Routing changed or decided").click()
        section.get_by_label("Filter queries").fill(qid[:16])
        opener = section.get_by_role("button", name=f"{OPEN_TRACE}{qid}")
    else:
        opener = section.get_by_role("button", name=re.compile(f"^{OPEN_TRACE}")).first
        qid = (opener.get_attribute("aria-label") or "")[len(OPEN_TRACE) :]
    opener.focus()
    page.keyboard.press("Enter")

    modal = page.get_by_role("dialog")
    modal.get_by_text(f"Query Journey: {qid}").wait_for(timeout=READY_MS)
    modal.get_by_role("tab", name="Why this engine").click()
    modal.get_by_test_id("query-trace-final").first.wait_for(timeout=READY_MS)
    first = modal.get_by_test_id("query-trace-first")
    no_ledger = modal.get_by_test_id("query-trace-no-ledger")
    assert (
        first.count() + no_ledger.count() == 1
    ), "neither the first assignment nor the no-ledger note"
    text = modal.inner_text()
    if trace is not None:
        move = next(
            m for m in trace["moves"] if m["from_engine"] and m["from_engine"] != m["to_engine"]
        )
        modal.get_by_test_id("query-trace-moves").get_by_text(move["explanation"]).wait_for(
            timeout=READY_MS
        )
        assert trace["final_engine_name"] in text, (r.db, "final engine missing from the trace")
        assert "No later routing step changed this query." not in text
    assert "undefined" not in text and "NaN" not in text
    assert ev == {"console": [], "pageerror": [], "failed": []}, (r.db, ev)


def _expand_query_routing(page: Page) -> None:
    """Open the query routing section, collapsed when the page opens (#1028)."""
    header = page.locator('#query-routing-section [aria-expanded="false"]').first
    header.click()
    page.locator('#query-routing-section [aria-expanded="true"]').first.wait_for(timeout=READY_MS)


def _report_wave_stats(r: PipelineResult) -> dict[str, tuple[str, str]]:
    """Each wave's headline as the reports compute it (``roadmap._wave_headline_stats``)
    and its count phrase as the engineering report prints it, by wave number."""
    from src.report.escaping import md_text
    from src.report.roadmap import _facts_by_wave, _wave_headline_stats, resolve_migration_waves

    report = json.loads((r.artifact_root / r.report["report"]).read_text())
    facts = _facts_by_wave(report)
    out = {}
    for w in resolve_migration_waves(report):
        count_phrase, table_phrase = _wave_headline_stats(w, facts.get(int(w.get("wave") or 0)))
        headline = " ".join(f"{count_phrase}{table_phrase}".split())
        out[str(w.get("wave"))] = (headline, f"- {md_text(count_phrase)}")
    return out


def test_results_page_shows_the_reports_wave_plan(
    page: Page, ui: str, all_runs: list[PipelineResult]
) -> None:
    """#957: the results page's migration roadmap shows the same waves as the
    PDF report and the engineering report, with the same headline for each wave,
    read from report.json."""
    for r in all_runs:
        stats = _report_wave_stats(r)
        assert stats, (r.db, "the report has no waves")
        md = r.deliverable("engineering-report.md").read_text(encoding="utf-8")
        for _, md_line in stats.values():
            assert md_line in md.splitlines(), (r.db, md_line)
        expected = {n: headline for n, (headline, _) in stats.items()}
        report = json.loads((r.artifact_root / r.report["report"]).read_text())
        gates = {str(w["wave"]): w.get("gate") or "" for w in report["migration_waves"]}
        ev = _watch(page)
        page.goto(f"{ui}/analysis/results/{r.job_id}")
        section = page.locator("#migration-roadmap-section")
        section.wait_for(timeout=READY_MS)
        cards = section.locator("[data-wave]").all()
        shown = {
            str(c.get_attribute("data-wave")): " ".join(
                c.locator("[data-figure=wave-stats]").inner_text().split()
            )
            for c in cards
        }
        assert shown == expected, r.db
        lists = {
            str(w["wave"]): [p["items"] for p in (w.get("gate_parts") or []) if p["items"]]
            for w in report["migration_waves"]
        }
        for c in cards:
            n = str(c.get_attribute("data-wave"))
            if gates[n]:
                gate = c.locator("[data-figure=wave-gate]")
                # #1028: each list of the gate shows as an inline "(…)" until
                # clicked; opened, the gate reads exactly as report.json's.
                closed = gate.locator('button[aria-expanded="false"]')
                assert closed.count() == len(lists[n]), (r.db, n)
                for items in lists[n]:
                    assert ", ".join(items) not in gate.inner_text(), (r.db, n)
                while closed.count():
                    closed.first.click()
                # Read as shown, without the small "hide" control after each list.
                shown_gate = gate.evaluate(
                    "el => { const c = el.cloneNode(true);"
                    " c.querySelectorAll('[data-gate-hide]').forEach(n => n.remove());"
                    " return c.textContent; }"
                )
                assert shown_gate == f"Gate before the next wave: {gates[n]}", (r.db, n)
                assert gate.locator("[data-gate-hide]").count() == len(lists[n]), (r.db, n)
        # #1120: a cross-engine wave 1 states the facts layer's note and lists every
        # relational gap by feature, as both reports do; no other wave does.
        notes = {str(w["wave"]): w.get("heterogeneous_note") for w in report["facts"]["waves"]}
        listed = {
            str(w["wave"]) for w in report["facts"]["waves"] if w.get("lists_relational_gaps")
        }
        all_gaps = (report["facts"].get("relational_gaps") or {}).get("gaps") or []
        for c in cards:
            n = str(c.get_attribute("data-wave"))
            note = c.locator("[data-figure=wave-heterogeneous-note]")
            shown_gaps = c.locator("[data-figure=wave-gap]").all_inner_texts()
            gaps = all_gaps if n in listed else []
            if notes.get(n):
                assert note.inner_text() == notes[n], (r.db, n)
                assert len(shown_gaps) == len(gaps), (r.db, n)
                for shown_gap, gap in zip(shown_gaps, gaps, strict=True):
                    assert shown_gap.startswith(f"{gap['feature']} ("), (r.db, n, shown_gap)
            else:
                assert note.count() == 0 and shown_gaps == [], (r.db, n)
        count = report["facts"]["migration_wave_count"]
        assert f"Migration roadmap ({count} waves)" in section.inner_text(), r.db
        assert ev == {"console": [], "pageerror": [], "failed": []}, (r.db, ev)


def test_results_page_states_each_tables_source_of_truth_and_replicas(
    page: Page, ui: str, all_runs: list[PipelineResult]
) -> None:
    """#992: the results page lists the replicated tables report.json's
    facts.table_sources lists (the rows the engineering report
    states), each with its CDC direction from the source of truth and its
    replica read count. #1232: the section opens collapsed, and the
    eventual-consistency risk is stated once, in the engineering report's words."""
    from src.shared.engine_names import display_engine
    from src.shared.report_facts import shared_replica_risk_text

    for r in all_runs:
        report = json.loads((r.artifact_root / r.report["report"]).read_text())
        facts = (report.get("facts") or {}).get("table_sources") or {}
        rows = facts.get("replicated_tables") or []
        assert rows, r.db
        ev = _watch(page)
        page.goto(f"{ui}/analysis/results/{r.job_id}")
        section = page.locator("#sources-of-truth-section")
        section.wait_for(timeout=READY_MS)
        n = facts["written_table_count"]
        assert f"Sources of truth and replicas ({n} written tables)" in section.inner_text(), r.db
        first = rows[0]["table"]
        assert not section.locator(f'[data-table="{first}"]').is_visible(), r.db
        # Opened from the keyboard, as a reader without a mouse would.
        section.locator('[aria-expanded="false"]').first.focus()
        page.keyboard.press("Enter")
        section.locator('[aria-expanded="true"]').first.wait_for(timeout=READY_MS)
        text = section.inner_text()
        if facts["single_engine_table_count"]:
            assert ", ".join(facts["single_engine_tables"]) in text, r.db
        for row in rows:
            replicas = ", ".join(display_engine(e) for e in row["replica_engines"])
            cdc = f"CDC: {display_engine(row['source_of_truth'])} → {replicas}"
            cell = section.locator(f'[data-table="{row["table"]}"]')
            line = cell.inner_text()
            assert " ".join(line.split()) == f"{row['table']} {cdc}", (r.db, line)
            reads = cell.locator("xpath=..").locator('[data-figure="replica-reads"]').inner_text()
            noun = "read" if row["replica_read_count"] == 1 else "reads"
            assert reads == f"{row['replica_read_count']} {noun} on a replica", (r.db, reads)
            assert row["shares_replica_risk"], (r.db, row["table"])  # a fresh report's rows
            assert row["risk"] not in text, (r.db, row["table"])
        sources = list(dict.fromkeys(row["source_of_truth"] for row in rows))
        assert text.count(shared_replica_risk_text(sources)) == 1, r.db
        assert text.count("Eventually consistent: ") == 1, r.db
        assert ev == {"console": [], "pageerror": [], "failed": []}, (r.db, ev)


def test_results_page_states_each_tables_owner_also_read_by_and_cache(
    page: Page, ui: str, all_runs: list[PipelineResult]
) -> None:
    """#212: the results page states one line per mapped table, as report.json's
    facts.table_owner_lines and the engineering report's Migration map do: its owner,
    the engines that also read it and its cache, with the same counts. Collapsed when
    the page opens; a job with no table_mappings has no such section. On the designed
    wordpress job, wp_postmeta is owned by DynamoDB and also read by Aurora MySQL, and
    wp_usermeta, which no query writes, is owned by Aurora MySQL."""
    from src.shared.engine_names import display_engine

    designed = _designed_runs(all_runs)
    assert designed, "no job with a schema design in this session"
    for r in all_runs:
        report = json.loads((r.artifact_root / r.report["report"]).read_text())
        facts = (report.get("facts") or {}).get("table_owner_lines")
        ev = _watch(page)
        page.goto(f"{ui}/analysis/results/{r.job_id}")
        page.locator("#sources-of-truth-section").wait_for(timeout=READY_MS)
        section = page.locator("#table-owners-section")
        if r not in designed:
            assert facts is None and section.count() == 0, r.job_id
            continue
        n = facts["table_count"]
        assert f"Table owners ({n} mapped tables)" in section.inner_text(), r.job_id
        first = facts["table_lines"][0]["table"]
        assert not section.locator(f'[data-table="{first}"]').is_visible(), r.job_id
        section.locator('[aria-expanded="false"]').first.focus()
        page.keyboard.press("Enter")
        section.locator('[aria-expanded="true"]').first.wait_for(timeout=READY_MS)
        owned = section.locator('[data-figure="owned-tables"] [data-engine]').all_inner_texts()
        assert owned == [
            f"{display_engine(e)} owns {k}" for e, k in facts["owned_table_counts"].items()
        ], (r.job_id, owned)
        k = facts["also_read_table_count"]
        also = section.locator('[data-figure="also-read-tables"]').inner_text()
        assert also.startswith(f"{k} {'table is' if k == 1 else 'tables are'} also read"), also
        k = facts["cached_table_count"]
        cached = section.locator('[data-figure="cached-tables"]')
        if k:
            text = cached.inner_text()
            assert text.startswith(f"{k} {'table is' if k == 1 else 'tables are'} cached by"), text
        else:
            assert cached.count() == 0, r.job_id
        for line in facts["table_lines"]:
            row = section.locator(f'[data-table="{line["table"]}"]').locator("xpath=ancestor::tr")
            owner = display_engine(line["owner"]) if line["owner"] else "—"
            readers = ", ".join(display_engine(e) for e in line["also_read_by"]) or "—"
            cache = display_engine(line["cached_by"]) if line["cached_by"] else "—"
            assert row.locator('[data-cell="owner"]').inner_text() == owner, line
            assert row.locator('[data-cell="also-read-by"]').inner_text() == readers, line
            assert row.locator('[data-cell="cached-by"]').inner_text() == cache, line
        if not r.external:
            postmeta = section.locator('[data-table="wordpress.wp_postmeta"]').locator(
                "xpath=ancestor::tr"
            )
            assert postmeta.locator('[data-cell="owner"]').inner_text() == "DynamoDB"
            assert postmeta.locator('[data-cell="also-read-by"]').inner_text() == "Aurora MySQL"
            usermeta = section.locator('[data-table="wordpress.wp_usermeta"]').locator(
                "xpath=ancestor::tr"
            )
            assert usermeta.locator('[data-cell="owner"]').inner_text() == "Aurora MySQL"
        assert ev == {"console": [], "pageerror": [], "failed": []}, (r.job_id, ev)


def test_results_page_downloads_the_deliverables_folder(
    page: Page, ui: str, all_runs: list[PipelineResult]
) -> None:
    """#957, #1028: the results page opens with three deliverable buttons,
    Executive Summary, Engineering Report and Assessment Detail, each the
    download of one file of the job's deliverables/ folder, as it is on disk."""
    buttons = {
        "executive-report.pdf": "Executive Summary",
        "engineering-report.md": "Engineering Report",
        "assessment-data.json": "Assessment Detail",
    }
    assert set(buttons) <= set(FOLDER_FILENAMES)
    for r in all_runs:
        folder = r.job_dir() / DELIVERABLES_DIR
        expected = [name for name in buttons if (folder / name).is_file()]
        assert expected == list(buttons), (r.db, expected)
        ev = _watch(page)
        page.goto(f"{ui}/analysis/results/{r.job_id}")
        section = page.locator("#deliverables-section")
        rows = section.locator("[data-deliverable]")
        rows.first.wait_for(timeout=READY_MS)
        assert [x.get_attribute("data-deliverable") for x in rows.all()] == expected, r.db
        assert [x.inner_text().strip() for x in rows.all()] == list(buttons.values()), r.db
        for name in expected:
            with page.expect_download() as info:
                section.get_by_role("link", name=f"Download {name}").click()
            download = info.value
            assert download.suggested_filename == name, (r.db, name)
            assert Path(download.path()).read_bytes() == (folder / name).read_bytes(), (r.db, name)
        # A download ends its navigation, which the browser reports as a failed
        # request for exactly that file; anything else failing is a real error.
        downloads = {f"{r.job_id}/deliverables/{n}" for n in expected}
        ev["failed"] = [u for u in ev["failed"] if u.split("/assessments/", 1)[-1] not in downloads]
        assert ev == {"console": [], "pageerror": [], "failed": []}, (r.db, ev)


# #1028: the section order, from the top; the rest of the page follows.
SECTION_ORDER = [
    "deliverables-section",
    "executive-summary-section",
    "query-flow-section",
    "cost-breakdown-section",
    "migration-roadmap-section",
    "unservable-section",
    "close-calls-section",
    "query-routing-section",
    # #1232: Sources of truth and replicas, collapsed, after the explorer.
    "access-patterns-section",
    # #212: Table owners, collapsed, before Sources of truth (a mapped job only).
    "table-owners-section",
    "sources-of-truth-section",
]
# Banners a report without fallback-engine queries or close calls leaves out,
# the explorer, which a job with no access patterns has none of, and the table
# owners, which a job with no schema design (no table_mappings) has none of.
OPTIONAL_SECTIONS = {
    "unservable-section",
    "close-calls-section",
    "access-patterns-section",
    "table-owners-section",
}


def test_results_page_states_the_price_date(
    page: Page, ui: str, all_runs: list[PipelineResult]
) -> None:
    """#1196: the projected-cost tile states the same price date report.json's
    facts.price_list holds (the line the PDF and engineering reports print)."""
    from src.report.formatting import price_list_text

    for r in all_runs:
        report = json.loads((r.artifact_root / r.report["report"]).read_text())
        assert (report.get("facts") or {}).get("price_list"), r.db
        ev = _watch(page)
        page.goto(f"{ui}/analysis/results/{r.job_id}")
        page.wait_for_load_state("networkidle")
        shown = page.locator("[data-figure=price-list]")
        shown.wait_for()
        assert shown.inner_text().strip() == f"List prices: {price_list_text(report['facts'])}"
        assert ev == {"console": [], "pageerror": [], "failed": []}, (r.db, ev)


def test_results_page_shows_the_headline_first(
    page: Page, ui: str, all_runs: list[PipelineResult]
) -> None:
    """#1028: the results page opens with the deliverable buttons, the executive
    summary, the query flow, the cost breakdown and the migration roadmap, in
    that order, then the fallback-engine warning and close-call banners (when
    the report has them), then query routing, collapsed. The close-call banner names its
    first 3 queries by a 5-character id, then "+N more"."""
    for r in all_runs:
        ev = _watch(page)
        page.goto(f"{ui}/analysis/results/{r.job_id}")
        page.locator("#migration-roadmap-section").wait_for(timeout=READY_MS)
        present = page.evaluate(
            "ids => ids.filter(id => document.getElementById(id))", SECTION_ORDER
        )
        assert set(SECTION_ORDER) - set(present) <= OPTIONAL_SECTIONS, (r.db, present)
        if not r.external and r.db == "wordpress":
            assert "close-calls-section" in present, (r.db, "the wordpress sample has close calls")
        in_order = page.evaluate(
            "ids => ids.map(id => document.getElementById(id))"
            "  .every((el, i, all) => i === 0 ||"
            "    (all[i - 1].compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING))",
            present,
        )
        assert in_order, (r.db, present)
        routing = page.locator("#query-routing-section")
        assert routing.locator('[aria-expanded="false"]').count() >= 1, r.db
        assert not routing.get_by_role("table", name="Query routing").is_visible(), r.db
        sources = page.locator("#sources-of-truth-section")
        assert sources.locator('[aria-expanded="false"]').count() == 1, r.db
        assert sources.locator('[aria-expanded="true"]').count() == 0, r.db
        report = json.loads((r.artifact_root / r.report["report"]).read_text())
        close_calls = (report.get("facts") or {}).get("close_calls") or {}
        pairs = close_calls.get("engine_pairs") or []
        if pairs and close_calls.get("close_call_query_count"):
            first = pairs[0]["query_ids"]
            banner = page.locator("#close-calls-section")
            opener = banner.get_by_role(
                "button", name=f"Open the decision trace for query {first[0]}"
            )
            assert opener.inner_text().strip() == first[0][:6], r.db
            shown = banner.get_by_role(
                "button", name=re.compile("^Open the decision trace for query ")
            )
            assert shown.count() == sum(min(3, len(p["query_ids"])) for p in pairs), r.db
        assert ev == {"console": [], "pageerror": [], "failed": []}, (r.db, ev)


def test_results_page_states_the_resolved_risks_by_severity(
    page: Page, ui: str, all_runs: list[PipelineResult]
) -> None:
    """#1020: the risk section's resolved-risks header reads the stored
    facts.resolved_risks_by_severity, as the executive and engineering reports do
    ("6 risks resolved by the assignment: 2 high, 4 medium")."""
    checked = 0
    for r in all_runs:
        facts = json.loads((r.artifact_root / r.report["report"]).read_text())["facts"]
        by_severity = facts.get("resolved_risks_by_severity") or {}
        if not by_severity:
            continue
        n = facts["resolved_risk_count"]
        severities = ", ".join(f"{k} {s.lower()}" for s, k in by_severity.items())
        noun = "risk" if n == 1 else "risks"
        expected = f"{n} {noun} resolved by the assignment: {severities}"
        md = r.deliverable("engineering-report.md").read_text(encoding="utf-8")
        assert f"({severities})" in md, r.db
        ev = _watch(page)
        page.goto(f"{ui}/analysis/results/{r.job_id}")
        page.locator("#migration-roadmap-section").wait_for(timeout=READY_MS)
        header = page.get_by_text(expected, exact=True)
        assert header.count() == 1, (r.db, expected)
        checked += 1
        assert ev == {"console": [], "pageerror": [], "failed": []}, (r.db, ev)
    assert checked, "no sample resolved a risk"


def test_results_page_states_the_distinct_query_patterns(
    page: Page, ui: str, all_runs: list[PipelineResult]
) -> None:
    """#1146: the results page prints "N queries in M distinct query patterns" from
    facts.query_pattern_counts, the same figures the engineering report prints."""
    figure = re.compile(r"([\d,]+) quer(?:y|ies) in ([\d,]+) distinct query patterns?")
    for r in all_runs:
        facts = json.loads((r.artifact_root / r.report["report"]).read_text())["facts"]
        counts = facts["query_pattern_counts"]
        expected = (counts["query_count"], counts["pattern_count"])
        md = r.deliverable("engineering-report.md").read_text(encoding="utf-8")
        found = figure.search(md)
        assert found and tuple(int(g.replace(",", "")) for g in found.groups()) == expected, r.db
        ev = _watch(page)
        page.goto(f"{ui}/analysis/results/{r.job_id}")
        line = page.locator('[data-figure="query-patterns"]')
        line.wait_for(timeout=READY_MS)
        shown = figure.fullmatch(line.inner_text().strip())
        assert shown, (r.db, line.inner_text())
        assert tuple(int(g.replace(",", "")) for g in shown.groups()) == expected, r.db
        assert ev == {"console": [], "pageerror": [], "failed": []}, (r.db, ev)


def test_results_page_states_the_no_open_risks_caveat_only_when_no_risk_is_open(
    page: Page, ui: str, all_runs: list[PipelineResult]
) -> None:
    """#996: the sentence the engineering report prints under "no open risks" is on the
    results page too, in the risk section, and only for a report with no open risk."""
    from src.report.risks import NO_OPEN_RISKS_CAVEAT

    for r in all_runs:
        facts = json.loads((r.artifact_root / r.report["report"]).read_text())["facts"]
        no_open = facts["open_risk_count"] == 0
        md = r.deliverable("engineering-report.md").read_text(encoding="utf-8")
        assert (NO_OPEN_RISKS_CAVEAT in md) == no_open, r.db
        ev = _watch(page)
        page.goto(f"{ui}/analysis/results/{r.job_id}")
        page.locator("#migration-roadmap-section").wait_for(timeout=READY_MS)
        note = page.locator("#no-open-risks-note")
        assert note.count() == (1 if no_open else 0), r.db
        if no_open:
            assert note.inner_text().strip() == NO_OPEN_RISKS_CAVEAT, r.db
        assert ev == {"console": [], "pageerror": [], "failed": []}, (r.db, ev)


def test_results_page_states_the_caveat_even_with_no_other_risk_content(
    page: Page, ui: str, all_runs: list[PipelineResult]
) -> None:
    """#996: a report with no risks, no resolved risks and no overall level still gets a
    risk section holding the no-open-risks sentence, as the engineering report prints it."""
    from src.report.risks import NO_OPEN_RISKS_CAVEAT

    r = all_runs[0]

    def strip_risks(route: object) -> None:
        response = route.fetch()  # type: ignore[attr-defined]
        body = response.json()
        body["synthesis"]["risk_assessment"] = {}
        body["synthesis"]["facts"].update(open_risk_count=0, resolved_risk_count=0)
        route.fulfill(response=response, json=body)  # type: ignore[attr-defined]

    page.route(re.compile(rf"/assessments/{r.job_id}/results$"), strip_risks)
    ev = _watch(page)
    page.goto(f"{ui}/analysis/results/{r.job_id}")
    page.locator("#migration-roadmap-section").wait_for(timeout=READY_MS)
    note = page.locator("#no-open-risks-note")
    assert note.count() == 1
    assert note.inner_text().strip() == NO_OPEN_RISKS_CAVEAT
    assert page.get_by_role("heading", name="Risk assessment").count() == 1
    assert ev == {"console": [], "pageerror": [], "failed": []}, ev


def test_results_page_names_a_resolved_risks_tables(
    page: Page, ui: str, all_runs: list[PipelineResult]
) -> None:
    """#950: a resolved risk keeps naming its tables on the results page, as the
    engineering report's "Affects" line does."""
    checked = 0
    for r in all_runs:
        report = json.loads((r.artifact_root / r.report["report"]).read_text())
        facts = report["facts"]
        resolved = [
            x
            for x in (report.get("risk_assessment") or {}).get("resolved_risks") or []
            if x.get("affected_tables")
        ]
        if not resolved or not facts.get("resolved_risks_by_severity"):
            continue
        n = facts["resolved_risk_count"]
        severities = ", ".join(
            f"{k} {s.lower()}" for s, k in facts["resolved_risks_by_severity"].items()
        )
        header = f"{n} {'risk' if n == 1 else 'risks'} resolved by the assignment: {severities}"
        ev = _watch(page)
        page.goto(f"{ui}/analysis/results/{r.job_id}")
        page.locator("#migration-roadmap-section").wait_for(timeout=READY_MS)
        page.get_by_text(header, exact=True).click()
        line = f"Affects: {', '.join(resolved[0]['affected_tables'])}"
        assert page.get_by_text(line, exact=True).count() >= 1, (r.db, line)
        checked += 1
        assert ev == {"console": [], "pageerror": [], "failed": []}, (r.db, ev)
    assert checked, "no sample resolved a risk with tables"


def test_results_page_shows_the_same_risks_as_report_json(
    page: Page, ui: str, all_runs: list[PipelineResult]
) -> None:
    """#950 (ADR-032 Decision 5): the results page's severity tabs count what
    facts.open_risks_by_severity counts, and each tab lists its risks by id, the
    migration risks included."""
    for r in all_runs:
        report = json.loads((r.artifact_root / r.report["report"]).read_text())
        by_severity = report["facts"].get("open_risks_by_severity") or {}
        risks = report["risk_assessment"]["risks"]
        ev = _watch(page)
        page.goto(f"{ui}/analysis/results/{r.job_id}")
        page.locator("#migration-roadmap-section").wait_for(timeout=READY_MS)
        for severity in ("CRITICAL", "HIGH", "MEDIUM", "LOW"):
            n = int(by_severity.get(severity) or 0)
            tab = page.get_by_role("tab", name=f"{severity} ({n})", exact=True)
            assert tab.count() == 1, (r.db, severity, n)
            if not n:
                continue
            tab.click()
            for risk in risks:
                if risk.get("severity") == severity:
                    shown = page.get_by_text(str(risk["risk_id"]), exact=True)
                    assert shown.count() >= 1, (r.db, risk["risk_id"])
        assert ev == {"console": [], "pageerror": [], "failed": []}, (r.db, ev)


def test_ui_shows_only_the_product_name(
    page: Page, ui: str, all_runs: list[PipelineResult]
) -> None:
    """#957: no page names "ATX", "AWS Transform" or "transform"; the header says
    "Database Modernizer Assessment Tool"."""
    r = all_runs[0]
    for path in (
        "/dashboard",
        "/analysis/create",
        f"/analysis/monitor/summary/{r.job_id}",
        f"/analysis/results/{r.job_id}",
    ):
        page.goto(f"{ui}{path}")
        # Content only the loaded page shows, so the text scan below never reads a spinner.
        if path == "/dashboard":
            expect(page.get_by_role("link", name=r.job_id).first).to_be_visible(timeout=READY_MS)
        elif path == "/analysis/create":
            expect(page.get_by_role("heading", name="Create analysis")).to_be_visible(
                timeout=READY_MS
            )
        elif path.startswith("/analysis/monitor/summary/"):
            page.locator("[data-sankey-node]").first.wait_for(timeout=READY_MS)
        else:
            _loaded(page, r)
        text = page.inner_text("body")
        assert not re.search(r"\bATX\b|AWS\s+Transform|transform", text, re.IGNORECASE), path
        assert "Database Modernizer Assessment Tool" in text, path
        assert page.title() == "Database Modernizer Assessment Tool", path


# ---------------------------------------------------------------------------
# The results page's browser checks (#987): the retired HTML reports were
# rendered in a browser for leaked placeholders, the customer text guard,
# horizontal overflow and accessibility. The results page is the deliverable
# that replaces them, so it gets the same checks.
# ---------------------------------------------------------------------------

# Placeholder values ("null" as a cell, not in a sentence) are checked by tests/placeholders.py;
# "[object Object]" and "{{" are leaks anywhere.
FORBIDDEN_WORDS = ("undefined", "NaN", "null")
FORBIDDEN_SUBSTRINGS = ("[object Object]", "{{")

# axe impacts that fail the scan; "minor" findings are printed for visibility only.
BLOCKING_IMPACTS = ("critical", "serious", "moderate")


def _leaked_forbidden_text(text: str) -> list[str]:
    leaked = placeholder_leaks(text, FORBIDDEN_WORDS)
    leaked += [s for s in FORBIDDEN_SUBSTRINGS if s in text]
    return leaked


def _open_results(page: Page, ui: str, r: PipelineResult) -> dict[str, list[str]]:
    ev = _watch(page)
    page.goto(f"{ui}/analysis/results/{r.job_id}")
    page.locator("#migration-roadmap-section").wait_for(timeout=READY_MS)
    return ev


def test_results_page_text_is_clean(page: Page, ui: str, all_runs: list[PipelineResult]) -> None:
    from src.tools.validation.customer_text_guard import scan_text

    for r in all_runs:
        ev = _open_results(page, ui, r)
        text = page.inner_text("body")
        assert _leaked_forbidden_text(text) == [], r.db
        findings = scan_text(text, f"results-page:{r.db}")
        assert findings == [], (r.db, [(f.kind, f.text, f.excerpt) for f in findings])
        assert ev == {"console": [], "pageerror": [], "failed": []}, (r.db, ev)


@pytest.mark.parametrize("width", [1280, 1920])
def test_results_page_has_no_horizontal_overflow(
    page: Page, ui: str, all_runs: list[PipelineResult], width: int
) -> None:
    page.set_viewport_size({"width": width, "height": 900})
    for r in all_runs:
        _open_results(page, ui, r)
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"), r.db


def _expand_every_section(page: Page) -> None:
    """Open every collapsed section of the page's content (the Table design's
    tables and query groups, the explorer's groups, ...), so the scan sees what
    a reader can open, not only what shows on load."""
    main = page.locator("main")
    for _ in range(4):  # nested sections open one level at a time
        # Section headers only (they control their content); never a menu, a
        # dropdown or a popover trigger.
        closed = main.locator('[aria-expanded="false"][aria-controls]:not([aria-haspopup])')
        n = closed.count()
        if not n:
            return
        for i in range(n - 1, -1, -1):
            item = closed.nth(i)
            if item.is_visible():
                item.click()
        page.wait_for_timeout(200)


def test_results_page_has_no_moderate_or_worse_accessibility_violations(
    page: Page, ui: str, all_runs: list[PipelineResult]
) -> None:
    from axe_playwright_python.sync_playwright import Axe

    for r in all_runs:
        _open_results(page, ui, r)
        _expand_every_section(page)
        violations = Axe().run(page).response["violations"]
        blocking = [v["id"] for v in violations if v.get("impact") in BLOCKING_IMPACTS]
        minor = [v["id"] for v in violations if v.get("impact") not in BLOCKING_IMPACTS]
        if minor:
            print(f"\naxe minor findings on the results page for {r.db}: {minor}")
        # Name the elements, so a failure on a headless run's job says what to fix.
        where = {
            v["id"]: [n.get("target") for n in v.get("nodes", [])][:5]
            for v in violations
            if v.get("impact") in BLOCKING_IMPACTS
        }
        assert blocking == [], (r.job_id, r.db, blocking, where)


def _designed_runs(all_runs: list[PipelineResult]) -> list[PipelineResult]:
    """The jobs with at least one schema design: ``ui-desi`` in the deterministic
    suite (``tests/e2e/designed_run.py``), the headless run's own job on e2e-llm."""
    return [r for r in all_runs if list(r.job_dir().glob("schema-*/v*/schema_output.json"))]


def test_the_designed_job_renders_the_schema_design_panels(
    page: Page, ui: str, all_runs: list[PipelineResult]
) -> None:
    """#987: the suite always has a job with schema designs, so the checks above
    also cover the panels only a schema-designed run shows: the explorer with
    its selection tables, the design trade-offs, the Table design when an
    Aurora engine was designed, and the explorer's pager on the deterministic
    fixture (which has more than one page). Fails when no such job renders them."""
    designed = _designed_runs(all_runs)
    assert designed, "no job with a schema design in this session"
    if not any(r.external for r in all_runs):
        assert [r.job_id for r in designed] == ["ui-desi"]
    for r in designed:
        ev = _open_results(page, ui, r)
        text = page.inner_text("body")
        headings = ["Access pattern explorer", "Trade-offs and design decisions"]
        aurora = bool(list(r.job_dir().glob("schema-aurora_*/v*/schema_output.json")))
        if aurora:
            headings.append("Table design")
        for heading in headings:
            assert heading in text, (r.job_id, heading)
        assert "Aurora design" not in text, r.job_id
        if aurora:
            # #1232: every section of the Table design opens collapsed.
            design = page.locator("#table-design-section")
            assert design.locator('[aria-expanded="false"]').count() >= 1, r.job_id
            assert design.locator('[aria-expanded="true"]').count() == 0, r.job_id
        assert page.get_by_role("radio").count() > 0, r.job_id
        if not r.external:
            assert page.get_by_role("button", name="Next page").count() > 0, r.job_id
        assert ev == {"console": [], "pageerror": [], "failed": []}, (r.job_id, ev)


def test_results_page_states_the_pending_engine_choices(
    page: Page, ui: str, all_runs: list[PipelineResult]
) -> None:
    """#1003: the results page states the pending engine choices with the counts
    report.json's facts hold (the ones the engineering report and the PDF state),
    and nothing when there are none."""
    for r in all_runs:
        report = json.loads((r.artifact_root / r.report["report"]).read_text())
        facts = report.get("facts") or {}
        n, q = facts.get("pending_choice_count") or 0, facts.get("pending_choice_query_count") or 0
        ev = _watch(page)
        page.goto(f"{ui}/analysis/results/{r.job_id}")
        page.locator("#sources-of-truth-section").wait_for(timeout=READY_MS)
        note = page.locator("#engine-choices-note")
        if n:
            tables = "table still ties" if n == 1 else "tables still tie"
            queries = "query" if q == 1 else "queries"
            assert f"{n} {tables} between engines ({q} {queries})" in note.inner_text(), r.db
        else:
            assert note.count() == 0, r.db
        assert ev == {"console": [], "pageerror": [], "failed": []}, (r.db, ev)


def test_query_routing_table_fits_its_card_at_1440(
    page: Page, ui: str, all_runs: list[PipelineResult]
) -> None:
    """#1244: at a 1440 px viewport the Query routing table was about 2083 px wide in a
    card about 1238 px wide, with Engine and Routing off-screen until the reader scrolled
    right. The table now fits its card, and Engine and Routing are on the first screen."""
    page.set_viewport_size({"width": 1440, "height": 900})
    for r in all_runs:
        page.goto(f"{ui}/analysis/results/{r.job_id}")
        expect(page.locator("#query-routing-section")).to_be_visible(timeout=READY_MS)
        _expand_query_routing(page)
        section = page.locator("#query-routing-section")
        section.get_by_role("table", name="Query routing").wait_for(timeout=READY_MS)
        section.locator("table tbody tr").first.wait_for(timeout=READY_MS)
        m = page.evaluate("""() => {
              const table = document.querySelector('#query-routing-section table');
              const scroller = table.parentElement;
              const box = scroller.getBoundingClientRect();
              const head = (name) => [...table.querySelectorAll('thead th')]
                .find((th) => th.innerText.trim() === name).getBoundingClientRect();
              return {tableWidth: table.scrollWidth, visibleWidth: scroller.clientWidth,
                      visibleRight: box.right, engineRight: head('Engine').right,
                      routingRight: head('Routing').right};
            }""")
        assert m["tableWidth"] <= m["visibleWidth"] + 1, (r.db, m)
        assert m["engineRight"] <= m["visibleRight"] + 1, (r.db, m)
        assert m["routingRight"] <= m["visibleRight"] + 1, (r.db, m)


# #1246: one colour per engine on every view. Measured in the browser on the results
# page and the pattern analysis page: every engine badge (cost cards, roadmap, explorer,
# risks), every Sankey node and every trade-off Mapping target chip is drawn in the
# engine's catalog colour (colors.ui), and source-table chips in a neutral grey.
_ENGINE_COLOURS_JS = """() => {
  const rgb = (el, prop) => getComputedStyle(el)[prop];
  const badges = [...document.querySelectorAll('[data-engine-badge]')].map((el) => ({
    engine: el.dataset.engineBadge, colour: rgb(el.firstElementChild, 'backgroundColor')}));
  const nodes = [...document.querySelectorAll('svg g[data-sankey-node]')].map((g) => ({
    engine: g.dataset.sankeyNode, colour: rgb(g.querySelector('rect'), 'fill')}));
  const chips = [...document.querySelectorAll('[data-mapping]')].map((el) => ({
    kind: el.dataset.mapping, engine: el.dataset.engine || null, colour: rgb(el, 'backgroundColor')}));
  return {badges, nodes, chips};
}"""


def _rgb(hex_colour: str) -> str:
    h = hex_colour.lstrip("#")
    return f"rgb({int(h[0:2], 16)}, {int(h[2:4], 16)}, {int(h[4:6], 16)})"


def test_each_engine_has_one_colour_across_the_ui(
    page: Page, ui: str, all_runs: list[PipelineResult]
) -> None:
    from src.shared import catalog

    colour = {e.id: _rgb(e.colors.ui) for e in catalog.engines()}
    neutral = _rgb("#5f6b7a")
    page.set_viewport_size({"width": 1440, "height": 900})
    seen: dict[str, int] = {"badges": 0, "nodes": 0, "chips": 0}
    for r in all_runs:
        _open_results(page, ui, r)
        _expand_every_section(page)
        found = page.evaluate(_ENGINE_COLOURS_JS)
        for b in found["badges"]:
            assert b["colour"] == colour.get(b["engine"], neutral), (r.db, b)
        for n in found["nodes"]:
            if n["engine"] in colour:
                assert n["colour"] == colour[n["engine"]], (r.db, n)
                seen["nodes"] += 1
        for c in found["chips"]:
            expected = neutral if c["kind"] == "source" else colour.get(c["engine"], neutral)
            assert c["colour"] == expected, (r.db, c)
        seen["badges"] += len(found["badges"])
        seen["chips"] += len(found["chips"])
        # The roadmap's wave badges are engine badges too: every wave engine is drawn.
        waves = page.locator("[data-wave] [data-engine] [data-engine-badge]")
        assert waves.count() > 0, r.db
    assert seen["badges"] > 0 and seen["nodes"] > 0, seen


# #1245: the cache layer's label ("ElastiCache (N)") and its "cache layer" sub-label
# used to be drawn over the small dashed cache node and over each other. Measured in
# the browser: no label box crosses its node, the two labels do not overlap, and both
# fit inside the diagram.
_SANKEY_CACHE_LABEL_BOXES = """() => {
  const box = (el) => { const r = el.getBoundingClientRect();
    return {left: r.left, right: r.right, top: r.top, bottom: r.bottom}; };
  return [...document.querySelectorAll('svg')].flatMap((svg) =>
    [...svg.querySelectorAll('g')].filter((g) =>
      [...g.querySelectorAll('text')].some((t) => t.textContent === 'cache layer'))
    .map((g) => {
      const [name, sub] = g.querySelectorAll('text');
      return {label: name.textContent, svg: box(svg), node: box(g.querySelector('rect')),
              name: box(name), sub: box(sub)};
    }));
}"""


def _overlap(a: dict[str, float], b: dict[str, float]) -> bool:
    return (
        a["left"] < b["right"]
        and b["left"] < a["right"]
        and a["top"] < b["bottom"]
        and b["top"] < a["bottom"]
    )


def test_sankey_cache_label_clears_its_node_and_sub_label(
    page: Page, ui: str, all_runs: list[PipelineResult]
) -> None:
    page.set_viewport_size({"width": 1440, "height": 900})
    checked = []
    expected = 0
    for r in all_runs:
        report = json.loads((r.artifact_root / r.report["report"]).read_text())
        if report.get("cache_overlay"):
            expected += 2  # the Job summary and the results page both draw it
        for url in (
            f"{ui}/analysis/monitor/summary/{r.job_id}",
            f"{ui}/analysis/results/{r.job_id}",
        ):
            page.goto(url)
            # The Sankey itself must be drawn before its labels are measured, whether or
            # not this run has a cache layer: an empty scan of a page that has not
            # rendered would pass vacuously.
            page.locator("[data-sankey-node]").first.wait_for(timeout=READY_MS)
            labels = page.evaluate(_SANKEY_CACHE_LABEL_BOXES)
            if report.get("cache_overlay"):
                assert labels, (
                    r.db,
                    url,
                    "the report keeps a cache layer but the Sankey draws none",
                )
            else:
                assert not labels, (r.db, url, "the Sankey draws a cache layer the report lacks")
            for g in labels:
                where = (r.db, url, g["label"])
                assert not _overlap(g["name"], g["node"]), where
                assert not _overlap(g["sub"], g["node"]), where
                assert not _overlap(g["name"], g["sub"]), where
                for label in (g["name"], g["sub"]):
                    assert g["svg"]["left"] <= label["left"], where
                    assert label["right"] <= g["svg"]["right"], where
                checked.append(where)
    # Every run whose report keeps a cache layer was measured on both pages (the
    # wordpress sample does, so the deterministic job is never vacuous).
    assert expected > 0 or all(not r.db.startswith("wordpress") for r in all_runs)
    assert len(checked) == expected, checked
