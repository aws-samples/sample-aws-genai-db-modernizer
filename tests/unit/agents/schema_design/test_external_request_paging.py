"""The single-pass external request is paged, never cut, whatever its size (#1103).

DocumentDB, OpenSearch and ElastiCache have no grouped external path, so their
one ``llm_requests/schema_design_<engine>.json`` was written as a single
multi-megabyte blob (the discourse DocumentDB request is about 2 MB). It is now
written one record per line and the status line lists the Read pages that cover
every line. No test calls a model.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.run_schema_design import run_external
from src.agents.schema_design.external_request import render_external_request
from src.agents.schema_design.group_splitter import MAX_GROUP_INPUT_CHARS, MAX_GROUP_INPUT_PAGES
from src.storage.local_store import LocalArtifactStore

DB, JOB = "mydb", "job-001"
ENGINES = ["documentdb", "opensearch", "elasticache"]
READ_LINE_LIMIT = 2000  # Read cuts a line longer than this


def _table(i: int, columns: int) -> dict:
    return {
        "table_id": f"{DB}.t{i}",
        "table_name": f"t{i}",
        "row_count": 1000,
        "primary_key": ["id"],
        "columns": [
            {"column_name": f"c{j}", "data_type": "varchar", "nullable": True}
            for j in range(columns)
        ],
    }


def _query(i: int, text: str | None = None) -> dict:
    return {
        "query_id": f"q{i}",
        "query_text": text or f"SELECT * FROM t{i} WHERE id = 1",
        "tables_accessed": [f"{DB}.t{i}"],
        "frequency_per_hour": 10.0,
    }


def _seed(
    tmp_path: Path, engine: str, tables: int, columns: int, long_query: str | None = None
) -> LocalArtifactStore:
    store = LocalArtifactStore(str(tmp_path))
    base = f"{DB}/{JOB}"
    queries = [_query(i, long_query if i == 0 else None) for i in range(tables)]
    store.write_json(
        f"{base}/collector/output.json",
        {
            "contract_version": "3.0",
            "database_schema": {"tables": [_table(i, columns) for i in range(tables)]},
            "queries": {"query_patterns": queries, "total_queries_analyzed": len(queries)},
        },
    )
    store.write_json(f"{base}/analysis-{engine}/analysis.json", {"contract_version": "2.1"})
    store.write_json(
        f"{base}/assignment/v1/assignment.json",
        {
            "version": 1,
            "co_dependency_groups": [],
            "query_assignments": [
                {
                    "query_id": q["query_id"],
                    "assigned_engine": engine,
                    "in_scope": True,
                    "source_tables": q["tables_accessed"],
                }
                for q in queries
            ],
        },
    )
    return store


def _run(store: LocalArtifactStore, engine: str, capsys) -> tuple[dict, str, Path]:
    run_external(store, JOB, DB, engine, assignment_version=1)
    status = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    path = Path(store.base_dir) / status["llm_request"]
    return status, path.read_text(encoding="utf-8"), path


def _paged_lines(text: str, pages: list[dict]) -> list[str]:
    lines = text.splitlines()
    covered: list[str] = []
    for page in pages:
        covered.extend(lines[page["offset"] - 1 : page["offset"] - 1 + page["limit"]])
    return covered


@pytest.mark.parametrize("engine", ENGINES)
class TestPagedRequest:
    def test_a_small_request_fits_the_group_budget(self, tmp_path, capsys, engine):
        status, text, _ = _run(_seed(tmp_path, engine, tables=3, columns=4), engine, capsys)
        assert status["status"] == "awaiting_llm"
        assert status["oversized"] is False
        assert len(text) <= MAX_GROUP_INPUT_CHARS
        assert len(status["input_pages"]) <= MAX_GROUP_INPUT_PAGES
        assert status["input_chars"] == len(text)

    def test_a_large_request_is_paged_and_every_line_is_covered(self, tmp_path, capsys, engine):
        store = _seed(tmp_path, engine, tables=60, columns=60)
        status, text, _ = _run(store, engine, capsys)
        assert status["oversized"] is True
        assert len(text) > MAX_GROUP_INPUT_CHARS
        pages = status["input_pages"]
        assert len(pages) > MAX_GROUP_INPUT_PAGES
        # The pages tile the file: no line skipped, none read twice.
        assert _paged_lines(text, pages) == text.splitlines()
        assert [p["offset"] for p in pages][0] == 1
        for prev, nxt in zip(pages, pages[1:], strict=False):
            assert nxt["offset"] == prev["offset"] + prev["limit"]

    def test_nothing_is_dropped_from_a_large_request(self, tmp_path, capsys, engine):
        store = _seed(tmp_path, engine, tables=60, columns=60)
        _, _, path = _run(store, engine, capsys)
        request = json.loads(path.read_text(encoding="utf-8"))
        assert len(request["collector_output"]["database_schema"]["tables"]) == 60
        assert len(request["collector_output"]["queries"]["query_patterns"]) == 60
        assert all(
            len(t["columns"]) == 60
            for t in request["collector_output"]["database_schema"]["tables"]
        )
        assert request["target_type"] == engine
        assert "output_schema" in request

    def test_a_page_stays_inside_the_read_limits(self, tmp_path, capsys, engine):
        store = _seed(tmp_path, engine, tables=60, columns=60)
        status, text, _ = _run(store, engine, capsys)
        lines = text.splitlines()
        for page in status["input_pages"]:
            chunk = lines[page["offset"] - 1 : page["offset"] - 1 + page["limit"]]
            assert page["limit"] <= 2000
            assert page["limit"] == 1 or sum(len(x) + 1 for x in chunk) <= 40_000

    def test_a_long_query_text_is_also_given_as_short_lines(self, tmp_path, capsys, engine):
        long_text = "SELECT " + ", ".join(f"col_{i}" for i in range(600)) + " FROM t0"
        store = _seed(tmp_path, engine, tables=2, columns=2, long_query=long_text)
        _, text, path = _run(store, engine, capsys)
        # The one line Read would cut is the original query_text; its full
        # text is also in the short query_text_lines below.
        too_long = [x for x in text.splitlines() if len(x) >= READ_LINE_LIMIT]
        assert len(too_long) == 1 and '"query_text"' in too_long[0]
        request = json.loads(path.read_text(encoding="utf-8"))
        first = request["collector_output"]["queries"]["query_patterns"][0]
        assert first["query_text"] == long_text  # the original is kept
        assert " ".join(first["query_text_lines"]) == " ".join(long_text.split())
        second = request["collector_output"]["queries"]["query_patterns"][1]
        assert "query_text_lines" not in second

    def test_finalize_still_reads_the_request(self, tmp_path, capsys, engine):
        """``--finalize`` reads the request back as JSON (design_metrics, fingerprint)."""
        store = _seed(tmp_path, engine, tables=3, columns=4)
        _run(store, engine, capsys)
        request = store.read_json(f"{DB}/{JOB}/llm_requests/schema_design_{engine}.json")
        assert request["job_id"] == JOB


def test_the_discourse_documentdb_scale_request_is_over_two_megabytes_and_paged(tmp_path, capsys):
    """The size from the issue: about 2 MB for the discourse DocumentDB request."""
    store = _seed(tmp_path, "documentdb", tables=250, columns=100)
    status, text, _ = _run(store, "documentdb", capsys)
    assert len(text) > 2_000_000
    assert status["oversized"] is True
    assert _paged_lines(text, status["input_pages"]) == text.splitlines()


def test_render_does_not_modify_the_request_it_is_given():
    long_text = "SELECT " + ", ".join(f"c{i}" for i in range(600))
    request = {
        "collector_output": {"queries": {"query_patterns": [_query(0, long_text)]}},
    }
    render_external_request(request)
    assert "query_text_lines" not in request["collector_output"]["queries"]["query_patterns"][0]


def test_an_empty_request_has_one_small_page():
    text, summary = render_external_request({"target_type": "documentdb"})
    assert summary["oversized"] is False
    assert summary["input_pages"] == [{"offset": 1, "limit": len(text.splitlines())}]


def test_elasticache_write_queries_in_the_invalidation_context_get_short_lines():
    """The discourse ElastiCache shape: long write-query texts outside query_patterns."""
    texts = {
        f"w{i}": "UPDATE t SET " + ", ".join(f"c{j} = {j}" for j in range(n))
        for i, n in enumerate((900, 400, 450, 60))
    }
    request = {
        "target_type": "elasticache",
        "collector_output": {
            "queries": {"query_patterns": [_query(0)]},
            "cache_invalidation_context": {
                "note": "Read-only context",
                "write_queries": [
                    {"query_id": qid, "query_text": text, "context_only": True}
                    for qid, text in texts.items()
                ],
            },
        },
    }
    text, _ = render_external_request(request)
    loaded = json.loads(text)
    writes = loaded["collector_output"]["cache_invalidation_context"]["write_queries"]
    for write in writes:
        original = texts[write["query_id"]]
        assert write["query_text"] == original
        if len(json.dumps(original)) > 1500:
            assert " ".join(write["query_text_lines"]) == " ".join(original.split())
        else:
            assert "query_text_lines" not in write
    # Every long text is reachable through short lines: the only lines Read would cut
    # are the original query_text fields, each repeated in full as query_text_lines.
    cut = [x for x in text.splitlines() if len(x) >= READ_LINE_LIMIT]
    assert all('"query_text"' in x for x in cut) and len(cut) == 3
    assert max(len(x) for x in text.splitlines() if '"query_text"' not in x) < READ_LINE_LIMIT


def test_any_long_string_field_gets_lines_and_existing_lines_are_left_alone():
    long = "word " * 600
    request = {
        "a": {"note": long, "short": "x", "note_lines": ["kept"]},
        "b": [{"free_text": long}],
    }
    loaded = json.loads(render_external_request(request)[0])
    assert loaded["a"]["note_lines"] == ["kept"]
    assert "short_lines" not in loaded["a"]
    assert " ".join(loaded["b"][0]["free_text_lines"]) == long.strip()
    assert request["b"][0].keys() == {"free_text"}
