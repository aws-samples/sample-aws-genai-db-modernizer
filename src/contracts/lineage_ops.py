# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Lineage Operations — Contract Model

The closed catalog of operations a transformation contract rule uses to build one
target field (ADR-033 Decision 4, #1762). Tables and columns are referenced by
collector id; no operation carries SQL or free text a model wrote. The union is
discriminated on ``op``, so later issues add operations (keys and joins, #1767)
without changing these.

Version History:
- 1.0 (2026-11-03, #1762): Initial version: copy, transform (function cast) and
  unsupported, with column references by collector id.
"""

from typing import Annotated, Literal, assert_never

from pydantic import BaseModel, Field

#: The conversion functions a ``transform`` may name. ``cast`` converts a value into
#: the target column's declared type, as Aurora does on insert (step 1 runs no DMS
#: transformation rule; the DDL's types convert).
TransformFunction = Literal["cast"]

#: A collector table id is ``f"{schema}.{name}"`` unescaped, so a name may hold spaces or
#: dots: the id is a schema, a dot, then the rest.
TABLE_ID_PATTERN = r"^[^.]+\..+$"
_TABLE_ID = TABLE_ID_PATTERN


class ColumnRef(BaseModel):
    """A source column, by collector id: ``table_id`` is ``schema.table``."""

    table_id: str = Field(..., pattern=_TABLE_ID, description="Collector table id (schema.table)")
    column: str = Field(..., min_length=1, description="Column name, as the collector records it")


class CopyOp(BaseModel):
    """The source column's value, unchanged."""

    op: Literal["copy"]
    column: ColumnRef


class TransformOp(BaseModel):
    """The source column's value converted by one function from the closed list."""

    op: Literal["transform"]
    column: ColumnRef
    function: TransformFunction
    from_type: str = Field(..., min_length=1, description="The source column's type")
    to_type: str = Field(..., min_length=1, description="The target column's type")


class UnsupportedOp(BaseModel):
    """A field no operation can express: a named customer hook, never code a model wrote."""

    op: Literal["unsupported"]
    description: str = Field(..., min_length=1, description="What the transformation should do")


LineageOp = Annotated[CopyOp | TransformOp | UnsupportedOp, Field(discriminator="op")]


def reads_own_row(op: CopyOp | TransformOp | UnsupportedOp, source_table: str) -> bool:
    """True when the operation reads only the rule's own source row (``copy`` and
    ``transform`` must; ``unsupported`` reads nothing). Exhaustive: a new operation
    in the union fails type checking here until it is decided."""
    if isinstance(op, CopyOp | TransformOp):
        return op.column.table_id == source_table
    if isinstance(op, UnsupportedOp):
        return True
    assert_never(op)
