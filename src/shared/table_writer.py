# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A table's writer, the one rule the slice plan and the step plans read (ADR-031
Amendment 9, ADR-033 Amendment 1).

A written table is owned by the engine every statement that writes it lands on, its
source of truth (``write_groups.sources_of_truth``). A table no statement writes (or whose
writes have no single engine) has no later owner: the relational step carries it, and a
later step reads a copy (ADR-033 Decisions 1, 7 and 11). Never a read preference or a
table majority. With no relational step, the source database keeps it.
"""

from __future__ import annotations

from collections.abc import Mapping

from src.shared.engine_names import SOURCE_DATABASE

__all__ = ["table_writer"]


def table_writer(table: str, sources_of_truth: Mapping[str, str], relational: str | None) -> str:
    """``table``'s writer: its source of truth, else the relational step's engine, else the
    source database."""
    return sources_of_truth.get(table) or relational or SOURCE_DATABASE
