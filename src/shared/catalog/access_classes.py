"""The access-pattern class table (ADR-031 Decision 14, Amendment 5, #728).

``src/catalog/access_classes.toml`` lists the seven classes a query can fall into
(:mod:`src.agents.referee.access_pattern` assigns them) and, for each, the pattern files
that describe that class. A class's pattern score on an engine is the highest rating
those patterns give the engine -- a ``fit`` entry's ``score``, or a ``native``/``unscored``
entry's ``class_score`` -- or 0 when none of them rates it:
the numbers stay in the pattern files (Decision 8), so a class score can never disagree
with the catalog. Loaded into strict Pydantic models like the rest of the catalog: an
unknown field, a missing one or a pattern id with no file fails at import time.

Shadow only: the per-query scorer reads these through
:class:`src.agents.referee.query_scoring.QueryPatternModel`, which routing does not
build yet (:mod:`src.agents.referee.access_pattern_shadow`).
"""

from __future__ import annotations

import tomllib
from functools import cache
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.shared.catalog.engines import ACTIVE, CatalogError, engine_ids
from src.shared.catalog.patterns import EngineSupport, pattern, patterns

ACCESS_CLASSES_FILE = Path(__file__).resolve().parents[2] / "catalog" / "access_classes.toml"

_ONE_LINE = r"^[^\n]+$"

# The query facts a class may make a pattern depend on (``only_when``), each read from
# the SQL facade by the classifier (``AccessClassification.facts``).
QUERY_FACTS = frozenset({"time_bucket"})


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


class AccessClassTable(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    classes: tuple[AccessClass, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _ids_are_unique(self) -> AccessClassTable:
        ids = [c.id for c in self.classes]
        if len(set(ids)) != len(ids):
            raise ValueError(f"duplicate class ids in {ids}")
        return self


def load_access_classes(path: Path = ACCESS_CLASSES_FILE) -> tuple[AccessClass, ...]:
    """The classes in ``path``, in severity order."""
    try:
        data = tomllib.loads(path.read_text())
        return AccessClassTable.model_validate(data).classes
    except (OSError, ValueError) as exc:
        raise CatalogError(f"{path}: {exc}") from exc


@cache
def access_classes() -> tuple[AccessClass, ...]:
    """The loaded class table (read once per process), in severity order."""
    return load_access_classes()


def _entry_score(support: EngineSupport) -> int | None:
    """An entry's rating for a class: a ``fit``'s ``score``, else its ``class_score``."""
    if support.kind == "fit":
        return support.score
    if support.kind in ("native", "unscored"):
        return support.class_score
    return None


def class_score(access_class: AccessClass, engine: str, facts: frozenset[str] = frozenset()) -> int:
    """The highest rating the class's patterns give ``engine`` for a query showing
    ``facts``, else 0. A pattern in ``only_when`` counts only when its fact is in
    ``facts``.

    A ``fit`` entry counts at its ``score``; a ``native`` or ``unscored`` entry at its
    ``class_score`` (the engine serves the pattern although its analysis does not report
    it). An anti-pattern, a penalised entry or an entry with no rating counts nothing.
    """
    scores = [
        score
        for pattern_id in access_class.patterns
        if access_class.only_when.get(pattern_id, "") in facts | {""}
        and (support := pattern(pattern_id).engines.get(engine)) is not None
        and (score := _entry_score(support)) is not None
    ]
    return max(scores, default=0)


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
