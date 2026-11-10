# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A PostgreSQL DDL upload keeps each trigger's enable mode (#2071).

``pg_dump`` writes each trigger's ``CREATE TRIGGER`` and, when its mode is not the default
``O``, an ``ALTER TABLE ... DISABLE | ENABLE REPLICA | ENABLE ALWAYS TRIGGER`` line after
it. The DDL reader keeps the letter as ``enabled_mode``; a trigger whose mode the file does
not settle (an ALTER it cannot read, a name it cannot place) is left unknown, never ``O``.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from src.agents.collector import postgres_collector
from src.agents.collector.mysql_collector import NoopCheckpointStore
from src.contracts.collector_input import CollectorInput
from src.tools.database.ddl_parser import parse_ddl_routines

_ROOT = Path(__file__).resolve().parents[4]
_DUMP = _ROOT / "tests/fixtures/routines/postgresql/pg_dump_trigger_modes.sql"

_FN = "EXECUTE FUNCTION public.fn()"


def _modes(text: str, dialect: str = "postgresql") -> dict[str, str | None]:
    return {
        t["trigger_name"]: t.get("enabled_mode")
        for t in parse_ddl_routines(text, dialect)["triggers"]
    }


def _create(name: str, table: str = "public.orders") -> str:
    return f"CREATE TRIGGER {name} AFTER INSERT ON {table} FOR EACH ROW {_FN};\n"


def test_a_pg_dump_file_gives_each_trigger_its_mode():
    assert _modes(_DUMP.read_text()) == {
        "Line Audit": "A",  # quoted, in a quoted schema
        "t_a": "A",
        "t_d": "D",
        "t_o": "O",  # no ALTER: created enabled
        "t_r": "R",
    }


def test_a_ddl_upload_brings_the_modes_to_the_contract():
    inp = CollectorInput.model_validate(
        {"job_id": "j1", "engine": "postgresql", "cluster_endpoint": "db.example.internal",
         "port": 5432, "database_name": "shop", "mode": "ddl",
         "ddl_config": {"s3_bucket": "b", "s3_key": "schema.sql"}}
    )  # fmt: skip
    with (
        patch(
            "src.agents.collector.mysql_collector.fetch_ddl_from_s3",
            return_value=_DUMP.read_text(),
        ),
        patch.object(postgres_collector, "_collect_aws_raw", return_value={}),
    ):
        out = postgres_collector._collect_ddl(inp, NoopCheckpointStore())
    got = {t.trigger_name: (t.enabled_mode, t.is_enabled) for t in out.database_schema.triggers}
    # is_enabled keeps what the DDL path always gave: not reported.
    assert got == {
        "Line Audit": ("A", None), "t_a": ("A", None), "t_d": ("D", None),
        "t_o": ("O", None), "t_r": ("R", None),
    }  # fmt: skip


def test_all_and_user_set_every_trigger_already_on_the_table():
    text = (
        _create("a") + _create("b") + _create("other", "public.items")
        + "ALTER TABLE ONLY public.orders DISABLE TRIGGER ALL;\n"
        + _create("c")  # created after the ALTER: still O
        + "ALTER TABLE IF EXISTS public.items ENABLE ALWAYS TRIGGER USER;\n"
    )  # fmt: skip
    assert _modes(text) == {"a": "D", "b": "D", "other": "A", "c": "O"}


def test_names_are_matched_as_postgresql_folds_them():
    text = (
        _create('"All"') + _create("Mixed") + _create('"Mixed"', '"Sales"."Order Lines"')
        + 'ALTER TABLE public.orders ENABLE REPLICA TRIGGER "All";\n'
        + "ALTER TABLE PUBLIC.ORDERS DISABLE TRIGGER MIXED;\n"
        + 'ALTER TABLE "Sales"."Order Lines" ENABLE ALWAYS TRIGGER "Mixed";\n'
    )  # fmt: skip
    got = parse_ddl_routines(text, "postgresql")["triggers"]
    assert [(t["trigger_name"], t["table_name"], t["enabled_mode"]) for t in got] == [
        ("All", "orders", "R"),  # a quoted "All" is a name, not ALL
        ("Mixed", "orders", "D"),  # unquoted: folded to mixed on both sides
        ("Mixed", "Order Lines", "A"),
    ]


def test_several_actions_in_one_alter_and_the_last_one_wins():
    text = (
        _create("a") + _create("b")
        + "-- a comment before the statement\n"
        + "ALTER TABLE public.orders ENABLE ALWAYS TRIGGER a, DISABLE TRIGGER b;\n"
        + "ALTER TABLE public.orders ENABLE TRIGGER b;\n"
    )  # fmt: skip
    assert _modes(text) == {"a": "A", "b": "O"}


@pytest.mark.parametrize(
    "alter",
    [
        # unqualified ALTER, qualified CREATE: the search path decides, the file does not
        "ALTER TABLE orders ENABLE ALWAYS TRIGGER a;\n",
        # a trigger action it cannot read
        "ALTER TABLE public.orders ENABLE SOMETIMES TRIGGER a;\n",
        # a rename: the later ALTERs may name it by another name
        "ALTER TRIGGER a ON public.orders RENAME TO b;\n",
    ],
)
def test_a_mode_the_file_does_not_settle_is_unknown_never_o(alter):
    assert _modes(_create("a") + alter + _create("z", "public.items")) == {"a": None, "z": "O"}


def test_create_or_replace_of_an_existing_trigger_is_unknown():
    text = (
        _create("a") + "ALTER TABLE public.orders DISABLE TRIGGER a;\n"
        + _create("a").replace("CREATE TRIGGER", "CREATE OR REPLACE TRIGGER")
        + _create("b").replace("CREATE TRIGGER", "CREATE OR REPLACE TRIGGER")  # a new one: O
    )  # fmt: skip
    got = parse_ddl_routines(text, "postgresql")["triggers"]
    assert [(t["trigger_name"], t["enabled_mode"]) for t in got] == [
        ("a", None), ("a", None), ("b", "O"),
    ]  # fmt: skip


def test_an_alter_naming_no_collected_trigger_changes_nothing():
    text = "ALTER TABLE public.orders DISABLE TRIGGER missing;\n" + _create("a")
    assert _modes(text) == {"a": "O"}


@pytest.mark.passes_on_main
@pytest.mark.parametrize("dialect", ["mysql", "mariadb", "sqlserver", "oracle"])
def test_the_other_dialects_give_no_mode(dialect):
    text = "CREATE TRIGGER tg AFTER INSERT ON orders FOR EACH ROW BEGIN END;\n"
    (t,) = parse_ddl_routines(text, dialect)["triggers"]
    assert "enabled_mode" not in t


def test_a_fresh_pg_dump_gives_what_the_server_holds(tmp_path):
    """The fixture is pg_dump's own output: dump a server in each mode and compare with
    pg_trigger.tgenabled. Skips when pgserver is not installed or cannot start."""
    pgserver = pytest.importorskip("pgserver")
    try:
        srv = pgserver.get_server(tmp_path / "pgdata", cleanup_mode="delete")
    except Exception as exc:  # no bundled server for this platform, or it cannot start
        pytest.skip(f"pgserver cannot start a PostgreSQL here: {exc}")
    try:
        srv.psql(
            "\\set ON_ERROR_STOP on\n"
            'CREATE SCHEMA "Sales";\n'
            "CREATE TABLE public.orders (id int PRIMARY KEY);\n"
            'CREATE TABLE "Sales"."Order Lines" (id int);\n'
            "CREATE FUNCTION public.fn() RETURNS trigger LANGUAGE plpgsql AS"
            " $$BEGIN RETURN NULL; END$$;\n"
            + "".join(_create(n) for n in ("t_o", "t_d", "t_r", "t_a"))
            + _create('"Line Audit"', '"Sales"."Order Lines"')
            + "ALTER TABLE public.orders DISABLE TRIGGER t_d;\n"
            "ALTER TABLE public.orders ENABLE REPLICA TRIGGER t_r;\n"
            "ALTER TABLE public.orders ENABLE ALWAYS TRIGGER t_a;\n"
            'ALTER TABLE "Sales"."Order Lines" ENABLE ALWAYS TRIGGER "Line Audit";\n'
        )
        bindir = Path(pgserver.__file__).parent / "pginstall" / "bin"
        dump = subprocess.run(  # nosec B603 -- the bundled pg_dump, fixed arguments
            [os.fspath(bindir / "pg_dump"), "--schema-only", "--no-owner", "-d", srv.get_uri()],
            capture_output=True, text=True, check=True, timeout=60,
        ).stdout  # fmt: skip
        held = srv.psql(
            "\\set QUIET on\n\\t on\n\\a\n"
            "SELECT tgname || '=' || tgenabled::text FROM pg_trigger WHERE NOT tgisinternal;\n"
        )
    finally:
        srv.cleanup()
    server = dict(line.split("=", 1) for line in held.splitlines() if "=" in line)
    assert _modes(dump) == server == _modes(_DUMP.read_text())
