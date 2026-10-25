"""The executive report PDF on every sample (#1020): figures, no table names, no
traceability, nothing clipped, determinism, fonts and branding.

The executive report is a landscape briefing built from ``report.json``'s facts
layer (ADR-032). These tests read its text back with pypdf and compare it with
``report.json`` and with the collector's own table list, on all three samples
(and on a headless model run, which renders through the same code).
"""

from __future__ import annotations

import json
import re
from typing import Any

from pypdf import PdfReader
from pypdf.generic import ArrayObject, IndirectObject

from src.report import executive_report
from src.report.cache_layer import cache_layer_text
from src.report.deliverables import FOLDER_FILENAMES
from src.report.executive_report import collected_identifiers, table_names, without_table_names
from src.report.formatting import _cost_baseline_text, _fmt_usd, cost_baseline_known, fmt_num
from src.report.risks import _severity_counts_text, filtered_risks
from src.report.roadmap import _wave_headline_stats, resolve_migration_waves
from src.shared.engine_names import display_engine
from src.shared.report_facts import build_commit_line
from src.tools.validation.customer_text_guard import scan_text
from tests.branding import hits as found
from tests.e2e.pipeline import PipelineResult
from tests.executive_pdf import (
    body_text,
    collected_table_names,
    normalize,
    outline,
    outside_the_page,
    page_texts,
    printed,
    sql_hits,
    table_name_hits,
)

FILENAME = executive_report.FILENAME
FORBIDDEN = ("undefined", "NaN", "[object Object]", "{{", "None%")


def _pdf(run: PipelineResult):
    return run.deliverable(FILENAME)


def _reader(run: PipelineResult) -> PdfReader:
    return PdfReader(str(_pdf(run)))


def _text(run: PipelineResult) -> str:
    return body_text(_pdf(run), run.db)


def _report(run: PipelineResult) -> dict[str, Any]:
    report: dict[str, Any] = json.loads(run.report_json().read_text())
    return report


def _collected(run: PipelineResult) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((run.job_dir() / "collector" / "output.json").read_text())
    return data


def _commit_line(run: PipelineResult) -> str:
    """The commit line the render step printed (#1166): from the folder's identity
    block, empty for a folder rendered before the deliverables named their commit."""
    data = json.loads(run.deliverable("assessment-data.json").read_text())
    commit = (data.get("_artifact") or {}).get("commit")
    return build_commit_line(commit) if commit else ""


def _names(run: PipelineResult) -> set[str]:
    """The names the render step keeps out: the report's own and the collector's."""
    return table_names(_report(run)) | collected_identifiers(_collected(run))


# -- the document --------------------------------------------------------------


def test_the_file_is_report_json_rendered_byte_for_byte(run: PipelineResult) -> None:
    """Determinism: the folder's PDF is exactly what report.json renders, and two
    renders of it are byte-identical."""
    report = _report(run)
    names = collected_identifiers(_collected(run))
    line = _commit_line(run)
    first = executive_report.render_executive_report_pdf(report, names, commit_line=line)
    assert first == executive_report.render_executive_report_pdf(report, names, commit_line=line)
    assert _pdf(run).read_bytes() == first


def test_every_section_once_in_order_on_landscape_pages(run: PipelineResult) -> None:
    assert outline(_pdf(run)) == list(executive_report.SECTION_HEADINGS)
    for page in _reader(run).pages:
        assert (float(page.mediabox.width), float(page.mediabox.height)) == (
            executive_report.PAGE_W,
            executive_report.PAGE_H,
        )


def test_the_cover_and_the_page_furniture(run: PipelineResult) -> None:
    pages = page_texts(_pdf(run))
    assert executive_report.PRODUCT_NAME in pages[0]
    assert f"Source database {run.db}" in pages[0]
    line = _commit_line(run)
    for n, page in enumerate(pages, start=1):
        assert page.count(executive_report.COPYRIGHT) == 1, f"page {n}"
        # #1166: the code commit of the render on every page, the cover's included.
        assert not line or line in page, f"page {n}"
        if n > 1:
            assert f"Page {n} of {len(pages)}" in page, f"page {n}"
            assert any(h in page for h in executive_report.SECTION_HEADINGS), f"page {n}"


def test_the_closing_pointer_names_files_in_the_folder(run: PipelineResult) -> None:
    assert executive_report.CLOSING_POINTER in page_texts(_pdf(run))[-1]
    folder = run.job_dir() / "deliverables"
    for name in re.findall(r"[\w-]+\.(?:md|pdf|json)\b", executive_report.CLOSING_POINTER):
        assert name in FOLDER_FILENAMES and (folder / name).is_file(), name


# -- every figure is report.json's ------------------------------------------------


def test_the_numbers_match_report_json(run: PipelineResult) -> None:
    report = _report(run)
    facts = report["facts"]
    tco = report["tco_analysis"]
    text = _text(run)
    assert _fmt_usd(facts["projected_monthly_cost_usd"]) == _fmt_usd(tco["projected_monthly_cost"])
    assert f"{_fmt_usd(facts['projected_monthly_cost_usd'])} Projected monthly cost" in text
    current, savings = _cost_baseline_text(tco)
    # #949 (#1028): an unknown baseline's two lines are left out, never a placeholder.
    known = cost_baseline_known(tco)
    assert (f"Current monthly (source): {current}" in text) is known
    assert (f"Savings: {savings}" in text) is known
    assert "source cost not provided" not in text
    assert f"{facts['engine_count']} Engines in the target architecture" in text
    n_in, moving = facts["in_scope_query_count"], facts["moving_query_count"]
    assert n_in == report["assignment_summary"]["in_scope_count"]
    assert f"{moving} of {n_in} quer" in text
    md = run.deliverable("engineering-report.md").read_text()
    assert f"Queries moved: {moving} of {n_in} quer" in md  # the same fact in both
    names = [e["engine"] for e in facts["removed_engines"]]
    if names:
        shown = ", ".join(display_engine(e) for e in names)
        assert f"{shown} Removed by the reality check" in text
    else:
        assert "None Removed by the reality check" in text
    n = facts["tables_migrate"]
    assert f"{n} {'table' if n == 1 else 'tables'} migrat" in text


def test_every_engine_its_role_share_and_cost(run: PipelineResult) -> None:
    facts = _report(run)["facts"]
    text = _text(run)
    for e in facts["engines"]:
        name = display_engine(e["engine"])
        share = (
            cache_layer_text(e["cached_queries"], e["cached_call_share_percent"] or 0)
            if e["cached_queries"] is not None
            else f"{fmt_num(e['workload_percent'], 1)}%"
        )
        assert normalize(f"{name} {e['role']} {share}") in text, e["engine"]
        assert f"{name} {e['role']} {_fmt_usd(e['monthly_cost_usd'])}" in text, e["engine"]
    assert f"Total {_fmt_usd(facts['engine_cost_total_usd'])}" in text
    total = fmt_num(facts["workload_total_percent"], 0)
    assert f"Total {total}% {facts['tables_migrate']} " in text


def test_every_wave_its_counts_and_its_reason(run: PipelineResult) -> None:
    report = _report(run)
    names = _names(run)
    by_wave = {w["wave"]: w for w in report["facts"]["waves"]}
    text = _text(run)
    for w in resolve_migration_waves(report):
        count, tables = _wave_headline_stats(w, by_wave.get(w["wave"]))
        assert f"WAVE {w['wave']}" in text
        assert printed(f"{count}{tables}") in text, w["wave"]
        why = without_table_names(w["rationale"], names)
        assert why, f"wave {w['wave']} lost its whole reason"
        assert printed(f"Why: {why}") in text, w["wave"]


def test_every_open_risk_and_the_risk_counts(run: PipelineResult) -> None:
    report = _report(run)
    facts = report["facts"]
    text = _text(run)
    level = report["risk_assessment"]["overall_risk_level"]
    assert f"{level} Overall risk" in text
    assert f"{facts['open_risk_count']} Open risks" in text
    assert f"{facts['resolved_risk_count']} Resolved by the assignment" in text
    sev = _severity_counts_text(facts["resolved_risks_by_severity"])
    if sev:
        assert f"Resolved by the assignment {sev}" in text  # the tile, "2 high, 4 medium"
        md = run.deliverable("engineering-report.md").read_text()
        assert f"({sev})" in md
    for r in filtered_risks(report):
        assert f"{r['risk_id']} · {r['severity']}" in text, r["risk_id"]


def test_the_pending_engine_choices(run: PipelineResult) -> None:
    facts = _report(run)["facts"]
    text = _text(run)
    tied = facts["tied_query_count"]
    if tied:
        # #1003: one choice per table, the tied queries counted under them.
        n = facts["pending_choice_count"]
        assert f"Pending engine choices ({n} table{'s' if n != 1 else ''}, {tied} quer" in text
        assert "Unresolved engine ties" not in text
    if not tied and not facts["post_schema_reroute_count"]:
        assert executive_report.L_NO_DECISIONS in text


# -- what it never shows ------------------------------------------------------------


def test_no_collected_table_name_appears(run: PipelineResult) -> None:
    """Compared with the collector's own names (tables, views, procedures and
    functions, triggers and columns; ids, bare and schema-qualified names) by the
    rule ``tests.executive_pdf.table_name_hits`` states, in the text and the metadata."""
    names = collected_table_names(_collected(run))
    assert names
    meta = " ".join(str(v) for v in (_reader(run).metadata or {}).values())
    assert table_name_hits(_text(run) + " " + meta, names) == []
    assert table_name_hits(_text(run), table_names(_report(run))) == []


def test_no_sql_and_no_traceability_appendix(run: PipelineResult) -> None:
    report = _report(run)
    text = _text(run)
    assert sql_hits(text) == []
    lowered = text.lower()
    for absent in ("traceability", "appendix", "per-query routing", "why this engine"):
        assert absent not in lowered, absent
    listed = (report["facts"].get("query_trace") or {}).get("listed_queries") or []
    assert listed, "the sample lists no query trace to keep out"
    assert [r["query_id"] for r in listed if r["query_id"] in text] == []
    assert [t["query_id"] for t in report.get("tied_queries") or [] if t["query_id"] in text] == []


# -- nothing clipped ------------------------------------------------------------------


def test_nothing_is_drawn_outside_the_page(run: PipelineResult) -> None:
    assert outside_the_page(_pdf(run)) == []


def test_nothing_is_cut(run: PipelineResult) -> None:
    """No ellipsis anywhere, and every reason, gate and mitigation is printed in full
    (less only what names a table)."""
    report = _report(run)
    names = _names(run)
    text = _text(run)
    assert "…" not in text and "..." not in text
    for w in resolve_migration_waves(report):
        gate = without_table_names(w["gate"], names)
        if gate:
            assert printed(f"Gate before the next wave: {gate}") in text, w["wave"]
    for r in filtered_risks(report):
        m = without_table_names(r.get("mitigation"), names)
        if m and m not in r.get("description", ""):
            assert printed(f"Mitigation: {m}") in text, r["risk_id"]
    for step in report["risk_assessment"].get("mitigation_strategies") or []:
        kept = without_table_names(step, names)
        if kept:
            assert printed(kept) in text


# -- wording, fonts and branding ----------------------------------------------------


def test_no_internal_wording(run: PipelineResult) -> None:
    text = "\n".join(page_texts(_pdf(run)))
    findings = scan_text(text, FILENAME)
    assert findings == [], [(f.kind, f.text, f.excerpt) for f in findings]


def test_no_placeholder_text(run: PipelineResult) -> None:
    text = " ".join(page_texts(_pdf(run)))
    assert [s for s in FORBIDDEN if s in text] == []
    assert run.db in text


def _resolve(obj):
    return obj.get_object() if isinstance(obj, IndirectObject) else obj


def _fonts_shown(content: bytes) -> set[str]:
    """Font resource names actually used to draw a glyph (reportlab registers an
    unused ``/F1`` Helvetica on every page)."""
    text = content.decode("latin1")
    current: str | None = None
    used: set[str] = set()
    for m in re.finditer(r"/(F\d+(?:\+\d+)?)\s+[\d.]+\s+Tf|(Tj|TJ)", text):
        if m.group(1):
            current = m.group(1)
        elif current:
            used.add(current)
    return used


def _page_content_bytes(page) -> bytes:
    contents = _resolve(page.get("/Contents"))
    if contents is None:
        return b""
    if isinstance(contents, ArrayObject):
        return b"".join(bytes(_resolve(part).get_data()) for part in contents)
    return bytes(contents.get_data())


def test_fonts_are_embedded(run: PipelineResult) -> None:
    missing = []
    checked_pages = 0
    for n, page in enumerate(_reader(run).pages, start=1):
        shown = _fonts_shown(_page_content_bytes(page))
        if not shown:
            continue
        checked_pages += 1
        fonts = _resolve(_resolve(page.get("/Resources") or {}).get("/Font") or {})
        for key, ref in fonts.items():
            name = str(key).lstrip("/")
            if name not in shown:
                continue
            font = _resolve(ref)
            base = str(font.get("/BaseFont"))
            if base.endswith(("Helvetica", "Helvetica-Bold")):
                missing.append(f"p{n}:{base} (fallback font used)")
                continue
            desc = font.get("/FontDescriptor")
            if font.get("/Subtype") == "/Type0":
                desc = _resolve(font["/DescendantFonts"][0]).get("/FontDescriptor")
            desc = _resolve(desc) if desc else None
            if not desc or not any(k in desc for k in ("/FontFile", "/FontFile2", "/FontFile3")):
                missing.append(f"p{n}:{base}")
    assert checked_pages > 0, "no page had any drawn text -- nothing was actually checked"
    assert missing == []


CLASSIFICATION = re.compile(r"confidential|pending_classification|msip_label", re.IGNORECASE)


def test_metadata_and_pages_name_only_this_product(run: PipelineResult) -> None:
    meta = _reader(run).metadata
    assert meta is not None
    assert meta.author == executive_report.PRODUCT_NAME
    assert meta.creator == executive_report.PRODUCT_NAME
    texts = [" ".join(str(v) for v in meta.values()), *page_texts(_pdf(run))]
    for n, text in enumerate(texts):
        assert [*found(text), *CLASSIFICATION.findall(text)] == [], n


def test_there_is_no_pptx_and_no_other_pdf(run: PipelineResult) -> None:
    paths = run.report.get("deliverables", [])
    assert [p.rsplit("/", 1)[-1] for p in paths if p.lower().endswith(".pdf")] == [FILENAME]
    assert not [p for p in paths if p.lower().endswith(".pptx")]
    assert not list(run.job_dir().rglob("*.pptx"))


def test_the_judge_reads_each_pages_title(run: PipelineResult) -> None:
    """The quality judge marks each page with its largest-font text: the page title."""
    import pypdf

    from ci.llm import judge

    titles = [t for t, _ in judge.extract_pdf_pages(_pdf(run), pypdf)]
    allowed = {
        *executive_report.SECTION_HEADINGS,
        *(h + executive_report.CONTINUED for h in executive_report.SECTION_HEADINGS),
    }
    assert titles[0] == executive_report.PRODUCT_NAME
    assert [t for t in titles[1:] if t not in allowed] == []
