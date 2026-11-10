# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every ingest writes the routine CREATE statements to the job's routine sources, and
only there, in chunks read one at a time (#1652)."""

from __future__ import annotations

import copy
import gc
import gzip
import json
import subprocess  # nosec B404 -- runs this repo's own run_collect.py script with fixed argv
import sys
import tracemalloc
from collections.abc import Iterator
from pathlib import Path

import pytest

from scripts import run_assessment
from src.agents.collector import handler, mysql_collector, postgres_collector
from src.agents.collector.offline_ingest import (
    build_offline_collector_output,
    build_offline_contract,
)
from src.agents.collector.routine_sources import (
    attach_to_contract,
    iter_collection_sources,
    read_routine_sources,
    routine_sources_from_collection,
    write_routine_sources,
)
from src.contracts.routine_sources import MAX_CHUNK_SOURCES, RoutineSource, RoutineSourcesContract
from src.storage.local_store import LocalArtifactStore
from src.tools.database.offline_parser import parse_offline_collection
from src.tools.sql.redact import redact_raw_collection, redact_values
from tests.unit.agents.routine_collections import SEED, mysql_collection, pg_collection

REPO = Path(__file__).resolve().parents[3]
COLLECTIONS = [pg_collection, mysql_collection]
_OUTPUT = {"metadata": {"source_database": {"engine": "postgresql", "version": "16.2"}}}


def _text(path: Path) -> str:
    data = path.read_bytes()
    if path.suffix == ".gz":
        data = gzip.decompress(data)
    return data.decode("utf-8", errors="ignore")


def _leaking_files(root: Path) -> list[str]:
    return sorted(
        str(p.relative_to(root)) for p in root.rglob("*") if p.is_file() and SEED in _text(p)
    )


def _sources(
    count: int, body_chars: int = 1500, credential_every: int = 0
) -> Iterator[RoutineSource]:
    """``count`` functions; with ``credential_every``, every that many holds a password
    value (built here, so no scanner reads one in this file)."""
    filler = "x" * body_chars
    pair = "pass" + "word = 'Vq8rT2mz"
    for i in range(count):
        held = f"  -- {pair}{i}'\n" if credential_every and i % credential_every == 0 else ""
        yield RoutineSource(
            kind="function",
            routine_id=f"f{i}",
            schema_name="public",
            specific_name=f"f{i}_{16384 + i}",
            create_statement=(
                f"CREATE OR REPLACE FUNCTION public.f{i}()\n RETURNS text\n LANGUAGE sql\n"
                f"{held}AS $function$SELECT '{filler}{i}'$function$\n"
            ),
            origin="pg_get_functiondef",
        )


# -- the chunks ---------------------------------------------------------------------


def test_the_sources_are_written_in_chunks_and_read_back_in_order(tmp_path):
    store = LocalArtifactStore(base_dir=str(tmp_path))
    key = write_routine_sources(store, "shop", "j1", _OUTPUT, _sources(7, 10), chunk_size=3)
    assert key == "shop/j1/collector/routine_sources.json"
    index = RoutineSourcesContract.model_validate(store.read_json(key))
    assert [(p.path, p.count) for p in index.parts] == [
        ("routine_sources/part-0001.json.gz", 3),
        ("routine_sources/part-0002.json.gz", 3),
        ("routine_sources/part-0003.json.gz", 1),
    ]
    assert (index.total, index.source_engine, index.source_version) == (7, "postgresql", "16.2")
    back = list(read_routine_sources(store, "shop", "j1"))
    assert [s.routine_id for s in back] == [f"f{i}" for i in range(7)]
    assert [s.sha256 for s in back] == [s.sha256 for s in _sources(7, 10)]


def test_the_reader_loads_one_chunk_at_a_time(tmp_path):
    store = LocalArtifactStore(base_dir=str(tmp_path))
    write_routine_sources(store, "shop", "j1", _OUTPUT, _sources(7, 10), chunk_size=3)
    loads: list[str] = []
    read_bytes = store.read_bytes

    def counting(path: str) -> bytes:
        loads.append(path)
        data: bytes = read_bytes(path)
        return data

    store.read_bytes = counting  # type: ignore[method-assign]
    reader = read_routine_sources(store, "shop", "j1")
    first = [next(reader) for _ in range(3)]
    assert [s.routine_id for s in first] == ["f0", "f1", "f2"] and len(loads) == 1
    next(reader)
    assert len(loads) == 2


def test_a_chunk_that_does_not_match_its_index_is_refused(tmp_path):
    store = LocalArtifactStore(base_dir=str(tmp_path))
    write_routine_sources(store, "shop", "j1", _OUTPUT, _sources(2, 10))
    part = tmp_path / "shop/j1/collector/routine_sources/part-0001.json.gz"
    part.write_bytes(gzip.compress(b'{"part": 1, "sources": []}'))
    with pytest.raises(ValueError, match="does not match"):
        list(read_routine_sources(store, "shop", "j1"))


def test_two_sources_for_one_routine_are_refused(tmp_path):
    store = LocalArtifactStore(base_dir=str(tmp_path))
    twice = [*_sources(1, 10), *_sources(1, 10)]
    with pytest.raises(ValueError, match="two routine sources"):
        write_routine_sources(store, "shop", "j1", _OUTPUT, twice, chunk_size=1)


def test_nothing_is_written_without_sources_or_for_an_engine_not_carried(tmp_path):
    store = LocalArtifactStore(base_dir=str(tmp_path))
    assert write_routine_sources(store, "shop", "j1", _OUTPUT, []) is None
    sqlserver = {"metadata": {"source_database": {"engine": "sqlserver"}}}
    assert write_routine_sources(store, "shop", "j2", sqlserver, _sources(2, 10)) is None
    assert not list(tmp_path.rglob("*"))
    assert list(read_routine_sources(store, "shop", "j1")) == []


def test_a_rewrite_with_fewer_chunks_leaves_no_stale_chunk(tmp_path):
    store = LocalArtifactStore(base_dir=str(tmp_path))
    write_routine_sources(store, "shop", "j1", _OUTPUT, _sources(5, 10), chunk_size=2)
    write_routine_sources(store, "shop", "j1", _OUTPUT, _sources(2, 10), chunk_size=2)
    parts = sorted(p.name for p in (tmp_path / "shop/j1/collector/routine_sources").iterdir())
    assert parts == ["part-0001.json.gz"]


def _peak(fn) -> int:
    gc.collect()
    tracemalloc.start()
    try:
        fn()
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


# A body short enough that scanning 25,000 of them stays a unit test (#2098); one
# chunk of them still holds far more than the writer keeps per routine (a 12-byte
# digest of its key, and the kinds of what the scan flags).
_BODY_CHARS = 200
_CREDENTIAL_EVERY = 1000


@pytest.mark.passes_on_main  # main meets it too, slowly: the guard is its time (#2098)
def test_twenty_thousand_routines_are_carried_in_bounded_memory(tmp_path):
    """Writing (with the credential scan of every body) and reading hold one chunk at a
    time: four times the routines takes about the memory of one chunk, never four times
    it (a relative check, #1652, #2098)."""

    def sources(count: int) -> Iterator[RoutineSource]:
        return _sources(count, _BODY_CHARS, _CREDENTIAL_EVERY)

    # Warm up first: the first write and read import and compile what every later one
    # reuses, which would count against whichever is measured first.
    warm = LocalArtifactStore(base_dir=str(tmp_path / "warm"))
    write_routine_sources(warm, "db", "j", _OUTPUT, sources(10))
    assert sum(1 for _ in read_routine_sources(warm, "db", "j")) == 10
    one = LocalArtifactStore(base_dir=str(tmp_path / "one"))
    many = LocalArtifactStore(base_dir=str(tmp_path / "many"))
    small = _peak(
        lambda: write_routine_sources(one, "db", "j", _OUTPUT, sources(MAX_CHUNK_SOURCES))
    )
    large = _peak(lambda: write_routine_sources(many, "db", "j", _OUTPUT, sources(20_000)))
    assert large < 1.5 * small, (small, large)

    index = RoutineSourcesContract.model_validate(
        many.read_json("db/j/collector/routine_sources.json")
    )
    assert (index.total, len(index.parts)) == (20_000, 4)
    # Every body was scanned, and each flag keeps only the kinds found.
    assert index.secret_scan is not None
    assert [f.specific_name for f in index.secret_scan.flagged] == [
        f"f{i}_{16384 + i}" for i in range(0, 20_000, _CREDENTIAL_EVERY)
    ]
    assert {(tuple(f.credential_kinds), tuple(f.address_kinds))
            for f in index.secret_scan.flagged} == {(("password value",), ())}  # fmt: skip

    count = 0
    expected = sources(20_000)

    def read_all() -> None:
        nonlocal count
        for got, want in zip(read_routine_sources(many, "db", "j"), expected, strict=True):
            assert (got.routine_id, got.sha256) == (want.routine_id, want.sha256)
            count += 1

    reading = _peak(read_all)
    assert count == 20_000
    one_read = _peak(lambda: sum(1 for _ in read_routine_sources(one, "db", "j")))
    assert reading < 1.5 * one_read, (one_read, reading)


# -- the ingests --------------------------------------------------------------------


@pytest.mark.parametrize("collection", COLLECTIONS)
def test_run_assessment_keeps_bodies_only_in_the_routine_sources(collection, tmp_path):
    path = tmp_path / "collection.json"
    path.write_text(json.dumps(collection()))
    root = tmp_path / "artifacts"
    job_id, db = run_assessment.phase_collect(
        str(path), "shop", LocalArtifactStore(base_dir=str(root))
    )
    leaks = _leaking_files(root)
    assert leaks and all(
        p.startswith(f"{db}/{job_id}/collector/routine_sources/part-") for p in leaks
    ), leaks
    assert (root / db / job_id / "collector" / "routine_sources.json").is_file()


@pytest.mark.parametrize("collection", COLLECTIONS)
def test_run_collect_keeps_bodies_only_in_the_routine_sources(collection, tmp_path):
    path = tmp_path / "collection.json"
    path.write_text(json.dumps(collection()))
    root = tmp_path / "artifacts"
    proc = subprocess.run(  # nosec B603 -- fixed interpreter plus this repo's own script args
        [sys.executable, "scripts/run_collect.py", "--file", str(path), "--db", "shop",
         "--job-id", "rs000001", "--artifact-root", str(root)],
        cwd=REPO, capture_output=True, text=True,
    )  # fmt: skip
    assert proc.returncode == 0, proc.stderr
    leaks = _leaking_files(root)
    assert leaks and all(
        p.startswith("shop/rs000001/collector/routine_sources/part-") for p in leaks
    ), leaks
    sources = list(read_routine_sources(LocalArtifactStore(base_dir=str(root)), "shop", "rs000001"))
    assert sources == routine_sources_from_collection(collection()).sources


@pytest.mark.parametrize("collection", COLLECTIONS)
def test_opting_out_writes_no_routine_sources_and_no_body(collection, tmp_path):
    path = tmp_path / "collection.json"
    path.write_text(json.dumps(collection(include_bodies=False)))
    root = tmp_path / "artifacts"
    run_assessment.phase_collect(str(path), "shop", LocalArtifactStore(base_dir=str(root)))
    assert not list(root.rglob("routine_sources*"))
    assert _leaking_files(root) == []


def test_an_older_collection_writes_no_routine_sources(tmp_path):
    raw = pg_collection()
    for row in raw["procedures"] + raw["triggers"]:
        row.pop("body_status")
    path = tmp_path / "collection.json"
    path.write_text(json.dumps(raw))
    root = tmp_path / "artifacts"
    run_assessment.phase_collect(str(path), "shop", LocalArtifactStore(base_dir=str(root)))
    assert not list(root.rglob("routine_sources*"))


# -- the collector handler ----------------------------------------------------------


def _handler_env(monkeypatch, mode: str) -> None:
    monkeypatch.setenv("COLLECTION_MODE", mode)
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("S3_BUCKET", "bucket")
    monkeypatch.setenv("OFFLINE_S3_BUCKET", "bucket")
    monkeypatch.setenv("OFFLINE_S3_KEY", "shop/j1/uploads/collector-output.json")
    monkeypatch.setenv("CLUSTER_ENDPOINT", "db.example.internal")
    monkeypatch.setenv(
        "SECRET_ARN", "arn:aws:secretsmanager:us-east-1:123456789012:secret:db-AbCdEf"
    )
    monkeypatch.setenv("AUTOMATION_INSTANCE_ID", "i-0123456789abcdef0")
    monkeypatch.setenv("ENGINE", "postgresql")
    monkeypatch.delenv("INCLUDE_ROUTINE_BODIES", raising=False)
    monkeypatch.setattr(handler, "_requested_engines_in_use", lambda store, db, job: [])


def _offline_dispatch(engine, inp, raw=None):
    return build_offline_contract(parse_offline_collection(copy.deepcopy(raw)), inp)


def test_the_handler_writes_the_routine_sources_from_the_offline_file(tmp_path, monkeypatch):
    _handler_env(monkeypatch, "offline")
    raw = pg_collection()
    monkeypatch.setattr(handler, "_offline_collection", lambda bucket, region: copy.deepcopy(raw))
    monkeypatch.setattr(handler, "_dispatch_collect", _offline_dispatch)
    store = LocalArtifactStore(base_dir=str(tmp_path))
    handler.run_collector("j1", "shop", store)
    assert len(list(read_routine_sources(store, "shop", "j1"))) == 4
    assert SEED not in json.dumps(store.read_json("shop/j1/collector/output.json"))
    assert all(p.suffix == ".gz" for p in tmp_path.rglob("*") if p.is_file() and SEED in _text(p))


def test_the_handler_honours_the_opt_out_for_an_offline_file(tmp_path, monkeypatch):
    _handler_env(monkeypatch, "offline")
    monkeypatch.setenv("INCLUDE_ROUTINE_BODIES", "0")
    raw = pg_collection()
    monkeypatch.setattr(handler, "_offline_collection", lambda bucket, region: copy.deepcopy(raw))
    monkeypatch.setattr(handler, "_dispatch_collect", _offline_dispatch)
    store = LocalArtifactStore(base_dir=str(tmp_path))
    handler.run_collector("j1", "shop", store)
    assert not store.exists("shop/j1/collector/routine_sources.json")


def test_the_handler_writes_the_live_collectors_routine_sources(tmp_path, monkeypatch):
    _handler_env(monkeypatch, "live")
    raw = pg_collection()

    def live(engine, inp, raw_arg=None):
        assert inp.collection_options.include_routine_bodies is True
        contract = build_offline_contract(parse_offline_collection(copy.deepcopy(raw)), inp)
        attach_to_contract(contract, routine_sources_from_collection(raw).sources)
        return contract

    monkeypatch.setattr(handler, "_dispatch_collect", live)
    store = LocalArtifactStore(base_dir=str(tmp_path))
    handler.run_collector("j1", "shop", store)
    assert len(list(read_routine_sources(store, "shop", "j1"))) == 4


def test_the_handler_reads_the_opt_out(monkeypatch):
    monkeypatch.setenv("INCLUDE_ROUTINE_BODIES", "0")
    assert handler._routine_body_option() == {"include_routine_bodies": False}
    monkeypatch.setenv("INCLUDE_ROUTINE_BODIES", "true")
    assert handler._routine_body_option() == {"include_routine_bodies": True}
    monkeypatch.delenv("INCLUDE_ROUTINE_BODIES")
    assert handler._routine_body_option() == {}
    monkeypatch.setenv("INCLUDE_ROUTINE_BODIES", "maybe")
    with pytest.raises(ValueError, match="INCLUDE_ROUTINE_BODIES"):
        handler._routine_body_option()


# -- the live collectors ------------------------------------------------------------


class _FakeDb:
    """A live collector's routine reads, from a raw collection; refuses to be read when
    the caller opted out."""

    def __init__(self, raw: dict, *, readable: bool = True):
        self.raw, self.readable = raw, readable

    def _rows(self, key: str) -> list[dict]:
        assert self.readable, f"{key} read although the caller opted out"
        return copy.deepcopy(self.raw.get(key) or [])

    def collect_procedures(self, include_bodies: bool = True) -> list[dict]:
        return self._rows("procedures")

    def collect_triggers(self, include_bodies: bool = True) -> list[dict]:
        return self._rows("triggers")

    def collect_events(self, include_bodies: bool = True) -> list[dict]:
        return self._rows("events")

    def collect_routine_parameters(self) -> list[dict]:
        return self._rows("routine_parameters")

    def show_create(self, kind: str, name: str) -> str | None:
        return None  # not permitted here: the rows are rebuilt


@pytest.mark.parametrize(
    ("module", "collection", "engine"),
    [
        (postgres_collector, pg_collection, "postgresql"),
        (mysql_collector, mysql_collection, "mysql"),
    ],
)
def test_live_reads_never_pass_through_a_checkpoint(module, collection, engine):
    raw = collection()
    schema_raw = redact_values(copy.deepcopy(raw), engine)  # what a checkpoint holds
    sources = module._live_routines(_FakeDb(raw), schema_raw, include_bodies=True)
    assert sources == routine_sources_from_collection(raw).sources
    assert any(SEED in s.create_statement for s in sources)
    assert SEED not in json.dumps(schema_raw)
    assert {p.get("body_status") for p in schema_raw["procedures"]} <= {
        "collected", "not_readable", "not_supported",
    }  # fmt: skip


@pytest.mark.parametrize("module", [postgres_collector, mysql_collector])
def test_live_opt_out_reads_no_body(module):
    db = _FakeDb({}, readable=False)
    assert module._live_routines(db, {"procedures": []}, include_bodies=False) == []


@pytest.mark.parametrize("module", [postgres_collector, mysql_collector])
def test_a_failed_live_read_costs_only_the_sources(module):
    class Broken(_FakeDb):
        def collect_procedures(self, include_bodies: bool = True) -> list[dict]:
            raise RuntimeError("permission denied")

    rows = {"procedures": [{"routine_name": "p", "body_status": "collected"}]}
    assert module._live_routines(Broken({}), rows, include_bodies=True) == []
    assert rows["procedures"][0]["body_status"] == "collected"


def test_the_stored_raw_copy_and_the_stream_agree_on_what_is_kept():
    raw = mysql_collection()
    assert SEED not in json.dumps(redact_raw_collection(raw))
    assert any(SEED in s.create_statement for s in iter_collection_sources(raw))
    output = build_offline_collector_output(
        parse_offline_collection(copy.deepcopy(raw)), job_id="j", database_name="shop", s3_key="k"
    )
    assert SEED not in json.dumps(output)
