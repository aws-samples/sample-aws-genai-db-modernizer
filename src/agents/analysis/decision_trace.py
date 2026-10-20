"""The one writer of an analysis agent's ``decision-trace.json`` (#973, #1187).

A trace registered as a contract for its path in ``src/shared/contract_versioning.py``
(today ``DynamoDBDecisionTrace``, for ``analysis-dynamodb/decision-trace.json``) is
written through its model, so a trace the model refuses (a renamed field, an unknown
``subtype``) is never stored, and the file says the contract's current
``contract_version`` (ADR-008 Decisions 1 and 2).

Any other trace is refused, except the trace of an engine in
:data:`UNTYPED_TRACE_ENGINES` (``src/shared/contract_versioning.py``): those engines' traces are not typed yet and are still
written as the plain dicts they are. Each engine leaves the set when its trace becomes
a contract (#1188 DocumentDB, #1189 OpenSearch, #1190 Aurora PostgreSQL and MySQL,
#1191 ElastiCache), and ``tests/unit/contracts/test_decision_trace_base.py`` pins the set
exactly, so an engine cannot go back without editing that test. When the set is empty, the untyped branch goes.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from src.shared.contract_versioning import (
    UNTYPED_TRACE_ENGINES,
    contract_for_artifact,
    load_contract,
    stamp_artifact_version,
)

_TRACE_PATH = re.compile(r"(?:^|/)analysis-(?P<engine>[^/]+)/decision-trace\.json$")


class UntypedDecisionTraceError(Exception):
    """A decision trace with no contract for its path, from an engine not in the pending set.

    Not a ``ValueError``, so an ``except ValueError`` around a write does not turn
    the refusal into a missing trace.
    """


def trace_engine(key: str) -> str | None:
    """The engine of an ``analysis-<engine>/decision-trace.json`` key, else ``None``."""
    found = _TRACE_PATH.search(key)
    return found.group("engine") if found else None


def write_decision_trace(store: Any, key: str, trace: Mapping[str, Any]) -> None:
    """Write ``trace`` to ``key`` through its contract; refuse an untyped trace.

    Raises :class:`UntypedDecisionTraceError` when ``key`` is not a decision-trace
    path, or no contract is registered for it and its engine is not in
    :data:`UNTYPED_TRACE_ENGINES`; nothing is written then.
    """
    engine = trace_engine(key)
    if engine is None:
        raise UntypedDecisionTraceError(f"not an analysis decision-trace path: {key}")
    model = contract_for_artifact(key)
    if model is not None:
        typed = load_contract(model, trace, artifact=key)
        store.write_json(key, typed.model_dump(mode="json"))
        return
    if engine not in UNTYPED_TRACE_ENGINES:
        raise UntypedDecisionTraceError(
            f"the {engine} decision trace has no registered contract: type it as one "
            f"(extending DecisionTraceBase, src/contracts/decision_trace_base.py, if it "
            f"shares that core; otherwise as its own model, listed as exempt in "
            f"tests/unit/contracts/test_decision_trace_base.py) and register its path "
            f"in src/shared/contract_versioning.py"
        )
    # Not typed yet (the pending set above): written as it is. Its path is registered
    # for no contract, so the stamp returns it unchanged.
    store.write_json(key, stamp_artifact_version(key, dict(trace)))
