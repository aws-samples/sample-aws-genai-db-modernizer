"""The cache layer section: which queries a cache fronts, and in which wave."""

from __future__ import annotations

from typing import Any

from src.shared.engine_names import display_engine
from src.shared.migration_wave_engines import CACHE_ENGINES as _WAVE_CACHE_ENGINES
from src.shared.migration_wave_engines import RELATIONAL_ENGINES as _WAVE_RELATIONAL_ENGINES
from src.shared.migration_wave_engines import cache_front_description

from . import escaping
from .formatting import fmt_num, plural_noun
from .roadmap import _moves_from_label, resolve_migration_waves


def cache_layer_counts(report: dict[str, Any], engine: str) -> tuple[int, float] | None:
    """``(cached queries, % of calls)`` of a cache-layer engine, or None (#296).

    The cache owns no query, so it has no workload share; it is described by the hot
    reads it fronts. ``None`` for a legacy report where the cache owned queries.
    """
    entry = next(
        (
            r
            for r in report.get("ranking") or []
            if isinstance(r, dict) and r.get("target") == engine
        ),
        {},
    )
    overlay = report.get("cache_overlay") or {}
    if entry.get("role") != "cache_layer" and overlay.get("engine") != engine:
        return None
    n = entry.get("cache_overlay_queries", overlay.get("query_count", 0))
    share = entry.get("cache_call_share_percent", overlay.get("call_share_percent", 0.0))
    return int(n or 0), float(share or 0.0)


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
    """``20 cached reads · 83.4% of calls`` (#296)."""
    return f"{n} cached {plural_noun(n, 'read')} \u00b7 {fmt_num(share, 1)}% of calls"


def _cache_note(engines: list[dict[str, Any]], report: dict[str, Any]) -> str:
    """The recommendation note for the cache layer (#296).

    #321 review (finding 6): names the engine the cache *actually* fronts,
    taken from the resolved roadmap's own cache wave (``fronts``, via
    ``_moves_from_label``) -- not re-derived from the architecture's engine
    roles. The architecture-role version answered "is there an Aurora engine
    in this report" rather than "what does the cache wave say it fronts",
    which silently disagreed with a report whose stored waves predate #321
    (cache still fronting the legacy source, not Aurora) or are otherwise
    out of the usual order. Falls back to "the current source database"
    only when there is no cache wave to ask (no cache overlay at all).
    """
    esc = escaping.html_text
    caches = [e for e in engines if e["role"] == "Cache layer"]
    names = ", ".join(esc(display_engine(e["engine"])) for e in caches)
    counted = [e for e in caches if e.get("cached_queries") is not None]
    cache_wave = next(
        (
            w
            for w in resolve_migration_waves(report)
            if w.get("engines") and w["engines"][0] in _WAVE_CACHE_ENGINES
        ),
        None,
    )
    fronted = cache_wave.get("fronts") if cache_wave else None
    if fronted in _WAVE_RELATIONAL_ENGINES:
        # Review of #375: say both stages when the cache's final owner
        # differs from the relational engine wave 2 fronts it with here --
        # the same shared helper every other deliverable's cache-fronting
        # sentence calls, so none of them can disagree about it.
        cache_overlay = report.get("cache_overlay") or {}
        front = esc(cache_front_description(fronted, cache_overlay.get("owners")))
    elif fronted:
        front = esc(_moves_from_label(fronted))
    else:
        front = "the current source database"
    # #424: the safety net's notes (dropped cached reads the schema design did
    # not cover, with their own before/after numbers) ran after the gate --
    # without them here, this is the only place the decision report describes
    # the cache layer's final scope, and it would silently disagree with the
    # number shown at the assignment gate. #459 review: ``safety_net_notes``
    # only, never the full ``notes`` (which also carries customer-edit notes
    # with full query hashes and the legacy-migration note -- internal,
    # engineering report only).
    overlay_notes = " ".join(
        esc(x) for x in (report.get("cache_overlay") or {}).get("safety_net_notes") or []
    )
    if not counted:
        base = f"<b>{names}</b> is an additive cache layer."
        return f"{base} {overlay_notes}" if overlay_notes else base
    n = sum(e["cached_queries"] for e in counted)
    share = sum(e["cached_call_share"] for e in counted)
    note = (
        f"<b>{names}</b> is an additive cache layer: it fronts {n} hot "
        f"{plural_noun(n, 'read')} ({esc(fmt_num(share, 1))}% of calls) cache-aside and owns "
        "none of the workload: the engines listed above still own every cached read. "
        + cache_wave_note(front)
    )
    return f"{note} {overlay_notes}" if overlay_notes else note


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
    safety_net_notes = [x for x in (overlay.get("safety_net_notes") or []) if x]
    if not engine and not safety_net_notes and not full_notes:
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
            f"{escaping.md_text(str(p).replace('_', ' '))} {c}"
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
            f"{escaping.md_text(name)} fronts {n} hot {plural_noun(n, 'read')} "
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
    out += [f"- {escaping.md_text(x)}" for x in safety_net_notes]
    earlier = [x for x in full_notes if x not in safety_net_notes]
    if earlier:
        out += ["", "### Earlier assignment versions", ""]
        out += [f"- {escaping.md_text(x)}" for x in earlier]
    out.append("")
    return out
