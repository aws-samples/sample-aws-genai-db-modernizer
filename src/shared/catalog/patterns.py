"""The pattern half of the rule catalog (ADR-031 Decision 8, #492).

One ``src/catalog/patterns/<pattern_type>.toml`` per access-pattern type the code knows
says, in one place, how every engine treats that pattern: whether its analysis reports it
as a fit (with the specialist's catalog score) or as an anti-pattern (with a severity),
whether it has the capability the pattern needs natively, whether a query showing it
costs the engine a penalty, and why. This module loads those files into strict Pydantic
models (an unknown, missing or mistyped field fails at import time) and derives the
constants the scorer and the analysis tools read.

This slice changes no behaviour (Option 3A): ``src.tools.analysis.*_pattern_catalog``
(the per-engine ``*_PATTERNS``/``*_ANTI_PATTERNS`` lists and the Aurora hint/guidance
sentences), ``src.agents.referee.query_scoring`` (``ANTI_PATTERN_PENALTIES`` and the
capability scoping tables) and ``src.shared.engine_capabilities`` (the capability engine
sets) build their existing constants, under their existing names, from here. Detection
stays in code: ``detectors`` only names the function that reports the pattern.

Pattern fields:

- ``id``: the ``pattern_type`` (or ``anti_pattern_type``) an analysis emits; equals the
  file name.
- ``detectors``: ``module:function`` of every analysis function that reports it (empty:
  catalog-only, nothing emits it today).
- ``triage_signal``: the triage signal of the same vocabulary (documentation today:
  triage still builds its own signals and targets).
- ``capability``: the engine capability that exempts an engine from ``penalty``
  (``native`` entries); the same capability must name the same engines everywhere.
- ``penalty``: points an engine loses on a query showing this pattern
  (``query_scoring.ANTI_PATTERN_PENALTIES``; 0 means none). It lands on an engine whose
  own analysis reports it as an anti-pattern, and on every other engine when some engine
  reports it as a fit.
- ``penalty_aliases``: other spellings of the same penalty key.
- ``exempt_note`` / ``redesign_note``: the decision-trace sentence for a ``native``
  exemption or a ``redesign`` penalty (``{engine}`` is the engine's display name).

Engine entry fields (``[engines.<engine_id>]``, one per engine):

- ``kind``: ``fit`` (the engine's analysis reports it as a fit), ``anti_pattern`` (as a
  concern), ``native`` (has ``capability``: never penalised), ``redesign`` (lacks it, has a
  bounded redesign: a reduced ``penalty``), ``penalized`` (cannot run it: takes the full
  penalty when another engine reports it as a fit), ``unscored`` (no effect) or ``future``
  (the engine is a future-enhancement placeholder, ``status = "future"``: not modelled yet,
  never a routing candidate, ADR-031 Decision 9, #495). A future engine's entries are all
  ``future``, and only a future engine's are. The loader enforces that consistency, not
  presence: a missing entry loads, and the completeness test
  (``tests/unit/shared/test_rule_catalog_complete.py``) is what fails.
- ``supported``: whether the engine can run the pattern at all. Always ``false`` for
  ``penalized`` and ``future`` and always ``true`` for ``fit``/``native``/``redesign``; an
  ``anti_pattern`` or ``unscored`` entry says so explicitly. For a pattern whose
  ``capability`` is backed by the hard capability gate
  (``capability_registry.ENGINE_CAPABILITIES``), it matches the gate engine by engine
  (``tests/unit/shared/test_pattern_catalog.py`` cross-checks it).
- ``why``: one line; for ``fit``/``anti_pattern`` it is the catalog description.
- ``catalog`` / ``catalog_id``: the engine catalog and entry id (``fit``/``anti_pattern``).
- ``score``: the specialist's base score, 0-100 (``fit``), or ``confidence`` where the
  analysis sets a confidence level instead (ElastiCache).
- ``class_score``: on a ``native`` or ``unscored`` entry, how well the engine serves the
  pattern although its analysis does not report it, 0-100, with the source of the number
  in ``why``. Only the per-query access-pattern class scores read it
  (``src/catalog/access_classes.toml``, #728), so it changes no current score.
- ``severity``: 0.0-1.0 (``anti_pattern``); ``guidance``: what to do about it;
  ``engine_hint``: the sentence of the description that names another engine (kept apart
  so report text can drop it); ``guidance_names_engine``: the guidance is an
  alternative-engine suggestion as a whole.
- ``concerns``: the fit's known caveats; ``profile``: OpenSearch's weight profile
  (``search`` or ``timeseries``).
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Iterable
from functools import cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.shared.catalog.engines import CatalogError, engine_ids

PATTERNS_DIR = Path(__file__).resolve().parents[2] / "catalog" / "patterns"

PatternKind = Literal[
    "fit", "anti_pattern", "native", "redesign", "penalized", "unscored", "future"
]
# The seven per-engine catalogs the analysis tools read (Aurora PostgreSQL and Aurora MySQL
# share ``aurora_common``; ElastiCache's analysis builds its patterns inline).
CatalogName = Literal[
    "dynamodb",
    "documentdb",
    "opensearch",
    "aurora_common",
    "aurora_pg",
    "aurora_mysql",
    "elasticache",
]
Capability = Literal["acid_transactions", "text_search", "fuzzy_search"]
Profile = Literal["search", "timeseries"]
ConfidenceLevel = Literal["high", "medium", "low"]

_DETECTOR = r"^[a-z_][a-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*$"
_ONE_LINE = r"^[^\n]+$"

# Which optional fields each kind may (and must) carry.
_KIND_FIELDS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    # kind: (required, allowed beyond kind/supported/why)
    "fit": (
        frozenset({"catalog", "catalog_id"}),
        frozenset({"catalog", "catalog_id", "score", "confidence", "concerns", "profile"}),
    ),
    "anti_pattern": (
        frozenset({"catalog", "catalog_id", "severity"}),
        frozenset(
            {
                "catalog",
                "catalog_id",
                "severity",
                "engine_hint",
                "guidance",
                "guidance_names_engine",
            }
        ),
    ),
    "redesign": (frozenset({"penalty"}), frozenset({"penalty"})),
    "native": (frozenset(), frozenset({"class_score"})),
    "penalized": (frozenset(), frozenset()),
    "unscored": (frozenset(), frozenset({"class_score"})),
    "future": (frozenset(), frozenset()),
}


class EngineSupport(BaseModel):
    """How one engine treats one pattern."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: PatternKind
    supported: bool
    why: str = Field(min_length=1, pattern=_ONE_LINE)
    catalog: CatalogName | None = None
    catalog_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9-]*$")
    score: int | None = Field(default=None, ge=0, le=100)
    class_score: int | None = Field(default=None, ge=0, le=100)
    confidence: ConfidenceLevel | None = None
    severity: float | None = Field(default=None, ge=0.0, le=1.0)
    penalty: int | None = Field(default=None, ge=0)
    profile: Profile | None = None
    engine_hint: str | None = Field(default=None, min_length=1, pattern=_ONE_LINE)
    concerns: tuple[str, ...] = ()
    guidance: str = ""
    guidance_names_engine: bool = False

    @model_validator(mode="after")
    def _fields_match_the_kind(self) -> EngineSupport:
        required, allowed = _KIND_FIELDS[self.kind]
        # Presence is "written in the file", not "truthy": ``score = 0``,
        # ``severity = 0.0`` and ``penalty = 0`` are values, not absences.
        present = set(_ALL_OPTIONAL) & self.model_fields_set
        if missing := sorted(required - present):
            raise ValueError(f"a {self.kind} entry needs {missing}")
        if extra := sorted(present - allowed):
            raise ValueError(f"a {self.kind} entry cannot carry {extra}")
        if self.kind == "fit" and (self.score is None) == (self.confidence is None):
            raise ValueError("a fit entry carries exactly one of score and confidence")
        if self.kind in ("penalized", "future") and self.supported:
            raise ValueError(f"a {self.kind} entry is never supported")
        if self.kind in ("fit", "native", "redesign") and not self.supported:
            raise ValueError(f"a {self.kind} entry is always supported")
        if (self.profile is not None) != (self.kind == "fit" and self.catalog == "opensearch"):
            raise ValueError("profile is required on an OpenSearch fit and allowed nowhere else")
        if self.guidance_names_engine and not self.guidance:
            raise ValueError("guidance_names_engine needs a guidance")
        return self


_ALL_OPTIONAL = (
    "catalog",
    "catalog_id",
    "score",
    "class_score",
    "confidence",
    "severity",
    "penalty",
    "profile",
    "engine_hint",
    "concerns",
    "guidance",
    "guidance_names_engine",
)


class PatternEntry(BaseModel):
    """One access-pattern type and how every engine treats it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    detectors: tuple[str, ...]
    triage_signal: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]*$")
    capability: Capability | None = None
    penalty: int = Field(ge=0)
    penalty_aliases: tuple[str, ...] = ()
    exempt_note: str | None = Field(default=None, min_length=1, pattern=_ONE_LINE)
    redesign_note: str | None = Field(default=None, min_length=1, pattern=_ONE_LINE)
    engines: dict[str, EngineSupport]

    @model_validator(mode="after")
    def _penalty_rules_are_consistent(self) -> PatternEntry:
        for detector in self.detectors:
            if not re.match(_DETECTOR, detector):
                raise ValueError(f"detector {detector!r} is not module:function")
        kinds = {engine: e.kind for engine, e in self.engines.items()}
        has_fit = "fit" in kinds.values()
        for engine, kind in kinds.items():
            if kind in ("native", "redesign") and self.capability is None:
                raise ValueError(f"{engine}: a {kind} entry needs the pattern's capability")
            if kind == "redesign":
                reduced = self.engines[engine].penalty or 0
                if not 0 < reduced < self.penalty:
                    raise ValueError(
                        f"{engine}: a redesign penalty is between 0 and {self.penalty}"
                    )
            if kind in ("penalized", "unscored"):
                # The cross-engine penalty reaches an engine that does not report the
                # pattern only when some engine reports it as a fit.
                expected = "penalized" if self.penalty and has_fit else "unscored"
                if kind != expected:
                    raise ValueError(
                        f"{engine}: {kind} should be {expected} (penalty {self.penalty}, "
                        f"{'a' if has_fit else 'no'} fit entry)"
                    )
        if self.capability and "native" not in kinds.values():
            raise ValueError("a capability names at least one native engine")
        if self.penalty_aliases and not self.penalty:
            raise ValueError("penalty_aliases need a penalty")
        if self.exempt_note and "native" not in kinds.values():
            raise ValueError("an exempt_note needs a native entry")
        if self.redesign_note and "redesign" not in kinds.values():
            raise ValueError("a redesign_note needs a redesign entry")
        return self


def load_pattern_catalog(directory: Path = PATTERNS_DIR) -> tuple[PatternEntry, ...]:
    """Every pattern file in ``directory``, validated, sorted by id."""
    entries: list[PatternEntry] = []
    for path in sorted(directory.glob("*.toml")):
        try:
            with path.open("rb") as fh:
                data = tomllib.load(fh)
            entry = PatternEntry.model_validate(data)
        except (tomllib.TOMLDecodeError, ValueError) as exc:
            raise CatalogError(f"{path.name}: {exc}") from exc
        if entry.id != path.stem:
            raise CatalogError(f"{path.name}: id {entry.id!r} does not match the file name")
        entries.append(entry)
    if not entries:
        raise CatalogError(f"no pattern files under {directory}")
    _check_across_files(entries)
    return tuple(entries)


def _check_across_files(entries: Iterable[PatternEntry]) -> None:
    entries = tuple(entries)
    # Every engine except a legacy id may (and, by the completeness test, must) have an
    # entry; stated by exclusion so a status added later is covered.
    decided = engine_ids() - engine_ids(status="legacy")
    future = engine_ids(status="future")
    keys: dict[str, str] = {}
    by_catalog_id: dict[tuple[str, str], tuple[str, EngineSupport]] = {}
    capability_engines: dict[str, tuple[str, frozenset[str]]] = {}
    for entry in entries:
        for key in (entry.id, *entry.penalty_aliases):
            if key in keys:
                raise CatalogError(f"{entry.id}.toml: {key!r} is already used by {keys[key]}.toml")
            keys[key] = entry.id
        if unknown := sorted(set(entry.engines) - decided):
            raise CatalogError(f"{entry.id}.toml: not a catalog engine, or a legacy id: {unknown}")
        for engine, support in entry.engines.items():
            if (support.kind == "future") != (engine in future):
                raise CatalogError(
                    f"{entry.id}.toml [engines.{engine}]: kind 'future' is for a future "
                    "engine only, and a future engine's entries are all 'future'"
                )
        for engine, support in entry.engines.items():
            if support.catalog_id is None or support.catalog is None:
                continue
            ref = (support.catalog, support.catalog_id)
            seen = by_catalog_id.get(ref)
            if seen is None:
                by_catalog_id[ref] = (entry.id, support)
            elif seen[0] != entry.id or seen[1] != support:
                raise CatalogError(
                    f"{entry.id}.toml [engines.{engine}]: {support.catalog_id} is defined "
                    f"differently in {seen[0]}.toml"
                )
        if entry.capability:
            native = frozenset(e for e, s in entry.engines.items() if s.kind == "native")
            seen_cap = capability_engines.setdefault(entry.capability, (entry.id, native))
            if seen_cap[1] != native:
                raise CatalogError(
                    f"{entry.id}.toml: capability {entry.capability!r} names "
                    f"{sorted(native)}, but {seen_cap[0]}.toml names {sorted(seen_cap[1])}"
                )


@cache
def patterns() -> tuple[PatternEntry, ...]:
    """The loaded pattern catalog (read once per process)."""
    return load_pattern_catalog()


def pattern(pattern_id: str) -> PatternEntry:
    """The pattern file for ``pattern_id`` (a KeyError names a missing file)."""
    for entry in patterns():
        if entry.id == pattern_id:
            return entry
    raise KeyError(f"no src/catalog/patterns/{pattern_id}.toml")


def catalog_entries(
    catalog: CatalogName, kind: Literal["fit", "anti_pattern"]
) -> tuple[tuple[str, EngineSupport], ...]:
    """``(pattern_type, entry)`` for every ``kind`` entry of one engine catalog, in
    ``catalog_id`` order (the order the analysis tools index into). An entry shared by
    two engines (``aurora_common``) appears once."""
    found: dict[str, tuple[str, EngineSupport]] = {}
    for entry in patterns():
        for support in entry.engines.values():
            if support.catalog == catalog and support.kind == kind and support.catalog_id:
                found.setdefault(support.catalog_id, (entry.id, support))
    return tuple(found[cid] for cid in sorted(found))


def anti_pattern_penalties() -> dict[str, int]:
    """``query_scoring.ANTI_PATTERN_PENALTIES``: pattern type (and alias) to penalty."""
    out: dict[str, int] = {}
    for entry in patterns():
        if entry.penalty:
            for key in (entry.id, *entry.penalty_aliases):
                out[key] = entry.penalty
    return out


def capability_engines(capability: Capability) -> frozenset[str]:
    """The engines that run ``capability`` natively (the same set on every pattern)."""
    for entry in patterns():
        if entry.capability == capability:
            return frozenset(e for e, s in entry.engines.items() if s.kind == "native")
    raise KeyError(f"no pattern file names capability {capability!r}")


def capability_signal_engines() -> dict[str, frozenset[str]]:
    """``query_scoring.CAPABILITY_SIGNAL_ENGINES``: pattern type to its native engines."""
    return {
        entry.id: capability_engines(entry.capability)
        for entry in patterns()
        if entry.capability and entry.penalty
    }


def capability_redesign_penalties() -> dict[str, dict[str, int]]:
    """``query_scoring.CAPABILITY_REDESIGN_PENALTIES``: pattern type to reduced penalties."""
    out: dict[str, dict[str, int]] = {}
    for entry in patterns():
        reduced = {
            e: s.penalty
            for e, s in entry.engines.items()
            if s.kind == "redesign" and s.penalty is not None
        }
        if reduced:
            out[entry.id] = reduced
    return out


def capability_redesign_notes() -> dict[str, str]:
    """``query_scoring.CAPABILITY_REDESIGN_NOTES``: pattern type to its redesign note."""
    return {e.id: e.redesign_note for e in patterns() if e.redesign_note}


def capability_exempt_notes() -> dict[str, str]:
    """``query_scoring.CAPABILITY_EXEMPT_NOTES``: pattern type to its exemption note."""
    return {e.id: e.exempt_note for e in patterns() if e.exempt_note}


def profile_pattern_types(profile: Profile) -> frozenset[str]:
    """OpenSearch's fit pattern types that score with the ``profile`` weights."""
    return frozenset(
        entry.id
        for entry in patterns()
        for support in entry.engines.values()
        if support.kind == "fit" and support.profile == profile
    )


def missing_engine_entries(entries: Iterable[PatternEntry], engines: Iterable[str]) -> list[str]:
    """Every ``<pattern>.toml: [engines.<id>]`` table missing for ``engines``, sorted.

    The completeness rule (ADR-031 Decision 8, #639): adding an engine or a pattern
    must write a decision for every pair down, never default one to "unsupported".
    """
    wanted = set(engines)
    return sorted(
        f"{entry.id}.toml: [engines.{engine}]"
        for entry in entries
        for engine in wanted - set(entry.engines)
    )
