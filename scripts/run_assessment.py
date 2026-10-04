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

Output modes (issue #278):
    * Compact (default when stdout is not a terminal, e.g. a headless Claude Code
      session or a pipe): stdout carries only the ``{"phase": ...}`` status lines
      and one final line pointing at the log that holds the banners and progress:
      ``{"log": "artifacts/<db>/<job>/_logs/run_assessment.log", "log_offset": N,
      "log_lines": M}`` (``log_offset``/``log_lines`` = this run's lines, as Read
      ``offset``/``limit``; the log is appended to by later runs of the job). An
      unexpected exception becomes ``{"phase": <phase>, "status": "error", ...}``
      with the traceback in the log. Before the job directory exists the final
      line is ``{"log": null, "output_tail": "..."}``.
    * Verbose (``--verbose``, or stdout is a terminal): the full colored
      progress on stdout and banners on stderr, as before; no log file.
"""

import json
import os
import re
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts._compact_output import CompactConsole, display_path, want_verbose  # noqa: E402

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


LOG_NAME = "run_assessment.log"

# Set by main() in compact mode (see the module docstring); None when verbose.
_CONSOLE: CompactConsole | None = None
# The phase running now, so an unexpected exception is reported against it.
_CURRENT_PHASE = "init"


def _want_verbose(flag: bool, stdout=None) -> bool:
    return want_verbose(flag, stdout)


def _output(phase: str, data: dict) -> None:
    """Print phase progress as JSON to stdout."""
    line = json.dumps({"phase": phase, **data})
    if _CONSOLE is not None:
        _CONSOLE.status(line)
    else:
        print(line, flush=True)


def _attach_log(artifact_root: str, db: str, job_id: str) -> None:
    """In compact mode, send progress to ``<artifact_root>/<db>/<job>/_logs/``."""
    if _CONSOLE is not None:
        _CONSOLE.attach(os.path.join(artifact_root, db, job_id, "_logs", LOG_NAME))


def _start_phase(phase: str) -> None:
    global _CURRENT_PHASE
    _CURRENT_PHASE = phase


def _error(phase: str, message: str) -> None:
    _output(phase, {"status": "error", "message": message})
    sys.exit(1)


def _log(phase: str, msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(f"[{phase}] {msg}  [{ts}]", flush=True)


def _path(store, key: str) -> str:
    """Filesystem path of artifact ``key``: cwd-relative (``artifacts/<db>/...``)
    under the default root, absolute when the root is outside the cwd (#283)."""
    return display_path(os.path.join(str(store.base_dir), key))


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
    with open(STATE_FILE, encoding="utf-8") as f:
        return json.load(f)  # type: ignore[no-any-return]


def _write_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
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
        from src.tools.database.offline_parser import (
            detect_source_engine,
            parse_offline_collection,
        )

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

        detected_engine, detected_port = detect_source_engine(parsed.get("metadata"))

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

    tables = collector_data.get("database_schema", {}).get("tables", [])
    queries = collector_data.get("queries", {}).get("query_patterns", [])

    artifact = _path(store, collector_path)
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

    artifact = _path(store, triage_path)
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
        engine: _path(store, f"{db}/{job_id}/analysis-{engine}/analysis.json")
        for engine in results
        if results[engine] == "complete"
        and store.exists(f"{db}/{job_id}/analysis-{engine}/analysis.json")
    }
    for engine, path in artifacts.items():
        _log_artifact(f"analysis/{engine}", path)
    data: dict = {"status": "complete", "results": results, "artifacts": artifacts}
    failed = sorted(set(results) - set(artifacts))
    if failed:
        data["failed"] = failed
    if not artifacts:
        data["status"] = "error"
        data["message"] = f"no analysis output written for {failed}"
    _output("analysis", data)
    return results


# ============================================================
# Phase: Assignment
# ============================================================
def phase_assignment(store, job_id: str, db: str) -> dict:
    _banner("ASSIGNMENT")
    from src.agents.referee.assignment_handler import run_assignment_resolver
    from src.storage.assignment_versioning import (
        assignment_artifact_path,
        resolve_downstream_assignment_version,
    )

    run_assignment_resolver(job_id, db, store)

    # The resolver writes next_assignment_version (v1 on a fresh job); read that.
    version = resolve_downstream_assignment_version(store, db, job_id)
    assignment_path = assignment_artifact_path(db, job_id, version)
    if not store.exists(assignment_path):
        _error("assignment", "Assignment output not produced.")

    assignment = store.read_json(assignment_path)
    distribution: dict[str, int] = {}
    for q in assignment.get("query_assignments", []):
        engine = q.get("assigned_engine", "unknown")
        distribution[engine] = distribution.get(engine, 0) + 1

    total = sum(distribution.values())
    artifact = _path(store, assignment_path)
    _log_artifact("assignment", artifact)
    _output(
        "assignment",
        {
            "status": "complete",
            "assignment_version": version,
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

    # Input/output versions are resolved by the handler (issue #189): it reads the
    # newest version Reality Check did not produce and writes the next version.
    summary = run_reality_check_handler(job_id, db, store, llm_mode=llm_mode)

    versions = {
        "input_version": summary.get("input_version"),
        "output_version": summary.get("output_version"),
    }
    if summary["status"] == "awaiting_llm":
        llm_input_path = f"{db}/{job_id}/reality-check/llm_input.json"
        if store.exists(llm_input_path):
            _output(
                "reality_check",
                {
                    "status": "awaiting_llm",
                    **versions,
                    "llm_request": _path(store, llm_input_path),
                },
            )
            return "awaiting_llm"

    data: dict = {"status": "complete", **versions, **_reality_check_artifact(store, job_id, db)}
    if summary["status"] == "skipped":
        # Nothing was (re)written: the lineage was already consolidated (#283).
        data["status"] = "skipped"
        data["reason"] = "reality check already ran for this assignment lineage"
    _output("reality_check", data)
    return "complete"


def _reality_check_artifact(store, job_id: str, db: str) -> dict:
    """``artifact`` (the RC output) and ``assignment_version`` (the effective
    assignment downstream phases use) for a reality_check status line."""
    from src.storage.assignment_versioning import resolve_downstream_assignment_version

    out: dict = {"assignment_version": resolve_downstream_assignment_version(store, db, job_id)}
    key = f"{db}/{job_id}/reality-check/output.json"
    if store.exists(key):
        out["artifact"] = _path(store, key)
        _log_artifact("reality-check", out["artifact"])
    return out


def phase_reality_check_finalize(
    store, job_id: str, db: str, assignment_version: int | None = None
) -> None:
    """Finalize reality check after LLM response has been written.

    ``assignment_version`` defaults to the same resolution the handler uses
    (``resolve_reality_check_input_version``), and the revision is written to
    ``next_assignment_version()`` (issue #189).
    """
    from src.agents.referee.reality_check_handler import finalize_reality_check

    llm_response_path = f"{db}/{job_id}/llm_responses/reality_check.json"
    if not store.exists(llm_response_path):
        _error("reality_check", f"LLM response not found at {llm_response_path}")

    finalize_reality_check(
        store, job_id, db, store.read_json(llm_response_path), assignment_version
    )

    _output(
        "reality_check",
        {"status": "complete", "finalized": True, **_reality_check_artifact(store, job_id, db)},
    )


def _surviving_engines(store, job_id: str, db: str, selected: list[str]) -> list[str]:
    """Return ``selected`` minus engines with no in-scope query in the effective assignment.

    Reality Check can consolidate an engine away entirely (all its queries move
    elsewhere). Schema design and synthesis must not run for it. Keeps the
    triage order; falls back to ``selected`` when the assignment is unreadable.
    """
    from src.storage.assignment_versioning import (
        engines_with_in_scope_queries,
        resolve_downstream_assignment_version,
    )

    version = resolve_downstream_assignment_version(store, db, job_id)
    in_scope = engines_with_in_scope_queries(store, db, job_id, version)
    if not in_scope:
        return selected
    return [e for e in selected if e in in_scope]


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

    _output("schema_design", _schema_design_status(store, job_id, db, llm_mode))


def _schema_design_status(store, job_id: str, db: str, llm_mode: str) -> dict:
    """Status line for schema design from the outputs that exist (#283).

    ``complete`` lists each engine's real ``schema_output.json``; engines in
    scope without one are ``skipped_engines``. With no output at all the phase
    is ``skipped`` with a ``reason`` (e.g. ``--llm-mode none``: every schema
    designer needs a model).
    """
    from src.storage.assignment_versioning import (
        engines_with_in_scope_queries,
        resolve_downstream_assignment_version,
    )

    version = resolve_downstream_assignment_version(store, db, job_id)
    in_scope = sorted(engines_with_in_scope_queries(store, db, job_id, version))
    artifacts: dict[str, str] = {}
    for engine in in_scope:
        key = f"{db}/{job_id}/schema-{engine}/v{version}/schema_output.json"
        if store.exists(key):
            artifacts[engine] = _path(store, key)
            _log_artifact(f"schema-design/{engine}", artifacts[engine])
    data: dict = {"status": "complete", "assignment_version": version, "artifacts": artifacts}
    missing = [e for e in in_scope if e not in artifacts]
    if missing:
        data["skipped_engines"] = missing
    if not artifacts:
        data["status"] = "skipped"
        data["reason"] = (
            "llm_mode=none: every schema designer needs a model"
            if llm_mode == "none"
            else "no engine wrote a schema_output.json"
        )
    return data


# ============================================================
# Phase: Synthesis
# ============================================================
def phase_synthesis(store, job_id: str, db: str, llm_mode: str) -> None:
    _banner("SYNTHESIS")
    from src.contracts.phase_models import Phase, PhaseStatus
    from src.orchestrator.local_orchestrator import LocalOrchestrator

    orch = LocalOrchestrator(store=store, llm_mode=llm_mode)
    progression = orch.get_progression(job_id)
    if progression.phases[Phase.SCHEMA_DESIGN].status != PhaseStatus.SKIPPED:
        progression.phases[Phase.SCHEMA_DESIGN].status = PhaseStatus.COMPLETED
    orch._save_progression(progression)

    orch.resume(job_id, Phase.SYNTHESIS)
    _output("synthesis", _synthesis_status(store, job_id, db))


def _synthesis_status(store, job_id: str, db: str) -> dict:
    """Status line naming the report synthesis actually wrote (#283):
    ``synthesis/v<N>/report.json`` (legacy ``referee-synthesis/`` only when the
    job has no versioned assignment)."""
    from src.storage.assignment_versioning import (
        resolve_downstream_assignment_version,
        synthesis_report_candidates,
    )

    version = resolve_downstream_assignment_version(store, db, job_id)
    for key in synthesis_report_candidates(store, db, job_id):
        if store.exists(key):
            artifact = _path(store, key)
            _log_artifact("synthesis", artifact)
            return {"status": "complete", "assignment_version": version, "artifact": artifact}
    return {
        "status": "error",
        "assignment_version": version,
        "message": "synthesis wrote no report.json",
    }


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
        "--mode",
        choices=["chat", "ui", "both"],
        default=None,
        help=(
            "Experience mode to record as experience_mode in .modernizer-state.json "
            "(default when the state is created: both)"
        ),
    )
    parser.add_argument(
        "--artifact-root",
        default="./artifacts",
        help="Root directory for local artifacts (default: ./artifacts)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help=(
            "Print the full progress to stdout (the default only when stdout is a "
            "terminal). Otherwise stdout has only the phase status lines and a "
            "pointer to artifacts/<db>/<job>/_logs/run_assessment.log"
        ),
    )
    args = parser.parse_args()

    if _want_verbose(args.verbose):
        sys.stdout = _ColorizedStream(sys.stdout)  # type: ignore[assignment]
        try:
            _run(args)
        finally:
            sys.stdout = sys.stdout._stream  # type: ignore[attr-defined]
        return

    global _CONSOLE
    _start_phase("init")
    console = CompactConsole(sys.stdout, sys.stderr)
    _CONSOLE = console
    sys.stdout = sys.stderr = console  # type: ignore[assignment]
    try:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"### run_assessment.py {' '.join(sys.argv[1:])}  [{ts}]", flush=True)
        _run(args)
    except SystemExit:
        raise  # _error() already printed the phase's error line
    except BaseException as exc:  # incl. KeyboardInterrupt: still report the phase
        import traceback

        traceback.print_exc()
        info = console.pointer()
        message = f"{type(exc).__name__}: {exc}"[:1000]
        _output(_CURRENT_PHASE, {"status": "error", "message": message, "log": info["log"]})
        exc.__compact_logged__ = True  # type: ignore[attr-defined]
        raise
    finally:
        sys.stdout, sys.stderr = console.stdout, console.stderr
        _CONSOLE = None
        console.close()


def _run(args) -> None:  # type: ignore[no-untyped-def]
    """Run the phases ``args`` asks for (main() has set up the output mode)."""

    from scripts._sandbox import sandbox_violation

    violation = sandbox_violation(args)
    if violation:
        _error("init", violation)

    from src.storage.local_store import LocalArtifactStore

    store = LocalArtifactStore(base_dir=args.artifact_root)

    # Resume reality check after LLM response has been written
    if args.resume_reality_check:
        if not args.job_id or not args.db:
            _error("init", "--resume-reality-check requires --job-id and --db")
        _attach_log(args.artifact_root, args.db, args.job_id)
        _start_phase("reality_check")
        phase_reality_check_finalize(store, args.job_id, args.db)
        state = _read_state()
        state["selected_engines"] = _surviving_engines(
            store, args.job_id, args.db, state.get("selected_engines", [])
        )
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
        _start_phase("collect")
        job_id, db_name = phase_collect(args.file, args.db, store)
        _attach_log(args.artifact_root, db_name, job_id)
        state = {
            "job_id": job_id,
            "database_name": db_name,
            "current_phase": "triage",
            "selected_engines": [],
            "llm_mode": args.llm_mode,
            "experience_mode": args.mode or "both",
            "phase_status": {"collect": "complete"},
        }
        _write_state(state)
    elif args.job_id:
        # Start from triage with existing job
        job_id = args.job_id
        db_name = args.db
        if not db_name:
            _error("init", "--db is required when using --job-id")
        _attach_log(args.artifact_root, db_name, job_id)
        state = _read_state()
        if state and args.mode:
            state["experience_mode"] = args.mode
            _write_state(state)
        if not state:
            state = {
                "job_id": job_id,
                "database_name": db_name,
                "current_phase": "triage",
                "selected_engines": [],
                "llm_mode": args.llm_mode,
                "experience_mode": args.mode or "both",
                "phase_status": {"collect": "complete"},
            }
    else:
        _error("init", "Either --file or --job-id is required")
        return  # unreachable

    # Phase: Triage
    _start_phase("triage")
    selected_engines = phase_triage(store, job_id, db_name)
    state["selected_engines"] = selected_engines
    state["phase_status"]["triage"] = "complete"
    state["current_phase"] = "analysis"
    _write_state(state)

    # Phase: Analysis — always deterministic (llm-mode none).
    # The LLM advisor enriches text but does not change routing decisions.
    # Real LLM value starts at reality-check. Pass --llm-mode to analysis only
    # if you explicitly want richer recommendation text (at ~9min cost).
    _start_phase("analysis")
    phase_analysis(store, job_id, db_name, selected_engines, llm_mode="none")
    state["phase_status"]["analysis"] = "complete"
    state["current_phase"] = "assignment"
    _write_state(state)

    # Phase: Assignment
    _start_phase("assignment")
    phase_assignment(store, job_id, db_name)
    state["phase_status"]["assignment"] = "complete"
    state["current_phase"] = "reality_check"
    _write_state(state)

    # Phase: Reality Check
    _start_phase("reality_check")
    rc_status = phase_reality_check(store, job_id, db_name, args.llm_mode)
    if rc_status == "awaiting_llm":
        state["phase_status"]["reality_check"] = "awaiting_llm"
        _write_state(state)
        return  # Stop here — resume with --resume-reality-check after LLM response
    state["selected_engines"] = _surviving_engines(
        store, job_id, db_name, state["selected_engines"]
    )
    state["phase_status"]["reality_check"] = "complete"
    state["current_phase"] = "schema_design"
    _write_state(state)

    # If --all, continue to schema design and synthesis
    if args.all:
        if not args.yes:
            print(
                "\n  Assignment complete. Press Enter to continue to Schema Design (Ctrl+C to stop).",
                file=_CONSOLE.stderr if _CONSOLE is not None else sys.stderr,
                flush=True,
            )
            input()

        _start_phase("schema_design")
        phase_schema_design(store, job_id, db_name, args.llm_mode)
        state["phase_status"]["schema_design"] = "complete"
        state["current_phase"] = "synthesis"
        _write_state(state)

        _start_phase("synthesis")
        phase_synthesis(store, job_id, db_name, args.llm_mode)
        state["phase_status"]["synthesis"] = "complete"
        state["current_phase"] = "done"
        _write_state(state)


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:
        if getattr(exc, "__compact_logged__", False):
            # Already reported on stdout; the traceback is in the log.
            sys.exit(130 if isinstance(exc, KeyboardInterrupt) else 1)
        raise
