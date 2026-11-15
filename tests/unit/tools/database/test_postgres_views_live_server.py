# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Each collected PostgreSQL view's CREATE loads back to the same view (#2082).

The live collector's view read and the offline script's section 7 run against a
PostgreSQL started by ``pgserver`` on a Unix socket in a temp folder (no TCP listener).
Both give the same fields; each view's CREATE is built from them, the views dropped and
the statements loaded back; collecting again must give the same views. A view an
extension owns is flagged and never built. The tests skip when ``pgserver`` is not
installed or cannot start.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.agents.collector.routine_sources import build_routine_sources
from src.tools.database.postgres_tools import PostgreSQLRemoteCollector

pgserver = pytest.importorskip("pgserver")

_ROOT = Path(__file__).resolve().parents[4]

_CORPUS = """
CREATE TABLE public.vw_orders (id bigint PRIMARY KEY, note text, total numeric(10,2));
CREATE VIEW public.vw_base AS SELECT id, note FROM public.vw_orders WHERE note <> 'it''s; void';
CREATE VIEW public."Vw Top" (order_id, "Note""s") WITH (security_barrier = on) AS
  SELECT b.id, b.note FROM public.vw_base b WITH LOCAL CHECK OPTION;
CREATE VIEW public.vw_invoker WITH (security_invoker = true) AS SELECT count(*) AS n FROM public.vw_orders;
CREATE VIEW public.vw_ext AS SELECT 1 AS one;
ALTER EXTENSION plpgsql ADD VIEW public.vw_ext;
"""
_FIELDS = ("view_name", "schema_name", "definition", "check_option", "security_barrier",
           "security_invoker", "extension_owned", "columns", "search_path")  # fmt: skip


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    try:
        srv = pgserver.get_server(tmp_path_factory.mktemp("pgviews"), cleanup_mode="delete")
    except Exception as exc:  # no bundled server for this platform, or it cannot start
        pytest.skip(f"pgserver cannot start a PostgreSQL here: {exc}")
    srv.psql("\\set ON_ERROR_STOP on\n" + _CORPUS)  # a bad corpus fails, never skips
    yield srv
    srv.cleanup()


def _run(server, sql: str) -> list[dict]:
    wrapped = f"SELECT coalesce(jsonb_agg(to_jsonb(t)), '[]')::text FROM ({sql}) t;"
    out = server.psql(f"\\set QUIET on\n\\t on\n\\a\n{wrapped}\n")
    rows: list[dict] = json.loads([ln for ln in out.splitlines() if ln.strip()][-1])
    return rows


def _live(server) -> PostgreSQLRemoteCollector:
    class Live(PostgreSQLRemoteCollector):
        def _query(self, sql):
            return _run(server, sql.strip().rstrip(";"))

    return Live.__new__(Live)


def _script(server) -> list[dict]:
    script = (_ROOT / "scripts/collect-postgresql.sql").read_text()
    cte = script.split("views_data AS (", 1)[1].split("\n),\n", 1)[0]
    sql = f"WITH views_data AS ({cte}\n) SELECT json_array_elements(data) AS r FROM views_data"
    return sorted((r["r"] for r in _run(server, sql)), key=lambda r: r["view_name"])


def _shape(rows: list[dict]) -> list[tuple]:
    return [tuple(json.dumps(r.get(f), sort_keys=True) for f in _FIELDS)
            for r in sorted(rows, key=lambda r: r["view_name"])]  # fmt: skip


def test_the_live_read_and_the_script_give_the_same_view_fields(server):
    live = _live(server).collect_views()
    assert _shape(live) == _shape(_script(server))
    top = next(r for r in live if r["view_name"] == "Vw Top")
    assert (top["check_option"], top["security_barrier"], top["security_invoker"]) == (
        "LOCAL",
        True,
        False,
    )
    assert [c["name"] for c in top["columns"]] == ["order_id", 'Note"s']
    assert next(r for r in live if r["view_name"] == "vw_invoker")["security_invoker"] is True
    assert {r["view_name"]: r["extension_owned"] for r in live}["vw_ext"] is True


def test_every_view_create_loads_back_to_the_same_view(server):
    rows = _script(server)
    build = build_routine_sources("postgresql", procedures=[], triggers=[], views=rows)
    assert sorted(s.specific_name for s in build.sources) == ["Vw Top", "vw_base", "vw_invoker"]
    assert any("it''s; void" in s.create_statement for s in build.sources)
    order = {"vw_base": 0, "Vw Top": 1, "vw_invoker": 2}
    loads = [
        s.create_statement for s in sorted(build.sources, key=lambda s: order[s.specific_name])
    ]
    server.psql('\\set ON_ERROR_STOP on\nDROP VIEW public."Vw Top"; DROP VIEW public.vw_base;'
                "DROP VIEW public.vw_invoker;\n" + "\n".join(loads))  # fmt: skip
    again = _script(server)
    assert _shape(again) == _shape(rows)
    # A second load replaces each view with itself.
    server.psql("\\set ON_ERROR_STOP on\n" + "\n".join(loads))
    assert _shape(_script(server)) == _shape(rows)
