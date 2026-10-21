"""The headless PE review step: request written, answer applied, flags recorded (#1059).

``run_pe_review.py`` writes one review request per design (per group for
DynamoDB), a review subagent writes the answer, and ``--finalize`` records it
in the design trace where the post-schema router reads ``routing_flags``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from scripts import run_pe_review, run_schema_design
from src.agents.referee.post_schema_router import routing_flags_from_trace
from src.agents.schema_design import pe_review_external as pe
from src.storage.local_store import LocalArtifactStore

DB, JOB = "shop", "job-1"
V = 2
PREFIX = f"{DB}/{JOB}"


def _flag(query_ids: list[str], engine: str = "documentdb") -> dict:
    return {
        "query_ids": query_ids,
        "flagged_engine": engine,
        "reason_category": "full_scan",
        "explanation": "filters on a non-indexed field only",
    }


def _review(flags: list[dict] | None = None, verdict: str = "APPROVED") -> dict:
    return {
        "verdict": verdict,
        "summary": "Serves the reads it was given.",
        "change_requests": [],
        "strengths": ["one collection per aggregate"],
        "pe_notes": ["watch the document size of orders"],
        "routing_flags": flags or [],
    }


@pytest.fixture
def store(tmp_path: Path) -> LocalArtifactStore:
    s = LocalArtifactStore(base_dir=str(tmp_path))
    s.write_json(f"{PREFIX}/collector/output.json", {"queries": {"query_patterns": []}})
    s.write_json(
        f"{PREFIX}/assignment/v{V}/assignment.json",
        {
            "version": V,
            "query_assignments": [
                {"query_id": "q1", "assigned_engine": "documentdb", "in_scope": True},
                {"query_id": "q2", "assigned_engine": "documentdb", "in_scope": True},
                {"query_id": "q3", "assigned_engine": "documentdb", "in_scope": False},
                {"query_id": "q4", "assigned_engine": "dynamodb", "in_scope": True},
                {"query_id": "q5", "assigned_engine": "dynamodb", "in_scope": True},
                {"query_id": "q6", "assigned_engine": "aurora_mysql", "in_scope": True},
            ],
        },
    )
    s.write_json(
        f"{PREFIX}/schema-documentdb/v{V}/schema_output.json",
        {
            "collections": [{"collection_name": "orders"}],
            "access_patterns": [{"query_ids": ["q1"]}],
        },
    )
    s.write_json(f"{PREFIX}/schema-aurora_mysql/v{V}/schema_output.json", {"tables": []})
    base = f"{PREFIX}/schema-dynamodb/v{V}"
    s.write_json(
        f"{base}/groups_manifest.json",
        {"groups": [{"group_index": 0}, {"group_index": 1}], "total_groups": 2},
    )
    for idx, qid in ((0, "q4"), (1, "q5")):
        s.write_json(
            f"{base}/input_group_{idx}.json",
            {
                "collector_output": {
                    "queries": {
                        "query_patterns": [{"query_id": qid, "tables_accessed": ["shop.carts"]}]
                    },
                    "tables": [{"table_name": "carts"}],
                }
            },
        )
        s.write_json(
            f"{base}/schema_draft_group_{idx}.json",
            {"table_definitions": [], "access_patterns": [{"query_ids": [qid]}]},
        )
    return s


def _script(module, monkeypatch, capsys, root: str, *extra: str) -> tuple[int, dict]:
    monkeypatch.setattr(
        sys,
        "argv",
        ["script", "--job-id", JOB, "--db", DB, "--artifact-root", root, *extra],
    )
    code = 0
    try:
        module.main()
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    return code, json.loads(lines[-1])


class TestPrepare:
    def test_one_request_per_design_and_per_dynamodb_group(self, store) -> None:
        prepared = pe.prepare_pe_review_llm_input(store, DB, JOB, V)
        assert [(r["engine"], r["group"]) for r in prepared["reviews"]] == [
            ("documentdb", None),
            ("dynamodb", 0),
            ("dynamodb", 1),
        ]
        request = store.read_json(f"{PREFIX}/llm_requests/pe_review_documentdb.json")
        assert request["skill_prompt"] == "src/skills/documentdb-pe-review.md"
        # Only the in-scope queries on the engine: a flag names only these.
        assert request["query_ids"] == ["q1", "q2"]
        assert "routing_flags" in request["output_schema"]["properties"]
        assert request["design"]["collections"] == [{"collection_name": "orders"}]
        group = store.read_json(f"{PREFIX}/llm_requests/pe_review_dynamodb_group_1.json")
        assert group["query_ids"] == ["q5"] and group["group"] == 1
        assert group["response_file"].endswith("llm_responses/pe_review_dynamodb_group_1.json")

    def test_every_request_says_its_content_is_data(self, store) -> None:
        # #1317: the design carries customer table and column names.
        from src.agents import prompt_framing

        prepared = pe.prepare_pe_review_llm_input(store, DB, JOB, V)
        assert prepared["reviews"]
        for review in prepared["reviews"]:
            request = json.loads(Path(review["request"]).read_text(encoding="utf-8"))
            first = next(iter(request))
            assert first == "untrusted_data_notice"
            assert request[first] == getattr(prompt_framing, "REQUEST_FILE_NOTICE", None)

    def test_engines_whose_review_cannot_flag_are_skipped_with_a_reason(self, store) -> None:
        prepared = pe.prepare_pe_review_llm_input(store, DB, JOB, V)
        [skip] = prepared["skipped"]
        assert skip["engine"] == "aurora_mysql" and "routing_flags" in skip["reason"]
        for engine in ("aurora_mysql", "aurora_postgresql", "elasticache"):
            assert not pe.can_flag(engine)
        for engine in ("dynamodb", "documentdb", "opensearch"):
            assert pe.can_flag(engine)

    def test_an_undesigned_engine_and_a_skipped_design_are_not_reviewed(self, store) -> None:
        store.write_json(
            f"{PREFIX}/schema-opensearch/v{V}/schema_output.json",
            {"status": "skipped", "reason": "nothing assigned", "target_type": "opensearch"},
        )
        prepared = pe.prepare_pe_review_llm_input(store, DB, JOB, V, ["opensearch", "documentdb"])
        assert [r["engine"] for r in prepared["reviews"]] == ["documentdb"]
        assert prepared["skipped"][0]["engine"] == "opensearch"
        assert pe.prepare_pe_review_llm_input(store, DB, JOB, 9, ["documentdb"])["reviews"] == []

    def test_a_recorded_review_is_not_asked_again(self, store) -> None:
        pe.prepare_pe_review_llm_input(store, DB, JOB, V)
        store.write_json(f"{PREFIX}/llm_responses/pe_review_documentdb.json", _review())
        assert (
            pe.apply_pe_review_llm_output(store, DB, JOB, V, "documentdb")["status"] == "complete"
        )
        again = pe.prepare_pe_review_llm_input(store, DB, JOB, V)
        assert [(r["engine"], r["group"]) for r in again["reviews"]] == [
            ("dynamodb", 0),
            ("dynamodb", 1),
        ]
        assert again["recorded"] == [{"engine": "documentdb", "group": None}]


class TestApply:
    def test_the_flags_land_where_the_router_reads_them(self, store) -> None:
        pe.prepare_pe_review_llm_input(store, DB, JOB, V)
        store.write_json(
            f"{PREFIX}/llm_responses/pe_review_documentdb.json", _review([_flag(["q2"])])
        )
        status = pe.apply_pe_review_llm_output(store, DB, JOB, V, "documentdb")
        assert status["status"] == "complete" and status["routing_flags"] == 1
        assert "warnings" not in status
        trace = store.read_json(f"{PREFIX}/schema-documentdb/v{V}/design_trace.json")
        assert routing_flags_from_trace(trace) == [_flag(["q2"])]
        assert trace["pe_review"]["source"] == "external"
        assert trace["pe_review"]["pe_notes"] == ["watch the document size of orders"]
        assert [e["type"] for e in trace["trace"]] == ["pe_review"]

    def test_a_dynamodb_group_review_goes_to_its_group_trace(self, store) -> None:
        pe.prepare_pe_review_llm_input(store, DB, JOB, V)
        store.write_json(
            f"{PREFIX}/llm_responses/pe_review_dynamodb_group_1.json",
            _review([_flag(["q5"], "dynamodb")]),
        )
        status = pe.apply_pe_review_llm_output(store, DB, JOB, V, "dynamodb", 1)
        assert status["status"] == "complete" and status["group"] == 1
        group_trace = store.read_json(f"{PREFIX}/schema-dynamodb/v{V}/design_trace_group_1.json")
        assert group_trace["routing_flags"] == [_flag(["q5"], "dynamodb")]
        assert not store.exists(f"{PREFIX}/schema-dynamodb/v{V}/design_trace.json")

    def test_dynamodb_needs_a_group_and_other_engines_have_none(self, store) -> None:
        pe.prepare_pe_review_llm_input(store, DB, JOB, V)
        assert pe.apply_pe_review_llm_output(store, DB, JOB, V, "dynamodb")["status"] == "error"
        bad = pe.apply_pe_review_llm_output(store, DB, JOB, V, "documentdb", 0)
        assert bad["status"] == "error"

    def test_an_answer_off_its_contract_is_refused_and_nothing_is_recorded(self, store) -> None:
        pe.prepare_pe_review_llm_input(store, DB, JOB, V)
        # CHANGES_REQUESTED needs change requests (the Bedrock contract's own rule).
        store.write_json(
            f"{PREFIX}/llm_responses/pe_review_dynamodb_group_0.json",
            _review(verdict="CHANGES_REQUESTED"),
        )
        status = pe.apply_pe_review_llm_output(store, DB, JOB, V, "dynamodb", 0)
        assert status["status"] == "validation_failed" and status["errors"]
        store.write_json(f"{PREFIX}/llm_responses/pe_review_documentdb.json", {"verdict": "MAYBE"})
        status = pe.apply_pe_review_llm_output(store, DB, JOB, V, "documentdb")
        assert status["status"] == "validation_failed"
        assert any(e.startswith("verdict:") for e in status["errors"])
        assert not any("MAYBE" in e for e in status["errors"])  # never the answer's text
        for key in (
            f"{PREFIX}/schema-documentdb/v{V}/design_trace.json",
            f"{PREFIX}/schema-dynamodb/v{V}/design_trace_group_0.json",
        ):
            assert not store.exists(key)

    def test_flags_the_router_will_ignore_are_warnings_and_kept_raw(self, store) -> None:
        """Recorded as the reviewer gave them (the router checks each on its own,
        as on the Bedrock path); the warnings tell the reviewer what it ignores."""
        pe.prepare_pe_review_llm_input(store, DB, JOB, V)
        flags = [
            _flag(["q1"]),
            _flag(["q4"], "dynamodb"),  # another engine
            _flag(["q3", "q9"]),  # out of scope, and not this engine's
            {"query_ids": ["q1"], "flagged_engine": "documentdb", "target_engine": "dynamodb"},
        ]
        store.write_json(f"{PREFIX}/llm_responses/pe_review_documentdb.json", _review(flags))
        status = pe.apply_pe_review_llm_output(store, DB, JOB, V, "documentdb")
        assert status["status"] == "complete" and status["routing_flags"] == 4
        warnings = status["warnings"]
        # q4 is another engine's query too, so flag 1 gets both notes.
        assert len(warnings) == 4
        assert any("off the flag contract" in w for w in warnings)
        assert any("flagged_engine is not documentdb" in w for w in warnings)
        assert any("2 query id(s)" in w for w in warnings)
        trace = store.read_json(f"{PREFIX}/schema-documentdb/v{V}/design_trace.json")
        assert trace["routing_flags"] == flags

    def test_no_request_or_no_answer_is_an_error(self, store) -> None:
        assert pe.apply_pe_review_llm_output(store, DB, JOB, V, "documentdb")["status"] == "error"
        pe.prepare_pe_review_llm_input(store, DB, JOB, V)
        missing = pe.apply_pe_review_llm_output(store, DB, JOB, V, "documentdb")
        assert missing["status"] == "error" and "no readable review answer" in missing["message"]
        # A request written for another version is not this one's.
        assert pe.apply_pe_review_llm_output(store, DB, JOB, 3, "documentdb")["status"] == "error"


class TestScript:
    def test_prepare_then_finalize_print_one_line_each(self, store, monkeypatch, capsys) -> None:
        root = str(store.base_dir)
        code, line = _script(run_pe_review, monkeypatch, capsys, root, "--assignment-version", "2")
        assert code == 0 and line["status"] == "awaiting_llm" and len(line["reviews"]) == 3
        documentdb = line["reviews"][0]
        assert documentdb["finalize"] == (
            f"uv run python scripts/run_pe_review.py --job-id {JOB} --db {DB} "
            "--engine documentdb --finalize"
        )
        response = Path(documentdb["response"])
        response.parent.mkdir(parents=True, exist_ok=True)
        response.write_text(json.dumps(_review()))
        code, done = _script(
            run_pe_review,
            monkeypatch,
            capsys,
            root,
            "--assignment-version",
            "2",
            "--engine",
            "documentdb",
            "--finalize",
        )
        assert code == 0 and done["status"] == "complete" and done["verdict"] == "APPROVED"

    def test_nothing_to_review_is_complete(self, store, monkeypatch, capsys) -> None:
        code, line = _script(
            run_pe_review,
            monkeypatch,
            capsys,
            str(store.base_dir),
            "--assignment-version",
            "2",
            "--engine",
            "aurora_mysql",
        )
        assert code == 0 and line["status"] == "complete" and line["reviews"] == []

    def test_finalize_needs_an_engine_and_group_needs_finalize(
        self, store, monkeypatch, capsys
    ) -> None:
        code, line = _script(run_pe_review, monkeypatch, capsys, str(store.base_dir), "--finalize")
        assert code == 1 and line["status"] == "error"
        code, line = _script(
            run_pe_review, monkeypatch, capsys, str(store.base_dir), "--group", "0"
        )
        assert code == 1 and line["status"] == "error"

    def test_a_review_recorded_after_the_merge_makes_the_merge_stale(
        self, store, monkeypatch, capsys
    ) -> None:
        """--merge consolidates the group traces, so --status asks for it again."""
        from src.agents.schema_design import handler

        base = f"{PREFIX}/schema-dynamodb/v{V}"
        groups = run_schema_design._group_entries(
            store, base, store.read_json(f"{base}/groups_manifest.json")
        )
        record = {
            "status": "complete",
            "errors": [],
            "warnings": [],
            "draft_hashes": run_schema_design._hashes(groups),
        }
        store.write_json(f"{base}/merge_record.json", record)
        store.write_json(f"{base}/schema_output.json", {"access_patterns": []})
        monkeypatch.setattr(handler, "run_schema_merge", None)  # never called by --status

        def status() -> str:
            monkeypatch.setattr(
                sys,
                "argv",
                [
                    "run_schema_design.py",
                    "--job-id",
                    JOB,
                    "--db",
                    DB,
                    "--engine",
                    "dynamodb",
                    "--assignment-version",
                    "2",
                    "--artifact-root",
                    str(store.base_dir),
                    "--status",
                ],
            )
            run_schema_design.main()
            return str(json.loads(capsys.readouterr().out.splitlines()[-1])["status"])

        assert status() == "merged"
        pe.prepare_pe_review_llm_input(store, DB, JOB, V)
        store.write_json(f"{PREFIX}/llm_responses/pe_review_dynamodb_group_0.json", _review())
        pe.apply_pe_review_llm_output(store, DB, JOB, V, "dynamodb", 0)
        assert status() == "merge_pending"


class TestShapeAndFailedReviews:
    def test_no_flags_means_no_routing_flags_key_like_bedrock(self, store) -> None:
        pe.prepare_pe_review_llm_input(store, DB, JOB, V)
        store.write_json(f"{PREFIX}/llm_responses/pe_review_documentdb.json", _review())
        pe.apply_pe_review_llm_output(store, DB, JOB, V, "documentdb")
        trace = store.read_json(f"{PREFIX}/schema-documentdb/v{V}/design_trace.json")
        assert "routing_flags" not in trace and trace["pe_review"]["verdict"] == "APPROVED"

    def test_a_failed_review_is_recorded_with_no_flags(self, store) -> None:
        pe.prepare_pe_review_llm_input(store, DB, JOB, V)
        assert pe.missing_reviews(
            store, DB, JOB, V, ["dynamodb", "documentdb", "aurora_mysql"]
        ) == [
            "dynamodb group 0",
            "dynamodb group 1",
            "documentdb",
        ]
        status = pe.apply_pe_review_llm_output(store, DB, JOB, V, "dynamodb", 1, failed=True)
        assert status["status"] == "complete" and status["review_failed"] is True
        trace = store.read_json(f"{PREFIX}/schema-dynamodb/v{V}/design_trace_group_1.json")
        assert routing_flags_from_trace(trace) == []
        assert [e["type"] for e in trace["trace"]] == ["pe_error"]
        assert pe.missing_reviews(store, DB, JOB, V, ["dynamodb"]) == ["dynamodb group 0"]

    def test_failed_needs_finalize(self, store, monkeypatch, capsys) -> None:
        code, line = _script(run_pe_review, monkeypatch, capsys, str(store.base_dir), "--failed")
        assert code == 1 and line["status"] == "error"
