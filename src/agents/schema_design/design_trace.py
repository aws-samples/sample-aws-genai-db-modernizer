"""Write and read the schema design trace through its contract (#1258, ADR-008 Decision 2).

``design_trace.json`` and ``design_trace_group_<G>.json`` are versioned contracts
(``src/contracts/schema_design_trace.py``). Every writer goes through
:func:`write_design_trace`, and every reader through :func:`read_design_trace` or
:func:`parse_design_trace`, so the readers behave alike on a bad file:

- a version this code cannot read raises ``ContractVersionError``;
- a shape the contract refuses raises :class:`DesignTraceShapeError`, which names the
  artifact and the fields, never reading it as "no flags" or "no trace".

The one lenient path is a trace written from a model's answer after the paid call (the
external PE review): ``write_design_trace(..., lenient=True)`` repairs the trace, dropping
what the contract refuses and noting it in ``dropped_fields``, so the paid answer is kept
and the file written always validates. A bad ``routing_flags`` is never repaired: it is
refused with the error, so the command's retry rule applies.
"""

from __future__ import annotations

import copy
import logging
from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from src.contracts.schema_design_trace import DesignTrace, DesignTraceGroup
from src.shared.contract_versioning import contract_for_artifact, load_contract
from src.storage.artifact_store import ArtifactStore

logger = logging.getLogger(__name__)


class DesignTraceShapeError(Exception):
    """A design trace whose sections do not match the contract.

    Not a ``ValueError``, so an ``except ValueError`` around an artifact read does not
    turn it into "not available". ``errors`` is one ``field: message`` line per problem;
    the trace's own values are never echoed.
    """

    def __init__(self, artifact: str, exc: ValidationError) -> None:
        self.artifact = artifact
        self.errors = [
            f"{'.'.join(str(p) for p in e['loc']) or '<trace>'}: {e['msg']}" for e in exc.errors()
        ]
        super().__init__(
            f"{artifact}: the schema design trace does not match its contract "
            f"({len(self.errors)} error(s)): " + "; ".join(self.errors[:5])
        )


def parse_design_trace(data: Mapping[str, Any], *, artifact: str) -> DesignTrace:
    """``data`` as the design-trace contract: ``ContractVersionError`` for a version this
    code cannot read, :class:`DesignTraceShapeError` for a shape it refuses."""
    try:
        return load_contract(DesignTrace, data, artifact=artifact)
    except ValidationError as exc:
        raise DesignTraceShapeError(artifact, exc) from None


_MAX_REPAIRS = 50


def repair_design_trace(
    trace: Mapping[str, Any], *, artifact: str
) -> tuple[dict[str, Any], list[str]]:
    """``trace`` with what the contract refuses dropped, and one note per drop.

    For a trace built from a model's answer after the paid call. A value that does not
    fit is dropped (an optional field of a trace entry, or the entry itself when a
    required field is missing or wrong, or an optional section), and each drop is noted
    as ``<path>: <reason>`` without the value. ``routing_flags``, anywhere in the trace,
    is never repaired: the router acts on it, so a bad one raises
    :class:`DesignTraceShapeError` instead. The result always validates.
    """
    repaired: dict[str, Any] = copy.deepcopy(dict(trace))
    dropped: list[str] = []
    for _ in range(_MAX_REPAIRS):
        try:
            load_contract(DesignTrace, repaired, artifact=artifact)
            return repaired, dropped
        except ValidationError as exc:
            errors = exc.errors()
            if any("routing_flags" in e["loc"] for e in errors):
                raise DesignTraceShapeError(artifact, exc) from None
            error = errors[0]
            loc = list(error["loc"])
            if error["type"] == "missing" or not loc:
                # A required field is absent: drop what it belongs to.
                loc = loc[:-1] or loc
            if not loc or not _drop(repaired, loc):
                raise DesignTraceShapeError(artifact, exc) from None
            dropped.append(f"{'.'.join(str(p) for p in loc)}: {error['msg']}")
    load_contract(DesignTrace, repaired, artifact=artifact)  # pragma: no cover
    return repaired, dropped


def _drop(data: Any, loc: list[Any]) -> bool:
    """Remove the value at ``loc`` from ``data``; ``False`` when it is not there."""
    node = data
    for part in loc[:-1]:
        try:
            node = node[part]
        except (KeyError, IndexError, TypeError):
            return False
    last = loc[-1]
    if isinstance(node, dict) and last in node:
        del node[last]
        return True
    if isinstance(node, list) and isinstance(last, int) and last < len(node):
        del node[last]
        return True
    return False


def write_design_trace(
    store: ArtifactStore, key: str, trace: Mapping[str, Any], *, lenient: bool = False
) -> None:
    """Write ``trace`` to ``key`` as the contract registered for that path.

    The trace is parsed first (a section the contract refuses raises
    :class:`DesignTraceShapeError` and nothing is written) and written at the contract's
    current version, whatever version the dict said. ``lenient``: for a trace built from
    a model's answer after the paid call, what the contract refuses is dropped and
    listed in ``dropped_fields`` (see :func:`repair_design_trace`) so the file written
    always validates; a bad ``routing_flags`` still raises. Raises ``TypeError`` for a
    path that is not a design trace.
    """
    model = contract_for_artifact(key)
    if model not in (DesignTrace, DesignTraceGroup):
        raise TypeError(f"not a schema design trace path: {key}")
    data: Mapping[str, Any] = trace
    if lenient:
        data, dropped = repair_design_trace(trace, artifact=key)
        if dropped:
            logger.warning("%s: dropped %d field(s) of the trace: %s", key, len(dropped), dropped)
            data = {**data, "dropped_fields": [*(data.get("dropped_fields") or []), *dropped]}
    try:
        typed = load_contract(model, data, artifact=key)
    except ValidationError as exc:
        raise DesignTraceShapeError(key, exc) from None
    store.write_json(key, typed.model_dump(mode="json"))


def read_design_trace(store: ArtifactStore, key: str) -> DesignTrace:
    """The design trace at ``key``: a saved trace with no ``contract_version`` loads as
    the current version."""
    return parse_design_trace(store.read_json(key), artifact=key)
