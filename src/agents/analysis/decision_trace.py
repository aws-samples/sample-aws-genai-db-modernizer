"""The one writer of an analysis agent's ``decision-trace.json`` (#973, #1187, #1190).

Every analysis engine's trace is a contract registered for its path in
``src/shared/contract_versioning.py`` (``DynamoDBDecisionTrace``,
``DocumentDBDecisionTrace``, ``ElastiCacheDecisionTrace``, ``OpenSearchDecisionTrace``
and ``AuroraDecisionTrace`` for both Aurora engines). A trace is written through its
model, so a trace the model refuses (a renamed field, an unknown ``subtype``) is never
stored, and the file says the contract's current ``contract_version`` (ADR-008
Decisions 1 and 2). A trace with no registered contract is refused, and
``tests/unit/contracts/test_decision_trace_base.py`` fails on an analysis engine with
no registration.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from src.shared.contract_versioning import contract_for_artifact, load_contract

_TRACE_PATH = re.compile(r"(?:^|/)analysis-(?P<engine>[^/]+)/decision-trace\.json$")


class UntypedDecisionTraceError(Exception):
    """A decision trace with no contract for its path.

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
    path, or no contract is registered for it; nothing is written then.
    """
    engine = trace_engine(key)
    if engine is None:
        raise UntypedDecisionTraceError(f"not an analysis decision-trace path: {key}")
    model = contract_for_artifact(key)
    if model is None:
        raise UntypedDecisionTraceError(
            f"the {engine} decision trace has no registered contract: type it as one "
            f"(extending DecisionTraceBase, src/contracts/decision_trace_base.py, if it "
            f"shares that core; otherwise as its own model, listed as exempt in "
            f"tests/unit/contracts/test_decision_trace_base.py) and register its path "
            f"in src/shared/contract_versioning.py"
        )
    typed = load_contract(model, trace, artifact=key)
    store.write_json(key, typed.model_dump(mode="json"))
