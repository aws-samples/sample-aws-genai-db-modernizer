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
- ``penalty_basis``: the consequence the engines' public documentation states for a query
  showing the pattern, which sizes ``penalty`` (``PENALTY_POINTS``, one rule for every
  engine, ADR-031 Decision 8 Amendment 6, #920). Required on a file that carries a penalty
  or has an ``anti_pattern``, ``penalized``, ``scan`` or ``redesign`` entry.
- ``penalty_why``: why the documented consequence is that basis, with its public source
  (optional, one line; rendered next to the penalty in the rule catalog, #916).
- ``penalty_aliases``: other spellings of the same penalty key.
- ``exempt_note`` / ``redesign_note``: the decision-trace sentence for a ``native``
  exemption or a ``redesign`` penalty (``{engine}`` is the engine's display name).
- ``scan_threshold``: for a pattern with ``scan`` entries, when the scan matters
  (``min_calls_per_second`` and ``min_table_rows``, both reached) and the sourced ``why``
  for those values. Only then does a ``scan`` engine lose ``penalty`` (#493).
- ``removed_by_redesign``: ``rows`` or ``write_shape``, what a redesign of the target
  schema removes so the pattern costs a query nothing (ADR-031 Decision 2 Amendment 13,
  #1231); absent, the pattern is intrinsic on every query.
- ``write_pattern``: the pattern describes writes only (an UPDATE, DELETE, INSERT or
  upsert shape). No parity anchor may borrow from one, and no access class lists one
  (#936: OpenSearch is not a system of record and is never credited for a write).

- ``segment_weighting``: how a table's score weighs this pattern's segment when the
  segment spans tables (ADR-017 Decision 5). ``segment`` (the default) weighs by the
  calls of every query in the segment; ``per_table`` by the calls of the segment's queries
  that access the table being scored, so one table's traffic does not move another's
  score (#720). Read by ``compute_catalog_pattern_score``; no pattern id is named in code.

Engine entry fields (``[engines.<engine_id>]``, one per engine):

- ``kind``: ``fit`` (the engine's analysis reports it as a fit), ``anti_pattern`` (as a
  concern), ``native`` (has ``capability``: never penalised), ``redesign`` (lacks it, has a
  bounded redesign: a reduced ``penalty``), ``scan`` (lacks it but runs the pattern as a
  scan of every row: it loses the pattern's ``penalty`` only on a query that crosses the
  pattern's ``scan_threshold``, #493), ``penalized`` (cannot run it: takes the full
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
  (``src/catalog/access_classes.toml``, #728), so it changes no current score. The source
  is either the engine's own public documentation for the access path, or a parity
  anchor (next field).
- ``parity_anchor``: on an entry whose ``class_score`` is borrowed because the catalog has
  no engine-specific evidence yet (#936), the pattern whose rating it borrows. Its ``why``
  says :data:`PARITY_ANCHOR_NOTE` and cites ``(<pattern>, <score>)``; the loader checks
  that the source is not a ``write_pattern`` and rates another engine at that score from
  its own evidence (a fit score, or a class score that is not itself an anchor).
- ``severity``: 0.0-1.0 (``anti_pattern``); ``guidance``: what to do about it (on a
  ``native`` or ``scan`` entry: the redesign a word search takes on that engine, #493);
  ``engine_hint``: the sentence of the description that names another engine (kept apart
  so report text can drop it); ``guidance_names_engine``: the guidance is an
  alternative-engine suggestion as a whole.
- ``concerns``: the fit's known caveats; ``profile``: OpenSearch's weight profile
  (``search`` or ``timeseries``).
- ``table_bonus`` / ``complexity_bonus`` (``fit`` only, #921): the points the engine's
  table-level score adds to the pattern-match and complexity dimensions when a table
  shows the pattern (the engine's analysis adjustments, before the 0-100 clamp).
  ``bonus_group``: the engine's entries that share a group give their bonuses once for
  a table showing any of them, and must agree on both values (a DynamoDB denormalizable
  join; OpenSearch's wildcard or fuzzy match). ``complexity_feature``: the pattern counts
  toward the engine file's ``complexity_by_feature_count`` (the Aurora SQL features).
- ``prefix_regex_index`` (``scan`` only, #982): the engine scans a regular expression
  but an index serves a prefix one (the ``prefix_regex`` query fact), so a query whose
  only scan is a prefix regular expression costs it nothing.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Iterable
from functools import cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.shared.catalog.engines import CatalogError, engine_entry, engine_ids, engines

PATTERNS_DIR = Path(__file__).resolve().parents[2] / "catalog" / "patterns"

# What a borrowed class score says in the catalog and the decision trace (#936).
PARITY_ANCHOR_NOTE = "parity anchor: no engine-specific evidence yet"

PatternKind = Literal[
    "fit", "anti_pattern", "native", "redesign", "scan", "penalized", "unscored", "future"
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
Capability = Literal["acid_transactions", "text_search", "fuzzy_search", "substring_index"]
SegmentWeighting = Literal["segment", "per_table"]
PenaltyBasis = Literal[
    "cannot_run",
    "needs_redesign",
    "reads_more_than_returns",
    "workload_fit",
    "bounded_limit",
    "guidance_only",
]

# The one rule that sizes every penalty (ADR-031 Decision 8 Amendment 6, #920): the
# points a query showing a pattern costs an engine follow from the consequence the
# engine's public documentation states for it, never from which engine it is.
PENALTY_POINTS: dict[str, int] = {
    # The engine cannot run the pattern at all.
    "cannot_run": 40,
    # No server-side operation serves the access path: precompute, redesign the data
    # model, or use another engine.
    "needs_redesign": 30,
    # The engine runs it, but reads more than it returns (a scan, a per-collection join
    # stage), so the work grows with the data, not with the result.
    "reads_more_than_returns": 20,
    # The engine runs it natively, but the query uses none of what distinguishes the
    # engine, and another candidate's documentation names this access path as its own.
    "workload_fit": 20,
    # The engine runs it within a documented bound the design must respect (a page
    # size, an item count, a document size, a version or cluster type).
    "bounded_limit": 10,
    # The documentation sets no limit the query hits, or another pattern already prices
    # the same fact: the entry is guidance only.
    "guidance_only": 0,
}
# What a redesign of the target schema removes from a query showing a pattern, so the
# pattern costs it nothing (ADR-031 Decision 2 Amendment 13, #1231):
# - ``rows``: the penalty is about the rows the query reaches (a scan, a join stage, an
#   aggregate over rows no key anchors). A redesign that anchors those rows with a key it
#   adds (``redesign_findings``), or a whole read of a small reference table, removes it.
# - ``write_shape``: the penalty is a write's shape (ADR-031 Decision 3). A redesign removes
#   it only when, with the keys it adds, the shape's own rule finds the write key scoped;
#   a write a range, join or subquery picks stays unbounded.
RemovedByRedesign = Literal["rows", "write_shape"]
# The penalty bases a ``removed_by_redesign`` pattern may have: the consequence is about
# the rows or items reached, not a capability the engine lacks (``cannot_run``) or which
# engine the access suits (``workload_fit``).
_REDESIGN_BASES = frozenset({"needs_redesign", "reads_more_than_returns", "bounded_limit"})
Profile = Literal["search", "timeseries"]
ConfidenceLevel = Literal["high", "medium", "low"]

_DETECTOR = r"^[a-z_][a-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*$"
_ONE_LINE = r"^[^\n]+$"

# Which optional fields each kind may (and must) carry.
_KIND_FIELDS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    # kind: (required, allowed beyond kind/supported/why)
    "fit": (
        frozenset({"catalog", "catalog_id"}),
        frozenset(
            {
                "catalog",
                "catalog_id",
                "score",
                "confidence",
                "concerns",
                "profile",
                "table_bonus",
                "complexity_bonus",
                "bonus_group",
                "complexity_feature",
            }
        ),
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
    "scan": (frozenset(), frozenset({"guidance", "prefix_regex_index"})),
    "native": (frozenset(), frozenset({"class_score", "guidance", "parity_anchor"})),
    "penalized": (frozenset(), frozenset()),
    "unscored": (frozenset(), frozenset({"class_score", "parity_anchor"})),
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
    parity_anchor: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_-]*$")
    confidence: ConfidenceLevel | None = None
    severity: float | None = Field(default=None, ge=0.0, le=1.0)
    penalty: int | None = Field(default=None, ge=0)
    profile: Profile | None = None
    engine_hint: str | None = Field(default=None, min_length=1, pattern=_ONE_LINE)
    concerns: tuple[str, ...] = ()
    guidance: str = ""
    guidance_names_engine: bool = False
    table_bonus: int = 0
    complexity_bonus: int = 0
    bonus_group: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9-]*$")
    complexity_feature: bool = False
    prefix_regex_index: bool = False

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
        if self.kind in ("fit", "native", "redesign", "scan") and not self.supported:
            raise ValueError(f"a {self.kind} entry is always supported")
        if (self.profile is not None) != (self.kind == "fit" and self.catalog == "opensearch"):
            raise ValueError("profile is required on an OpenSearch fit and allowed nowhere else")
        if self.guidance_names_engine and not self.guidance:
            raise ValueError("guidance_names_engine needs a guidance")
        if self.bonus_group and not (self.table_bonus or self.complexity_bonus):
            raise ValueError("a bonus_group entry carries a table_bonus or complexity_bonus")
        if self.parity_anchor is not None:
            if self.class_score is None:
                raise ValueError("a parity_anchor entry needs the class_score it borrows")
            if PARITY_ANCHOR_NOTE not in self.why:
                raise ValueError(f"a parity_anchor entry's why says {PARITY_ANCHOR_NOTE!r}")
        return self


_ALL_OPTIONAL = (
    "catalog",
    "catalog_id",
    "score",
    "class_score",
    "parity_anchor",
    "confidence",
    "severity",
    "penalty",
    "profile",
    "engine_hint",
    "concerns",
    "guidance",
    "guidance_names_engine",
    "table_bonus",
    "complexity_bonus",
    "bonus_group",
    "complexity_feature",
    "prefix_regex_index",
)


class ScanThreshold(BaseModel):
    """When a ``scan`` entry's scan matters: a frequent query on a large table (#493)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    min_calls_per_second: float = Field(gt=0)
    min_table_rows: int = Field(gt=0)
    why: str = Field(min_length=1, pattern=_ONE_LINE)


class PatternEntry(BaseModel):
    """One access-pattern type and how every engine treats it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    detectors: tuple[str, ...]
    triage_signal: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]*$")
    capability: Capability | None = None
    penalty: int = Field(ge=0)
    penalty_basis: PenaltyBasis | None = None
    penalty_why: str | None = Field(default=None, min_length=1, pattern=_ONE_LINE)
    penalty_aliases: tuple[str, ...] = ()
    exempt_note: str | None = Field(default=None, min_length=1, pattern=_ONE_LINE)
    redesign_note: str | None = Field(default=None, min_length=1, pattern=_ONE_LINE)
    scan_threshold: ScanThreshold | None = None
    # A detector's share of a table's calls below which its reads are negligible: a
    # table read less than this is still "unread" (``unbounded-time-series-ingest``,
    # maintainer decision on #904).
    negligible_read_share: float | None = Field(default=None, gt=0, lt=1)
    write_pattern: bool = False
    segment_weighting: SegmentWeighting = "segment"
    removed_by_redesign: RemovedByRedesign | None = None
    engines: dict[str, EngineSupport]

    @model_validator(mode="after")
    def _penalty_rules_are_consistent(self) -> PatternEntry:
        for detector in self.detectors:
            if not re.match(_DETECTOR, detector):
                raise ValueError(f"detector {detector!r} is not module:function")
        kinds = {engine: e.kind for engine, e in self.engines.items()}
        has_fit = "fit" in kinds.values()
        priced = bool(self.penalty) or bool(
            set(kinds.values()) & {"anti_pattern", "penalized", "scan", "redesign"}
        )
        if self.penalty_why and self.penalty_basis is None:
            raise ValueError("a penalty_why explains a penalty_basis")
        if self.removed_by_redesign and self.penalty_basis not in _REDESIGN_BASES:
            raise ValueError(
                "removed_by_redesign needs a penalty_basis about rows or items reached "
                f"({sorted(_REDESIGN_BASES)}), not {self.penalty_basis!r}"
            )
        if priced and self.penalty_basis is None:
            raise ValueError("a pattern that carries a penalty names its penalty_basis")
        if self.penalty_basis is not None and self.penalty != penalty_points(self.penalty_basis):
            raise ValueError(
                f"penalty {self.penalty} is not the {self.penalty_basis!r} penalty_basis's "
                f"{penalty_points(self.penalty_basis)} points"
            )
        for engine, kind in kinds.items():
            if kind in ("native", "redesign", "scan") and self.capability is None:
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
        has_scan = "scan" in kinds.values()
        if has_scan != (self.scan_threshold is not None):
            raise ValueError("a scan entry needs the pattern's scan_threshold, and only it does")
        if has_scan and not self.penalty:
            raise ValueError("a scan entry needs the pattern's penalty")
        return self


def penalty_points(basis: PenaltyBasis) -> int:
    """The points ``basis`` costs, the same for every engine (``PENALTY_POINTS``)."""
    return PENALTY_POINTS[basis]


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


def _check_parity_anchors(entries: tuple[PatternEntry, ...]) -> None:
    """Each parity anchor borrows a read pattern's own evidence (#936)."""
    by_id = {entry.id: entry for entry in entries}
    for entry in entries:
        for engine, support in entry.engines.items():
            if support.parity_anchor is None:
                continue
            where = f"{entry.id}.toml [engines.{engine}]"
            source = by_id.get(support.parity_anchor)
            if source is None:
                raise CatalogError(f"{where}: parity_anchor {support.parity_anchor!r} has no file")
            if source.write_pattern:
                raise CatalogError(
                    f"{where}: parity_anchor {source.id!r} is a write pattern; an anchor "
                    "borrows a read pattern's rating"
                )
            rated = {
                other.score if other.kind == "fit" else other.class_score
                for name, other in source.engines.items()
                if name != engine and other.parity_anchor is None
            }
            if support.class_score not in rated:
                raise CatalogError(
                    f"{where}: {source.id}.toml rates no other engine {support.class_score} "
                    "from its own evidence"
                )
            if f"({source.id}, {support.class_score})" not in support.why:
                raise CatalogError(f"{where}: why cites ({source.id}, {support.class_score})")


def _check_bonus_groups(entries: tuple[PatternEntry, ...]) -> None:
    """An engine's entries in one ``bonus_group`` agree on its bonuses (#921)."""
    seen: dict[tuple[str, str], tuple[str, int, int]] = {}
    for entry in entries:
        for engine, support in entry.engines.items():
            if not support.bonus_group:
                continue
            values = (support.table_bonus, support.complexity_bonus)
            first = seen.setdefault((engine, support.bonus_group), (entry.id, *values))
            if first[1:] != values:
                raise CatalogError(
                    f"{entry.id}.toml [engines.{engine}]: bonus_group {support.bonus_group!r} "
                    f"gives {values}, but {first[0]}.toml gives {first[1:]}"
                )


def _check_across_files(entries: Iterable[PatternEntry]) -> None:
    entries = tuple(entries)
    _check_parity_anchors(entries)
    _check_bonus_groups(entries)
    tiered = {engine.id for engine in engines() if engine.complexity_by_feature_count}
    for entry in entries:
        for engine_id, support in entry.engines.items():
            if support.complexity_feature and engine_id not in tiered:
                raise CatalogError(
                    f"{entry.id}.toml [engines.{engine_id}]: a complexity_feature needs the "
                    "engine file's complexity_by_feature_count"
                )
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


def segment_weighting(pattern_type: str) -> SegmentWeighting:
    """The declared ``segment_weighting`` of one pattern type (``segment`` when unknown)."""
    for entry in patterns():
        if entry.id == pattern_type:
            return entry.segment_weighting
    return "segment"


def anti_pattern_penalties() -> dict[str, int]:
    """``query_scoring.ANTI_PATTERN_PENALTIES``: pattern type (and alias) to penalty.

    A pattern with a ``scan_threshold`` is not one: no analysis reports it, and its
    penalty lands only through its threshold (``src.agents.referee.substring_match``)."""
    out: dict[str, int] = {}
    for entry in patterns():
        if entry.penalty and entry.scan_threshold is None:
            for key in (entry.id, *entry.penalty_aliases):
                out[key] = entry.penalty
    return out


def removed_by_redesign() -> dict[str, str]:
    """Pattern type (and alias) to what a redesign removes (``RemovedByRedesign``), for
    every pattern that names it; a pattern absent here is intrinsic on every query."""
    out: dict[str, str] = {}
    for entry in patterns():
        if entry.removed_by_redesign is not None:
            for key in (entry.id, *entry.penalty_aliases):
                out[key] = entry.removed_by_redesign
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
        if entry.capability and entry.penalty and entry.scan_threshold is None
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


def scan_engines(pattern_id: str) -> frozenset[str]:
    """The engines that run ``pattern_id`` only as a scan (``scan`` entries)."""
    return frozenset(e for e, s in pattern(pattern_id).engines.items() if s.kind == "scan")


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


def table_bonuses(
    engine: str, pattern_types: Iterable[str], profile: Profile | None = None
) -> tuple[int, int]:
    """``(pattern_match, complexity)`` points ``engine``'s table score adds for a table
    showing ``pattern_types`` (each fit entry's ``table_bonus`` / ``complexity_bonus``, a
    ``bonus_group`` counted once), limited to the fits of one OpenSearch ``profile`` when
    given (#921)."""
    shown = set(pattern_types)
    pattern_points = complexity_points = 0
    groups: set[str] = set()
    for entry in patterns():
        support = entry.engines.get(engine)
        if support is None or support.kind != "fit" or entry.id not in shown:
            continue
        if profile is not None and support.profile != profile:
            continue
        if support.bonus_group:
            if support.bonus_group in groups:
                continue
            groups.add(support.bonus_group)
        pattern_points += support.table_bonus
        complexity_points += support.complexity_bonus
    return pattern_points, complexity_points


def complexity_features(engine: str) -> frozenset[str]:
    """The pattern types whose fit entry on ``engine`` is a ``complexity_feature``."""
    return frozenset(
        entry.id
        for entry in patterns()
        if (support := entry.engines.get(engine)) is not None
        and support.kind == "fit"
        and support.complexity_feature
    )


def feature_complexity_bonus(engine: str, pattern_types: Iterable[str]) -> int:
    """The complexity points for the ``complexity_feature`` patterns a table shows on
    ``engine``: the engine file's ``complexity_by_feature_count`` for that count (its last
    value for any higher count), 0 for none (#921)."""
    entry = engine_entry(engine)
    tiers = entry.complexity_by_feature_count if entry else ()
    count = len(set(pattern_types) & complexity_features(engine))
    return tiers[min(count, len(tiers)) - 1] if count and tiers else 0


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
