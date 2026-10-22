"""The write-intent catalog (#1362): how each target engine serves what a set-based write is for.

A write group moves as one unit (ADR-031 Decision 1 Amendment 9), so it can carry a member
that fits the group's engine poorly: a retention delete, an orphan clean-up, a
consistency repair sweep. Routing is right to keep the group together; the designer and
the customer are told what the statement is for (its intent) and the target-native way
to serve that intent, instead of replaying the source's set-based statement. The source
shape is where the investigation starts, not the design (maintainer decision on #1359).

One ``src/catalog/write_intents/<id>.toml`` per intent (the rule catalog's one-file-per-
entry layout, ADR-031 Decision 8), and ``src/catalog/carried_writes.toml`` for the fit
floor below which a member counts as carried. This module loads both into strict Pydantic
models (an unknown, missing or mistyped field fails at load time). The classifier
(:mod:`src.agents.referee.carried_writes`) decides a statement's intent from its parsed
facts; this catalog only words it. Routing reads none of it.

Intent fields:

- ``id``: equals the file name.
- ``name``: the intent's short name (one line).
- ``summary``: what such a statement is for, in one plain sentence.
- ``[engines.<engine>]``: per active engine, ``approach`` (the target-native way to serve
  the intent, one line) and ``docs`` (the public documentation it rests on, by name).
  Every active engine that can own a write (a system of record) has one, since a write
  group's engine is always one of them; a cache or search engine may have one.
"""

from __future__ import annotations

import tomllib
from functools import cache
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.shared.catalog.engines import ACTIVE, CatalogError, Threshold, engine_ids

__all__ = [
    "CARRIED_WRITES_FILE",
    "WRITE_INTENTS_DIR",
    "CarriedWriteSettings",
    "IntentApproach",
    "WriteIntent",
    "carried_write_floor",
    "carried_write_settings",
    "load_carried_write_settings",
    "load_write_intents",
    "write_intent",
    "write_intents",
]

_CATALOG = Path(__file__).resolve().parents[2] / "catalog"
WRITE_INTENTS_DIR = _CATALOG / "write_intents"
CARRIED_WRITES_FILE = _CATALOG / "carried_writes.toml"

_ONE_LINE = r"^[^\n]+$"
_ID = r"^[a-z][a-z0-9-]*$"


class IntentApproach(BaseModel):
    """How one engine serves an intent, and the public documentation it rests on."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    approach: str = Field(min_length=1, pattern=_ONE_LINE)
    docs: str = Field(min_length=1, pattern=_ONE_LINE)


class WriteIntent(BaseModel):
    """One write intent and each engine's target-native approach to it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=_ID)
    name: str = Field(min_length=1, pattern=_ONE_LINE)
    summary: str = Field(min_length=1, pattern=_ONE_LINE)
    engines: dict[str, IntentApproach]

    @model_validator(mode="after")
    def _every_owner_engine(self) -> WriteIntent:
        active = engine_ids(status=ACTIVE)
        unknown = sorted(set(self.engines) - active)
        if unknown:
            raise ValueError(f"{self.id}: not an active engine: {unknown}")
        missing = sorted(engine_ids(status=ACTIVE, system_of_record=True) - set(self.engines))
        if missing:
            raise ValueError(f"{self.id}: no approach for the write-owning engines {missing}")
        return self

    def approach(self, engine: str) -> IntentApproach | None:
        """``engine``'s approach to this intent, or None when the catalog gives none."""
        return self.engines.get(engine)


class CarriedWriteSettings(BaseModel):
    """``src/catalog/carried_writes.toml``: the fit floor, with the reason for its value."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    thresholds: dict[str, Threshold]

    @model_validator(mode="after")
    def _the_floor(self) -> CarriedWriteSettings:
        floor = self.thresholds.get("carried_write_fit_floor")
        if floor is None:
            raise ValueError("carried_writes.toml needs [thresholds.carried_write_fit_floor]")
        if not 0 < floor.value <= 100:
            raise ValueError("carried_write_fit_floor is a score between 0 and 100")
        return self


def load_write_intents(directory: Path = WRITE_INTENTS_DIR) -> tuple[WriteIntent, ...]:
    """Every intent file in ``directory``, validated, sorted by id."""
    intents: list[WriteIntent] = []
    for path in sorted(directory.glob("*.toml")):
        try:
            with path.open("rb") as fh:
                intent = WriteIntent.model_validate(tomllib.load(fh))
        except (tomllib.TOMLDecodeError, ValueError) as exc:
            raise CatalogError(f"{path.name}: {exc}") from exc
        if intent.id != path.stem:
            raise CatalogError(f"{path.name}: id {intent.id!r} does not match the file name")
        intents.append(intent)
    if not intents:
        raise CatalogError(f"no write intent files under {directory}")
    return tuple(intents)


def load_carried_write_settings(path: Path = CARRIED_WRITES_FILE) -> CarriedWriteSettings:
    """``carried_writes.toml``, validated."""
    try:
        with path.open("rb") as fh:
            return CarriedWriteSettings.model_validate(tomllib.load(fh))
    except (tomllib.TOMLDecodeError, ValueError) as exc:
        raise CatalogError(f"{path.name}: {exc}") from exc


@cache
def write_intents() -> tuple[WriteIntent, ...]:
    """The loaded intents (read once per process), sorted by id."""
    return load_write_intents()


def write_intent(intent_id: str) -> WriteIntent:
    """The intent ``intent_id``; a ``KeyError`` for an id the catalog has no file for."""
    for intent in write_intents():
        if intent.id == intent_id:
            return intent
    raise KeyError(intent_id)


@cache
def carried_write_settings() -> CarriedWriteSettings:
    """The loaded ``carried_writes.toml`` (read once per process)."""
    return load_carried_write_settings()


def carried_write_floor() -> float:
    """The score below which a write-group member is carried by its group (#1362)."""
    return carried_write_settings().thresholds["carried_write_fit_floor"].value
