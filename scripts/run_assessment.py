#!/usr/bin/env python3
"""Run the database modernization assessment pipeline.

By default, runs collect → triage → analysis → assignment → reality-check and STOPS.
Schema design and synthesis require LLM reasoning and are handled by Claude Code
via /modernize slash commands. Use --all only for local testing with the orchestrator.

Usage:
    # Assessment (collect through reality-check, then stops):
    uv run python scripts/run_assessment.py --file <collector_file> [--db <name>]

    # Resume from existing job (triage onward):
    uv run python scripts/run_assessment.py --job-id <id> --db <name>

    # Finalize reality check after LLM response is written:
    uv run python scripts/run_assessment.py --job-id <id> --db <name> --resume-reality-check

    # Full pipeline with Bedrock LLM (schema design + synthesis via orchestrator):
    uv run python scripts/run_assessment.py --file <collector_file> --all -y --llm-mode bedrock

Outputs JSON to stdout after each phase (one line per phase for UI/orchestrator progress).
Updates .modernizer-state.json after each phase so the UI can track progress.
"""

import json
import os
import re
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("RUNTIME_MODE", "local")
os.environ.setdefault("ARTIFACT_DIR", "./artifacts")

STATE_FILE = ".modernizer-state.json"

# ============================================================
# Colored output: auto-colorize [phase] prefixes on stdout/stderr
# ============================================================

_PHASE_RE = re.compile(r"^(\[[\w./-]+\])")


class _ColorizedStream:
    """Wraps a stream to colorize [phase-name] prefixes in cyan."""

    def __init__(self, stream):
        self._stream = stream

    def write(self, text: str) -> int:
        # Colorize each line that starts with [something]
        lines = text.split("\n")
        colored = []
        for line in lines:
            m = _PHASE_RE.match(line)
            if m:
                prefix = m.group(1)
                rest = line[m.end() :]
                colored.append(f"\033[36m{prefix}\033[0m{rest}")
            else:
                colored.append(line)
        return self._stream.write("\n".join(colored))  # type: ignore[no-any-return]

    def flush(self) -> None:
        self._stream.flush()

    def __getattr__(self, name):
        return getattr(self._stream, name)


sys.stdout = _ColorizedStream(sys.stdout)  # type: ignore[assignment]


def _output(phase: str, data: dict) -> None:
    """Print phase progress as JSON to stdout."""
    print(json.dumps({"phase": phase, **data}), flush=True)


def _error(phase: str, message: str) -> None:
    _output(phase, {"status": "error", "message": message})
    sys.exit(1)


def _log(phase: str, msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(f"[{phase}] {msg}  [{ts}]", flush=True)


def _log_artifact(phase: str, path: str) -> None:
    """Print artifact output path with green highlight."""
    print(f"[{phase}] Output available at: \033[32m{path}\033[0m", flush=True)


def _banner(title: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(f"\n{'='*60}", file=sys.stderr, flush=True)
    print(f"  {title}  [{ts}]", file=sys.stderr, flush=True)
    print(f"{'='*60}", file=sys.stderr, flush=True)


def _read_state() -> dict:  # type: ignore[type-arg]
    if not os.path.exists(STATE_FILE):
        return {}
    with open(STATE_FILE) as f:
        return json.load(f)  # type: ignore[no-any-return]


def _write_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)
        f.write("\n")


# ============================================================
# Phase: Collect
# ============================================================
def phase_collect(collector_file: str, db_name: str | None, store) -> tuple[str, str]:
    """Parse collector output and initialize artifacts. Returns (job_id, db_name)."""
    _banner("COLLECT")
    if not os.path.exists(collector_file):
        _error("collect", f"File not found: {collector_file}")

    with open(collector_file, encoding="utf-8") as f:
        content = f.read().strip()
        if not content.startswith("{") and "\n" in content:
            content = content[content.index("\n") + 1 :]
        input_data = json.loads(content)

    is_contract = isinstance(input_data.get("contract_version"), str) and input_data[
        "contract_version"
    ].startswith("3")
    is_raw = "collection_version" in input_data or isinstance(input_data.get("queries"), list)

    job_id = str(uuid.uuid4())[:8]

    if is_contract:
        collector_data = input_data
        db_name = db_name or (
            collector_data.get("metadata", {})
            .get("source_database", {})
            .get("database_name", "unknown_db")
        )
    elif is_raw:
        db_name = db_name or input_data.get("metadata", {}).get("database_name", "unknown_db")
        if not db_name and isinstance(input_data.get("metadata"), str):
            meta = json.loads(input_data["metadata"])
            db_name = meta.get("database_name", meta.get("schema_name", "unknown_db"))

        # Upload raw file
        upload_path = f"{db_name}/{job_id}/uploads/collector-output.json"
        store.write_json(upload_path, input_data)

        # Parse offline collection
        from src.tools.database.offline_parser import parse_offline_collection

        parsed = parse_offline_collection(input_data)

        # Build collector contract output
        import time

        from src.agents.collector.mysql_collector import (
            _build_metrics,
            _build_output,
            _build_procedures,
            _build_queries,
            _build_tables,
            _build_triggers,
            _build_views,
        )
        from src.contracts.collector_input import CollectorInput

        start = time.monotonic()
        tables_built = _build_tables(parsed["tables"], db_name)
        queries_built = _build_queries(parsed.get("queries", []))
        views = _build_views(parsed.get("views", []))
        procedures = _build_procedures(parsed.get("procedures", []))
        triggers = _build_triggers(parsed.get("triggers", []))
        metrics = _build_metrics(queries_built, None)

        version_str = (parsed.get("metadata", {}).get("version") or "").lower()
        if "postgresql" in version_str or "postgres" in version_str:
            detected_engine = "postgresql"
            detected_port = 5432
        else:
            detected_engine = "mysql"
            detected_port = 3306

        inp = CollectorInput.model_validate(
            {
                "job_id": job_id,
                "engine": detected_engine,
                "cluster_endpoint": "offline",
                "port": detected_port,
                "database_name": db_name,
                "mode": "offline",
                "offline_config": {"s3_bucket": "local", "s3_key": upload_path},
            }
        )

        offline_meta = parsed.get("metadata", {})
        collector_data = json.loads(
            _build_output(
                inp,
                start,
                version=offline_meta.get("version", "unknown"),
                db_size=offline_meta.get("database_size_gb"),
                tables=tables_built,
                queries=queries_built,
                metrics=metrics,
                rds_meta=None,
                views=views,
                procedures=procedures,
                triggers=triggers,
            ).model_dump_json()
        )
    else:
        _error("collect", "Unrecognized file format.")
        return "", ""  # unreachable

    # Write collector output
    collector_path = f"{db_name}/{job_id}/collector/output.json"
    store.write_json(collector_path, collector_data)

    # Materialize query journey files
    from src.agents.query_journey_materializer import materialize_source

    materialize_source(collector_data, db_name, job_id, store)

    tables = collector_data.get("database_schema", {}).get("tables", [])
    queries = collector_data.get("queries", {}).get("query_patterns", [])

    artifact = f"{db_name}/{job_id}/collector/output.json"
    _log_artifact("collect", artifact)
    _output(
        "collect",
        {
            "status": "complete",
            "job_id": job_id,
            "database_name": db_name,
            "tables": len(tables),
            "queries": len(queries),
            "artifact": artifact,
        },
    )
    return job_id, db_name


# ============================================================
# Phase: Triage
# ============================================================
def phase_triage(store, job_id: str, db: str) -> list[str]:
    _banner("TRIAGE")
    from src.agents.referee.triage_handler import run_triage

    run_triage(job_id, db, store)

    triage_path = f"{db}/{job_id}/referee-triage/triage.json"
    triage_output = store.read_json(triage_path)

    selected = [a["agent_type"] for a in triage_output.get("selected_agents", [])]
    skipped = [a["agent_type"] for a in triage_output.get("skipped_agents", [])]

    artifact = f"{db}/{job_id}/referee-triage/triage.json"
    _log_artifact("triage", artifact)
    _output(
        "triage",
        {"status": "complete", "selected": selected, "skipped": skipped, "artifact": artifact},
    )
    return selected


# ============================================================
# Phase: Analysis
# ============================================================
def phase_analysis(
    store, job_id: str, db: str, selected_engines: list[str], llm_mode: str = "none"
) -> dict:
    _banner(f"ANALYSIS ({len(selected_engines)} engines in parallel)")
    from src.agents.analysis.handler import run_analysis

    _log("analysis", f"Engines: {selected_engines} (llm_mode={llm_mode})")
    results = {}
    if len(selected_engines) > 1:
        with ThreadPoolExecutor(max_workers=len(selected_engines)) as pool:
            futures = {
                pool.submit(run_analysis, job_id, db, engine, store, llm_mode=llm_mode): engine
                for engine in selected_engines
            }
            for future in as_completed(futures):
                engine = futures[future]
                try:
                    future.result()
                    results[engine] = "complete"
                    _log("analysis", f"{engine} complete")
                except Exception as e:
                    results[engine] = f"error: {e}"
                    _log("analysis", f"{engine} FAILED: {e}")
    else:
        for engine in selected_engines:
            try:
                run_analysis(job_id, db, engine, store, llm_mode=llm_mode)
                results[engine] = "complete"
                _log("analysis", f"{engine} complete")
            except Exception as e:
                results[engine] = f"error: {e}"
                _log("analysis", f"{engine} FAILED: {e}")

    artifacts = {
        engine: f"{db}/{job_id}/analysis-{engine}/"
        for engine in results
        if results[engine] == "complete"
    }
    for engine, path in artifacts.items():
        _log_artifact(f"analysis/{engine}", path)
    _output("analysis", {"status": "complete", "results": results, "artifacts": artifacts})
    return results


# ============================================================
# Phase: Assignment
# ============================================================
def phase_assignment(store, job_id: str, db: str) -> dict:
    _banner("ASSIGNMENT")
    from src.agents.referee.assignment_handler import run_assignment_resolver

    run_assignment_resolver(job_id, db, store)

    assignment_path = f"{db}/{job_id}/assignment/v1/assignment.json"
    if not store.exists(assignment_path):
        _error("assignment", "Assignment output not produced.")

    assignment = store.read_json(assignment_path)
    distribution: dict[str, int] = {}
    for q in assignment.get("query_assignments", []):
        engine = q.get("assigned_engine", "unknown")
        distribution[engine] = distribution.get(engine, 0) + 1

    total = sum(distribution.values())
    artifact = f"{db}/{job_id}/assignment/v1/assignment.json"
    _log_artifact("assignment", artifact)
    _output(
        "assignment",
        {
            "status": "complete",
            "distribution": distribution,
            "total_queries": total,
            "artifact": artifact,
        },
    )
    return distribution


# ============================================================
# Phase: Reality Check
# ============================================================
def phase_reality_check(store, job_id: str, db: str, llm_mode: str) -> str:
    _banner("REALITY CHECK")
    from src.agents.referee.reality_check_handler import run_reality_check_handler

    run_reality_check_handler(job_id, db, store, assignment_version=1, llm_mode=llm_mode)

    if llm_mode == "external":
        llm_input_path = f"{db}/{job_id}/reality-check/llm_input.json"
        if store.exists(llm_input_path):
            _output("reality_check", {"status": "awaiting_llm", "llm_request": llm_input_path})
            return "awaiting_llm"

    artifact = f"{db}/{job_id}/reality-check/output.json"
    _log_artifact("reality-check", artifact)
    _output("reality_check", {"status": "complete", "artifact": artifact})
    return "complete"


def phase_reality_check_finalize(store, job_id: str, db: str, assignment_version: int = 1) -> None:
    """Finalize reality check after LLM response has been written."""
    from src.agents.referee.reality_check_handler import (
        apply_reality_check_llm_output,
        run_reality_check_deterministic,
    )
    from src.contracts.reality_check_output import RealityCheckOutputContract

    det = run_reality_check_deterministic(job_id, db, store, assignment_version)

    llm_response_path = f"{db}/{job_id}/llm_responses/reality_check.json"
    if not store.exists(llm_response_path):
        _error("reality_check", f"LLM response not found at {llm_response_path}")

    llm_response = store.read_json(llm_response_path)
    result = apply_reality_check_llm_output(det, llm_response)

    output = RealityCheckOutputContract.model_validate(
        {
            "source_assignment_version": assignment_version,
            "unique_value_assessment": result["unique_value_assessment"],
            "consolidations": result["consolidations"],
            "architectural_patterns": result["architectural_patterns"],
            "executive_summary": result["executive_summary"],
            "recommendations": result["recommendations"],
            "before_distribution": result["before_distribution"],
            "after_distribution": result["after_distribution"],
            "lightweight_recommendations": result.get("lightweight_recommendations", []),
        }
    )
    output_key = f"{db}/{job_id}/reality-check/output.json"
    store.write_json(output_key, output.model_dump(mode="json"))

    # If consolidation occurred, write a new assignment version
    assignment = result["assignment"]
    if result["consolidations"]:
        new_version = assignment_version + 1
        revised_assignment = {
            **assignment,
            "version": new_version,
            "query_assignments": result["revised_assignments"],
            "reality_check_applied": True,
        }
        revised_key = f"{db}/{job_id}/assignment/v{new_version}/assignment.json"
        store.write_json(revised_key, revised_assignment)

    artifact = f"{db}/{job_id}/reality-check/output.json"
    _log_artifact("reality-check", artifact)
    _output("reality_check", {"status": "complete", "finalized": True, "artifact": artifact})


# ============================================================
# Phase: Schema Design
# ============================================================
def phase_schema_design(store, job_id: str, db: str, llm_mode: str) -> None:
    _banner("SCHEMA DESIGN")
    from src.contracts.phase_models import Phase, PhaseStatus
    from src.orchestrator.local_orchestrator import LocalOrchestrator

    orch = LocalOrchestrator(store=store, llm_mode=llm_mode)
    progression = orch.get_progression(job_id)
    progression.phases[Phase.REALITY_CHECK].status = PhaseStatus.COMPLETED
    from src.contracts.phase_models import Phase as P

    orch._set_phase_status(progression, P.ASSIGNMENT_REVIEW, PhaseStatus.COMPLETED)
    orch._save_progression(progression)

    orch.resume(job_id, Phase.SCHEMA_DESIGN)

    # Post-schema routing
    orch._run_post_schema_routing(job_id, db)

    artifact = f"{db}/{job_id}/schema-*/v1/schema_output.json"
    _log_artifact("schema-design", artifact)
    _output("schema_design", {"status": "complete", "artifact": artifact})


# ============================================================
# Phase: Synthesis
# ============================================================
def phase_synthesis(store, job_id: str, db: str, llm_mode: str) -> None:
    _banner("SYNTHESIS")
    from src.contracts.phase_models import Phase, PhaseStatus
    from src.orchestrator.local_orchestrator import LocalOrchestrator

    orch = LocalOrchestrator(store=store, llm_mode=llm_mode)
    progression = orch.get_progression(job_id)
    progression.phases[Phase.SCHEMA_DESIGN].status = PhaseStatus.COMPLETED
    orch._save_progression(progression)

    orch.resume(job_id, Phase.SYNTHESIS)
    artifact = f"{db}/{job_id}/referee-synthesis/report.json"
    _log_artifact("synthesis", artifact)
    _output("synthesis", {"status": "complete", "artifact": artifact})


# ============================================================
# Main
# ============================================================
def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Run the database modernization pipeline.")
    parser.add_argument("--file", help="Path to collector output JSON (starts from collect phase)")
    parser.add_argument("--job-id", help="Existing job ID (skips collect phase)")
    parser.add_argument("--db", help="Database name (auto-detected from file if not provided)")
    parser.add_argument(
        "--llm-mode",
        default="external",
        choices=["none", "bedrock", "external"],
        help="LLM mode for reality check and schema design (default: external)",
    )
    parser.add_argument(
        "--resume-reality-check",
        action="store_true",
        help="Resume reality check after LLM response has been written",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Run full pipeline including schema design and synthesis",
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Skip interactive pauses (auto-approve)",
    )
    parser.add_argument(
        "--artifact-root",
        default="./artifacts",
        help="Root directory for local artifacts (default: ./artifacts)",
    )
    args = parser.parse_args()

    from src.storage.local_store import LocalArtifactStore

    store = LocalArtifactStore(base_dir=args.artifact_root)

    # Resume reality check after LLM response has been written
    if args.resume_reality_check:
        if not args.job_id or not args.db:
            _error("init", "--resume-reality-check requires --job-id and --db")
        phase_reality_check_finalize(store, args.job_id, args.db)
        state = _read_state()
        state["phase_status"]["reality_check"] = "complete"
        state["current_phase"] = "schema_design"
        _write_state(state)
        return

    # Header
    print(f"\n{'='*60}", file=sys.stderr)
    print("  Database Modernizer Assessment — Assessment Pipeline", file=sys.stderr)
    print(f"{'='*60}", file=sys.stderr)
    if args.file:
        print(f"  Input:     {args.file}", file=sys.stderr)
    if args.db:
        print(f"  Database:  {args.db}", file=sys.stderr)
    print(f"  LLM mode:  {args.llm_mode}", file=sys.stderr)
    print("  Artifacts: ./artifacts/", file=sys.stderr)
    print(f"{'='*60}\n", file=sys.stderr, flush=True)

    # Determine starting point
    if args.file:
        # Full pipeline from collector file
        job_id, db_name = phase_collect(args.file, args.db, store)
        state = {
            "job_id": job_id,
            "database_name": db_name,
            "current_phase": "triage",
            "selected_engines": [],
            "llm_mode": args.llm_mode,
            "experience_mode": "both",
            "phase_status": {"collect": "complete"},
        }
        _write_state(state)
    elif args.job_id:
        # Start from triage with existing job
        job_id = args.job_id
        db_name = args.db
        if not db_name:
            _error("init", "--db is required when using --job-id")
        state = _read_state()
        if not state:
            state = {
                "job_id": job_id,
                "database_name": db_name,
                "current_phase": "triage",
                "selected_engines": [],
                "llm_mode": args.llm_mode,
                "experience_mode": "both",
                "phase_status": {"collect": "complete"},
            }
    else:
        _error("init", "Either --file or --job-id is required")
        return  # unreachable

    # Phase: Triage
    selected_engines = phase_triage(store, job_id, db_name)
    state["selected_engines"] = selected_engines
    state["phase_status"]["triage"] = "complete"
    state["current_phase"] = "analysis"
    _write_state(state)

    # Phase: Analysis — always deterministic (llm-mode none).
    # The LLM advisor enriches text but does not change routing decisions.
    # Real LLM value starts at reality-check. Pass --llm-mode to analysis only
    # if you explicitly want richer recommendation text (at ~9min cost).
    phase_analysis(store, job_id, db_name, selected_engines, llm_mode="none")
    state["phase_status"]["analysis"] = "complete"
    state["current_phase"] = "assignment"
    _write_state(state)

    # Phase: Assignment
    phase_assignment(store, job_id, db_name)
    state["phase_status"]["assignment"] = "complete"
    state["current_phase"] = "reality_check"
    _write_state(state)

    # Phase: Reality Check
    rc_status = phase_reality_check(store, job_id, db_name, args.llm_mode)
    if rc_status == "awaiting_llm":
        state["phase_status"]["reality_check"] = "awaiting_llm"
        _write_state(state)
        return  # Stop here — resume with --resume-reality-check after LLM response
    state["phase_status"]["reality_check"] = "complete"
    state["current_phase"] = "schema_design"
    _write_state(state)

    # If --all, continue to schema design and synthesis
    if args.all:
        if not args.yes:
            print(
                "\n  Assignment complete. Press Enter to continue to Schema Design (Ctrl+C to stop).",
                file=sys.stderr,
            )
            input()

        phase_schema_design(store, job_id, db_name, args.llm_mode)
        state["phase_status"]["schema_design"] = "complete"
        state["current_phase"] = "synthesis"
        _write_state(state)

        phase_synthesis(store, job_id, db_name, args.llm_mode)
        state["phase_status"]["synthesis"] = "complete"
        state["current_phase"] = "done"
        _write_state(state)


if __name__ == "__main__":
    main()
