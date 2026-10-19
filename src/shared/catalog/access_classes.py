"""The access-pattern class table (ADR-031 Decision 14, Amendment 5, #728).

``src/catalog/access_classes.toml`` lists the eight classes a query can fall into
(:mod:`src.agents.referee.access_pattern` assigns them) and, for each, the pattern files
that describe that class. A class's pattern score on an engine is the highest rating
those patterns give the engine -- a ``fit`` entry's ``score``, or a ``native``/``unscored``
entry's ``class_score`` -- or 0 when none of them rates it:
the numbers stay in the pattern files (Decision 8), so a class score can never disagree
with the catalog. Loaded into strict Pydantic models like the rest of the catalog: an
unknown field, a missing one or a pattern id with no file fails at import time.

The per-query scorer reads these through
:class:`src.agents.referee.query_pattern.QueryPatternModel` (ADR-031 Decision 14,
Amendment 5): a query's pattern dimension on an engine is its class's score there.
"""

from __future__ import annotations

import tomllib
from functools import cache
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.shared.catalog.engines import ACTIVE, CatalogError, engine_ids
from src.shared.catalog.patterns import PARITY_ANCHOR_NOTE, EngineSupport, pattern, patterns

__all__ = [
    "ACCESS_CLASSES_FILE",
    "PARITY_ANCHOR_NOTE",
    "QUERY_FACTS",
    "AccessClass",
    "access_classes",
    "class_engine_scores",
    "class_parity_anchors",
    "class_score",
    "feature_patterns",
    "load_access_class_table",
    "unrated_native_entries",
]

ACCESS_CLASSES_FILE = Path(__file__).resolve().parents[2] / "catalog" / "access_classes.toml"

_ONE_LINE = r"^[^\n]+$"

# The query facts a class score may depend on, each read from the SQL facade by the
# classifier (``AccessClassification.facts``): a pattern listed in ``only_when`` counts
# only for a query showing its fact (a date/time bucket, each text-search kind, a
# window function, a partition-key equality plus a range on one table), ``write``
# (an INSERT, UPDATE, DELETE or MERGE) is credited only to a system of record
# (``class_score``), and ``key_scoped_count`` (a key-scoped read that only counts one
# table's rows) is what the capability waiver in ``gates.toml`` reads (#377).
QUERY_FACTS = frozenset(
    {
        "time_bucket",
        "full_text",
        "leading_wildcard",
        "pattern_match",
        "similarity_match",
        "window_function",
        "write",
        "partition_range",
        "recursive_cte",
        "lateral_join",
        "stored_routine",
        "upsert",
        "prefix_regex",
        "json_access",
        "key_scoped_count",
    }
)
# The fact that marks a write (ADR-031 Decision 2 step 3: an engine that is not a
# system of record never owns one, so no class credits it for one).
WRITE_FACT = "write"


class AccessClass(BaseModel):
    """One access-pattern class and the pattern files that describe it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z][a-z_]*$")
    label: str = Field(min_length=1, pattern=_ONE_LINE)
    leaning: str = Field(min_length=1, pattern=_ONE_LINE)
    why: str = Field(min_length=1, pattern=_ONE_LINE)
    patterns: tuple[str, ...] = Field(min_length=1)
    # pattern id -> the query fact it needs: the pattern counts for a query only when
    # the query shows that fact (``time-aggregation`` only for a time bucket, #920).
    only_when: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _patterns_exist(self) -> AccessClass:
        if stray := sorted(set(self.only_when) - set(self.patterns)):
            raise ValueError(f"class {self.id!r}: only_when names unlisted patterns {stray}")
        if unknown := sorted(set(self.only_when.values()) - QUERY_FACTS):
            raise ValueError(f"class {self.id!r}: only_when names unknown facts {unknown}")
        known = {p.id for p in patterns()}
        if missing := sorted(set(self.patterns) - known):
            raise ValueError(f"class {self.id!r} names patterns with no file: {missing}")
        if len(set(self.patterns)) != len(self.patterns):
            raise ValueError(f"class {self.id!r} lists a pattern twice")
        return self


class FeaturePattern(BaseModel):
    """A SQL feature a query of any class can show (#985): its pattern rates the query,
    alongside its class's own patterns, only when the query shows the feature's fact."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    pattern: str = Field(min_length=1)
    fact: str = Field(min_length=1)
    why: str = Field(min_length=1, pattern=_ONE_LINE)

    @model_validator(mode="after")
    def _known(self) -> FeaturePattern:
        if self.fact not in QUERY_FACTS:
            raise ValueError(f"feature {self.pattern!r} names an unknown fact {self.fact!r}")
        if self.pattern not in {p.id for p in patterns()}:
            raise ValueError(f"feature {self.pattern!r} has no pattern file")
        return self


class AccessClassTable(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    classes: tuple[AccessClass, ...] = Field(min_length=1)
    features: tuple[FeaturePattern, ...] = ()

    @model_validator(mode="after")
    def _ids_are_unique(self) -> AccessClassTable:
        ids = [c.id for c in self.classes]
        if len(set(ids)) != len(ids):
            raise ValueError(f"duplicate class ids in {ids}")
        features = [f.pattern for f in self.features]
        if len(set(features)) != len(features):
            raise ValueError(f"a feature pattern is listed twice: {features}")
        if both := sorted({p for c in self.classes for p in c.patterns} & set(features)):
            raise ValueError(f"a feature pattern is also a class pattern: {both}")
        return self


def load_access_class_table(path: Path = ACCESS_CLASSES_FILE) -> AccessClassTable:
    """The class table in ``path``: its classes, in severity order, and its features."""
    try:
        data = tomllib.loads(path.read_text())
        return AccessClassTable.model_validate(data)
    except (OSError, ValueError) as exc:
        raise CatalogError(f"{path}: {exc}") from exc


@cache
def _table() -> AccessClassTable:
    return load_access_class_table()


def access_classes() -> tuple[AccessClass, ...]:
    """The loaded class table (read once per process), in severity order."""
    return _table().classes


def feature_patterns() -> tuple[FeaturePattern, ...]:
    """The SQL features a query of any class can show, each with its pattern (#985)."""
    return _table().features


def _entry_score(support: EngineSupport) -> int | None:
    """An entry's rating for a class: a ``fit``'s ``score``, else its ``class_score``."""
    if support.kind == "fit":
        return support.score
    if support.kind in ("native", "unscored"):
        return support.class_score
    return None


def _ratings(
    access_class: AccessClass, engine: str, facts: frozenset[str], *, with_features: bool = True
) -> list[tuple[int, EngineSupport]]:
    """``(rating, entry)`` for each of the class's patterns that rates ``engine`` for a
    query showing ``facts`` (see :func:`class_score`), then each feature pattern whose
    fact the query shows, but only where the class's own patterns rate the engine above
    0 (#1123): a feature raises a class score, it never lifts a 0."""
    if WRITE_FACT in facts and engine not in engine_ids(system_of_record=True):
        return []

    def rated(listed: list[tuple[str, str]]) -> list[tuple[int, EngineSupport]]:
        return [
            (score, support)
            for pattern_id, needs in listed
            if needs in facts | {""}
            and (support := pattern(pattern_id).engines.get(engine)) is not None
            and (score := _entry_score(support)) is not None
        ]

    own = rated([(p, access_class.only_when.get(p, "")) for p in access_class.patterns])
    if not with_features or max((score for score, _ in own), default=0) == 0:
        return own
    return own + rated([(f.pattern, f.fact) for f in feature_patterns()])


def class_score(
    access_class: AccessClass,
    engine: str,
    facts: frozenset[str] = frozenset(),
    *,
    with_features: bool = True,
) -> int:
    """The highest rating the class's patterns give ``engine`` for a query showing
    ``facts``, else 0. A pattern in ``only_when`` counts only when its fact is in
    ``facts``, a feature pattern (:func:`feature_patterns`, #985) counts in every class
    for a query that shows its fact, and a write is rated only on a system of record.
    A feature only raises a score the class's own patterns give: where they give the
    engine 0, the class score stays 0 whatever features the query shows (#1123).
    ``with_features=False`` gives the class's own score, with no feature pattern.

    A ``fit`` entry counts at its ``score``; a ``native`` or ``unscored`` entry at its
    ``class_score`` (the engine serves the pattern although its analysis does not report
    it). An anti-pattern, a penalised entry or an entry with no rating counts nothing.
    """
    ratings = _ratings(access_class, engine, facts, with_features=with_features)
    return max((score for score, _ in ratings), default=0)


@cache
def class_parity_anchors(class_id: str, facts: frozenset[str] = frozenset()) -> dict[str, str]:
    """``active engine -> trace note`` for each engine whose class score, for a query of
    class ``class_id`` showing ``facts``, is a parity anchor (#936): no entry with its own
    evidence rates the engine as high. The note is :data:`PARITY_ANCHOR_NOTE` and the
    borrowed source, ``(<pattern>, <score>)``."""
    access_class = next(c for c in access_classes() if c.id == class_id)
    notes: dict[str, str] = {}
    for engine in sorted(engine_ids(status=ACTIVE)):
        ratings = _ratings(access_class, engine, facts)
        if not ratings:
            continue
        best = max(score for score, _ in ratings)
        winners = [support for score, support in ratings if score == best]
        if best and all(support.parity_anchor for support in winners):
            notes[engine] = f"{PARITY_ANCHOR_NOTE} ({winners[0].parity_anchor}, {best})"
    return notes


def unrated_native_entries() -> list[str]:
    """``pattern/engine`` for every ``native`` entry of a listed pattern with no ``class_score``.

    A native entry means the engine serves the pattern, so a class that lists the
    pattern must say how well (empty when every one is rated).
    """
    listed = {p for c in access_classes() for p in c.patterns}
    return sorted(
        f"{pattern_id}/{engine}"
        for pattern_id in listed
        for engine, support in pattern(pattern_id).engines.items()
        if support.kind == "native" and support.class_score is None
    )


@cache
def class_engine_scores(facts: frozenset[str] = frozenset()) -> dict[str, dict[str, int]]:
    """``class id -> active engine -> pattern score`` for every class, for a query
    showing ``facts`` (none by default)."""
    engines = sorted(engine_ids(status=ACTIVE))
    return {c.id: {e: class_score(c, e, facts) for e in engines} for c in access_classes()}
