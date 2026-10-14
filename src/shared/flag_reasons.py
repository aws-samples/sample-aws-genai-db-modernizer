"""Customer-facing wording for a model flag's reason category (#647, #629).

One copy, read by the routing code (``src.agents.referee.model_flags``) and the
deliverables (``src.report``), so a reason reads the same everywhere. Keys are
``src.contracts.model_flag.FlagReason`` values.
"""

from __future__ import annotations

FLAG_REASON_TEXT: dict[str, str] = {
    "full_scan": "it would need a full scan",
    "client_side_join": "it would need a client-side join",
    "missing_feature": "it needs a feature the engine lacks",
    "other": "a capability gap",
}


def flag_reason_text(category: str | None) -> str:
    """The wording for ``category`` (a ``FlagReason`` value); the generic one otherwise."""
    return FLAG_REASON_TEXT.get(str(category or ""), FLAG_REASON_TEXT["other"])
