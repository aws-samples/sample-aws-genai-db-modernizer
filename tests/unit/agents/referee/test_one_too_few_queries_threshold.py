"""One "too few queries to keep an engine" threshold in routing (#493).

The #493 review counted five: ``MIN_QUERIES_THRESHOLD``,
``TINY_MANDATORY_QUERY_THRESHOLD``, ``AURORA_ABSORPTION_QUERY_THRESHOLD``
(the reality check's old passes, since replaced by the shared Decision 7 rule),
the "< 5 queries" text in the uncalled lightweight-alternatives table, and the
sanity sweep's ``ORPHAN_ENGINE_THRESHOLD``. Only the sweep's is a routing rule
today; this pins it so a second, disagreeing count does not come back.
``group_splitter.SMALL_GROUP_THRESHOLD`` is a schema-design batching size, not
a routing rule, and is out of scope here.

It matches module-level constant names only: a floor with another name (say
``MIN_ENGINE_QUERIES``) or an inline ``len(queries) < 5`` would not trip it.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from src.agents.referee import consolidation_validator

REFEREE = Path(consolidation_validator.__file__).resolve().parent
# A module-level name that reads as a query-count floor for an engine.
_COUNT_FLOOR_NAME = re.compile(
    r"^_?[A-Z_]*(ORPHAN|TINY|ABSORPTION|MIN_QUERIES|QUERY_THRESHOLD|QUERIES_THRESHOLD)[A-Z_]*$"
)


def _module_level_names(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.parse(path.read_text(), filename=str(path)).body:
        targets = (
            [node.target]
            if isinstance(node, ast.AnnAssign)
            else node.targets if isinstance(node, ast.Assign) else []
        )
        names |= {t.id for t in targets if isinstance(t, ast.Name)}
    return names


def test_routing_has_one_too_few_queries_threshold() -> None:
    found = {
        f"{path.stem}.{name}"
        for path in sorted(REFEREE.glob("*.py"))
        for name in _module_level_names(path)
        if _COUNT_FLOOR_NAME.match(name)
    }
    assert found == {"consolidation_validator.ORPHAN_ENGINE_THRESHOLD"}


def test_the_sweep_keeps_an_engine_above_five_queries() -> None:
    assert consolidation_validator.ORPHAN_ENGINE_THRESHOLD == 5
