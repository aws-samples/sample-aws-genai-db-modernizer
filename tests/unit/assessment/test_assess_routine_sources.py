# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``assess()`` takes routine sources and writes the job's routine sources (#2022).

A 1.4 run (read only when pinned until a release step records it) extracts a raw
collection's routine bodies as the scripts do, and a library caller may pass each
routine's ``CREATE`` in the routine-sources format (``RoutineSource`` entries,
``AssessmentRequest.routine_sources``), which replaces its routine's. The run writes the
same ``collector/routine_sources.json`` and chunks the scripts and the live collectors
write, under their rules: the exact text stays only in the chunks; a
collection made with ``include_routine_bodies = 0`` writes none; only a routine the
collection lists with its body collected gets one (a ``not_readable``, ``opted_out`` or
``not_supported`` routine, or one the collection does not list, gets none); each body
goes through the repository's credential scan. A 1.3 request writes none, as before 1.4.
"""

from __future__ import annotations

import gzip
import json
import logging
import traceback
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from src.agents.collector.routine_sources import iter_collection_sources, read_routine_sources
from src.assessment import AssessmentRequest, RoutineSourcesError, assess
from src.contracts.routine_sources import MAX_CHUNK_SOURCES, RoutineSourcesContract
from src.storage.local_store import LocalArtifactStore
from tests.unit.agents.routine_collections import (
    SEED,
    mysql_collection,
    pg_collection,
    pg_collection_with_non_dml_triggers,
)

#: A literal only the given sources hold (the collection holds SEED), so a test can tell
#: where the given text went.
GIVEN = "given-literal-7731@example.org"


def _given(collection: dict[str, Any]) -> list[dict[str, Any]]:
    """The collection's own sources, as a caller passes them, each holding GIVEN."""
    out = []
    for source in iter_collection_sources(collection):
        entry = source.model_dump(mode="json")
        entry["create_statement"] = entry["create_statement"].replace(SEED, GIVEN)
        if GIVEN not in entry["create_statement"]:
            entry["create_statement"] += f"\n-- '{GIVEN}'"
        entry.pop("sha256")
        out.append(entry)
    return out


def _run(
    tmp_path: Path,
    collection: dict[str, Any],
    sources: list[dict[str, Any]] | None,
    **options: Any,
) -> tuple[Any, LocalArtifactStore]:
    store = LocalArtifactStore(base_dir=str(tmp_path / "artifacts"))
    request: dict[str, Any] = {
        "contract_version": "1.4",
        "database_name": "shop",
        "collection": collection,
        "options": {"run_through": "analysis", **options},
    }
    if sources is not None:
        request["routine_sources"] = sources
    return assess(request, store=store, job_id="rs000001"), store


def _files_holding(root: Path, text: str) -> list[str]:
    found = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        data = path.read_bytes()
        if path.suffix == ".gz" and data:
            data = gzip.decompress(data)
        if text in data.decode("utf-8", errors="ignore"):
            found.append(str(path.relative_to(root)))
    return found


def _index(store: LocalArtifactStore) -> RoutineSourcesContract | None:
    key = "shop/rs000001/collector/routine_sources.json"
    if not store.exists(key):
        return None
    return RoutineSourcesContract.model_validate(store.read_json(key))


@pytest.mark.parametrize(
    "collection", [pg_collection, mysql_collection, pg_collection_with_non_dml_triggers]
)
def test_the_given_sources_are_written_and_their_text_stays_only_in_the_chunks(
    collection: Any, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    given = _given(collection())
    result, store = _run(tmp_path, collection(), given, include_artifacts=True)

    index = _index(store)
    assert index is not None and index.total == len(given)
    written = list(read_routine_sources(store, "shop", "rs000001"))
    assert [s.model_dump(mode="json", exclude={"sha256"}) for s in written] == given
    root = tmp_path / "artifacts"
    leaks = _files_holding(root, GIVEN)
    assert leaks and all(
        p.startswith("shop/rs000001/collector/routine_sources/part-") for p in leaks
    )
    # Not in the result (its artifacts block included), nor in any log record.
    assert GIVEN not in result.model_dump_json()
    assert all(GIVEN not in record.getMessage() for record in caplog.records)
    assert all(GIVEN not in (record.exc_text or "") for record in caplog.records)
    assert GIVEN not in caplog.text


@pytest.mark.passes_on_main
@pytest.mark.parametrize("pinned", ["1.3", None])
def test_a_request_for_1_3_writes_no_routine_sources_even_from_a_collection_with_bodies(
    pinned: str | None, tmp_path: Path
) -> None:
    """A 1.3 caller (pinned, or unpinned while 1.3 is the default) sees no change: the
    run writes no routine sources, whatever the collection holds (as before 1.4, when
    assess() wrote none)."""
    store = LocalArtifactStore(base_dir=str(tmp_path / "artifacts"))
    request: dict[str, Any] = {
        "database_name": "shop",
        "collection": pg_collection(),
        "options": {"run_through": "analysis"},
    }
    if pinned is not None:
        request["contract_version"] = pinned
    result = assess(request, store=store, job_id="rs000001")
    assert result.contract_version == "1.3"
    assert not list((tmp_path / "artifacts").rglob("routine_sources*"))
    assert _index(store) is None
    assert _files_holding(tmp_path / "artifacts", SEED) == []


def _scripts_write(tmp_path: Path, raw: dict[str, Any], collector_output: Any) -> Path:
    """What the scripts' path writes for ``raw`` (``scripts/run_assessment.py --file``:
    ``write_routine_sources`` over ``iter_collection_sources`` of the file as read)."""
    from src.agents.collector.routine_sources import write_routine_sources

    store = LocalArtifactStore(base_dir=str(tmp_path / "scripts"))
    write_routine_sources(store, "shop", "rs000001", collector_output, iter_collection_sources(raw))
    return tmp_path / "scripts" / "shop" / "rs000001" / "collector"


def _chunks(collector: Path) -> list[bytes]:
    return [p.read_bytes() for p in sorted((collector / "routine_sources").glob("part-*"))]


@pytest.mark.parametrize("collection", [pg_collection, mysql_collection])
def test_a_1_4_run_extracts_the_collections_bodies_as_the_scripts_do(
    collection: Any, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A 1.4 request with no routine_sources and a collection holding routine bodies (the
    audit's pg_collection: 4 bodies) writes the routine sources the scripts write from
    the same file, byte for byte: the same writer, exact rule, scan and chunks. A string
    literal of a body (SEED) stays only in the chunks: not in the result, a log record or
    any other file. Comment text is kept by the collector output's redaction (#2234), so
    the claim, and this test, are about literals."""
    caplog.set_level(logging.DEBUG)
    # SEED sits inside a string literal of each body it is in: what the claim covers.
    assert all(
        f"'{SEED}'" in s.create_statement or SEED not in s.create_statement
        for s in iter_collection_sources(collection())
    )
    result, store = _run(tmp_path, collection(), None, include_artifacts=True)

    index = _index(store)
    expected = list(iter_collection_sources(collection()))
    assert index is not None and index.total == len(expected) > 0
    if collection is pg_collection:
        assert index.total == 4
    written = list(read_routine_sources(store, "shop", "rs000001"))
    assert written == expected  # exact as the collectors decide (mysql: 3 not exact)
    output = store.read_json("shop/rs000001/collector/output.json")
    collector = tmp_path / "artifacts" / "shop" / "rs000001" / "collector"
    assert _chunks(collector) == _chunks(_scripts_write(tmp_path, collection(), output))
    leaks = _files_holding(tmp_path / "artifacts", SEED)
    assert leaks and all(
        p.startswith("shop/rs000001/collector/routine_sources/part-") for p in leaks
    )
    assert SEED not in result.model_dump_json()
    assert SEED not in caplog.text
    assert all(SEED not in (record.exc_text or "") for record in caplog.records)


def test_a_1_4_run_with_the_opt_out_extracts_none_even_from_a_file_holding_bodies(
    tmp_path: Path,
) -> None:
    """A file that says include_routine_bodies = 0 yields no source, whatever it holds,
    as the collectors' iter_collection_sources does."""
    raw = pg_collection()
    raw["metadata"]["routine_capture"]["include_bodies"] = False
    assert any(row.get("definition") for row in raw["procedures"])
    _, store = _run(tmp_path, raw, None)
    assert _index(store) is None
    assert _files_holding(tmp_path / "artifacts", SEED) == []


def test_given_entries_win_for_their_routine_and_the_rest_are_extracted(
    tmp_path: Path,
) -> None:
    """Both present: an entry the caller gives is used as given for its routine (under
    the writer's rules), and every other routine the collection holds a body for is
    extracted, so each routine has one source."""
    given = _given(pg_collection())[:1]
    _, store = _run(tmp_path, pg_collection(), given)

    written = {s.key(): s for s in read_routine_sources(store, "shop", "rs000001")}
    extracted = {s.key(): s for s in iter_collection_sources(pg_collection())}
    assert set(written) == set(extracted) and len(written) == 4
    mine = (given[0]["kind"], given[0]["schema_name"], given[0]["specific_name"])
    assert GIVEN in written[mine].create_statement
    for key, source in written.items():
        if key != mine:
            assert source == extracted[key]
            assert GIVEN not in source.create_statement


def test_an_empty_list_is_no_entry_and_a_rerun_keeps_no_earlier_given_text(
    tmp_path: Path,
) -> None:
    """[] gives no entry: the run writes what the collection holds, and the job's
    earlier routine sources are replaced whole, so no earlier given literal is kept."""
    _run(tmp_path, pg_collection(), _given(pg_collection()))
    assert _files_holding(tmp_path / "artifacts", GIVEN)
    _, store = _run(tmp_path, pg_collection(), [])
    assert list(read_routine_sources(store, "shop", "rs000001")) == list(
        iter_collection_sources(pg_collection())
    )
    assert _files_holding(tmp_path / "artifacts", GIVEN) == []


def test_neither_present_writes_none_and_the_1_4_result_has_every_field(
    tmp_path: Path,
) -> None:
    """No entry and no routine body in the collection: no routine sources are written
    (and a job's earlier ones are cleared), and the 1.4 result has every field a 1.3
    result has: 1.4 adds no result field, so nothing is missing."""
    raw = pg_collection()
    for name in ("procedures", "triggers"):
        raw.pop(name)
    _run(tmp_path, pg_collection(), None)  # an earlier run of the job with bodies
    result, store = _run(tmp_path, raw, None)
    assert result.contract_version == "1.4"
    assert _index(store) is None
    assert _files_holding(tmp_path / "artifacts", SEED) == []

    old = LocalArtifactStore(base_dir=str(tmp_path / "v13"))
    request = {
        "contract_version": "1.3",
        "database_name": "shop",
        "collection": raw,
        "options": {"run_through": "analysis"},
    }
    as_1_3 = assess(request, store=old, job_id="rs000001")
    shown, shown_1_3 = json.loads(result.model_dump_json()), json.loads(as_1_3.model_dump_json())
    assert set(shown) == set(shown_1_3) == set(type(result).model_fields)
    for key in set(shown) - {"contract_version", "library_version"}:
        assert shown[key] == shown_1_3[key], key


def test_an_extracted_body_that_looks_like_a_credential_is_flagged_by_the_scan(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The collectors' scan (strict, as the scripts run it) reads every extracted body;
    the index names the routine and the kinds, and no log line holds the value."""
    caplog.set_level(logging.WARNING)
    raw = pg_collection()
    secret = "password = 'not-a-real-one-8812'"  # pragma: allowlist secret
    raw["procedures"][2]["definition"] = raw["procedures"][2]["definition"].replace(
        "RETURN NEW;", f"RETURN NEW; -- {secret}"
    )
    _, store = _run(tmp_path, raw, None)
    index = _index(store)
    assert index is not None and index.secret_scan is not None
    flagged = {f.specific_name: f.credential_kinds for f in index.secret_scan.flagged}
    assert flagged.get("audit_orders_16392")
    assert "not-a-real-one-8812" not in caplog.text and SEED not in caplog.text


def test_a_row_with_code_but_no_body_status_gets_no_source(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """#2124: routine migration reads a row with code and no body_status (a DDL upload,
    an assess() collection), but no collector writes a source for one:
    iter_routine_sources carries only a row whose body_status is collected. assess()
    follows the collectors: such a row is neither extracted nor written from a given
    entry, which is skipped as having no body status."""
    caplog.set_level(logging.WARNING)
    raw = pg_collection()
    raw["procedures"][2].pop("body_status")
    assert raw["procedures"][2]["definition"]
    extracted = {s.specific_name for s in iter_collection_sources(raw)}
    assert "audit_orders_16392" not in extracted  # the collectors' rule
    given = [e for e in _given(pg_collection()) if e["specific_name"] == "audit_orders_16392"]
    _, store = _run(tmp_path, raw, given)

    written = {s.specific_name for s in read_routine_sources(store, "shop", "rs000001")}
    assert written == extracted
    assert _files_holding(tmp_path / "artifacts", GIVEN) == []
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "no body status: function public.audit_orders_16392" in messages


def test_the_opt_out_writes_none(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING)
    given = _given(pg_collection())
    _, store = _run(tmp_path, pg_collection(include_bodies=False), given)
    assert _index(store) is None
    assert _files_holding(tmp_path / "artifacts", GIVEN) == []
    assert any("include_routine_bodies = 0" in r.getMessage() for r in caplog.records)


def test_a_routine_whose_body_was_not_collected_gets_no_source(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """mysql_collection lists secret_proc not_readable and js_label not_supported: a
    source given for either, or for a routine the collection does not list, is not
    written; the others are."""
    caplog.set_level(logging.WARNING)
    carried = _given(mysql_collection())
    extra = [
        {**carried[0], "routine_id": name, "specific_name": name,
         "create_statement": f"CREATE PROCEDURE {name}() SELECT '{GIVEN}'"}
        for name in ("secret_proc", "missing_proc")
    ] + [
        {**carried[1], "routine_id": "js_label", "specific_name": "js_label",
         "create_statement": f"CREATE FUNCTION js_label() RETURNS int RETURN '{GIVEN}'"}
    ]  # fmt: skip
    _, store = _run(tmp_path, mysql_collection(), carried + extra)

    names = [s.specific_name for s in read_routine_sources(store, "shop", "rs000001")]
    assert names == [s["specific_name"] for s in carried]
    assert not {"secret_proc", "missing_proc", "js_label"} & set(names)
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "not_readable" in messages and "not_supported" in messages
    assert "not listed" in messages
    assert GIVEN not in messages


def test_a_source_the_collection_marks_not_exact_is_written_not_exact(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The MySQL script rebuilds place_order, orders.tg_a and purge around a literal, so
    the collection marks them ``body_exact: false``: a caller's entry for each (with no
    ``exact``, or ``exact: true``) is written not exact; the others keep theirs."""
    from src.agents.collector.routine_sources import routine_sources_from_collection

    caplog.set_level(logging.WARNING)
    sources = routine_sources_from_collection(mysql_collection()).sources
    inexact = {(s.kind, s.specific_name) for s in sources if not s.exact}
    assert inexact == {("procedure", "place_order"), ("trigger", "orders.tg_a"), ("event", "purge")}
    given = _given(mysql_collection())
    for entry in given:
        entry.pop("exact", None)
        entry.pop("inexact_reason", None)
    given[0]["exact"] = True
    _, store = _run(tmp_path, mysql_collection(), given)

    written = {
        (s.kind, s.specific_name): s for s in read_routine_sources(store, "shop", "rs000001")
    }
    assert len(written) == len(given)
    for key, source in written.items():
        if key in inexact:
            assert source.exact is False and source.inexact_reason, key
        else:
            assert source.exact is True, key
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "written as not exact" in messages and "orders.tg_a" in messages
    assert GIVEN not in messages


def test_an_engine_routines_are_not_carried_from_writes_none(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from tests.unit.assessment.conftest import sample_collection

    caplog.set_level(logging.WARNING)
    source = _given(pg_collection())[0]
    _, store = _run(tmp_path, sample_collection("trailgear"), [source])
    assert _index(store) is None
    assert _files_holding(tmp_path / "artifacts", GIVEN) == []
    assert any("not carried" in r.getMessage() for r in caplog.records)


def test_a_body_that_looks_like_a_credential_is_flagged_by_the_scan(tmp_path: Path) -> None:
    given = _given(pg_collection())
    given[0]["create_statement"] += "\n-- password = 'not-a-real-one'"  # pragma: allowlist secret
    _, store = _run(tmp_path, pg_collection(), given)
    index = _index(store)
    assert index is not None and index.secret_scan is not None
    flagged = {f.specific_name: f.credential_kinds for f in index.secret_scan.flagged}
    assert given[0]["specific_name"] in flagged and flagged[given[0]["specific_name"]]


def _ingested(tmp_path: Path, raw: dict[str, Any]) -> dict[str, Any]:
    """``raw`` as the ingest writes it: a collector output, as a caller may upload one."""
    from src.atx_orchestrator.core import ingest_offline_collection

    store = LocalArtifactStore(base_dir=str(tmp_path / "ingest"))
    uploaded: dict[str, Any] = json.loads(
        json.dumps(ingest_offline_collection(store, "up000001", "shop", raw))
    )
    return uploaded


@pytest.mark.parametrize("flag", [0, "0", "false", "no", False])
def test_an_uploaded_collector_output_with_the_opt_out_writes_none_at_any_spelling(
    flag: Any, tmp_path: Path
) -> None:
    """The opt-out is read with the collectors' own collection_includes_bodies, so a
    collector output whose routine_capture.include_bodies is 0, "0", "false", "no" or
    False writes none of the given entries, as the collectors' reader yields none."""
    from src.agents.collector.routine_sources import collection_includes_bodies

    uploaded = _ingested(tmp_path, pg_collection())
    uploaded["metadata"]["routine_capture"]["include_bodies"] = flag
    assert collection_includes_bodies(uploaded) is False
    _, store = _run(tmp_path, uploaded, _given(pg_collection()))
    assert _index(store) is None
    assert _files_holding(tmp_path / "artifacts", GIVEN) == []


def _without_session(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for entry in entries:
        entry.pop("session", None)
    return entries


@pytest.mark.parametrize("uploaded", [False, True])
def test_a_given_entry_without_a_session_takes_the_collections(
    uploaded: bool, tmp_path: Path
) -> None:
    """mysql_collection records sql_mode STRICT_TRANS_TABLES (and its character sets) for
    every routine. A given entry that omits session is written with the collection's
    session, as the extracted source has it, so a replay never runs with strict mode off.
    From an uploaded collector output, which keeps only sql_mode, it takes that."""
    extracted = {s.key(): s.session for s in iter_collection_sources(mysql_collection())}
    collection = _ingested(tmp_path, mysql_collection()) if uploaded else mysql_collection()
    given = _without_session(_given(mysql_collection()))
    _, store = _run(tmp_path, collection, given)

    written = list(read_routine_sources(store, "shop", "rs000001"))
    assert len(written) == len(given)
    for source in written:
        assert source.session.sql_mode == "STRICT_TRANS_TABLES", source.key()
        if not uploaded:
            assert source.session == extracted[source.key()], source.key()


def test_a_given_entry_with_its_own_session_keeps_it(tmp_path: Path) -> None:
    given = _given(mysql_collection())
    for entry in given:
        entry["session"] = {"sql_mode": "ANSI_QUOTES"}
    _, store = _run(tmp_path, mysql_collection(), given)
    written = list(read_routine_sources(store, "shop", "rs000001"))
    assert written and all(s.session.sql_mode == "ANSI_QUOTES" for s in written)
    assert all(s.session.character_set_client is None for s in written)


def test_a_sample_shaped_collection_with_routine_bodies_is_extracted(tmp_path: Path) -> None:
    """A small raw collection in the bundled samples' shape (every section the collection
    script writes) whose routine rows carry body_status, as today's script writes them:
    a 1.4 run extracts its 3 functions and its trigger as the scripts do. No bundled
    sample exercises this: their routine rows predate body_status."""
    fixture = Path(__file__).resolve().parents[2] / "fixtures" / "routine_collections"
    raw = json.loads((fixture / "pg_shop_routines_collection.json").read_text(encoding="utf-8"))
    _, store = _run(tmp_path, raw, None)
    written = list(read_routine_sources(store, "shop", "rs000001"))
    assert written == list(iter_collection_sources(raw))
    assert {s.kind for s in written} == {"function", "trigger"} and len(written) == 4
    assert _files_holding(tmp_path / "artifacts", SEED) and all(
        p.startswith("shop/rs000001/collector/routine_sources/part-")
        for p in _files_holding(tmp_path / "artifacts", SEED)
    )


# --------------------------------------------------------------------------- chunks


def _pg_output(count: int) -> dict[str, Any]:
    return {
        "metadata": {"source_database": {"engine": "postgresql", "version": "16.2"}},
        "database_schema": {
            "procedures": [
                {"procedure_id": f"f{i}", "procedure_name": f"f{i}", "schema_name": "public",
                 "procedure_type": "FUNCTION", "specific_name": f"f{i}_1",
                 "body_status": "collected"}
                for i in range(count)
            ],  # fmt: skip
        },
    }


def test_the_writer_keeps_at_most_5000_routines_a_chunk(tmp_path: Path) -> None:
    from src.assessment.routine_sources import write_request_routine_sources

    count = MAX_CHUNK_SOURCES + 1
    store = LocalArtifactStore(base_dir=str(tmp_path))
    store.write_json("shop/rs000001/collector/output.json", _pg_output(count))
    entries = (
        {"kind": "function", "routine_id": f"f{i}", "schema_name": "public",
         "specific_name": f"f{i}_1", "origin": "pg_get_functiondef",
         "create_statement": f"CREATE FUNCTION public.f{i}() RETURNS int AS 'SELECT {i}' LANGUAGE sql"}
        for i in range(count)
    )  # fmt: skip
    key = write_request_routine_sources(store, "shop", "rs000001", list(entries))
    assert key == "shop/rs000001/collector/routine_sources.json"
    index = RoutineSourcesContract.model_validate(store.read_json(key))
    assert [p.count for p in index.parts] == [MAX_CHUNK_SOURCES, 1]
    raw = gzip.decompress(store.read_bytes("shop/rs000001/collector/" + index.parts[0].path))
    assert len(json.loads(raw)["sources"]) == MAX_CHUNK_SOURCES


# --------------------------------------------------------------------------- the request


def _request(sources: Any, **extra: Any) -> dict[str, Any]:
    return {
        "contract_version": "1.4",
        "database_name": "shop",
        "collection": pg_collection(),
        "routine_sources": sources,
        **extra,
    }


def test_an_entry_not_in_the_routine_sources_format_is_refused() -> None:
    entry = _given(pg_collection())[0]
    entry.pop("create_statement")
    with pytest.raises(RoutineSourcesError, match=r"routine_sources\[0\].*create_statement"):
        AssessmentRequest.model_validate(_request([entry]))


def test_an_unknown_key_is_refused() -> None:
    """A misspelt key (``exacts`` for ``exact``) is never dropped silently; the error
    names it as an unknown key, never by its own text."""
    entry = {**_given(pg_collection())[0], "exacts": False}
    with pytest.raises(RoutineSourcesError, match=r"routine_sources\[0\].*\[unknown key\]"):
        AssessmentRequest.model_validate(_request([entry]))


def test_two_entries_for_one_routine_are_refused() -> None:
    entry = _given(pg_collection())[0]
    with pytest.raises(RoutineSourcesError, match="two routine sources for function"):
        AssessmentRequest.model_validate(_request([entry, dict(entry)]))


def test_a_sha256_that_does_not_match_its_statement_is_refused() -> None:
    entry = {**_given(pg_collection())[0], "sha256": "0" * 64}
    with pytest.raises(RoutineSourcesError, match=r"routine_sources\[0\].*sha256 does not match"):
        AssessmentRequest.model_validate(_request([entry]))


def test_an_origin_of_another_engine_is_refused() -> None:
    entry = {**_given(pg_collection())[0], "origin": "show_create"}
    with pytest.raises(RoutineSourcesError, match="origin 'show_create' is not one a postgresql"):
        AssessmentRequest.model_validate(_request([entry]))
    mysql = {**_given(mysql_collection())[0], "origin": "pg_get_functiondef"}
    with pytest.raises(RoutineSourcesError, match="origin 'pg_get_functiondef'"):
        AssessmentRequest.model_validate({**_request([mysql]), "collection": mysql_collection()})


def test_the_entries_are_kept_with_their_sha256() -> None:
    entry = _given(pg_collection())[0]
    request = AssessmentRequest.model_validate(_request([entry]))
    assert request.routine_sources is not None
    kept = request.routine_sources[0]
    assert kept["create_statement"] == entry["create_statement"]
    assert len(kept["sha256"]) == 64


def test_the_request_never_shows_or_serializes_the_entries() -> None:
    request = AssessmentRequest.model_validate(_request(_given(pg_collection())))
    assert request.routine_sources
    assert GIVEN not in repr(request) and GIVEN not in str(request)
    assert "routine_sources" not in request.model_dump()
    assert GIVEN not in request.model_dump_json()
    assert AssessmentRequest.model_validate_json(request.model_dump_json()).routine_sources is None


#: A literal at both ends of a statement: a quoted input keeps about 25 characters of
#: each end, so a leak shows either way.
EDGE = "EDGE-LITERAL-4471"


def _bad_requests() -> list[dict[str, Any]]:
    good = _given(pg_collection())[0]
    text = f"SELECT '{EDGE}' /* {GIVEN} */ ; SELECT '{EDGE}'"
    entries: list[Any] = [
        [{**good, "create_statement": text, "sha256": "0" * 64}],  # sha256 mismatch
        [{**good, "create_statement": text, "origin": "nowhere"}],  # bad enum value
        [{**good, "create_statement": text, "unknown": text}],  # unknown key
        [text],  # a bare string
        [{**good, "create_statement": text}, {**good, "create_statement": text}],  # duplicate
        text,  # not a list
        [{**good, "create_statement": text, "origin": "show_create"}],  # another engine's
    ]
    valid = [{**good, "create_statement": text}]
    unpinned = _request(valid)
    del unpinned["contract_version"]
    return [
        *(_request(e) for e in entries),
        # Valid entries on a request for 1.3, pinned or (while 1.3 is the default) not:
        # the version refusal comes from the field's validator, so it quotes no input
        # either (a model-level error carried the whole request in errors() and json()).
        _request(valid, contract_version="1.3"),
        unpinned,
    ]


def _chain(exc: BaseException) -> list[BaseException]:
    out: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and current not in out:
        out.append(current)
        current = current.__cause__ or current.__context__
    return out


@pytest.mark.parametrize("number", range(9))
def test_no_error_carries_the_routine_text(number: int, tmp_path: Path) -> None:
    payload = _bad_requests()[number]
    paths = (
        lambda: AssessmentRequest.model_validate(payload),
        lambda: AssessmentRequest(**payload),
        lambda: AssessmentRequest.model_validate_json(json.dumps(payload)),
        lambda: assess(
            payload, store=LocalArtifactStore(base_dir=str(tmp_path)), job_id="bad00001"
        ),
    )
    for build in paths:
        with pytest.raises(RoutineSourcesError) as caught:
            build()
        error: RoutineSourcesError = caught.value
        assert issubclass(RoutineSourcesError, ValueError)
        assert _chain(error) == [error]
        shown = [
            str(error),
            repr(error),
            json.dumps(error.errors(), default=str),
            error.json(),
            "".join(traceback.format_exception(error)),
        ]
        for text in shown:
            assert EDGE not in text and GIVEN not in text and SEED not in text
        assert all(set(e) == {"loc", "msg"} for e in error.errors())
        assert error.errors()[0]["loc"][0] == "routine_sources"
        assert json.loads(error.json()) == [
            {"loc": list(e["loc"]), "msg": e["msg"]} for e in error.errors()
        ]
    assert not any(tmp_path.iterdir())


@pytest.mark.parametrize("pinned", ["1.3", None])
def test_a_request_for_1_3_with_routine_sources_is_refused_with_no_input(
    pinned: str | None,
) -> None:
    """A caller who forgets the 1.4 pin gets the input-free RoutineSourcesError, from the
    field's own validator: str(), errors() and json() hold no routine text and no other
    input (the collection's own SEED included)."""
    payload = _request(_given(pg_collection()))
    if pinned is None:
        del payload["contract_version"]
    else:
        payload["contract_version"] = pinned
    with pytest.raises(RoutineSourcesError) as caught:
        AssessmentRequest.model_validate(payload)
    error = caught.value
    assert [e["loc"] for e in error.errors()] == [("routine_sources",)]
    assert 'pin contract_version "1.4"' in error.errors()[0]["msg"]
    for text in (str(error), json.dumps(error.errors(), default=str), error.json()):
        assert GIVEN not in text and SEED not in text and "CREATE" not in text
        assert "shop" not in text  # no other input either


def test_another_request_error_quotes_no_input() -> None:
    """Another field's error quotes no input in its message, and its errors() and json()
    hold only that field's own value: never the routine text or the collection's."""
    payload = {**_request(_given(pg_collection())), "database_name": f"bad name {EDGE}"}
    with pytest.raises(ValidationError) as caught:
        AssessmentRequest.model_validate(payload)
    assert EDGE not in str(caught.value) and GIVEN not in str(caught.value)
    assert [e["loc"] for e in caught.value.errors()] == [("database_name",)]
    for text in (json.dumps(caught.value.errors(), default=str), caught.value.json()):
        assert GIVEN not in text and SEED not in text


def test_a_type_adapter_or_a_holding_model_keeps_pydantics_own_errors() -> None:
    """Outside the four covered calls (a TypeAdapter, a model holding the request), an
    invalid entry raises pydantic's ValidationError: its str() quotes no input, its
    errors() and json() carry the entries' text as input, and include_input=False drops
    it, as assess.md says."""
    from pydantic import BaseModel, TypeAdapter

    class Holder(BaseModel):
        request: AssessmentRequest

    payload = _request([{**_given(pg_collection())[0], "sha256": "0" * 64}])
    for build in (
        lambda: TypeAdapter(AssessmentRequest).validate_python(payload),
        lambda: Holder.model_validate({"request": payload}),
    ):
        with pytest.raises(ValidationError) as caught:
            build()
        error = caught.value
        assert GIVEN not in str(error)
        assert GIVEN in json.dumps(error.errors(), default=str) and GIVEN in error.json()
        assert GIVEN not in json.dumps(error.errors(include_input=False), default=str)
        assert GIVEN not in error.json(include_input=False)


def test_an_unknown_key_is_named_by_no_part_of_itself() -> None:
    """An unknown key's name is the caller's text too (it can hold routine code): the
    error says [unknown key] and never quotes it, in str(), errors() and json()."""
    entry = {**_given(pg_collection())[0], f"CREATE {EDGE} {GIVEN}": 1}
    with pytest.raises(RoutineSourcesError) as caught:
        AssessmentRequest.model_validate(_request([entry]))
    error = caught.value
    shown = [str(error), repr(error), json.dumps(error.errors(), default=str), error.json()]
    assert "[unknown key]" in str(error)
    for text in [*shown, "".join(traceback.format_exception(error))]:
        assert EDGE not in text and GIVEN not in text


def test_an_unvalidated_request_is_checked_before_anything_is_written(tmp_path: Path) -> None:
    built = AssessmentRequest.model_validate(_request(None))
    bad = {**_given(pg_collection())[0], "sha256": "0" * 64}
    store = LocalArtifactStore(base_dir=str(tmp_path))
    for request in (
        built.model_copy(update={"routine_sources": [bad]}),
        built.model_copy(
            update={"routine_sources": [{**bad, "sha256": "", "origin": "show_create"}]}
        ),
    ):
        with pytest.raises(ValueError, match=r"routine_sources\[0\]") as caught:
            assess(request, store=store, job_id="early001")
        assert _chain(caught.value) == [caught.value]
        assert GIVEN not in "".join(traceback.format_exception(caught.value))
        assert not any(tmp_path.iterdir())
