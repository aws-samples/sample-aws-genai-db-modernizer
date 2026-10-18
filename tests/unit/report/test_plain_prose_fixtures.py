"""Plain-prose style list (#1034): sources and saved model-run text.

- The rule catalog (the ``why``/``concerns``/``engine_hint`` text risks and the
  UI quote) and the UI's English strings carry no item of the style list.
- The model-written text of the saved model runs (``e2e09_report.json``, the
  judge's ``judge-run3-wordpress`` report), passed through the LLM seams that
  write customer prose, comes out with no item of the style list.
- No renderer reads the audit fields that keep a model's text as received or
  the reasons it was rejected (``STYLE_AUDIT_FIELDS``), so excluding them from
  the deliverable guard (``tests/e2e/test_plain_prose.py``) hides nothing a
  customer sees.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import Any

import pytest

from src.agents.referee.reality_check_place import consolidation_reason
from src.agents.referee.synthesis_handler import apply_synthesis_llm_output
from src.agents.referee.synthesis_prose import clean_schema_design_prose
from src.report import roadmap
from src.shared.prose_style import find_style_hits
from src.tools.validation.customer_text_guard import STYLE_AUDIT_FIELDS

REPO = Path(__file__).resolve().parents[3]
FIXTURES = REPO / "tests" / "unit" / "report" / "fixtures"
MODEL_RUNS = [
    FIXTURES / "e2e09_report.json",
    REPO / "tests" / "unit" / "ci" / "fixtures" / "judge-run3-wordpress" / "report.json",
]


def _strings(node: Any, path: str = "$") -> list[tuple[str, str]]:
    if isinstance(node, dict):
        return [s for k, v in node.items() for s in _strings(v, f"{path}.{k}")]
    if isinstance(node, list):
        return [s for i, v in enumerate(node) for s in _strings(v, f"{path}[{i}]")]
    return [(path, node)] if isinstance(node, str) else []


def _hits(pairs: list[tuple[str, str]]) -> list[tuple[str, str, str]]:
    return [(p, h.term, h.excerpt) for p, text in pairs for h in find_style_hits(text)]


@pytest.mark.parametrize(
    "path", sorted((REPO / "src" / "catalog").rglob("*.toml")), ids=lambda p: p.name
)
def test_catalog_text_has_no_style_hits(path: Path) -> None:
    assert _hits(_strings(tomllib.loads(path.read_text()))) == []


def test_ui_strings_have_no_style_hits() -> None:
    strings = json.loads((REPO / "src" / "ui" / "src" / "locales" / "en.json").read_text())
    assert _hits(_strings(strings)) == []


def test_suggested_path_note_is_plain() -> None:
    assert find_style_hits(roadmap._SUGGESTED_PATH_NOTE) == []


@pytest.mark.parametrize("path", MODEL_RUNS, ids=lambda p: p.parent.name + "/" + p.name)
def test_model_summary_comes_out_of_its_seam_plain(path: Path) -> None:
    report = json.loads(path.read_text())
    det = {
        "table_mappings": report.get("table_mappings") or [],
        "database_name": report.get("database_name", ""),
        "effective_architecture": None,
        "summary": report["summary_deterministic"],
        "migration_waves": report.get("migration_waves"),
        "risk_assessment": report.get("risk_assessment"),
    }
    out = apply_synthesis_llm_output(det, {"executive_summary": report["summary"]})
    assert find_style_hits(out["executive_summary"]) == []


@pytest.mark.parametrize("path", MODEL_RUNS, ids=lambda p: p.parent.name + "/" + p.name)
def test_model_schema_text_comes_out_of_its_seam_plain(path: Path) -> None:
    report = json.loads(path.read_text())
    designs = dict(report.get("schema_designs") or {})
    by_engine: dict[str, list[dict]] = {}
    for t in report.get("trade_offs") or []:
        by_engine.setdefault(str(t.get("engine") or "general"), []).append(t)
    schemas = [(e, d) for e, d in designs.items() if isinstance(d, dict)]
    schemas += [(e, {"trade_offs": ts}) for e, ts in by_engine.items()]
    assert schemas, "the fixture has model-written schema text to check"
    for group in report.get("query_groups") or []:
        for ap in group.get("access_patterns") or []:
            schemas.append((str(ap.get("engine") or ""), {"access_patterns": [ap]}))
    fields = {
        "trade_offs": ("description", "impact"),
        "access_patterns": ("description",),
        "optimizations": ("rationale", "recommendation"),
        "unsupported_patterns": ("reason", "workaround", "recommendation"),
        "migration_notes": ("application_logic_required",),
    }
    for engine, schema in schemas:
        cleaned, _ = clean_schema_design_prose(engine, schema)
        if engine in ("aurora_mysql", "aurora_postgresql"):
            cleaned.pop("access_patterns", None)  # this code's own structural text
        texts = [
            (f"{engine}.{key}", str(entry.get(f) or ""))
            for key, names in fields.items()
            for entry in cleaned.get(key) or []
            if isinstance(entry, dict)
            for f in names
        ]
        assert _hits(texts) == [], engine


@pytest.mark.parametrize("n, noun", [(1, "query"), (3, "queries")])
def test_consolidation_reason_is_plain_and_counts_right(n: int, noun: str) -> None:
    reason = consolidation_reason("documentdb", "aurora_mysql", n)
    assert reason == (
        f"DocumentDB provides no unique capabilities: {n} {noun} can be served by Aurora MySQL"
    )
    assert find_style_hits(reason) == []


def test_no_renderer_reads_the_style_audit_fields() -> None:
    sources = [
        *sorted((REPO / "src" / "report").rglob("*.py")),
        *[
            p
            for p in sorted((REPO / "src" / "ui" / "src").rglob("*.js"))
            if "__tests__" not in p.parts
        ],
    ]
    readers = {
        field: [
            p.relative_to(REPO).as_posix()
            for p in sources
            if re.search(rf"(?<![A-Za-z_]){field}(?![A-Za-z_])", p.read_text())
        ]
        for field in STYLE_AUDIT_FIELDS
    }
    # The withheld-summary notice reads the warnings for their marker only and
    # maps it to its own plain clause (formatting.fallback_withheld_reason and
    # its UI mirror); the reality-check warnings show in the approval gate's
    # expandable detail, never in a deliverable.
    allowed = {
        "summary_validation_warnings": {
            "src/report/formatting.py",
            "src/ui/src/utils/fallbackWithheldReason.js",
            "src/ui/src/utils/summaryFallback.js",
            "src/ui/src/pages/AnalysisResults.js",
        },
        "executive_summary_validation_warnings": {
            "src/ui/src/utils/summaryFallback.js",
            "src/ui/src/pages/AssignmentGate.js",
        },
    }
    for field, files in readers.items():
        assert set(files) <= allowed.get(field, set()), (field, files)


def test_executive_report_wave_label_is_plain() -> None:
    """The executive PDF's wave panel says "Open critical or high risks" (#1034),
    never the all-caps "CRITICAL/HIGH" label. (The saved fixture's own older
    risk text may still say "HIGH risks"; only the renderer's label is checked.)"""
    from tests.executive_pdf import executive_text

    report = json.loads((FIXTURES / "discourse_merged_wave_report.json").read_text())
    text = " ".join(executive_text(report).split())
    assert "Open critical or high risks" in text
    assert "CRITICAL/HIGH" not in text
