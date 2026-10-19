"""The routing half of the rule catalog that is neither an engine nor a pattern (#921).

Three files, loaded into strict Pydantic models like the rest of the catalog (an unknown,
missing or mistyped field fails at import time):

- ``src/catalog/routing.toml``: the routing thresholds every engine shares
  (``[thresholds.<name>]``: ``value`` and ``why``). A threshold for one engine alone is in
  that engine's file (``engines.EngineEntry.thresholds``). It also holds the source
  features that tip the heterogeneous Aurora choice (ADR-031 Decision 6, #381, #1081):
  ``[heterogeneous_features.<id>]`` with the active relational engine the feature
  ``favours``, its ``basis`` (``workload``: read from the collected schema and queries,
  decides first; ``dialect``: a property of the source engine, consulted only when no
  workload feature decides) and ``why``. The keys are the features
  ``aurora_choice.detect_heterogeneous_features`` reports, in the order they are listed.
- ``src/catalog/gates.toml``: the hard gates (ADR-031 Decision 2 steps 2 and 3).
  ``[capabilities.<id>]`` gives the ``engines`` that have the capability, the query
  ``facts`` and ``triage_signals`` that require it, an optional ``pattern_capability``
  (the pattern files' capability of the same meaning, whose ``supported`` flags must name
  the same engines) and ``why``. ``[[exclusions]]`` are per-engine bars, in order: the
  ``engine``, a stable ``rule_id``, the query ``fact`` (with ``min`` for a count), the
  technical ``description`` and the customer-facing ``customer_message`` (#548).
  ``[[waivers]]`` lift one capability for one engine on a narrower query shape (a class
  fact) when the query's table has its source of truth on that engine (#377).
- ``src/catalog/triage_signals.toml``: each triage signal's ``targets`` (the engines it
  is evidence for, in triage's order) and, for a signal override, ``override_engine``
  and ``override_reason_code`` (a move-ledger ``ReasonCode`` value, #616).

``src.agents.referee.capability_registry``, ``engine_exclusions``, ``triage`` and
``assignment_resolver`` build their existing constants, under their existing names, from
here, so the move changes no routing.
"""

from __future__ import annotations

import tomllib
from functools import cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.shared.catalog.access_classes import QUERY_FACTS
from src.shared.catalog.engines import ACTIVE, CatalogError, Threshold, engine_ids, engines
from src.shared.catalog.patterns import Capability, patterns

CATALOG_ROOT = Path(__file__).resolve().parents[2] / "catalog"
ROUTING_FILE = CATALOG_ROOT / "routing.toml"
GATES_FILE = CATALOG_ROOT / "gates.toml"
TRIAGE_SIGNALS_FILE = CATALOG_ROOT / "triage_signals.toml"

_ONE_LINE = r"^[^\n]+$"
_ID = r"^[a-z][a-z0-9_]*$"


class HeterogeneousFeature(BaseModel):
    """One source feature of the heterogeneous Aurora choice and the engine it favours."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    favours: str = Field(pattern=_ID)
    basis: Literal["workload", "dialect"]
    why: str = Field(min_length=1, pattern=_ONE_LINE)


class RoutingFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    thresholds: dict[str, Threshold]
    heterogeneous_features: dict[str, HeterogeneousFeature] = Field(default_factory=dict)


class HardCapability(BaseModel):
    """One hard capability: who has it and what requires it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    engines: tuple[str, ...]
    facts: tuple[str, ...]
    triage_signals: tuple[str, ...] = ()
    pattern_capability: Capability | None = None
    why: str = Field(min_length=1, pattern=_ONE_LINE)


class Exclusion(BaseModel):
    """One hard exclusion: ``engine`` cannot serve a query showing ``fact``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    engine: str
    rule_id: str = Field(pattern=r"^[a-z][a-z0-9-]*$")
    fact: str = Field(pattern=_ID)
    min: int | None = Field(default=None, ge=1)
    description: str = Field(min_length=1, pattern=_ONE_LINE)
    customer_message: str = Field(min_length=1, pattern=_ONE_LINE)


class Waiver(BaseModel):
    """One capability waiver: ``engine`` serves ``capability`` for a query showing the
    class fact ``fact``, when ``when`` holds (ADR-031 Decision 2 Amendment 12, #377).

    ``when = "source_of_truth"``: the query reads one table, and that table's writes
    are on ``engine`` (its write group, ADR-031 Amendment 9).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    engine: str
    capability: str = Field(pattern=_ID)
    rule_id: str = Field(pattern=r"^[a-z][a-z0-9-]*$")
    fact: str = Field(pattern=_ID)
    when: Literal["source_of_truth"]
    why: str = Field(min_length=1, pattern=_ONE_LINE)


class GatesFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    capabilities: dict[str, HardCapability]
    exclusions: tuple[Exclusion, ...]
    waivers: tuple[Waiver, ...] = ()

    @model_validator(mode="after")
    def _one_signal_per_capability(self) -> GatesFile:
        seen: dict[str, str] = {}
        for cap, entry in self.capabilities.items():
            for signal in entry.triage_signals:
                if signal in seen:
                    raise ValueError(f"signal {signal!r} requires both {seen[signal]} and {cap}")
                seen[signal] = cap
        rule_ids = [e.rule_id for e in self.exclusions]
        if len(set(rule_ids)) != len(rule_ids):
            raise ValueError(f"exclusion rule_ids repeat: {sorted(rule_ids)}")
        waiver_ids = [w.rule_id for w in self.waivers]
        if len(set(waiver_ids)) != len(waiver_ids) or set(waiver_ids) & set(rule_ids):
            raise ValueError(f"waiver rule_ids repeat: {sorted(waiver_ids)}")
        for waiver in self.waivers:
            waived = self.capabilities.get(waiver.capability)
            if waived is None:
                raise ValueError(f"waiver {waiver.rule_id} names unknown {waiver.capability!r}")
            if waiver.engine in waived.engines:
                raise ValueError(
                    f"waiver {waiver.rule_id}: {waiver.engine} already has {waiver.capability}"
                )
        return self


class TriageSignal(BaseModel):
    """One triage signal's engines."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    targets: tuple[str, ...]
    override_engine: str | None = None
    override_reason_code: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_.]*$")

    @model_validator(mode="after")
    def _a_code_needs_an_override(self) -> TriageSignal:
        if self.override_reason_code and not self.override_engine:
            raise ValueError("override_reason_code needs an override_engine")
        if len(set(self.targets)) != len(self.targets):
            raise ValueError(f"targets repeat: {list(self.targets)}")
        return self


class TriageSignalsFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    signals: dict[str, TriageSignal]


def _load[M: BaseModel](path: Path, model: type[M]) -> M:
    try:
        with path.open("rb") as fh:
            return model.model_validate(tomllib.load(fh))
    except (OSError, tomllib.TOMLDecodeError, ValueError) as exc:
        raise CatalogError(f"{path.name}: {exc}") from exc


def load_routing_file(path: Path = ROUTING_FILE) -> RoutingFile:
    """``routing.toml``; each heterogeneous feature favours an active relational engine."""
    loaded = _load(path, RoutingFile)
    relational = engine_ids(status=ACTIVE, role="relational")
    for name, feature in loaded.heterogeneous_features.items():
        if feature.favours not in relational:
            raise CatalogError(
                f"{path.name}: heterogeneous_features.{name} favours {feature.favours!r}, "
                "not an active relational engine"
            )
    return loaded


def load_gates_file(path: Path = GATES_FILE) -> GatesFile:
    """``gates.toml``, checked against the engine and pattern files."""
    loaded = _load(path, GatesFile)
    known = engine_ids(status=ACTIVE)
    for cap, entry in loaded.capabilities.items():
        if unknown := sorted(set(entry.engines) - known):
            raise CatalogError(
                f"{path.name}: capabilities.{cap} names {unknown}, not routing targets"
            )
        if entry.pattern_capability:
            _matches_the_pattern_files(path.name, cap, entry)
    for rule in loaded.exclusions:
        if rule.engine not in known:
            raise CatalogError(f"{path.name}: exclusion {rule.rule_id} names {rule.engine!r}")
    owners = engine_ids(status=ACTIVE, system_of_record=True)
    for waiver in loaded.waivers:
        if waiver.engine not in known:
            raise CatalogError(f"{path.name}: waiver {waiver.rule_id} names {waiver.engine!r}")
        if waiver.fact not in QUERY_FACTS:
            raise CatalogError(
                f"{path.name}: waiver {waiver.rule_id} reads unknown fact {waiver.fact!r}"
            )
        if waiver.when == "source_of_truth" and waiver.engine not in owners:
            raise CatalogError(
                f"{path.name}: waiver {waiver.rule_id}: {waiver.engine} owns no writes, "
                "so no table's source of truth is on it"
            )
    return loaded


def _matches_the_pattern_files(file: str, cap: str, entry: HardCapability) -> None:
    """Every pattern file with ``entry.pattern_capability`` supports exactly the gate's
    engines (the gate and the catalog collapse into one fact, #921)."""
    gate = set(entry.engines)
    for p in patterns():
        if p.capability != entry.pattern_capability:
            continue
        for engine, support in p.engines.items():
            if support.kind == "future":
                continue
            if support.supported is not (engine in gate):
                raise CatalogError(
                    f"{file}: capabilities.{cap} says {engine} "
                    f"{'has' if engine in gate else 'lacks'} it, but {p.id}.toml "
                    f"[engines.{engine}] has supported = {str(support.supported).lower()}"
                )


def load_triage_signals_file(path: Path = TRIAGE_SIGNALS_FILE) -> TriageSignalsFile:
    loaded = _load(path, TriageSignalsFile)
    known = {e.id for e in engines()}
    for name, signal in loaded.signals.items():
        named = {*signal.targets, *([signal.override_engine] if signal.override_engine else [])}
        if unknown := sorted(named - known):
            raise CatalogError(f"{path.name}: signals.{name} names {unknown}, not catalog engines")
    return loaded


@cache
def routing_file() -> RoutingFile:
    return load_routing_file()


@cache
def gates_file() -> GatesFile:
    return load_gates_file()


@cache
def triage_signals_file() -> TriageSignalsFile:
    return load_triage_signals_file()


def threshold(name: str) -> int | float:
    """The shared routing threshold ``name`` (``routing.toml``); ``KeyError`` if absent."""
    entry = routing_file().thresholds.get(name)
    if entry is None:
        raise KeyError(f"routing.toml has no thresholds.{name}")
    return entry.value


def heterogeneous_features() -> dict[str, HeterogeneousFeature]:
    """The heterogeneous Aurora choice's source features, in file order (#1081)."""
    return dict(routing_file().heterogeneous_features)


def hard_capabilities() -> dict[str, HardCapability]:
    return dict(gates_file().capabilities)


def exclusions() -> tuple[Exclusion, ...]:
    return gates_file().exclusions


def waivers() -> tuple[Waiver, ...]:
    return gates_file().waivers


def triage_signals() -> dict[str, TriageSignal]:
    return dict(triage_signals_file().signals)


def signal_targets(signal: str) -> list[str]:
    """The engines triage names for ``signal``, in file order (``KeyError`` if absent)."""
    return list(triage_signals_file().signals[signal].targets)
