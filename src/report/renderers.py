"""Pure renderers for the synthesis deliverables (decision report HTML, engineering
report Markdown, architecture SVG, provenance).

The code lives in one module per deliverable or section (``decision_report``,
``engineering_report``, ``architecture``, ``roadmap``, ``cache_layer``,
``engine_choices``, ``risks``, ``engine_roles``, ``formatting``); this module
re-exports their names so callers keep importing from ``renderers``.
"""

from __future__ import annotations

from src.shared.report_facts import cache_layer_counts

from .architecture import SHARED_TABLES_LABEL, SHARED_TABLES_NOTE, architecture_svg
from .cache_layer import cache_layer_text, cache_wave_note
from .decision_report import render_decision_report_html
from .engine_roles import SEARCH_READ_MODEL
from .engineering_report import render_engineering_report_md
from .formatting import (
    DASH,
    access_pattern_scope,
    artifact_stem,
    clip,
    fallback_withheld_reason,
    fmt_num,
    label_in_scope_access_patterns,
    label_resolved_risks,
    label_summary_counts,
    plural_noun,
    plural_verb,
    provenance,
)
from .risks import NO_OPEN_RISKS_CAVEAT, filtered_risks, resolved_risk_label, resolved_risks
from .roadmap import resolve_migration_waves

__all__ = [
    "DASH",
    "NO_OPEN_RISKS_CAVEAT",
    "SEARCH_READ_MODEL",
    "SHARED_TABLES_LABEL",
    "SHARED_TABLES_NOTE",
    "access_pattern_scope",
    "architecture_svg",
    "artifact_stem",
    "cache_layer_counts",
    "cache_layer_text",
    "cache_wave_note",
    "clip",
    "fallback_withheld_reason",
    "filtered_risks",
    "fmt_num",
    "label_in_scope_access_patterns",
    "label_resolved_risks",
    "label_summary_counts",
    "plural_noun",
    "plural_verb",
    "provenance",
    "render_decision_report_html",
    "render_engineering_report_md",
    "resolve_migration_waves",
    "resolved_risk_label",
    "resolved_risks",
]
