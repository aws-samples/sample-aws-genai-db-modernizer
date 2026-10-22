"""Read a ``decisions/<name>.json`` record the one way (#1257).

``scripts/check_decision.py``, ``is_assignment_reviewed`` and, through it, the local
API's approval check all ask the same question of a record: does a valid one exist?
They share this reader so a record that is not valid JSON, is at another MAJOR or has
no ``status`` is refused the same way by all three, and never counts as a decision.

The reason a record is refused is short and carries nothing from the file: a
decision file is written by hand, and its content is not echoed into logs or output.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import ValidationError

from src.contracts.decision_record import DecisionRecord
from src.shared.contract_versioning import ContractVersionError, load_contract

logger = logging.getLogger(__name__)


class DecisionRecordError(Exception):
    """A decision record that cannot be used; ``str(exc)`` is a short reason."""


def read_decision(store: Any, key: str) -> tuple[dict, DecisionRecord]:
    """The record at ``key`` as stored, and as a :class:`DecisionRecord`.

    Raises :class:`DecisionRecordError` for a file that is not a JSON object, is at
    another MAJOR, or fails the model (no ``status``). A missing file is the
    caller's to test first (``store.exists``).
    """
    try:
        data = store.read_json(key)
    except ContractVersionError as exc:
        raise DecisionRecordError(str(exc)) from exc
    except ValueError as exc:  # invalid JSON or bytes that are not UTF-8
        raise DecisionRecordError(f"{key}: not valid JSON ({type(exc).__name__})") from exc
    if not isinstance(data, dict):
        raise DecisionRecordError(f"{key}: not a JSON object")
    try:
        return data, load_contract(DecisionRecord, data, artifact=key)
    except ContractVersionError as exc:
        raise DecisionRecordError(str(exc)) from exc
    except ValidationError as exc:
        fields = ", ".join(
            ".".join(str(part) for part in error["loc"]) or "record"
            for error in exc.errors(include_input=False, include_url=False)
        )
        raise DecisionRecordError(f"{key}: not a valid decision record (check: {fields})") from exc


def has_valid_decision(store: Any, key: str) -> bool:
    """``True`` when a valid record exists at ``key``; a refused one is logged, not counted."""
    if not store.exists(key):
        return False
    try:
        read_decision(store, key)
    except DecisionRecordError as exc:
        logger.warning("decision record ignored: %s", exc)
        return False
    return True
