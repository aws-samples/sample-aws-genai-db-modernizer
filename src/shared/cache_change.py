"""The one sentence that says the cache design covers fewer reads than the gate's candidates.

#1252, ADR-032: its numbers are ``facts.cache_layer``'s (every deliverable calls this with the
fact's counts); the UI states the same sentence from the same fact with its own i18n keys.
"""

from __future__ import annotations


def cache_change_sentence(candidates: int, designed: int) -> str | None:
    """``20 cache candidates, 14 designed; 6 stay on their owner engine.``, or None when the
    design covers every candidate (nothing changed, so nothing to say)."""
    stay = candidates - designed
    if stay <= 0:
        return None
    head = f"{candidates} cache {'candidate' if candidates == 1 else 'candidates'}"
    done = "none designed" if designed == 0 else f"{designed} designed"
    if stay == 1:
        tail = "it stays on its owner engine"
    elif designed == 0:
        tail = f"all {stay} stay on their owner engine"
    else:
        tail = f"{stay} stay on their owner engine"
    return f"{head}, {done}; {tail}."
