# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The demo routine scripts of the bundled samples (#1954, ADR-034).

``scripts/render_sample_routines.py`` writes, from each sample's synthetic routine set,
the script that installs the set on the sample's source database for the step 1
rehearsal, and the script that removes it. The committed scripts must be the rendered
ones; and installed on a PostgreSQL from ``pgserver`` (a Unix socket, no TCP
listener), the discourse set collects back to the same routine sources the bundled set
gives the deterministic e2e, so the rehearsal carries what the e2e checks. The MySQL twin
needs a ``mysqld``: ``tests/integration/test_sample_routine_install_mysql.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import scripts.render_sample_routines as render
from src.agents.collector.routine_sources import build_routine_sources
from src.tools.database.postgres_tools import PostgreSQLRemoteCollector
from tests.pg_server import PgServerUnavailable, start_pg_server

EXAMPLES = Path(render.REPO_ROOT) / "docs" / "examples"


def test_the_committed_scripts_are_the_rendered_ones() -> None:
    rendered = render.outputs()
    assert {p.name for p, _ in rendered} == {
        "wordpress-routines-install.sql",
        "wordpress-routines-remove.sql",
        "discourse-routines-install.sql",
        "discourse-routines-remove.sql",
    }
    for path, text in rendered:
        assert path.read_text(encoding="utf-8") == text, f"{path.name}: run the script"
    assert render.main(["--check"]) == 0


def test_check_names_a_stale_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    folder = tmp_path / "docs" / "examples" / "discourse"
    folder.mkdir(parents=True)
    for name in ("discourse-routines.json", "discourse-routines-install.sql"):
        (folder / name).write_text((EXAMPLES / "discourse" / name).read_text(encoding="utf-8"))
    monkeypatch.setattr(render, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(render, "SAMPLES", {"discourse": render.SAMPLES["discourse"]})
    assert render.main(["--check"]) == 1
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["stale"] == [
        "docs/examples/discourse/discourse-routines-remove.sql"
    ]


@pytest.mark.parametrize("sample", sorted(render.SAMPLES))
def test_the_install_creates_every_routine_of_the_set_in_dependency_order(sample: str) -> None:
    data = render.routine_set(sample)
    install = (EXAMPLES / sample / f"{sample}-routines-install.sql").read_text(encoding="utf-8")
    remove = (EXAMPLES / sample / f"{sample}-routines-remove.sql").read_text(encoding="utf-8")
    sources = render._sources(sample)
    expected = (
        len(data.get("procedures") or [])
        + len(data.get("triggers") or [])
        + len(data.get("events") or [])
    )
    assert len(sources) == expected
    at = [install.index(s.create_statement.rstrip().rstrip(";")) for s in sources]
    assert at == sorted(at)  # functions, procedures, triggers, events
    kinds = [s.kind for s in sources]
    assert kinds == sorted(kinds, key=render.KIND_ORDER.index)
    assert remove.count("DROP ") == expected  # the remove script drops each one
    assert "CREATE" not in remove


# --- discourse: installed on a stand-in source, it collects back to the same set -----

# The tables and columns the discourse routines read, and the role one of them is owned
# by: on the rehearsal source they are the sample's own.
_SETUP = """
CREATE ROLE discourse;
CREATE TABLE public.topics (id integer PRIMARY KEY, bumped_at timestamp);
CREATE TABLE public.posts (id integer PRIMARY KEY, topic_id integer, created_at timestamp,
                           deleted_at timestamp);
CREATE TABLE public.notifications (id integer PRIMARY KEY, read boolean, created_at timestamp);
"""


@pytest.fixture
def source() -> Any:
    pytest.importorskip("pgserver")
    try:
        srv = start_pg_server()
    except PgServerUnavailable as exc:  # the one failure that skips
        pytest.skip(f"no pgserver PostgreSQL here: {exc}")
    try:
        srv.psql("\\set ON_ERROR_STOP on\n" + _SETUP)
        yield srv
    finally:
        srv.cleanup()


def _collector(server: Any) -> PostgreSQLRemoteCollector:
    class Live(PostgreSQLRemoteCollector):
        def _query(self, sql: str) -> list[dict]:
            wrapped = f"SELECT coalesce(jsonb_agg(to_jsonb(t)), '[]')::text FROM ({sql.strip().rstrip(';')}) t;"
            out = server.psql(f"\\set QUIET on\n\\t on\n\\a\n{wrapped}\n")
            rows: list[dict] = json.loads([ln for ln in out.splitlines() if ln.strip()][-1])
            return rows

    return Live.__new__(Live)


def _sources_key(build: Any) -> list[tuple[str, str, str]]:
    return sorted((s.kind, s.routine_id, s.create_statement) for s in build.sources)


def test_the_discourse_install_collects_back_to_the_bundled_set(source: Any) -> None:
    install = (EXAMPLES / "discourse" / "discourse-routines-install.sql").read_text()
    source.psql(install)
    source.psql(install)  # it can run again
    db = _collector(source)
    procedures, triggers = db.collect_procedures(), db.collect_triggers()
    data = render.routine_set("discourse")
    bundled = build_routine_sources(
        "postgresql", procedures=data["procedures"], triggers=data["triggers"]
    )
    collected = build_routine_sources("postgresql", procedures=procedures, triggers=triggers)
    assert collected.statuses == {}
    assert _sources_key(collected) == _sources_key(bundled)

    # What the checks read besides the text: each routine's kind, owner and security.
    def shape(rows: list[dict]) -> list[tuple[Any, ...]]:
        return sorted(
            (r["routine_name"], r["identity_arguments"], r["routine_type"], r["owner"],
             r["security_definer"], str(r["language"]).lower())
            for r in rows
        )  # fmt: skip

    assert shape(procedures) == shape(data["procedures"])
    [trigger] = triggers
    assert (trigger["table_name"], trigger["is_enabled"]) == ("posts", "O")

    source.psql((EXAMPLES / "discourse" / "discourse-routines-remove.sql").read_text())
    assert db.collect_procedures() == [] and db.collect_triggers() == []
