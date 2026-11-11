# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The DMS replication instance sizing table (#1798, ADR-033 Decision 7).

``src/catalog/dms_sizing.toml`` lists, in order, the rows step 1's replication instance is
chosen from (the first row whose database-size and write-rate bounds both hold; the last row
has no bounds) the lag limit of the step's gate and the LOB rules the task settings follow. Loaded into strict Pydantic models
like the rest of the catalog: an unknown field, a missing one or rows out of order fail at
load time. The choice itself is ``src.tools.data_movement.dms_sizing``.
"""

from __future__ import annotations

import tomllib
from functools import cache
from pathlib import Path
from typing import cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from src.shared.catalog.engines import CatalogError

__all__ = [
    "DMS_SIZING_FILE",
    "DmsSizingTable",
    "EngineRules",
    "GateRules",
    "LobInstanceClass",
    "LobRules",
    "NotMovedRules",
    "SizingRow",
    "dms_sizing",
    "load_dms_sizing",
]

DMS_SIZING_FILE = Path(__file__).resolve().parents[2] / "catalog" / "dms_sizing.toml"

_ONE_LINE = r"^[^\n]+$"


class SizingRow(BaseModel):
    """One row: the instance class and storage for databases within its two bounds."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    instance_class: str = Field(pattern=r"^dms\.[a-z0-9]+\.[a-z0-9]+$")
    max_database_gb: float | None = Field(None, gt=0)
    max_writes_per_second: float | None = Field(None, gt=0)
    allocated_storage_gb: int = Field(ge=5, le=6144)
    # The storage the instance's hourly price includes; only the rest is billed per GB.
    included_storage_gb: int = Field(ge=0)
    why: str = Field(min_length=1, pattern=_ONE_LINE)
    note: str | None = Field(None, min_length=1, pattern=_ONE_LINE)

    @model_validator(mode="after")
    def _included_fits(self) -> SizingRow:
        if self.included_storage_gb > self.allocated_storage_gb:
            raise ValueError(f"row {self.id!r}: included_storage_gb is above allocated_storage_gb")
        return self


class LobInstanceClass(BaseModel):
    """A replication instance class and the largest LobMaxSize it holds (#1918)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    instance_class: str = Field(pattern=r"^dms\.[a-z0-9]+\.[a-z0-9]+$")
    memory_gib: int = Field(ge=1)
    # The largest task-wide LobMaxSize it holds, and the largest limit one table may have of its
    # own (a table-settings rule) while the task runs at most at max_lob_kb.
    max_lob_kb: int = Field(ge=1, le=102400)
    max_table_lob_kb: int = Field(ge=1, le=102400)
    # The largest sum, over every LOB column of the moved tables, of the limit each runs at (#2123).
    max_total_lob_kb: int = Field(ge=1)
    included_storage_gb: int = Field(ge=0)
    why: str = Field(min_length=1, pattern=_ONE_LINE)


class LobRules(BaseModel):
    """When a table's LOB columns run in limited LOB mode, and at what size."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    limited_max_kb: int = Field(ge=1, le=102400)
    default_max_kb: int = Field(ge=1)
    # PostgreSQL's cap for a varchar without a length unless the endpoint setting
    # UnboundedVarcharMaxSize raises it: the setting's maximum, in bytes.
    unbounded_varchar_max_bytes: int = Field(ge=1)
    why: str = Field(min_length=1, pattern=_ONE_LINE)
    scalars_why: str = Field(min_length=1, pattern=_ONE_LINE)
    # Source dialects whose step 1 task never runs in full LOB mode (#1918, #2123): their
    # tables with an unknown or larger LOB size run in limited mode at undeclared_max_kb.
    limited_only: tuple[str, ...] = ()
    undeclared_max_kb: int = Field(ge=1, le=102400)
    limited_only_why: str = Field(min_length=1, pattern=_ONE_LINE)
    # Source dialects whose endpoint has FailTasksOnLobTruncation, which step 1 sets (#1918).
    fails_on_truncation: tuple[str, ...] = ()
    # Source dialects whose LOB types declare their type's ceiling, not a bound on the values
    # (MySQL: every LONGTEXT declares 4294967295, #2123).
    declared_is_type_ceiling: tuple[str, ...] = ()
    # Source dialects whose unmeasured LOB tables stop step 1 until measured (#2123), and the
    # size such a table runs at when the customer explicitly skips measuring.
    measure_required: tuple[str, ...] = ()
    skipped_max_kb: int = Field(1024, ge=1, le=102400)
    skipped_why: str = Field("", pattern=r"^[^\n]*$")
    # Measured sizes (#1918): bytes DMS counts per stored byte of a character LOB, the
    # headroom factor and the KB step LobMaxSize is rounded up to.
    measured_bytes_per_character: int = Field(ge=1, le=4)
    measured_headroom: float = Field(ge=1)
    measured_round_kb: int = Field(ge=1)
    # Source dialect -> a finer rounding step for measured sizes (#2123: MySQL 64 KB).
    measured_round_kb_by_source: dict[str, int] = Field(default_factory=dict)
    measured_why: str = Field(min_length=1, pattern=_ONE_LINE)
    # Why a table above the task's LobMaxSize gets its own limit (a table-settings rule, #1918).
    table_settings_why: str = Field(min_length=1, pattern=_ONE_LINE)
    # The class each LobMaxSize needs, smallest first (memory and max_lob_kb both increase).
    instance_classes: tuple[LobInstanceClass, ...] = Field(min_length=1)
    # Source dialect -> lower-case base type names DMS reads as a LOB.
    types: dict[str, tuple[str, ...]]
    # Source dialect -> base type names known to map to a non-LOB type. A dialect listed here
    # runs every type in neither list in full LOB mode.
    scalars: dict[str, tuple[str, ...]]

    @model_validator(mode="after")
    def _names(self) -> LobRules:
        for section, lists in (("types", self.types), ("scalars", self.scalars)):
            for dialect, names in lists.items():
                if any(n != n.strip().lower() or not n for n in names):
                    raise ValueError(
                        f"lob.{section}.{dialect}: names must be lower-case and trimmed"
                    )
                if len(set(names)) != len(names):
                    raise ValueError(f"lob.{section}.{dialect}: a type is listed twice")
        for before, after in zip(self.instance_classes, self.instance_classes[1:], strict=False):
            if not (
                after.memory_gib > before.memory_gib
                and after.max_lob_kb > before.max_lob_kb
                and after.max_table_lob_kb >= before.max_table_lob_kb
                and after.max_total_lob_kb > before.max_total_lob_kb
            ):
                raise ValueError(
                    f"lob.instance_classes: {after.instance_class} must have more memory, a "
                    f"larger max_lob_kb and at least the max_table_lob_kb of {before.instance_class}"
                )
        for c in self.instance_classes:
            if c.max_table_lob_kb < c.max_lob_kb:
                raise ValueError(
                    f"lob.instance_classes: {c.instance_class}'s max_table_lob_kb is below its max_lob_kb"
                )
        for name in ("fails_on_truncation", "measure_required"):
            if extra := sorted(set(getattr(self, name)) - set(self.limited_only)):
                raise ValueError(f"lob.{name}: {extra} not in lob.limited_only")
        if self.measure_required and not self.skipped_why:
            raise ValueError("lob.measure_required needs lob.skipped_why")
        for dialect, names in self.scalars.items():
            if both := sorted(set(names) & set(self.types.get(dialect, ()))):
                raise ValueError(f"lob.scalars.{dialect}: {both} also listed as LOB types")
        return self


class EngineRules(BaseModel):
    """The DMS engine version of step 1's replication instance, with its reason."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    why: str = Field(min_length=1, pattern=_ONE_LINE)
    # Source dialect -> the reasoning for that source, appended to ``why`` (#2123).
    source_why: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _one_line(self) -> EngineRules:
        if bad := sorted(d for d, w in self.source_why.items() if not w or "\n" in w):
            raise ValueError(f"engine.source_why: {bad} must be one non-empty line")
        return self

    def why_for(self, dialect: str | None) -> str:
        """``why`` plus the source dialect's own reasoning, if the catalog has one."""
        family = {"mariadb": "mysql"}.get(dialect or "", dialect or "")
        extra = self.source_why.get(family)
        return f"{self.why} {extra}" if extra else self.why


class NotMovedRules(BaseModel):
    """Source types DMS does not migrate: step 1 creates their tables but does not move them (#1918)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    postgresql: tuple[str, ...] = ()
    why: str = Field(min_length=1, pattern=_ONE_LINE)

    def types(self, dialect: str | None) -> tuple[str, ...]:
        """The lower-case base types of ``dialect`` DMS does not migrate (none for others)."""
        return self.postgresql if dialect == "postgresql" else ()


class GateRules(BaseModel):
    """The lag limit of step 1's DMS gate, with its reason."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_lag_seconds: int = Field(strict=True, ge=1)
    why: str = Field(min_length=1, pattern=_ONE_LINE)


class DmsSizingTable(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    rows: tuple[SizingRow, ...] = Field(min_length=1)
    engine: EngineRules
    gate: GateRules
    lob: LobRules
    not_moved: NotMovedRules

    @model_validator(mode="after")
    def _ordered(self) -> DmsSizingTable:
        ids = [r.id for r in self.rows]
        if len(set(ids)) != len(ids):
            raise ValueError(f"duplicate row ids in {ids}")
        *bounded, last = self.rows
        if last.max_database_gb is not None or last.max_writes_per_second is not None:
            raise ValueError("the last row has no bounds: it takes every database above the others")
        for row in bounded:
            if row.max_database_gb is None or row.max_writes_per_second is None:
                raise ValueError(f"row {row.id!r} needs both bounds (only the last row has none)")
        for before, after in zip(bounded, bounded[1:], strict=False):
            gb_before, gb_after = cast(float, before.max_database_gb), cast(
                float, after.max_database_gb
            )
            ws_before = cast(float, before.max_writes_per_second)
            ws_after = cast(float, after.max_writes_per_second)
            if not (gb_after > gb_before and ws_after > ws_before):
                raise ValueError(
                    f"row {after.id!r}: both bounds must increase from row {before.id!r}"
                )
        if self.lob.default_max_kb > self.lob.limited_max_kb:
            raise ValueError("lob.default_max_kb is above lob.limited_max_kb")
        return self


def load_dms_sizing(path: Path = DMS_SIZING_FILE) -> DmsSizingTable:
    """The sizing table in ``path``."""
    try:
        return DmsSizingTable.model_validate(tomllib.loads(path.read_text()))
    except (OSError, ValueError, ValidationError) as exc:
        raise CatalogError(f"{path}: {exc}") from exc


@cache
def dms_sizing() -> DmsSizingTable:
    """The committed table (read once per process)."""
    return load_dms_sizing()
