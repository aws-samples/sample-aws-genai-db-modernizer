# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Each migration target's Scope row sums to the facts' totals, and both reports print
the facts' own figures (#2162, ADR-032).

On discourse the rows said 35 + 5 = 40 tables kept current by CDC while the wave plan
said 36: both engines of the merged wave read 4 of the same tables. Runs on every
sample job: the facts' per-engine figures add up to the wave's and to
``facts.tables_migrate``, every row adds up to its served tables, and the executive
report's and engineering report's Scope cells read back as those figures.
"""

from __future__ import annotations

import json
from typing import Any

from src.report.facts import report_facts
from tests.e2e.deliverable_figures import (
    Figures,
    engineering_figures,
    pdf_figures,
    pdf_page_texts,
)
from tests.e2e.pipeline import PipelineResult


def _facts(run: PipelineResult) -> dict[str, Any]:
    report: dict[str, Any] = json.loads(run.report_json().read_text())
    return report_facts(report)


def _targets(facts: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        e
        for e in facts["engines"]
        if e["role"] == "Migration target" and isinstance(e.get("engine_source_tables"), int)
    ]


def _expected(row: dict[str, Any]) -> dict[str, int]:
    """The Scope cell's figures, keyed as ``deliverable_figures.scope_split`` reads them."""
    eng = row["engine"]
    out = {f"tables:{eng}": row["engine_source_tables"], f"migrate:{eng}": row["tables_migrate"]}
    out[f"synced:{eng}"] = row["kept_in_sync_tables"] or 0
    out.update({f"moveto:{eng}:{o}": k for o, k in (row["moves_to_tables"] or {}).items() if k})
    under = row.get("kept_in_sync_counted_under") or {}
    out.update({f"under:{eng}:{o}": k for o, k in under.items() if k})
    return out


def test_the_scope_rows_add_up_to_the_wave_figures(run: PipelineResult) -> None:
    facts = _facts(run)
    rows = _targets(facts)
    by_engine = {r["engine"]: r for r in rows}
    for row in rows:
        assert row["engine_source_tables"] == (
            row["tables_migrate"]
            + sum((row["moves_to_tables"] or {}).values())
            + (row["kept_in_sync_tables"] or 0)
            + sum((row.get("kept_in_sync_counted_under") or {}).values())
        ), row["engine"]
    for wave in facts["waves"]:
        if not wave["migrating"] or wave["served_by_engine"] is None:
            continue
        engines = [e for e in wave["engines"] if e in by_engine]
        assert sum(by_engine[e]["kept_in_sync_tables"] for e in engines) == (
            wave["kept_in_sync_tables"]
        ), f"wave {wave['wave']}"
        moved = sum(sum((by_engine[e]["moves_to_tables"] or {}).values()) for e in engines)
        assert moved == len(wave["shared_in_wave"]), f"wave {wave['wave']}"
    if not any(w.get("heterogeneous_note") for w in facts["waves"]):
        assert sum(r["tables_migrate"] for r in rows) == facts["tables_migrate"]


def _shown(figs: Figures, eng: str) -> dict[str, int]:
    prefixes = (f"tables:{eng}", f"migrate:{eng}", f"synced:{eng}")
    return {
        k: v[0][0]
        for k, v in figs.values.items()
        if k in prefixes or k.startswith((f"moveto:{eng}:", f"under:{eng}:"))
    }


def test_both_reports_print_the_facts_scope_figures(run: PipelineResult) -> None:
    rows = _targets(_facts(run))
    pdf = pdf_figures(" ".join(pdf_page_texts(str(run.deliverable("executive-report.pdf")))))
    md = engineering_figures(run.deliverable("engineering-report.md").read_text())
    for figs in (pdf, md):
        assert not [k for k in figs.values if k.endswith(":unparsed")], figs.deliverable
        for row in rows:
            assert _shown(figs, row["engine"]) == _expected(row), (
                figs.deliverable,
                row["engine"],
            )
