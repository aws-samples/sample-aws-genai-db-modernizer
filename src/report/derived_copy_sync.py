"""The stream sync of DynamoDB derived copies, printed where its overlap risk sits (#1527).

``facts.derived_copy_sync`` (``src.agents.referee.derived_copy_sync``, ADR-032) lists
each sync with its sentences; this only picks the entry of a risk and escapes it. A
report written before the fact has none, and prints nothing extra.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from . import escaping
from .facts import report_facts


def _entry(report: Mapping[str, Any], risk_id: Any) -> tuple[Mapping[str, Any], bool] | None:
    """The sync listed under ``risk_id`` and whether it is the first one listed."""
    fact = report_facts(dict(report)).get("derived_copy_sync")
    if not isinstance(fact, Mapping):
        return None
    syncs = [s for s in fact.get("syncs") or [] if isinstance(s, Mapping)]
    for i, sync in enumerate(syncs):
        if risk_id and sync.get("risk_id") == risk_id:
            return {**sync, "assumptions": fact.get("assumptions") or []}, i == 0
    return None


def derived_copy_sync_md(report: Mapping[str, Any], risk_id: Any) -> list[str]:
    """The engineering report's sub-bullets for the risk ``risk_id``: the path, the cost
    and, under the first sync, the basis the figures rest on."""
    found = _entry(report, risk_id)
    if found is None:
        return []
    sync, first = found
    lines = [str(sync.get("path_line") or ""), str(sync.get("cost_line") or "")]
    if first and sync["assumptions"]:
        lines.append("Basis: " + " ".join(str(a) for a in sync["assumptions"]))
    return [f"  - {escaping.md_text(line)}" for line in lines if line]


def derived_copy_sync_pdf(report: Mapping[str, Any], risk_id: Any) -> list[tuple[str, str]]:
    """``(label, text)`` pairs for the executive report's risk panel: the sync in one
    sentence that names no table, and under the first sync the basis."""
    found = _entry(report, risk_id)
    if found is None:
        return []
    sync, first = found
    out = [("Stream sync", str(sync.get("summary_line") or ""))]
    if first and sync["assumptions"]:
        out.append(("Sync basis", " ".join(str(a) for a in sync["assumptions"])))
    return [(label, text) for label, text in out if text]
