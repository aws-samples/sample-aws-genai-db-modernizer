# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""An ingest keeps every statement by default and cuts only to an explicit cap (#384).

``run_collect.py`` and ``run_assessment.py`` take ``--query-limit``; without it, and
on the library ingest, nothing is cut. ``metadata.query_capture`` records the
settings either way. A cap keeps every write and locking read before any read, so a
table that is written in the collection never ends up read-only (ADR-031 Amendment
9, one writer per table); when the writes alone exceed the cap, the costliest writes
are kept and the dropped ones are counted per table.
"""

from __future__ import annotations

import json
import subprocess  # nosec B404 -- runs this repo's own scripts with fixed argv
import sys
import zipfile
from collections import defaultdict
from pathlib import Path

import pytest

from src.agents.referee.write_groups import written_tables
from src.contracts.collector_output import CollectorOutputContract, QueryCapture
from src.shared.query_capture import cap_collection, parse_query_limit

REPO = Path(__file__).resolve().parents[3]


def _read(i: int, total: float | None, table: str = "app.t") -> dict:
    return {
        "query_id": f"q{i}",
        "query_text": f"SELECT a FROM {table.split('.')[-1]} WHERE id = ?",
        "query_type": "SELECT",
        "tables_accessed": [table],
        "total_time_ms": total,
    }


def _write(i: int, total: float | None, table: str = "app.t") -> dict:
    return {
        "query_id": f"q{i}",
        "query_text": f"UPDATE {table.split('.')[-1]} SET a = ? WHERE id = ?",
        "query_type": "UPDATE",
        "tables_accessed": [table],
        "total_time_ms": total,
    }


def _locking(i: int, total: float | None, table: str = "app.t") -> dict:
    return {
        **_read(i, total, table),
        "query_text": f"SELECT a FROM {table.split('.')[-1]} WHERE id = ? FOR UPDATE",
    }


def _collection(patterns: list[dict], capture: dict | None = None) -> dict:
    metadata: dict = {"collector_version": "1.0.0", "source_database": {"engine": "mysql"}}
    if capture is not None:
        metadata["query_capture"] = capture
    return {"metadata": metadata, "queries": {"query_patterns": patterns}}


def _reads(totals: list[float | None]) -> dict:
    return _collection([_read(i, t) for i, t in enumerate(totals)])


def _ids(data: dict) -> list[str]:
    return [p["query_id"] for p in data["queries"]["query_patterns"]]


# -- no cap ----------------------------------------------------------------------------


def test_with_no_cap_every_statement_is_kept_and_the_capture_recorded():
    data = _reads([5.0, 1.0, 3.0] * 10_000)
    result = cap_collection(data)
    assert len(_ids(data)) == 30_000 and not result.cut and result.kept_query_ids is None
    assert result.capture == {
        "query_limit": None,
        "min_executions": None,
        "captured": 30_000,
        "query_shapes": 30_000,
        "available": None,
        "truncated": None,
        "error": None,
        "writes_dropped_by_table": {},
    }
    assert data["metadata"]["query_capture"] == result.capture


def test_with_no_cap_a_live_capture_is_kept_as_it_was():
    live = {
        "query_limit": None,
        "min_executions": 10,
        "captured": 3,
        "query_shapes": 3,
        "available": None,
        "truncated": False,
        "error": None,
    }
    data = _collection([_read(i, 1.0) for i in range(3)], live)
    result = cap_collection(data)
    assert not result.cut
    assert result.capture == {**live, "writes_dropped_by_table": {}}


def test_a_live_error_survives_the_ingest():
    live = {"query_limit": None, "min_executions": 10, "captured": 0, "error": "failed (X)"}
    result = cap_collection(_collection([], live))
    assert result.capture["error"] == "failed (X)"


# -- an explicit cap -------------------------------------------------------------------


def test_a_cap_keeps_the_costliest_reads_in_their_own_order():
    data = _reads([5.0, 1.0, None, 9.0, 3.0])
    result = cap_collection(data, 3)
    # The three costliest by total time; an unknown total ranks last.
    assert _ids(data) == ["q0", "q3", "q4"]
    assert result.cut and result.kept_query_ids == {"q0", "q3", "q4"}
    assert result.capture == {
        "query_limit": 3,
        "min_executions": None,
        "captured": 5,  # the rows held; the cap cuts shapes
        "query_shapes": 3,
        "available": 5,
        "truncated": True,
        "error": None,
        "writes_dropped_by_table": {},
    }


def test_an_unknown_total_ranks_after_a_known_zero():
    data = _reads([None, 0.0])
    cap_collection(data, 1)
    assert _ids(data) == ["q1"]


def test_equal_totals_keep_the_earlier_statement():
    data = _reads([1.0, 1.0, 1.0])
    cap_collection(data, 2)
    assert _ids(data) == ["q0", "q1"]


def test_a_collection_exactly_at_the_cap_is_kept_whole():
    data = _reads([1.0, 2.0])
    result = cap_collection(data, 2)
    assert _ids(data) == ["q0", "q1"] and not result.cut
    assert result.capture["truncated"] is None


def test_a_cheap_write_is_kept_over_a_costly_read():
    data = _collection([_read(0, 900.0), _write(1, 0.1), _read(2, 800.0), _locking(3, 0.2)])
    result = cap_collection(data, 3)
    assert _ids(data) == ["q0", "q1", "q3"]
    assert result.capture["writes_dropped_by_table"] == {}


def test_writes_beyond_the_cap_keep_the_costliest_and_count_the_dropped_per_table():
    data = _collection(
        [
            _write(0, 5.0, "app.a"),
            _write(1, 1.0, "app.a"),
            _write(2, 3.0, "app.b"),
            _write(3, 0.5, "app.c"),
            _read(4, 99.0, "app.d"),
        ]
    )
    result = cap_collection(data, 2)
    assert _ids(data) == ["q0", "q2"]
    assert result.capture["writes_dropped_by_table"] == {"app.a": 1, "app.c": 1}
    QueryCapture.model_validate(result.capture)


def test_a_live_capture_is_kept_and_narrowed_by_a_smaller_ingest_cap():
    live = {
        "query_limit": 10_000,
        "min_executions": 10,
        "captured": 4,
        "query_shapes": 4,
        "available": None,
        "truncated": False,
        "error": None,
    }
    data = _collection([_read(i, float(i)) for i in range(4)], live)
    result = cap_collection(data, 2)
    assert result.capture == {
        "query_limit": 2,
        "min_executions": 10,
        "captured": 4,  # the rows held; the cap cuts shapes, not this count
        "query_shapes": 2,
        "available": 4,
        "truncated": True,
        "error": None,
        "writes_dropped_by_table": {},
    }


def test_a_truncated_live_capture_stays_truncated_with_an_unknown_source_count():
    live = {"query_limit": 4, "min_executions": 10, "captured": 4, "truncated": True}
    data = _collection([_read(i, float(i)) for i in range(4)], live)
    assert cap_collection(data).capture["truncated"] is True
    result = cap_collection(data, 2)
    # The source held more than the 4 the collector kept: how many is unknown.
    assert result.capture["available"] is None and result.capture["truncated"] is True


def test_dropped_writes_add_to_the_ones_an_earlier_cut_recorded():
    earlier = {"query_limit": 3, "captured": 3, "writes_dropped_by_table": {"app.a": 2}}
    data = _collection([_write(i, float(i), "app.a") for i in range(3)], earlier)
    result = cap_collection(data, 1)
    assert result.capture["writes_dropped_by_table"] == {"app.a": 4}


def test_null_sections_hold_no_statements_and_are_left_as_they_are():
    data: dict = {"metadata": None, "queries": None}
    result = cap_collection(data, 5)
    assert result.capture["captured"] == 0
    assert data == {"metadata": None, "queries": None}


def test_the_recorded_capture_validates_against_the_contract():
    assert QueryCapture.model_validate(cap_collection(_reads([1.0, 2.0, 3.0]), 2).capture)
    assert QueryCapture.model_validate(cap_collection(_reads([1.0])).capture).query_limit is None


@pytest.mark.parametrize("value", [0, -5, True, "10"])
def test_a_cap_that_is_not_a_positive_count_is_refused(value):
    with pytest.raises(ValueError):
        cap_collection(_reads([1.0]), value)


@pytest.mark.parametrize("text", ["0", "-5", "ten", "1.5", ""])
def test_a_limit_option_that_is_not_a_positive_count_is_refused(text):
    with pytest.raises(ValueError):
        parse_query_limit(text)


def test_a_positive_limit_option_is_read():
    assert parse_query_limit("20000") == 20_000
    assert parse_query_limit("20_000") == 20_000


# -- the scripts -----------------------------------------------------------------------


def _sample(name: str, dest: Path) -> Path:
    with zipfile.ZipFile(REPO / "docs" / "examples" / name / f"{name}.zip") as z:
        z.extractall(dest)
    return dest / f"{name}-collection.json"


def _run_collect(src: Path, root: Path, *extra: str) -> tuple[int, dict]:
    proc = subprocess.run(  # nosec B603 -- fixed interpreter plus this repo's own script args
        [
            sys.executable,
            "scripts/run_collect.py",
            "--file",
            str(src),
            "--db",
            "sample",
            "--job-id",
            "cap00001",
            "--artifact-root",
            str(root),
            *extra,
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    out = proc.stdout.strip().splitlines()
    return proc.returncode, (json.loads(out[-1]) if out else {})


def _stored(root: Path, db: str = "sample", job: str = "cap00001", name: str = "output") -> dict:
    data: dict = json.loads((root / db / job / "collector" / f"{name}.json").read_text())
    return data


def test_run_collect_keeps_a_sample_whole_by_default(tmp_path):
    code, status = _run_collect(_sample("wordpress", tmp_path / "in"), tmp_path / "a")
    assert code == 0 and status["status"] == "complete"
    stored = _stored(tmp_path / "a")
    capture = stored["metadata"]["query_capture"]
    assert capture["query_limit"] is None
    assert capture["captured"] == status["queries"] == len(stored["queries"]["query_patterns"])
    assert "query_capture" not in status
    CollectorOutputContract.model_validate(stored)


def _tables(patterns: list[dict], engine: str) -> tuple[set[str], set[str]]:
    """The tables some statement writes, and the tables some statement only reads."""
    written: set[str] = set()
    read: set[str] = set()
    for p in patterns:
        w = set(written_tables(p, engine))
        written |= w
        read |= {t for t in p.get("tables_accessed") or [] if t not in w}
    return written, read


@pytest.fixture(scope="module")
def discourse_full(tmp_path_factory) -> tuple[Path, list[dict]]:
    root = tmp_path_factory.mktemp("discourse")
    src = _sample("discourse", root / "in")
    assert _run_collect(src, root / "full")[0] == 0
    return src, _stored(root / "full")["queries"]["query_patterns"]


def _dropped_by_table(full: list[dict], kept: list[dict]) -> dict[str, int]:
    kept_ids = {p["query_id"] for p in kept}
    by_table: dict[str, int] = defaultdict(int)
    for p in full:
        if p["query_id"] not in kept_ids:
            for t in written_tables(p, "postgresql"):
                by_table[t] += 1
    return dict(sorted(by_table.items()))


def test_a_cap_the_writes_fit_in_leaves_no_written_table_read_only(discourse_full, tmp_path):
    # Discourse holds 362 writes and locking reads, on 127 tables, of 1,654 statements.
    src, full = discourse_full
    written_before, _ = _tables(full, "postgresql")
    assert sum(1 for p in full if written_tables(p, "postgresql")) == 362
    code, status = _run_collect(src, tmp_path / "a", "--query-limit", "400")
    assert code == 0, status
    stored = _stored(tmp_path / "a")
    kept = stored["queries"]["query_patterns"]
    written_after, read_after = _tables(kept, "postgresql")
    assert written_after == written_before
    assert not (written_before & read_after) - written_after
    assert stored["metadata"]["query_capture"]["writes_dropped_by_table"] == {}
    # The 38 places left over go to the costliest reads.
    assert len(kept) == 400


def test_a_cap_below_the_writes_lists_every_table_that_lost_its_writes(discourse_full, tmp_path):
    # 100 places for 362 writes on 127 tables: some written tables keep none of their
    # writes, but none does so silently.
    src, full = discourse_full
    code, status = _run_collect(src, tmp_path / "a", "--query-limit", "100")
    assert code == 0, status
    stored = _stored(tmp_path / "a")
    kept = stored["queries"]["query_patterns"]
    assert len(kept) == status["queries"] == 100
    # Every kept statement is a write or a locking read: no read took a write's place.
    assert all(written_tables(p, "postgresql") for p in kept)
    written_before, _ = _tables(full, "postgresql")
    written_after, read_after = _tables(kept, "postgresql")
    dropped = stored["metadata"]["query_capture"]["writes_dropped_by_table"]
    assert dropped == _dropped_by_table(full, kept)
    assert ((written_before & read_after) - written_after) <= set(dropped)


def test_run_collect_applies_a_cap_and_counts_the_kept_shapes(tmp_path):
    code, status = _run_collect(
        _sample("wordpress", tmp_path / "in"), tmp_path / "a", "--query-limit", "5"
    )
    assert code == 0, status
    stored = _stored(tmp_path / "a")
    assert len(stored["queries"]["query_patterns"]) == status["queries"] == 5
    capture = stored["metadata"]["query_capture"]
    assert (capture["query_limit"], capture["query_shapes"], capture["truncated"]) == (5, 5, True)
    assert capture["available"] > 5
    assert capture["captured"] == capture["available"]  # rows held, whatever the cap
    # The status line says this ingest cut the collection.
    assert status["query_capture"] == capture
    # The table-reference cross-check counts the kept statements, not the full file.
    refs = status["table_refs"]
    assert refs["agree"] + refs["disagree"] == 5
    assert _stored(tmp_path / "a", name="sql_shadow")["summary"]["queries"] == 5


def test_the_status_line_omits_a_capture_this_ingest_did_not_cut(tmp_path):
    assert _run_collect(_sample("wordpress", tmp_path / "in"), tmp_path / "a")[0] == 0
    processed = tmp_path / "a" / "sample" / "cap00001" / "collector" / "output.json"
    stored = json.loads(processed.read_text())
    stored["metadata"]["query_capture"] = {
        "query_limit": 50,
        "min_executions": 10,
        "captured": 50,
        "truncated": True,
    }
    processed.write_text(json.dumps(stored))
    code, status = _run_collect(processed, tmp_path / "b")
    assert code == 0, status
    assert "query_capture" not in status
    assert _stored(tmp_path / "b")["metadata"]["query_capture"]["truncated"] is True


def test_run_collect_refuses_a_limit_below_one(tmp_path):
    code, _ = _run_collect(
        _sample("wordpress", tmp_path / "in"), tmp_path / "a", "--query-limit", "0"
    )
    assert code != 0
    assert not (tmp_path / "a" / "sample").exists()


def test_run_collect_narrows_an_already_processed_collection(tmp_path):
    assert _run_collect(_sample("wordpress", tmp_path / "in"), tmp_path / "a")[0] == 0
    processed = tmp_path / "a" / "sample" / "cap00001" / "collector" / "output.json"
    code, status = _run_collect(processed, tmp_path / "b", "--query-limit", "3")
    assert code == 0, status
    capture = _stored(tmp_path / "b")["metadata"]["query_capture"]
    assert (capture["query_limit"], capture["query_shapes"], capture["truncated"]) == (3, 3, True)
    assert capture["captured"] == capture["available"]


def test_run_assessment_applies_an_explicit_cap(tmp_path):
    from scripts import run_assessment
    from src.storage.local_store import LocalArtifactStore

    store = LocalArtifactStore(str(tmp_path / "artifacts"))
    job_id, db = run_assessment.phase_collect(
        str(_sample("wordpress", tmp_path / "in")), "sample", store, query_limit=4
    )
    stored = _stored(tmp_path / "artifacts", db, job_id)
    assert len(stored["queries"]["query_patterns"]) == 4
    assert stored["metadata"]["query_capture"]["truncated"] is True
    assert _stored(tmp_path / "artifacts", db, job_id, "sql_shadow")["summary"]["queries"] == 4


def test_run_assessment_keeps_every_statement_by_default(tmp_path):
    from scripts import run_assessment
    from src.storage.local_store import LocalArtifactStore

    src = _sample("wordpress", tmp_path / "in")
    store = LocalArtifactStore(str(tmp_path / "artifacts"))
    job_id, db = run_assessment.phase_collect(str(src), "sample", store)
    stored = _stored(tmp_path / "artifacts", db, job_id)
    capture = stored["metadata"]["query_capture"]
    assert capture["query_limit"] is None
    assert capture["captured"] == len(stored["queries"]["query_patterns"])


def test_the_library_ingest_cuts_nothing_and_records_the_capture(tmp_path):
    from src.atx_orchestrator.core import ingest_offline_collection
    from src.storage.local_store import LocalArtifactStore

    text = _sample("wordpress", tmp_path / "in").read_text()
    raw = json.loads(text[text.index("{") :])
    store = LocalArtifactStore(str(tmp_path / "artifacts"))
    data = ingest_offline_collection(store, "lib00001", "sample", raw)
    capture = data["metadata"]["query_capture"]
    # #1955: the refreshed sample records its script settings (no limit), so the ingest
    # knows nothing was cut; an older collection's unknown is covered by
    # tests/unit/shared/test_offline_query_capture.py.
    assert capture["query_limit"] is None and capture["truncated"] is False
    assert capture["captured"] == len(data["queries"]["query_patterns"])
