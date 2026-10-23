"""The cache layer section: which queries a cache fronts, and in which wave."""

from __future__ import annotations

from typing import Any

from src.shared.cache_change import cache_change_sentence
from src.shared.cache_pattern_labels import cache_pattern_label
from src.shared.engine_names import display_engine
from src.shared.migration_wave_engines import CACHE_ENGINES as _WAVE_CACHE_ENGINES

from . import escaping
from .facts import report_facts
from .formatting import fmt_num, plural_noun
from .roadmap import _moves_from_label, resolve_migration_waves


def cache_wave_note(front: str) -> str:
    """The cache's role in the migration-waves roadmap (#296, reworded by #321).

    ``front`` is the display name of the engine the cache fronts: the
    retained Aurora engine once wave 1 has reached it, or "the current source
    database" when there is no Aurora wave to front instead (#321 -- the
    cache no longer unconditionally "goes first": wave 1 may now precede it).
    """
    return (
        f"It fronts {front} with no data migration of its own, and keeps serving the same "
        "reads as their owners move in later waves: invalidation follows the engine that "
        "owns each cached table."
    )


def cache_layer_text(n: int, share: float) -> str:
    """``14 designed cached reads · 76.1% of calls``: what the cache's design covers (#296, #1252)."""
    return f"{n} designed cached {plural_noun(n, 'read')} \u00b7 {fmt_num(share, 1)}% of calls"


def cache_change_notes(report: dict[str, Any]) -> list[str]:
    """The one sentence that says the design covers fewer reads than the gate's candidates,
    from ``facts.cache_layer`` (#1252, ADR-032); empty when it covers every candidate."""
    cache = report_facts(report).get("cache_layer")
    if not isinstance(cache, dict):
        return []
    sentence = cache_change_sentence(
        int(cache.get("candidate_read_count") or 0), int(cache.get("designed_read_count") or 0)
    )
    return [sentence] if sentence else []


def _cache_layer_md(report: dict[str, Any]) -> list[str]:
    """Engineering Report section for the cache layer (#296); empty without one.

    The cache owns no query: each cached read stays with its owner engine and the
    cache serves it cache-aside. The section states the eligibility rule so the
    build team can see why these reads, and only these, are cached.

    #459 round 3: a full safety-net drop clears ``engine`` (``overlay_summary``
    returns ``None`` with nothing cached) -- the heading omits the engine name
    rather than hiding the section outright (#424's own repro: the cache engine
    leaves every list that keys on it, but the explanation must still show).
    Named with ``display_engine`` when ``engine`` is known, like every other
    deliverable (#459 review).

    Only this version's own ``safety_net_notes`` render as the section's
    current explanation; the rest of the cumulative ``notes`` (a customer-edit
    note with a full query hash, or a prior version's own safety-net note that
    was not reset, #459 round 2) go under their own "Earlier assignment
    versions" sub-heading -- the audit trail stays, but never presented as
    current.
    """
    overlay = report.get("cache_overlay") or {}
    engine = overlay.get("engine")
    n = int(overlay.get("query_count") or 0)
    full_notes = [x for x in (overlay.get("notes") or []) if x]
    # Superseded wording of the change stays out of "Earlier assignment versions" too.
    safety_net_notes = [x for x in (overlay.get("safety_net_notes") or []) if x]
    changes = cache_change_notes(report)
    if not engine and not changes and not full_notes:
        return []
    name = display_engine(engine) if engine else None
    out = [f"## Cache layer ({escaping.md_text(name)})" if name else "## Cache layer", ""]
    if n:
        owners = overlay.get("owners") or {}
        owned = ", ".join(
            f"{escaping.md_text(display_engine(e))} {c}" for e, c in sorted(owners.items()) if e
        )
        patterns = overlay.get("patterns") or {}
        shapes = ", ".join(
            f"{escaping.md_text(cache_pattern_label(str(p)))} {c}"
            for p, c in sorted(patterns.items())
            if p
        )
        # #321 review (finding 6): the fronted engine comes from the resolved
        # cache wave itself, not re-derived from architecture roles -- see
        # ``_cache_note``'s docstring for why that disagreed with the roadmap.
        cache_wave = next(
            (
                w
                for w in resolve_migration_waves(report)
                if w.get("engines") and w["engines"][0] in _WAVE_CACHE_ENGINES
            ),
            None,
        )
        fronted = cache_wave.get("fronts") if cache_wave else None
        front = (
            escaping.md_text(_moves_from_label(fronted))
            if fronted
            else ("the current source database")
        )
        out += [
            f"{escaping.md_text(name)} fronts {n} designed cached {plural_noun(n, 'read')} "
            f"({fmt_num(overlay.get('call_share_percent', 0), 1)}% of calls, "
            f"{fmt_num(overlay.get('calls_per_second', 0), 1)} calls/s) cache-aside. It owns "
            "none of the workload: each cached read stays with its owner engine, which "
            "serves every miss and every write. " + cache_wave_note(front),
            "",
            f"- Owner engines: {owned or '-'}",
            f"- Read shapes: {shapes or '-'}",
            "- Eligible: a SELECT at "
            f"\u2265 {fmt_num(overlay.get('min_calls_per_second', 0))} calls/s returning "
            f"\u2264 {fmt_num(overlay.get('max_rows_avg', 0))} rows on average, with a point, "
            "top-N, session or small reference-read shape, on tables that are not "
            "write-heavy.",
        ]
    out += [f"- {escaping.md_text(x)}" for x in changes]
    earlier = [x for x in full_notes if x not in safety_net_notes]
    if earlier:
        out += ["", "### Earlier assignment versions", ""]
        out += [f"- {escaping.md_text(x)}" for x in earlier]
    out.append("")
    return out
