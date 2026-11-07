# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Which routine sources are exact, a cut parameter list, re-runs and the opt-out on
every engine (#1652, review of #2009)."""

from __future__ import annotations

import copy
import json
import logging

import pytest

from src.agents.collector import handler, mysql_collector, oracle_collector
from src.agents.collector.mysql_routine_create import rebuild_exactness
from src.agents.collector.offline_ingest import build_offline_contract
from src.agents.collector.routine_sources import (
    build_routine_sources,
    clear_routine_sources,
    drop_routine_bodies,
    read_routine_sources,
    routine_capture,
    routine_sources_from_collection,
    write_routine_sources,
)
from src.contracts.routine_sources import RoutineSource
from src.storage.local_store import LocalArtifactStore
from src.tools.database.offline_parser import parse_offline_collection
from src.tools.database.oracle_tools import OracleRemoteCollector
from src.tools.database.sqlserver_tools import SQLServerRemoteCollector
from tests.unit.agents.routine_collections import SEED, mysql_collection

_OUTPUT = {"metadata": {"source_database": {"engine": "mysql", "version": "8.0.36"}}}


def _function(body: str, sql_mode: str = "STRICT_TRANS_TABLES") -> dict:
    return {
        "routine_name": "f", "schema_name": "shop", "specific_name": "f",
        "routine_type": "FUNCTION", "definer": "app@%", "security_type": "DEFINER",
        "is_deterministic": "YES", "sql_data_access": "NO SQL", "routine_comment": "",
        "external_language": "SQL", "sql_mode": sql_mode, "body_status": "collected",
        "definition": body,
    }  # fmt: skip


_RETURNS = [{"routine_name": "f", "schema_name": "shop", "routine_type": "FUNCTION",
             "ordinal": 0, "dtd_identifier": "int"}]  # fmt: skip


# -- a rebuild is exact only around a body with no literal -------------------------


# The reviewer's three cases, as information_schema gives their bodies back: the
# introducer gone, so the rebuilt routine can return something else.
@pytest.mark.parametrize(
    "body",
    [
        "RETURN 'Admin' = 'admin'",  # was RETURN _binary'Admin' = 'admin'
        "BEGIN DECLARE n INT; SELECT COUNT(*) INTO n FROM users WHERE u = 'Admin'; RETURN n; END",
        "RETURN HEX('')",  # was RETURN HEX(_utf16'a')
        "RETURN 0x41 = 'a'",
        'RETURN "x"',
    ],
)
def test_a_rebuild_around_a_literal_is_not_exact(body):
    assert rebuild_exactness(body, "STRICT_TRANS_TABLES") is not None
    build = build_routine_sources("mysql", procedures=[_function(body)], triggers=[],
                                  parameters=_RETURNS)  # fmt: skip
    (source,) = build.sources
    assert (source.origin, source.exact) == ("information_schema", False)
    assert "SHOW CREATE" in source.inexact_reason
    assert build.exact == {("function", "shop", "f"): False}


@pytest.mark.parametrize(
    ("body", "mode"), [("RETURN 1", ""), ("RETURN p + 1", ""), ('RETURN "id"', "ANSI_QUOTES")]
)
def test_a_rebuild_around_no_literal_is_exact(body, mode):
    (source,) = build_routine_sources(
        "mysql", procedures=[_function(body, mode)], triggers=[], parameters=_RETURNS
    ).sources
    assert (source.exact, source.inexact_reason) == (True, None)


def test_the_servers_own_text_is_exact_whatever_it_holds():
    row = {**_function("RETURN 'Admin' = 'admin'"),
           "show_create": "CREATE DEFINER=`app`@`%` FUNCTION `f`() RETURNS int\n"
                          "    NO SQL\n    DETERMINISTIC\nRETURN _binary'Admin' = 'admin'"}  # fmt: skip
    (source,) = build_routine_sources("mysql", procedures=[row], triggers=[], parameters=[]).sources
    assert (source.origin, source.exact) == ("show_create", True)
    assert source.create_statement == row["show_create"]


def test_the_collection_marks_which_bodies_are_exact():
    out = build_offline_contract(
        parse_offline_collection(mysql_collection()),
        _input(),
    ).model_dump(mode="json")
    procs = {
        (p["procedure_type"], p["procedure_name"]): p for p in out["database_schema"]["procedures"]
    }
    assert procs[("PROCEDURE", "place_order")]["body_exact"] is False  # literals in its body
    assert procs[("FUNCTION", "place_order")]["body_exact"] is True  # RETURN 1
    assert procs[("PROCEDURE", "secret_proc")]["body_exact"] is None  # no CREATE kept
    (event,) = out["database_schema"]["events"]
    assert event["body_exact"] is False


def _input():
    from src.contracts.collector_input import CollectorInput

    return CollectorInput.model_validate(
        {"job_id": "j1", "engine": "mysql", "cluster_endpoint": "offline", "port": 3306,
         "database_name": "shop", "mode": "offline",
         "offline_config": {"s3_bucket": "b", "s3_key": "k"}}
    )  # fmt: skip


class _LiveDb:
    def __init__(self, raw: dict, shown: dict[tuple[str, str], str]):
        self.raw, self.shown = raw, shown
        self.asked: list[tuple[str, str]] = []

    def collect_procedures(self, include_bodies: bool = True) -> list[dict]:
        return copy.deepcopy(self.raw["procedures"])

    def collect_triggers(self, include_bodies: bool = True) -> list[dict]:
        return copy.deepcopy(self.raw["triggers"])

    def collect_events(self, include_bodies: bool = True) -> list[dict]:
        return copy.deepcopy(self.raw["events"])

    def collect_routine_parameters(self) -> list[dict]:
        return copy.deepcopy(self.raw["routine_parameters"])

    def show_create(self, kind: str, name: str) -> str | None:
        self.asked.append((kind, name))
        return self.shown.get((kind, name))


def test_the_live_collector_reads_show_create_and_rebuilds_only_where_it_may_not():
    raw = mysql_collection()
    shown = {("procedure", "place_order"): f"CREATE PROCEDURE `place_order`() SELECT '{SEED}'"}
    db = _LiveDb(raw, shown)
    rows = mysql_collector._collect_routine_creates_raw(db, include_bodies=True)
    assert ("procedure", "secret_proc") not in db.asked  # not readable: nothing to show
    assert {("procedure", "place_order"), ("function", "place_order"), ("trigger", "tg_a"),
            ("event", "purge")} <= set(db.asked)  # fmt: skip
    build = build_routine_sources("mysql", **rows)
    by_kind = {(s.kind, s.specific_name): s for s in build.sources}
    assert by_kind[("procedure", "place_order")].origin == "show_create"
    assert by_kind[("procedure", "place_order")].exact is True
    assert by_kind[("trigger", "orders.tg_a")].origin == "information_schema"
    assert by_kind[("trigger", "orders.tg_a")].exact is False  # 'new' in its body


def test_the_live_mysql_reader_decodes_the_clients_batch_escapes():
    from src.tools.database.mysql_tools import MySQLRemoteCollector

    db = MySQLRemoteCollector.__new__(MySQLRemoteCollector)
    db._query = lambda sql: [  # type: ignore[method-assign]
        {"create procedure": "CREATE PROCEDURE `p`()\\nBEGIN\\n  SELECT 'a\\\\\\\\b';\\nEND"}
    ]
    assert (
        db.show_create("procedure", "p") == "CREATE PROCEDURE `p`()\nBEGIN\n  SELECT 'a\\\\b';\nEND"
    )
    assert db.show_create("package", "p") is None

    def refused(sql: str) -> list[dict]:
        raise RuntimeError("SHOW CREATE denied")

    db._query = refused  # type: ignore[method-assign]
    assert db.show_create("trigger", "t") is None


# -- a cut parameter list ----------------------------------------------------------


@pytest.mark.parametrize("cut", [None, "missing"])
def test_a_cut_parameter_list_rebuilds_no_routine(cut, caplog):
    raw = mysql_collection()
    if cut is None:
        raw["routine_parameters"] = None
    else:
        raw.pop("routine_parameters")
    build = routine_sources_from_collection(raw)
    assert {s.kind for s in build.sources} == {"trigger", "event"}
    assert build.statuses[("procedure", "shop", "place_order")] == "not_readable"
    assert "parameter list came back cut" in build.reasons[("procedure", "shop", "place_order")]
    with caplog.at_level(logging.WARNING):
        out = build_offline_contract(parse_offline_collection(raw), _input()).model_dump(
            mode="json"
        )
    place = next(p for p in out["database_schema"]["procedures"]
                 if p["procedure_name"] == "place_order" and p["procedure_type"] == "PROCEDURE")  # fmt: skip
    assert place["body_status"] == "not_readable"
    assert "parameter list came back cut" in place["body_status_reason"]
    assert out["metadata"]["routine_capture"]["parameters_counted"] == 3
    assert "routine parameters of the 3 the server counts" in caplog.text


def test_a_server_with_no_parameters_is_not_a_cut_list():
    capture = routine_capture({"parameters_counted": 0}, listed=1, parameters=[])
    assert capture["parameters_counted"] == 0


def test_include_bodies_reads_false_in_any_spelling():
    for off in (False, "false", "0", 0):
        assert routine_capture({"include_bodies": off}, listed=0)["include_bodies"] is False
    assert routine_capture({"include_bodies": "true"}, listed=0)["include_bodies"] is True


# -- re-runs replace or clear the routine sources ----------------------------------


def _sources(n: int) -> list[RoutineSource]:
    return [RoutineSource(kind="event", routine_id=f"e{i}", schema_name="shop",
                          specific_name=f"e{i}", create_statement=f"CREATE EVENT e{i} '{SEED}'",
                          origin="information_schema") for i in range(n)]  # fmt: skip


def _files(root) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


def test_a_rerun_with_no_sources_clears_the_earlier_ones(tmp_path):
    store = LocalArtifactStore(base_dir=str(tmp_path))
    write_routine_sources(store, "shop", "j1", _OUTPUT, _sources(3), chunk_size=2)
    assert write_routine_sources(store, "shop", "j1", _OUTPUT, []) is None
    assert _files(tmp_path) == []
    assert list(read_routine_sources(store, "shop", "j1")) == []


def test_a_failed_write_leaves_no_index_and_no_literal(tmp_path):
    store = LocalArtifactStore(base_dir=str(tmp_path))
    write_routine_sources(store, "shop", "j1", _OUTPUT, _sources(3), chunk_size=1)
    with pytest.raises(ValueError):
        write_routine_sources(store, "shop", "j1", _OUTPUT, [*_sources(1), *_sources(1)],
                              chunk_size=1)  # fmt: skip
    assert _files(tmp_path) == []


class _NoDeleteStore(LocalArtifactStore):
    def delete(self, path: str) -> bool:
        return False  # ArtifactStore's default: a store that cannot delete


def test_a_store_that_cannot_delete_keeps_an_empty_index_and_no_literal(tmp_path):
    store = _NoDeleteStore(base_dir=str(tmp_path))
    write_routine_sources(store, "shop", "j1", _OUTPUT, _sources(3), chunk_size=2)
    clear_routine_sources(store, "shop", "j1")
    assert list(read_routine_sources(store, "shop", "j1")) == []
    assert not any(SEED in (tmp_path / f).read_bytes().decode("latin-1") for f in _files(tmp_path))


def test_a_rerun_with_the_opt_out_clears_the_earlier_routine_sources(tmp_path, monkeypatch):
    from tests.unit.agents.test_routine_sources_wiring import _handler_env, _offline_dispatch

    _handler_env(monkeypatch, "offline")
    raw = mysql_collection()
    monkeypatch.setattr(handler, "_offline_collection", lambda bucket, region: copy.deepcopy(raw))
    monkeypatch.setattr(handler, "_dispatch_collect", _offline_dispatch)
    store = LocalArtifactStore(base_dir=str(tmp_path))
    handler.run_collector("j1", "shop", store)
    assert store.exists("shop/j1/collector/routine_sources.json")
    monkeypatch.setenv("INCLUDE_ROUTINE_BODIES", "0")
    handler.run_collector("j1", "shop", store)
    assert not store.exists("shop/j1/collector/routine_sources.json")
    assert not list((tmp_path / "shop/j1/collector").rglob("*.gz"))


# -- the opt-out on every engine ---------------------------------------------------


def test_sqlserver_live_reads_no_body_when_opted_out():
    db = SQLServerRemoteCollector.__new__(SQLServerRemoteCollector)
    seen: list[str] = []
    db._query = lambda sql: seen.append(sql) or []  # type: ignore[method-assign]
    db.collect_procedures(include_bodies=False)
    db.collect_triggers(include_bodies=False)
    assert all("CASE WHEN 0 = 1 THEN OBJECT_DEFINITION" in sql for sql in seen)
    assert all("CASE WHEN 0 = 0 THEN 'opted_out' END AS body_status" in sql for sql in seen)


def test_oracle_live_reads_no_body_when_opted_out():
    class Db:
        def collect_tables(self):
            return []

        def collect_procedures(self):
            return [{"routine_name": "p", "schema_name": "app", "routine_type": "PROCEDURE"}]

        def collect_routine_sources(self):
            raise AssertionError("read although the caller opted out")

        def __getattr__(self, name):
            return lambda *a, **k: []

    raw = oracle_collector._collect_schema_raw(Db(), include_bodies=False)
    (p,) = oracle_collector._build_procedures(raw["procedures"])
    assert (p.definition, p.body_status.value) == (None, "opted_out")
    assert OracleRemoteCollector  # the live tool reads ALL_SOURCE only when asked


def test_the_handler_drops_every_engines_bodies_when_opted_out(tmp_path, monkeypatch):
    from tests.unit.agents.test_routine_sources_wiring import _handler_env, _offline_dispatch

    raw = {
        "metadata": {"database_name": "shop", "engine": "sqlserver"},
        "tables": [{"table_name": "orders", "schema_name": "dbo", "row_count": 1}],
        "columns": [{"table_name": "orders", "column_name": "id", "data_type": "int",
                     "is_nullable": "NO", "ordinal_position": 1}],
        "queries": [],
        "procedures": [{"routine_name": "usp", "schema_name": "dbo", "routine_type": "PROCEDURE",
                        "definition": "CREATE PROCEDURE dbo.usp AS SELECT 1"}],
        "triggers": [{"trigger_name": "tr", "table_name": "orders", "schema_name": "dbo",
                      "timing": "AFTER", "events": "INSERT",
                      "definition": "CREATE TRIGGER tr ON dbo.orders AFTER INSERT AS SELECT 1"}],
    }  # fmt: skip
    _handler_env(monkeypatch, "offline")
    monkeypatch.setenv("INCLUDE_ROUTINE_BODIES", "0")
    monkeypatch.setattr(handler, "_offline_collection", lambda bucket, region: copy.deepcopy(raw))
    monkeypatch.setattr(handler, "_dispatch_collect", _offline_dispatch)
    store = LocalArtifactStore(base_dir=str(tmp_path))
    handler.run_collector("j1", "shop", store)
    schema = store.read_json("shop/j1/collector/output.json")["database_schema"]
    for row in schema["procedures"] + schema["triggers"]:
        assert (row["definition"], row["body_status"]) == (None, "opted_out")
    assert "CREATE PROCEDURE" not in json.dumps(schema)


def test_dropping_bodies_leaves_a_collection_with_no_routines_as_it_is():
    out = {"database_schema": {"tables": [], "procedures": None}}
    drop_routine_bodies(out)
    assert out == {"database_schema": {"tables": [], "procedures": None}}


def test_a_clear_always_ends_whatever_the_store_answers():
    from unittest.mock import MagicMock

    store = MagicMock()  # every answer truthy, never a plain True
    clear_routine_sources(store, "shop", "j1")
    write_routine_sources(store, "shop", "j1", _OUTPUT, [])
    assert store.exists.call_count < 10


# -- MariaDB moves to Aurora MySQL: its routines are carried as MySQL's -----------


def _mariadb_collection() -> dict:
    raw = mysql_collection()
    raw["metadata"] = {**raw["metadata"], "version": "10.5.29-MariaDB"}
    return raw


def test_a_mariadb_collection_carries_its_routines_as_mysqls(tmp_path):
    from src.agents.collector.offline_ingest import build_offline_collector_output
    from src.tools.database.offline_parser import detect_source_engine

    raw = _mariadb_collection()
    assert detect_source_engine(raw["metadata"])[0] == "mariadb"
    build = routine_sources_from_collection(raw)
    assert sorted((s.kind, s.specific_name) for s in build.sources) == [
        ("event", "purge"), ("function", "place_order"), ("procedure", "place_order"),
        ("trigger", "orders.tg_a"), ("trigger", "orders.tg_b"),
    ]  # fmt: skip
    by_kind = {(s.kind, s.specific_name): s for s in build.sources}
    assert by_kind[("procedure", "place_order")].exact is False  # literals in its body
    assert by_kind[("function", "place_order")].exact is True  # RETURN 1
    assert build.statuses == {("function", "shop", "js_label"): "not_supported"}

    output = build_offline_collector_output(
        parse_offline_collection(copy.deepcopy(raw)), job_id="j1", database_name="shop", s3_key="k"
    )
    assert output["metadata"]["source_database"]["engine"] == "mariadb"
    store = LocalArtifactStore(base_dir=str(tmp_path))
    key = write_routine_sources(store, "shop", "j1", output, build.sources)
    assert store.read_json(key)["source_engine"] == "mariadb"
    assert len(list(read_routine_sources(store, "shop", "j1"))) == 5
    secret = next(
        p for p in output["database_schema"]["procedures"] if p["procedure_name"] == "secret_proc"
    )
    assert "SHOW_ROUTINE" in secret["body_status_reason"]


def test_the_live_mariadb_collector_reads_show_create_too():
    raw = _mariadb_collection()
    shown = {("procedure", "place_order"): f"CREATE PROCEDURE `place_order`() SELECT '{SEED}'"}
    sources = mysql_collector._live_routines(
        _LiveDb(raw, shown), copy.deepcopy(raw), include_bodies=True, engine="mariadb"
    )
    by_kind = {(s.kind, s.specific_name): s for s in sources}
    assert (by_kind[("procedure", "place_order")].origin, by_kind[("procedure", "place_order")].exact) == (
        "show_create", True,
    )  # fmt: skip
