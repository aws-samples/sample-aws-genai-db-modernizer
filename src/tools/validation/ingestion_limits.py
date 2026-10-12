"""Size and row-count ceilings for an uploaded collection, enforced before
ingestion (#142).

A pathological or oversized offline collection (an enormous query or table
set, or simply a very large file) can drive excessive memory use, processing
time, and LLM/compute cost before the pipeline ever reaches analysis. This
module is the one place every ingestion path checks a collection against
configurable limits, as early as possible: the local API upload (confirm
step), ``scripts/run_assessment.py`` and ``scripts/run_collect.py --file``,
the live S3-backed offline collector (``fetch_offline_json``), and the AWS
Transform upload-discovery/ingestion path (``AtxArtifactStore.read_json`` and
``_discover_uploaded_input``).

Two checks, used at different points in each ingestion path:

- ``check_file_size`` -- the raw file/object size, checked before the body is
  read into memory wherever that's possible (a local file's ``os.path.getsize``,
  or S3's ``head_object``/an artifact store's downloaded tmp file). This is
  the main defense: it bounds memory regardless of what the collection
  contains.
- ``check_collection_counts`` -- the parsed collection's table and query
  counts, checked once the (necessarily already in-memory) dict is available,
  before the more expensive nested structures are built from it.

Both raise ``IngestionLimitError`` (a ``ValueError``) naming the limit that
was hit and the environment variable that raises it. Defaults are generous
enough for the bundled samples (``docs/examples/wordpress``,
``docs/examples/discourse``) and for the up-to-10,000-query collections
proposed in #384; raise them with the environment variables below for larger
real-world collections.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)

ENV_MAX_FILE_BYTES = "MODERNIZER_MAX_UPLOAD_BYTES"
ENV_MAX_TABLES = "MODERNIZER_MAX_UPLOAD_TABLES"
ENV_MAX_QUERIES = "MODERNIZER_MAX_UPLOAD_QUERIES"

# 100 MB comfortably covers the bundled samples (wordpress ~220 KB, discourse
# ~2.5 MB uncompressed) with headroom for real-world collections. This is the
# primary memory guard; the table/query counts below exist to reject a
# pathological shape early, not to bound memory on their own.
DEFAULT_MAX_FILE_BYTES = 100 * 1024 * 1024
# The bundled samples have at most a few hundred tables. The byte cap above
# is what actually protects memory, so this stays generous (round-1 review,
# #142): 50,000 leaves ample headroom for a very large real-world schema.
DEFAULT_MAX_TABLES = 50_000
# #384 proposes raising every collector's default query cap to 10,000; this
# ceiling must stay comfortably above that.
DEFAULT_MAX_QUERIES = 20_000


class IngestionLimitError(ValueError):
    """Raised when an uploaded collection exceeds a configured ingestion limit."""


@dataclass(frozen=True)
class IngestionLimits:
    """Configurable ceilings checked before and during ingestion."""

    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES
    max_tables: int = DEFAULT_MAX_TABLES
    max_queries: int = DEFAULT_MAX_QUERIES

    @classmethod
    def from_env(cls) -> IngestionLimits:
        """Build limits from the environment, falling back to the defaults
        above for any variable that is unset, non-numeric, or non-positive."""
        return cls(
            max_file_bytes=_env_int(ENV_MAX_FILE_BYTES, DEFAULT_MAX_FILE_BYTES),
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


def check_file_size(size_bytes: int, *, source: str, limits: IngestionLimits | None = None) -> None:
    """Reject a file before it is read/parsed if it exceeds the size ceiling.

    Raises:
        IngestionLimitError: ``size_bytes`` exceeds ``limits.max_file_bytes``.
    """
    limits = limits or IngestionLimits.from_env()
    if size_bytes > limits.max_file_bytes:
        raise IngestionLimitError(
            f"{source}: upload is {size_bytes:,} bytes, which exceeds the "
            f"{limits.max_file_bytes:,}-byte ingestion limit. Set "
            f"{ENV_MAX_FILE_BYTES} to raise it."
        )


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
