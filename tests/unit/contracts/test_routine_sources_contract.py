# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The routine sources contract (#1652): each routine's exact CREATE, in chunks of at
most 5,000 routines, listed by an index."""

from __future__ import annotations

import hashlib

import pytest
from pydantic import ValidationError

from src.contracts.routine_sources import (
    MAX_CHUNK_SOURCES,
    RoutineSource,
    RoutineSourcesChunk,
    RoutineSourcesContract,
)
from src.shared.contract_versioning import (
    contract_for_artifact,
    current_version,
    stamp_artifact_version,
)

_CREATE = (
    "CREATE OR REPLACE FUNCTION public.f()\n RETURNS integer\n LANGUAGE sql\n"
    "AS $function$SELECT 1$function$\n"
)


def _source(**extra) -> RoutineSource:
    return RoutineSource(
        **{"kind": "function", "routine_id": "f", "schema_name": "public",
           "specific_name": "f_16390", "create_statement": _CREATE,
           "origin": "pg_get_functiondef", **extra}
    )  # fmt: skip


def _part(path: str = "routine_sources/part-0001.json.gz", count: int = 1) -> dict:
    return {"path": path, "count": count, "sha256": "0" * 64}


def test_a_routine_source_hashes_its_create():
    assert _source().sha256 == hashlib.sha256(_CREATE.encode("utf-8")).hexdigest()


def test_a_routine_source_refuses_the_hash_of_another_text():
    with pytest.raises(ValidationError, match="sha256"):
        _source(sha256="0" * 64)


def test_a_chunk_keeps_one_entry_per_routine_and_at_most_the_cap():
    with pytest.raises(ValidationError, match="two entries"):
        RoutineSourcesChunk(part=1, sources=[_source(), _source()])
    both = RoutineSourcesChunk(part=1, sources=[_source(), _source(specific_name="f_16391")])
    assert len(both.sources) == 2
    too_many = [_source(specific_name=f"f_{i}") for i in range(MAX_CHUNK_SOURCES + 1)]
    with pytest.raises(ValidationError):
        RoutineSourcesChunk(part=1, sources=too_many)


def test_the_index_counts_add_up_and_name_each_chunk_once():
    index = RoutineSourcesContract(
        job_id="j1", source_engine="mysql", total=3,
        parts=[_part(count=2), _part("routine_sources/part-0002.json.gz", 1)],
    )  # fmt: skip
    assert index.total == 3
    with pytest.raises(ValidationError, match="add up"):
        RoutineSourcesContract(job_id="j1", source_engine="mysql", total=4, parts=[_part()])
    with pytest.raises(ValidationError, match="same chunk"):
        RoutineSourcesContract(
            job_id="j1", source_engine="mysql", total=2, parts=[_part(), _part()]
        )


def test_a_part_path_stays_in_the_collector_folder():
    with pytest.raises(ValidationError):
        RoutineSourcesContract(
            job_id="j1", source_engine="mysql", total=1, parts=[_part("../../x.json.gz")]
        )


def test_only_postgresql_and_mysql_are_carried():
    with pytest.raises(ValidationError):
        RoutineSourcesContract(job_id="j1", source_engine="sqlserver", total=1, parts=[_part()])


def test_the_index_path_is_a_registered_contract():
    stamped = stamp_artifact_version("shop/j1/collector/routine_sources.json", {})
    assert stamped["contract_version"] == current_version(RoutineSourcesContract)
    assert contract_for_artifact("shop/j1/collector/routine_sources.json") is RoutineSourcesContract
