"""Aurora schema-design delta contract (issue #273).

An Aurora PostgreSQL / MySQL design is a 1:1 carry-over of the deterministic
draft (``src/tools/schema/aurora_common/draft_builder.py``) plus a few
Aurora-level decisions. The model writes only those decisions, as a delta
against the draft, so its output size does not depend on the schema size.
``merge_design_delta`` (``src/tools/schema/aurora_common/delta_merge.py``)
rebuilds the draft, applies the delta, regenerates the DDL and returns the
full ``Aurora*ModelOutputContract``.

Shared by both Aurora engines: the merged output is validated against the
engine's own contract afterwards.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .aurora_postgresql_model_output import AppLayerNote, Optimization
from .schema_design_output import TradeOff

DELTA_VERSION = "1.0"


class ColumnTypeChange(BaseModel):
    """Set one column's Aurora type (resolve a residual, or change a draft type)."""

    column: str = Field(..., description="Column name, exactly as in the design view")
    aurora_type: str = Field(..., min_length=1, description="Target Aurora type, e.g. BIGINT")
    reason: str = Field(default="", description="Why this type")

    model_config = ConfigDict(extra="forbid")


class IndexChange(BaseModel):
    """Replace one of the draft's indexes with a new statement."""

    index_name: str = Field(..., description="Name of an existing draft index on this table")
    statement: str = Field(..., description="Replacement CREATE [UNIQUE] INDEX statement")

    model_config = ConfigDict(extra="forbid")


class TableDelta(BaseModel):
    """Changes to one draft table. Tables not listed carry over unchanged."""

    table_name: str = Field(..., description="A table from the design view")
    add_indexes: list[str] = Field(
        default_factory=list, description="New CREATE [UNIQUE] INDEX statements on this table"
    )
    modify_indexes: list[IndexChange] = Field(
        default_factory=list, description="Draft indexes to replace"
    )
    remove_indexes: list[str] = Field(
        default_factory=list, description="Names of draft indexes to drop"
    )
    column_types: list[ColumnTypeChange] = Field(
        default_factory=list, description="Per-column type overrides"
    )

    model_config = ConfigDict(extra="forbid")


class TypeRule(BaseModel):
    """Resolve every residual column of one source data type at once.

    Applies only to residual columns (``needs_judgment`` in the draft) whose
    source ``data_type`` matches, case-insensitively, and that have no
    per-column override in ``tables[].column_types``.
    """

    source_data_type: str = Field(
        ..., min_length=1, description="Source data_type, as listed in residual_types"
    )
    aurora_type: str = Field(..., min_length=1, description="Target Aurora type")
    reason: str = Field(default="", description="Why this type")

    model_config = ConfigDict(extra="forbid")


class AuroraDesignDeltaContract(BaseModel):
    """What the model decides on top of the deterministic Aurora draft."""

    delta_version: Literal["1.0"] = Field(..., description='Delta contract version; must be "1.0"')
    tables: list[TableDelta] = Field(
        default_factory=list, description="Per-table index and type changes"
    )
    type_rules: list[TypeRule] = Field(
        default_factory=list, description="Residual resolutions by source data type"
    )
    optimizations: list[Optimization] = Field(
        default_factory=list,
        description="Aurora optimizations (partitioning, index, read_replica, io_optimized)",
    )
    app_layer_notes: list[AppLayerNote] = Field(
        default_factory=list, description="Source features that need application-layer handling"
    )
    trade_offs: list[TradeOff] = Field(
        default_factory=list, description="Significant design decisions, written for a CTO"
    )

    model_config = ConfigDict(extra="forbid")


def is_design_delta(response: object) -> bool:
    """True when an LLM response is a delta rather than a full output contract."""
    return isinstance(response, dict) and "delta_version" in response
