"""The external Reality Check request is compact, bounded and complete (#285).

The request used to embed the full collector and analysis outputs: about 7.5 MB
on the discourse sample, more than a headless ``/reality-check`` subagent can
read, and without the list of moved queries. These tests generate the real
requests for both bundled samples (``run_assessment.py --llm-mode external``,
no model calls) and check the size bound, the Read pages, that every field the
command uses is there, and that a correction taken from the request is applied
at finalize.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from src.agents.referee.consolidation_validator import (
    MAX_QUERIES_PER_CALL,
    validate_consolidations,
)
from src.agents.referee.reality_check_request import (
    SQL_CHARS,
    moved_queries,
    moved_query_record,
    with_read_pages,
)
from src.agents.schema_design.group_input import READ_PAGE_CHARS, READ_PAGE_LINES

REPO = Path(__file__).resolve().parents[4]
SCRIPT = REPO / "scripts" / "run_assessment.py"

# What the discourse request must stay under (it was about 7.5 MB).
MAX_REQUEST_BYTES = 400_000
# Read truncates longer lines, so a record must fit on one line.
MAX_LINE_CHARS = 2000

MOVED_QUERY_FIELDS = {"query_id", "type", "cps", "tables", "signals", "sql"}


def _env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("AWS_")}
    env["AWS_DEFAULT_REGION"] = "us-east-1"  # and no credentials: no model calls
    return env


def _run(args: list[str], cwd: Path) -> dict:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=600,
        env=_env(),
    )
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    status: dict = json.loads(proc.stdout.strip().splitlines()[-1])
    return status


def _external_run(name: str, tmp: Path) -> dict:
    with zipfile.ZipFile(REPO / "docs" / "examples" / name / f"{name}.zip") as z:
        z.extractall(tmp)
    root = tmp / "artifacts"
    status = _run(
        [
            "--file",
            str(tmp / f"{name}-collection.json"),
            "--db",
            name,
            "--llm-mode",
            "external",
            "--artifact-root",
            str(root),
        ],
        tmp,
    )
    assert status["status"] == "awaiting_llm"
    path = root / status["llm_request"]
    return {"cwd": tmp, "root": root, "db": name, "path": path, "text": path.read_text()}


@pytest.fixture(scope="module")
def discourse(tmp_path_factory) -> dict:
    return _external_run("discourse", tmp_path_factory.mktemp("discourse"))


@pytest.fixture(scope="module")
def wordpress(tmp_path_factory) -> dict:
    return _external_run("wordpress", tmp_path_factory.mktemp("wordpress"))


def _assert_readable(text: str) -> dict:
    request: dict = json.loads(text)
    lines = text.splitlines()
    assert lines[1].startswith('  "read_pages": '), "read_pages must be on line 2"
    pages = request["read_pages"]
    assert pages[0]["offset"] == 1
    covered = 0
    for page in pages:
        assert page["offset"] == covered + 1, "pages must be contiguous"
        chunk = lines[page["offset"] - 1 : page["offset"] - 1 + page["limit"]]
        assert len(chunk) == page["limit"]
        assert sum(len(ln) + 1 for ln in chunk) <= READ_PAGE_CHARS
        assert page["limit"] <= READ_PAGE_LINES
        covered += page["limit"]
    assert covered == len(lines), "pages must cover the whole file"
    assert max(len(ln) for ln in lines) <= MAX_LINE_CHARS
    return request


class TestDiscourseRequestIsBounded:
    def test_size_bound(self, discourse):
        assert len(discourse["text"].encode()) < MAX_REQUEST_BYTES

    def test_every_page_is_readable(self, discourse):
        _assert_readable(discourse["text"])

    def test_no_full_collector_or_analysis(self, discourse):
        cv = json.loads(discourse["text"])["consolidation_validation"]
        assert set(cv) == {"consolidations"}

    def test_every_moved_query_is_listed(self, discourse):
        request = json.loads(discourse["text"])
        consolidations = request["consolidation_validation"]["consolidations"]
        assert consolidations, "the discourse sample consolidates"
        for c in consolidations:
            assert len(c["moved_queries"]) == c["query_count"]
            for q in c["moved_queries"]:
                assert MOVED_QUERY_FIELDS <= set(q)
                assert len(q["sql"]) <= SQL_CHARS


class TestWordpressRequestCarriesWhatTheCommandUses:
    def test_fields(self, wordpress):
        request = _assert_readable(wordpress["text"])
        consolidations = request["consolidation_validation"]["consolidations"]
        assert consolidations
        for c in consolidations:
            assert {"from_engine", "to_engine", "query_count", "reason", "moved_queries"} <= set(c)
            assert len(c["moved_queries"]) == c["query_count"]
            for q in c["moved_queries"]:
                assert MOVED_QUERY_FIELDS <= set(q)
        es = request["executive_summary"]
        for key in (
            "before_distribution",
            "after_distribution",
            "consolidations",
            "unique_value_assessment",
            "architectural_patterns",
            "recommendations",
            "absorption_candidates",
            "scope",
        ):
            assert key in es
        for entry in es["unique_value_assessment"].values():
            assert "unique_queries_count" in entry and "is_mandatory" in entry
            assert "unique_queries" not in entry

    def test_command_reads_the_fields_it_names(self):
        command = (REPO / ".claude" / "commands" / "reality-check.md").read_text()
        for name in ("read_pages", "moved_queries", "absorption_candidates", "sql_chars"):
            assert name in command
        assert "query_signals" not in command
        assert "one shot" not in command

    def test_a_correction_from_the_request_is_applied_at_finalize(self, wordpress):
        request = json.loads(wordpress["text"])
        consolidation = request["consolidation_validation"]["consolidations"][0]
        query_id = consolidation["moved_queries"][0]["query_id"]
        job_dir = wordpress["path"].parent.parent
        response = job_dir / "llm_responses" / "reality_check.json"
        response.parent.mkdir(parents=True, exist_ok=True)
        response.write_text(
            json.dumps(
                {
                    "consolidation_corrections": [
                        {
                            "query_id": query_id,
                            "original_engine": consolidation["from_engine"],
                            "reason": "test",
                        }
                    ],
                    "executive_summary": "Your workload runs on purpose-built AWS databases.",
                }
            )
        )
        _run(
            [
                "--job-id",
                job_dir.name,
                "--db",
                wordpress["db"],
                "--resume-reality-check",
                "--artifact-root",
                str(wordpress["root"]),
            ],
            wordpress["cwd"],
        )
        output = json.loads((job_dir / "reality-check" / "output.json").read_text())
        version = output["output_assignment_version"]
        final = json.loads((job_dir / "assignment" / f"v{version}" / "assignment.json").read_text())
        moved = next(qa for qa in final["query_assignments"] if qa["query_id"] == query_id)
        assert moved["assigned_engine"] != consolidation["to_engine"]


class TestSharedRecords:
    def test_moved_by_engine_change_includes_absorption(self):
        original = [
            {"query_id": "a", "assigned_engine": "documentdb"},
            {"query_id": "b", "assigned_engine": "documentdb"},
            {"query_id": "c", "assigned_engine": "dynamodb"},
        ]
        revised = [
            {
                "query_id": "a",
                "assigned_engine": "dynamodb",
                "assignment_reason": "consolidated from documentdb",
            },
            {
                "query_id": "b",
                "assigned_engine": "dynamodb",
                "assignment_reason": "absorbed from documentdb",
            },
            {"query_id": "c", "assigned_engine": "dynamodb", "assignment_reason": "x"},
        ]
        c = {"from_engine": "documentdb", "to_engine": "dynamodb"}
        assert [q["query_id"] for q in moved_queries(c, revised, original)] == ["a", "b"]
        assert [q["query_id"] for q in moved_queries(c, revised)] == ["a"]

    def test_record_truncates_sql(self):
        rec = moved_query_record("q", {"q": {"query_text": "x" * 900}}, {"q": ["joins"]})
        assert len(rec["sql"]) == SQL_CHARS and rec["sql_chars"] == 900
        assert rec["signals"] == ["joins"]
        assert "sql_chars" not in moved_query_record("q", {"q": {"query_text": "s"}}, {})

    def test_read_pages_cover_the_render(self):
        request = with_read_pages({"a": [{"k": "v" * 900} for _ in range(200)]})
        from src.agents.referee.reality_check_request import render_reality_check_request

        _assert_readable(render_reality_check_request(request))


class TestBedrockReviewsEveryMovedQuery:
    def test_batches_all_moved_queries_with_the_shared_records(self):
        n = MAX_QUERIES_PER_CALL * 2 + 5
        original = [{"query_id": f"q{i}", "assigned_engine": "documentdb"} for i in range(n)]
        revised = [
            {"query_id": f"q{i}", "assigned_engine": "dynamodb", "assignment_reason": "absorbed"}
            for i in range(n)
        ]
        query_map = {f"q{i}": {"query_text": "SELECT 1", "query_type": "SELECT"} for i in range(n)}
        consolidations = [{"from_engine": "documentdb", "to_engine": "dynamodb"}]
        with patch(
            "src.agents.referee.consolidation_validator._call_llm_validator", return_value=[]
        ) as call:
            validate_consolidations(consolidations, revised, query_map, {}, original)
        sent = [q for args in call.call_args_list for q in args.kwargs["queries"]]
        assert [q["query_id"] for q in sent] == [f"q{i}" for i in range(n)]
        assert sent[0] == moved_query_record("q0", query_map, {})
        assert all(
            len(args.kwargs["queries"]) <= MAX_QUERIES_PER_CALL for args in call.call_args_list
        )
