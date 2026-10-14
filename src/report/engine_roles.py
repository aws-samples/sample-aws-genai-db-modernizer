"""Each target engine's role in the architecture (owner, cache, search read model).

The rule lives in the facts layer (``src.shared.report_facts.engine_role``,
#615): synthesis records every engine's role in ``report.json``'s ``facts``,
and this module only re-exports the names the renderers already use.
"""

from __future__ import annotations

from src.shared.report_facts import CACHE_ENGINES as _CACHE_ENGINES
from src.shared.report_facts import RELATIONAL_ENGINES as _RELATIONAL_ENGINES
from src.shared.report_facts import SEARCH_ENGINES as _SEARCH_ENGINES
from src.shared.report_facts import SEARCH_READ_MODEL
from src.shared.report_facts import engine_role as _engine_role

__all__ = [
    "SEARCH_READ_MODEL",
    "_CACHE_ENGINES",
    "_RELATIONAL_ENGINES",
    "_ROLE_STROKE",
    "_SEARCH_ENGINES",
    "_engine_role",
]

_ROLE_STROKE = {
    "Retained": "#1F7A3D",
    "Migration target": "#146EB4",
    "Cache layer": "#8B5CF6",
    SEARCH_READ_MODEL: "#2EA597",
    # Muted grey: an engine that was evaluated but serves nothing is not an active
    # component of the target architecture, and must not read as one in the diagram.
    "Evaluated": "#6B7280",
    "Assessed": "#6B7280",
}
