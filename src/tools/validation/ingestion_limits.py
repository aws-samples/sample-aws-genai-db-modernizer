# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Table and query ceilings for an uploaded collection, enforced before ingestion
(#142, #384).

A pathological collection (an enormous query or table set) can drive excessive
memory use, processing time and LLM/compute cost before the pipeline ever reaches
analysis. This module is the one place every ingestion path checks a collection
against configurable limits, as early as possible: the local API upload (confirm
step), ``scripts/run_assessment.py`` and ``scripts/run_collect.py --file``, the
live S3-backed offline collector (``fetch_offline_json``), and the AWS Transform
upload-discovery/ingestion path.

``check_collection_counts`` checks the parsed collection's table and query counts,
once the (necessarily already in-memory) dict is available, before the more
expensive nested structures are built from it. It raises ``IngestionLimitError`` (a
``ValueError``) naming the limit that was hit and the environment variable that
raises it.

There is no limit on a file's size (maintainer decision on #384, 2026-11-03: "it
takes what it takes"): a production collection is as large as its workload, and the
query count is what bounds the work. The default of 250,000 queries is above the
100,000-statement collection the pipeline was measured on (#384) with room to spare.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)

ENV_MAX_TABLES = "MODERNIZER_MAX_UPLOAD_TABLES"
ENV_MAX_QUERIES = "MODERNIZER_MAX_UPLOAD_QUERIES"

# The bundled samples have at most a few hundred tables; 50,000 leaves ample headroom
# for a very large real-world schema (round-1 review, #142).
DEFAULT_MAX_TABLES = 50_000
# Every collector captures every statement by default (#384), so this is the one bound
# on a collection's statements: the maintainer's 250,000 (2026-11-03).
DEFAULT_MAX_QUERIES = 250_000


class IngestionLimitError(ValueError):
    """Raised when an uploaded collection exceeds a configured ingestion limit."""


@dataclass(frozen=True)
class IngestionLimits:
    """Configurable ceilings checked before and during ingestion."""

    max_tables: int = DEFAULT_MAX_TABLES
    max_queries: int = DEFAULT_MAX_QUERIES

    @classmethod
    def from_env(cls) -> IngestionLimits:
        """Build limits from the environment, falling back to the defaults
        above for any variable that is unset, non-numeric, or non-positive."""
        return cls(
            max_tables=_env_int(ENV_MAX_TABLES, DEFAULT_MAX_TABLES),
            max_queries=_env_int(ENV_MAX_QUERIES, DEFAULT_MAX_QUERIES),
        )


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s=%r is not an integer; using the default %d", name, raw, default)
        return default
    if value <= 0:
        logger.warning("%s=%d is not positive; using the default %d", name, value, default)
        return default
    return value


def check_collection_counts(
    *,
    table_count: int,
    query_count: int,
    source: str,
    limits: IngestionLimits | None = None,
) -> None:
    """Reject a parsed collection if its table or query count exceeds the ceiling.

    Raises:
        IngestionLimitError: either count exceeds its configured limit.
    """
    limits = limits or IngestionLimits.from_env()
    if table_count > limits.max_tables:
        raise IngestionLimitError(
            f"{source}: collection has {table_count:,} tables, which exceeds "
            f"the {limits.max_tables:,}-table ingestion limit. Set "
            f"{ENV_MAX_TABLES} to raise it."
        )
    if query_count > limits.max_queries:
        raise IngestionLimitError(
            f"{source}: collection has {query_count:,} queries, which exceeds "
            f"the {limits.max_queries:,}-query ingestion limit. Set "
            f"{ENV_MAX_QUERIES} to raise it."
        )
