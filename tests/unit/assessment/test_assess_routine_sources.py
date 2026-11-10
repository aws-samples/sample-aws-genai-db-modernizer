# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``assess()`` takes routine sources and writes the job's routine sources (#2022).

A library caller passes each routine's ``CREATE`` in the routine-sources format
(``RoutineSource`` entries, ``AssessmentRequest.routine_sources``, contract 1.4, read
only when pinned until a release records it), and the
run writes the same ``collector/routine_sources.json`` and chunks the scripts and the
live collectors write, under their rules: the exact text stays only in the chunks; a
collection made with ``include_routine_bodies = 0`` writes none; only a routine the
collection lists with its body collected gets one (a ``not_readable``, ``opted_out`` or
``not_supported`` routine, or one the collection does not list, gets none); each body
goes through the repository's credential scan. A caller that passes none sees no change.
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
def test_a_request_with_no_routine_sources_writes_none(tmp_path: Path) -> None:
    """A caller that passes none sees no change: the run writes no routine sources (as
    before 1.4, when assess() wrote none)."""
    _, store = _run(tmp_path, pg_collection(), None)
    assert not list((tmp_path / "artifacts").rglob("routine_sources*"))
    assert _index(store) is None


def test_an_empty_list_clears_the_jobs_earlier_routine_sources(tmp_path: Path) -> None:
    """An empty list is a caller's "none": the job's earlier routine sources are cleared,
    as a script's re-run does, so no earlier literal is kept."""
    _run(tmp_path, pg_collection(), _given(pg_collection()))
    _, store = _run(tmp_path, pg_collection(), [])
    assert _index(store) is None
    assert _files_holding(tmp_path / "artifacts", GIVEN) == []


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
    """A misspelt key (``exacts`` for ``exact``) is never dropped silently."""
    entry = {**_given(pg_collection())[0], "exacts": False}
    with pytest.raises(RoutineSourcesError, match=r"routine_sources\[0\].*exacts"):
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
    return [_request(e) for e in entries]


def _chain(exc: BaseException) -> list[BaseException]:
    out: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and current not in out:
        out.append(current)
        current = current.__cause__ or current.__context__
    return out


@pytest.mark.parametrize("number", range(7))
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
            "".join(traceback.format_exception(error)),
        ]
        for text in shown:
            assert EDGE not in text and GIVEN not in text
        assert all(set(e) == {"loc", "msg"} for e in error.errors())
        assert error.errors()[0]["loc"][0] == "routine_sources"
    assert not any(tmp_path.iterdir())


def test_another_request_error_quotes_no_input() -> None:
    payload = {**_request(_given(pg_collection())), "database_name": f"bad name {EDGE}"}
    with pytest.raises(ValidationError) as caught:
        AssessmentRequest.model_validate(payload)
    assert EDGE not in str(caught.value) and GIVEN not in str(caught.value)


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
