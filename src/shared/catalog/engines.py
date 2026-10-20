"""The engine half of the rule catalog (ADR-031 Decision 8, #492).

One ``src/catalog/engines/<id>.toml`` per engine carries the engine-wide facts every
consumer must agree on: its display name, its colours, whether it is a real routing
target today (``status``), its architectural role and whether it can be a system of
record. This module loads those files into strict Pydantic models (an unknown, missing
or mistyped field fails at import time, so a broken catalog never reaches a run) and
derives the engine sets the rest of the code reads.

This first slice changes no behaviour: ``src.shared.engine_names``,
``src.shared.cache_engines``, ``src.shared.migration_wave_engines``,
``src.shared.report_facts`` and the PDF build their existing
constants, under their existing names, from here. The UI reads the same facts through
``src/ui/src/utils/engineCatalog.generated.js``, written by
``scripts/render_catalog.py`` (``--check`` runs in lint).

Fields:

- ``id``: the engine id the pipeline emits; must equal the file name.
- ``display_name``: the AWS-branded name every deliverable shows.
- ``display_order``: position in the display-name table (unique).
- ``ui_picker_order``: position in the assignment gate's engine picker (unique). Required,
  so a new engine always appears in the picker.
- ``status``: ``active`` (a routing target), ``future`` (a named placeholder that is
  never a routing candidate, ADR-031 Decision 9) or ``legacy`` (an id only older
  reports carry, kept so they still render; routing never emits it).
- ``role``: what the engine is for in the target architecture.
- ``system_of_record``: whether the engine can durably own a table. A cache fronts hot
  reads and a search read model indexes data synced from an owner; neither owns.
- ``colors.ui``: the engine's one colour (#1246): every UI view (badges, Sankey nodes,
  the assignment gate, the roadmap, the trade-off mapping chips) and the executive
  report's accents draw the engine in it, so a reader learns "colour = engine" once.
- ``pricing`` (optional table, #494): the engine's cost inputs. ``base_monthly`` is the
  fixed monthly operating cost the reality check weighs (minimum cluster plus ops
  burden); ``extra_engine_burden_monthly`` is the non-monetary burden of running this
  engine beyond the target engine count; ``max_burden_per_unique_query`` is the cut-off
  above which that burden per uniquely-served query makes the engine removable;
  ``acu_price_per_hour``, ``storage_price_per_gb_month`` and ``instance_hourly`` (instance
  type to hourly price) are the unit prices cost estimation multiplies.
  ``read_unit_price_per_million`` / ``read_unit_bytes`` and
  ``write_unit_price_per_million`` / ``write_unit_bytes`` are an engine's per-request
  prices (USD per million units, and the bytes one unit reads or writes); an engine
  billed by capacity has none. ``reads_billed_on_cache_miss_only``: a read served from
  the engine's buffer cache is not billed. The per-query cost dimension
  (``src.agents.referee.query_cost``, #494) and the cost estimates read them. An engine
  with no ``pricing`` table gets the ``EnginePricing`` defaults.
- ``score_weights`` (optional, #1002): the engine's score-dimension weights (ADR-031
  Decision 5), one ``[score_weights.<profile>]`` table per weight profile, each giving
  ``pattern_match``, ``complexity``, ``performance`` and ``cost`` and summing to 1. The
  first profile is the engine's default; OpenSearch has a second one for time-series
  tables. An engine with no ``score_weights`` uses ``DEFAULT_SCORE_WEIGHTS``. Every
  profile of every engine, and the default, gives cost the same weight (maintainer
  decision, #1002): the cost dimension is per query and engine-aware, so a per-engine
  cost weight would only add a flat bonus. The loader rejects a catalog where they differ.
- ``thresholds`` (optional, #921): the routing thresholds and floors that apply to this
  engine alone, one ``[thresholds.<name>]`` table each with a ``value`` and the ``why``
  for it (OpenSearch's justification floors, the cache overlay's hot-read floor). A
  threshold every engine shares lives in ``src/catalog/routing.toml`` instead.
- ``source_engines`` (optional, #921): the source database engines (the collector's
  ``source_database.engine``, lower case) whose own dialect this engine is: the Aurora
  engine a homogeneous migration keeps. A source engine names at most one engine.
- ``pattern_only`` (#475, #921): the engine scores a query only on the query's own
  matched pattern, never on a table-wide average (``engine_capabilities.
  PATTERN_ONLY_ENGINES``).
- ``absorbs_only`` (optional, #296, #921): the reality check lets this engine absorb only
  a query showing one of these triage ``signals`` or requiring one of these hard
  ``capabilities`` (``reality_check_rules.may_absorb``).
- ``override_needs_justification`` (#475, #921): a triage signal override onto this
  engine applies only when the reality check's justification floor agrees the signal is
  real (``assignment_resolver``).
- ``complexity_by_feature_count`` (optional, #921): the complexity points a table earns
  for showing 1, 2, ... of the patterns whose entry on this engine is a
  ``complexity_feature`` (the last value for any higher count; the Aurora SQL features).

The engine-specific routing branches #1081 moved out of the routing code:

- ``justification_floor`` (optional table): the reality check's floor this engine must
  clear to keep a query, beyond the cost-share floor every other mandatory engine faces.
  ``kind`` names the floor (``search_depth``: real search depth plus the engine's
  ``min_calls_per_second``, ``native_override_min_calls_per_second`` and
  ``min_corpus_rows_avg`` thresholds, #326), ``reason_code`` the move-ledger
  ``ReasonCode`` value a move off the engine records (checked against ``ReasonCode``
  where the floors are built, ``reality_check_floors.FLOOR_RULES``), and ``why`` the
  reason. Any number of engines may carry a floor of a known kind; each must define the
  kind's thresholds (``justification_floor_engines``). An engine with
  ``override_needs_justification`` must carry one, since that is the floor its signal
  override is judged by.
- ``joins_need_denormalized_design`` (#375 finding B2): a join query the reality check
  consolidates away from an engine that is neither one of these nor relational never
  lands here, since serving it needs a denormalised design nothing proposes yet
  (``denormalized_join_engines``).
- ``exact_tie_preferred`` (ADR-031 Decision 6 Amendment 1, #656): for a heterogeneous
  source, the relational engine that wins an exact tie on total adjusted score that no
  source feature breaks. This is a named-engine preference held as data: a maintainer
  rule, not an engine property (``exact_tie_engine``).
- ``short_name`` (optional): the short name the reason text uses for the engine's
  dialect (``"PostgreSQL"``); the display name when unset.

There are at most two active relational engines (the heterogeneous Aurora choice of
ADR-031 Decision 6 compares exactly two), and when there are two, ``exact_tie_preferred``
is set on exactly one of them and on no other engine; the loader rejects anything else.
Which engine each source feature favours is per-feature data in ``routing.toml``
(``[heterogeneous_features.<id>]``), not an engine flag.
"""

from __future__ import annotations

import math
import tomllib
from collections.abc import Iterable, Mapping
from functools import cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

CATALOG_DIR = Path(__file__).resolve().parents[2] / "catalog" / "engines"

EngineStatus = Literal["active", "future", "legacy"]
EngineRole = Literal[
    "relational", "key_value", "document", "search", "cache", "graph", "wide_column"
]
_HEX = r"^#[0-9A-Fa-f]{6}$"
# The four score dimensions a weight profile weighs (ADR-031 Decision 5).
SCORE_DIMENSIONS = ("pattern_match", "complexity", "performance", "cost")
# The split an engine with no ``score_weights`` uses (ADR-031 Decision 5's default).
DEFAULT_SCORE_WEIGHTS: Mapping[str, float] = {
    "pattern_match": 0.40,
    "complexity": 0.30,
    "performance": 0.20,
    "cost": 0.10,
}


class EngineColors(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ui: str = Field(pattern=_HEX)


class EnginePricing(BaseModel):
    """An engine's cost inputs (all USD; see the module docstring)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # The defaults apply to an engine whose file sets no value (and to an id the catalog
    # does not know): the same numbers the reality check used before the catalog held them.
    base_monthly: float = Field(default=100, ge=0)
    extra_engine_burden_monthly: float = Field(default=300, ge=0)
    max_burden_per_unique_query: float = Field(default=100, ge=0)
    acu_price_per_hour: float | None = Field(default=None, ge=0)
    storage_price_per_gb_month: float | None = Field(default=None, ge=0)
    instance_hourly: dict[str, float] = Field(default_factory=dict)
    read_unit_price_per_million: float | None = Field(default=None, ge=0)
    read_unit_bytes: int | None = Field(default=None, gt=0)
    write_unit_price_per_million: float | None = Field(default=None, ge=0)
    write_unit_bytes: int | None = Field(default=None, gt=0)
    reads_billed_on_cache_miss_only: bool = False

    @model_validator(mode="after")
    def _a_unit_price_has_its_unit_size(self) -> EnginePricing:
        for side in ("read", "write"):
            priced = getattr(self, f"{side}_unit_price_per_million") is not None
            sized = getattr(self, f"{side}_unit_bytes") is not None
            if priced != sized:
                raise ValueError(f"{side}_unit_price_per_million needs {side}_unit_bytes")
        return self


class Threshold(BaseModel):
    """One routing threshold or floor and the reason for its value (#921)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    value: int | float
    why: str = Field(min_length=1, pattern=r"^[^\n]+$")

    @model_validator(mode="before")
    @classmethod
    def _a_number(cls, data: object) -> object:
        if isinstance(data, Mapping) and isinstance(data.get("value"), bool):
            raise ValueError("a threshold value is a number")
        return data


class AbsorbOnly(BaseModel):
    """What a query must show for the reality check to let the engine absorb it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    signals: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()


# The justification floors an engine file can name (``JustificationFloor.kind``), each
# with the thresholds the engine's file must define for it.
FLOOR_THRESHOLDS: Mapping[str, tuple[str, ...]] = {
    "search_depth": (
        "min_calls_per_second",
        "native_override_min_calls_per_second",
        "min_corpus_rows_avg",
    ),
}
JustificationFloorKind = Literal["search_depth"]


class JustificationFloor(BaseModel):
    """The reality check's own floor for one engine (#326, #1081)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: JustificationFloorKind
    reason_code: str = Field(pattern=r"^[a-z][a-z0-9_.]*$")
    why: str = Field(min_length=1, pattern=r"^[^\n]+$")


class EngineEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    display_name: str = Field(min_length=1)
    display_order: int = Field(ge=1)
    ui_picker_order: int = Field(ge=1)
    status: EngineStatus
    role: EngineRole
    system_of_record: bool
    colors: EngineColors
    pricing: EnginePricing | None = None
    score_weights: dict[str, dict[str, float]] = Field(default_factory=dict)
    thresholds: dict[str, Threshold] = Field(default_factory=dict)
    source_engines: tuple[str, ...] = ()
    pattern_only: bool = False
    absorbs_only: AbsorbOnly | None = None
    override_needs_justification: bool = False
    complexity_by_feature_count: tuple[int, ...] = ()
    justification_floor: JustificationFloor | None = None
    joins_need_denormalized_design: bool = False
    exact_tie_preferred: bool = False
    short_name: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _a_justified_override_has_a_floor(self) -> EngineEntry:
        if self.override_needs_justification and self.justification_floor is None:
            raise ValueError(f"{self.id}: override_needs_justification needs a justification_floor")
        return self

    @model_validator(mode="after")
    def _a_floor_has_its_thresholds(self) -> EngineEntry:
        if self.justification_floor is not None:
            needed = FLOOR_THRESHOLDS[self.justification_floor.kind]
            if missing := [t for t in needed if t not in self.thresholds]:
                raise ValueError(
                    f"{self.id}: justification_floor {self.justification_floor.kind} needs "
                    f"thresholds {missing}"
                )
        return self

    @model_validator(mode="after")
    def _roles_that_never_own(self) -> EngineEntry:
        if self.role in ("cache", "search") and self.system_of_record:
            raise ValueError(f"{self.id}: a {self.role} engine is never a system of record")
        return self

    @model_validator(mode="before")
    @classmethod
    def _score_weights_are_whole_profiles(cls, data: object) -> object:
        if not isinstance(data, Mapping) or "score_weights" not in data:
            return data
        profiles = data["score_weights"]
        if not isinstance(profiles, Mapping) or not profiles:
            raise ValueError("score_weights needs at least one [score_weights.<profile>] table")
        for name, weights in profiles.items():
            if not isinstance(weights, Mapping) or set(weights) != set(SCORE_DIMENSIONS):
                raise ValueError(
                    f"score_weights.{name} must give exactly {', '.join(SCORE_DIMENSIONS)}"
                )
            if any(isinstance(w, bool) or not isinstance(w, int | float) for w in weights.values()):
                raise ValueError(f"score_weights.{name}: every weight is a number")
            if any(not 0 <= w <= 1 for w in weights.values()):
                raise ValueError(f"score_weights.{name}: every weight is between 0 and 1")
            if not math.isclose(sum(weights.values()), 1.0, abs_tol=1e-9):
                raise ValueError(f"score_weights.{name}: the weights must sum to 1")
        return data


class CatalogError(ValueError):
    """The engine catalog on disk is inconsistent (raised at import time)."""


def load_engine_catalog(directory: Path = CATALOG_DIR) -> tuple[EngineEntry, ...]:
    """Every engine file in ``directory``, validated, in ``display_order``.

    A deleted engine file is not detected here: the only lists to check it against are
    the hand-kept ones the catalog replaces. ``tests/unit/shared/test_engine_catalog.py``
    (a release gate) fails instead, naming the contract enums, triage sets and runners
    that still name the engine.
    """
    entries: list[EngineEntry] = []
    for path in sorted(directory.glob("*.toml")):
        try:
            with path.open("rb") as fh:
                data = tomllib.load(fh)
            entry = EngineEntry.model_validate(data)
        except (tomllib.TOMLDecodeError, ValueError) as exc:
            raise CatalogError(f"{path.name}: {exc}") from exc
        if entry.id != path.stem:
            raise CatalogError(f"{path.name}: id {entry.id!r} does not match the file name")
        entries.append(entry)
    if not entries:
        raise CatalogError(f"no engine files under {directory}")
    for field in ("display_order", "ui_picker_order"):
        orders = [getattr(e, field) for e in entries]
        if len(set(orders)) != len(orders):
            raise CatalogError(f"{field} values repeat: {sorted(orders)}")
    _one_cost_weight(entries)
    _one_engine_per_source(entries)
    _the_relational_choice(entries)
    return tuple(sorted(entries, key=lambda e: e.display_order))


def _the_relational_choice(entries: Iterable[EngineEntry]) -> None:
    """At most two active relational engines, and with two, ``exact_tie_preferred`` on
    exactly one of them and on no other engine (ADR-031 Decision 6 and its Amendment 1:
    the heterogeneous Aurora choice compares exactly two, #1081)."""
    entries = list(entries)
    relational = {e.id for e in entries if e.status in ACTIVE and e.role == "relational"}
    if len(relational) > 2:
        raise CatalogError(
            f"{len(relational)} active relational engines {sorted(relational)}: the "
            "heterogeneous Aurora choice (ADR-031 Decision 6) compares exactly two; adding "
            "a third needs a new Decision 6 rule first"
        )
    flagged = sorted(e.id for e in entries if e.exact_tie_preferred)
    if set(flagged) - relational:
        raise CatalogError(
            f"exact_tie_preferred is set on {flagged}: only an active relational engine"
        )
    if len(flagged) > 1 or (len(relational) == 2 and not flagged):
        raise CatalogError(
            f"exact_tie_preferred must be set on exactly one active relational engine, not "
            f"{flagged}"
        )


def _one_engine_per_source(entries: Iterable[EngineEntry]) -> None:
    """A source engine names at most one target engine as its own dialect (#921)."""
    seen: dict[str, str] = {}
    for e in entries:
        for source in e.source_engines:
            if source in seen:
                raise CatalogError(
                    f"source engine {source!r} is named by both {seen[source]} and {e.id}"
                )
            seen[source] = e.id


def _one_cost_weight(entries: Iterable[EngineEntry]) -> None:
    """Every engine weights cost the same (ADR-031 Decision 5, #1002), the default too:
    an engine with no ``score_weights`` scores with ``DEFAULT_SCORE_WEIGHTS``."""
    costs = {"<default>": DEFAULT_SCORE_WEIGHTS["cost"]}
    for e in entries:
        for name, weights in e.score_weights.items():
            costs[f"{e.id}.{name}"] = weights["cost"]
    if len(set(costs.values())) > 1:
        raise CatalogError(
            f"the cost weight must be the same for every engine (ADR-031 Decision 5): {costs}"
        )


@cache
def engines() -> tuple[EngineEntry, ...]:
    """The loaded catalog (read once per process)."""
    return load_engine_catalog()


def engine_ids(
    *,
    status: EngineStatus | Iterable[EngineStatus] | None = None,
    role: EngineRole | Iterable[EngineRole] | None = None,
    system_of_record: bool | None = None,
) -> frozenset[str]:
    """Ids of the engines matching every given filter (``None`` means any)."""
    statuses = {status} if isinstance(status, str) else set(status) if status else None
    roles = {role} if isinstance(role, str) else set(role) if role else None
    return frozenset(
        e.id
        for e in engines()
        if (statuses is None or e.status in statuses)
        and (roles is None or e.role in roles)
        and (system_of_record is None or e.system_of_record is system_of_record)
    )


def pricing(engine: str) -> EnginePricing:
    """``engine``'s cost inputs: its ``[pricing]`` table, else the ``EnginePricing`` defaults."""
    for e in engines():
        if e.id == engine and e.pricing is not None:
            return e.pricing
    return EnginePricing()


def default_score_weights() -> dict[str, float]:
    """The score-dimension weights an engine with no ``score_weights`` uses."""
    return dict(DEFAULT_SCORE_WEIGHTS)


def score_weight_profiles(engine: str) -> tuple[dict[str, float], ...]:
    """``engine``'s score-weight profiles in file order (its default first), or the
    default weights alone for an engine with none or an id the catalog does not know."""
    for e in engines():
        if e.id == engine and e.score_weights:
            return tuple(dict(w) for w in e.score_weights.values())
    return (default_score_weights(),)


def score_weights(engine: str, profile: str | None = None) -> dict[str, float]:
    """One of ``engine``'s score-weight profiles: ``profile`` by name, else its default.

    Raises ``KeyError`` for a profile the engine's file does not define.
    """
    if profile is None:
        return score_weight_profiles(engine)[0]
    for e in engines():
        if e.id == engine and profile in e.score_weights:
            return dict(e.score_weights[profile])
    raise KeyError(f"{engine} has no score_weights.{profile}")


def engine_threshold(engine: str, name: str) -> int | float:
    """``engine``'s ``[thresholds.<name>]`` value (#921). Raises ``KeyError`` for a
    threshold the engine's file does not define."""
    for e in engines():
        if e.id == engine and name in e.thresholds:
            return e.thresholds[name].value
    raise KeyError(f"{engine} has no thresholds.{name}")


def engine_entry(engine: str) -> EngineEntry | None:
    """The engine file for ``engine``, or ``None`` for an id the catalog does not know."""
    return next((e for e in engines() if e.id == engine), None)


def justification_floors() -> dict[str, JustificationFloor]:
    """Every active engine's own ``justification_floor``, by engine id (#1081)."""
    return {
        e.id: e.justification_floor
        for e in engines()
        if e.status in ACTIVE and e.justification_floor is not None
    }


def denormalized_join_engines() -> frozenset[str]:
    """The active engines with ``joins_need_denormalized_design`` (#375, #1081)."""
    return frozenset(
        e.id for e in engines() if e.status in ACTIVE and e.joins_need_denormalized_design
    )


def exact_tie_engine() -> str | None:
    """The relational engine that wins an exact heterogeneous Aurora tie (ADR-031 Decision 6
    Amendment 1), or None with fewer than two relational engines (#1081)."""
    return next((e.id for e in engines() if e.exact_tie_preferred), None)


def source_engine_dialects() -> dict[str, str]:
    """Source database engine (lower case) to the target engine that is its own dialect,
    from every active engine's ``source_engines`` (``triage.SOURCE_ENGINE_TO_AURORA``)."""
    return {s: e.id for e in engines() if e.status in ACTIVE for s in e.source_engines}


# Engines routing can target today: every routing-facing role set is restricted to these.
ACTIVE: tuple[EngineStatus, ...] = ("active",)
# Engines a report may name as a real component: active ones plus legacy ids.
REPORTABLE: tuple[EngineStatus, ...] = ("active", "legacy")
