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

--all ends by rendering every deliverable (scripts/run_report.py's step) into
artifacts/<db>/<job>/deliverables/, in --llm-mode none and bedrock alike.

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
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts._compact_output import CompactConsole, display_path, want_verbose  # noqa: E402
from src.shared.bedrock_guardrail import (  # noqa: E402
    describe_active_guardrail,
    exit_on_invalid_guardrail_config,
)
from src.shared.collection_database_name import (  # noqa: E402
    resolve_database_name,
)
from src.shared.database_name import validate_database_name  # noqa: E402
from src.shared.gate_facts import cache_candidate_reads  # noqa: E402
from src.shared.job_id import validate_job_id  # noqa: E402
from src.tools.database.collection_text import extract_json_object  # noqa: E402
from src.tools.validation.ingestion_limits import (  # noqa: E402
    IngestionLimitError,
    check_collection_counts,
    check_file_size,
)

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
# Set once job/db are known (alongside _attach_log) so _error() and main()'s
# own exception handler can record a durable failure marker (#517).
_JOB: dict = {}


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


# The two phases this script itself fans out across every selected engine
# (#517 round-2 review, I3): clearing one of these must match a FAILED
# marker for *any* engine, since the whole phase -- not one specific
# engine -- is about to rerun. phase_analysis/phase_schema_design run every
# selected engine every time this script calls them; there is no per-engine
# retry entry point here the way scripts/run_analysis.py and
# scripts/run_schema_design.py are (those still clear with their own,
# specific --engine).
_FAN_OUT_PHASES = {"analysis", "schema_design"}


def _clear_phase_marker(store, db: str, job_id: str, phase: str) -> None:
    from scripts._job_status import clear_failure_marker
    from src.shared.job_status import ANY_ENGINE

    engine = ANY_ENGINE if phase in _FAN_OUT_PHASES else None
    clear_failure_marker(store, db, job_id, phase, engine=engine)


def _set_job_context(store, db: str, job_id: str) -> None:
    """Record job/db once both are known, alongside ``_attach_log`` (#517),
    so ``_error()`` and ``main()``'s own exception handler can write a
    durable failure marker, and clear a stale marker for the phase running
    right now (usually "collect", or "init" for a resume -- harmless, since
    nothing ever fails as "init"). Each later ``_start_phase`` call clears
    its own phase the same way, since this one script can run several
    phases in a single invocation (#517 round-1 review, I4: clearing is
    scoped to the exact phase about to (re)run, so retrying "triage" after
    an "analysis" failure doesn't erase that still-relevant record before
    "analysis" actually runs again)."""
    _JOB.update(store=store, db=db, job_id=job_id)
    _clear_phase_marker(store, db, job_id, _CURRENT_PHASE)


def _start_phase(phase: str) -> None:
    global _CURRENT_PHASE
    _CURRENT_PHASE = phase
    if _JOB:
        _clear_phase_marker(_JOB["store"], _JOB["db"], _JOB["job_id"], phase)


def _error(phase: str, message: str) -> None:
    # Status line first (#517 round-1 review, I1): the marker write is a
    # best-effort side effect and must never delay or risk the one output
    # callers actually parse.
    _output(phase, {"status": "error", "message": message})
    if _JOB:
        from scripts._job_status import write_failure_marker

        write_failure_marker(
            _JOB["store"], _JOB["db"], _JOB["job_id"], phase, "ScriptError", message
        )
    sys.exit(1)


def _mark_unhandled_failure(exc: BaseException) -> None:
    """Record an unhandled exception against ``_CURRENT_PHASE`` (#517), the
    same job/db ``_error()`` writes to. A no-op before job/db are known
    (``_set_job_context`` hasn't run yet -- e.g. collect itself failed).

    Delegates to the shared, public ``record_unhandled_failure`` (#517
    round-2 review nit: this script has its own try/except shape, since it
    prints its own status line first, so it can't just use ``run_main``
    like the other scripts/run_*.py entry points -- but it must still go
    through the one public helper rather than reaching into
    ``scripts._job_status``'s private message constants). That helper is
    also where the content-free message itself comes from (#143's logging
    rule, #517 round-1 review C1): never str(exc), which can carry
    customer-derived content. The traceback and the str(exc)-carrying
    stdout status line this script already printed are unaffected; this
    only changes what ``_status.json`` records.
    """
    if not _JOB:
        return
    from scripts._job_status import record_unhandled_failure

    record_unhandled_failure(
        (_JOB["store"], _JOB["db"], _JOB["job_id"], _CURRENT_PHASE, None, True), exc
    )


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
def phase_collect(
    collector_file: str,
    db_name: str | None,
    store,
    engines_in_use: list[str] | None = None,
    from_file_name: bool = False,
) -> tuple[str, str]:
    """Parse collector output and initialize artifacts. Returns (job_id, db_name).

    ``engines_in_use`` (#963): engine ids the operator declared as already running.
    ``from_file_name`` (#1096): name the job from the file name, not its metadata.
    """
    from src.shared.engines_in_use import merge_engines_in_use

    _banner("COLLECT")
    from src.shared.sample_collection import CollectionPathError, resolve_collection_path

    try:
        collector_file = resolve_collection_path(collector_file)
    except CollectionPathError as exc:
        _error("collect", str(exc))

    # Check the file size before reading it into memory (#142).
    try:
        check_file_size(os.path.getsize(collector_file), source=collector_file)
    except IngestionLimitError as exc:
        _error("collect", str(exc))

    if zipfile.is_zipfile(collector_file):
        _error(
            "collect",
            f"{collector_file} is a zip archive; unzip it first and pass the .json file inside.",
        )

    try:
        with open(collector_file, encoding="utf-8") as f:
            raw_text = f.read()
    except UnicodeDecodeError:
        _error("collect", f"{collector_file} is not a UTF-8 JSON collection.")

    content = extract_json_object(raw_text, source=collector_file)
    input_data = json.loads(content)

    from src.contracts.collector_output import CollectorOutputContract
    from src.shared.contract_versioning import (
        ContractVersionError,
        carries_contract,
        stamp_artifact_version,
    )

    # A stored collector output carries a contract_version (ADR-008 Decision 4: same
    # MAJOR only); a raw collection carries none.
    try:
        is_contract = carries_contract(input_data, CollectorOutputContract, artifact=collector_file)
    except ContractVersionError as exc:
        _error("collect", str(exc))
    is_raw = "collection_version" in input_data or isinstance(input_data.get("queries"), list)

    if is_contract:
        try:
            check_collection_counts(
                table_count=len((input_data.get("database_schema") or {}).get("tables") or []),
                query_count=len((input_data.get("queries") or {}).get("query_patterns") or []),
                source=collector_file,
            )
        except IngestionLimitError as exc:
            _error("collect", str(exc))

    job_id = str(uuid.uuid4())[:8]
    # No --db: the file's own valid metadata name, else one derived from its file
    # name (#1096). The collect status line says which.
    db_source = "explicit"
    if db_name is None:
        db_name, db_source = resolve_database_name(
            input_data, collector_file, from_file_name=from_file_name
        )
    table_refs: dict | None = None  # parser counts, raw collections only (#553)

    if is_contract:
        collector_data = input_data
    elif is_raw:
        # Upload raw file
        upload_path = f"{db_name}/{job_id}/uploads/collector-output.json"
        store.write_json(upload_path, input_data)

        # Parse offline collection
        from src.tools.database.offline_parser import parse_offline_collection
        from src.tools.sql.shadow import TableShadow, write_shadow_artifact

        try:
            # Table references come from the sqlglot facade, with the regex as
            # its fallback (#553); the regex also runs as a cross-check, and
            # both counts go on the collect status line (``table_refs``).
            sql_shadow = TableShadow()
            parsed = parse_offline_collection(input_data, shadow=sql_shadow)
        except IngestionLimitError as exc:
            _error("collect", str(exc))
        write_shadow_artifact(store, db_name, job_id, sql_shadow)
        table_refs = sql_shadow.status_summary()

        # Build collector contract output (the one source-engine detector, #1177)
        from src.agents.collector.offline_ingest import build_offline_collector_output

        collector_data = build_offline_collector_output(
            parsed, job_id=job_id, database_name=db_name, s3_key=upload_path
        )
    else:
        _error("collect", "Unrecognized file format.")
        return "", ""  # unreachable

    # Write collector output
    collector_path = f"{db_name}/{job_id}/collector/output.json"
    if engines_in_use:
        # #963: engines the operator declared, added to any the collection lists.
        meta = collector_data.setdefault("metadata", {})
        meta["engines_in_use"] = merge_engines_in_use(meta.get("engines_in_use"), engines_in_use)
    # An uploaded collector output may be at an older MINOR: written at today's (#1018).
    store.write_json(collector_path, stamp_artifact_version(collector_path, collector_data))

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
            "database_name_source": db_source,
            "tables": len(tables),
            "queries": len(queries),
            "artifact": artifact,
            **({"table_refs": table_refs} if table_refs is not None else {}),
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
    scope = triage_output.get("engine_scope")
    _output(
        "triage",
        {
            "status": "complete",
            "selected": selected,
            "skipped": skipped,
            **({"engine_scope": scope} if scope is not None else {}),
            "artifact": artifact,
        },
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
    line = {
        "status": "complete",
        "assignment_version": version,
        "distribution": distribution,
        "total_queries": total,
        # Queries the cache layer fronts (#296); never part of the distribution
        "cache_overlay": assignment.get("cache_overlay"),
        "artifact": artifact,
    }
    unservable = len(assignment.get("unservable_queries") or [])
    if unservable:
        # #1329: queries no engine in the customer's engine scope can serve (no engine,
        # so never in the distribution or the total); the run goes on.
        line["unservable_queries"] = unservable
    _output("assignment", line)
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


_REASON_LIMIT = 140  # keeps the reality_check status line compact (#329)

# A draft reasoning pass inside reality_check_floors.py's mandatory-engine
# justification floor builds its reason text with a bare issue tag, e.g.
# "... does not meet its justification floor (#167) -- ...". Reconciliation
# (consolidations_from_moves) usually replaces that text once query counts
# settle, but review of #329 found a scenario where the tag survives all
# the way into reality-check/output.json unchanged. An internal issue
# number is not something an approval gate should show a customer, so this
# is stripped defensively here regardless of what the upstream text does.
_ISSUE_REF_RE = re.compile(r"\s*\(#\d+(?:,\s*#\d+)*\)|\s#\d+\b")


def _scrub_issue_refs(text: str) -> str:
    return _ISSUE_REF_RE.sub("", text)


def _truncate(text: str, limit: int = _REASON_LIMIT) -> str:
    text = _scrub_issue_refs(text or "")
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


_CACHE_OVERLAY_FIELDS = (
    "query_count",
    "calls_per_second",
    "call_share_percent",
    "candidate_query_count",
    "candidate_call_share_percent",
)


def _compact_consolidations(consolidations: list[dict]) -> list[dict]:
    """``from``/``to``/``queries`` per entry, ``reason`` only where it adds
    information.

    Several consolidation records from the same ``from_engine`` often carry
    the identical reason text verbatim (e.g. two "documentdb has no unique
    capabilities ..." records, one per destination engine) -- printing it on
    every one makes the status line grow without telling the approver
    anything new. Keep it on the first record for that ``from_engine``;
    later ones with the exact same text omit it.
    """
    out: list[dict] = []
    last_reason: dict[str, str] = {}
    for c in consolidations:
        from_engine = c["from_engine"]
        reason = c.get("reason", "")
        entry = {"from": from_engine, "to": c["to_engine"], "queries": c["query_count"]}
        if last_reason.get(from_engine) != reason:
            entry["reason"] = _truncate(reason)
            last_reason[from_engine] = reason
        out.append(entry)
    return out


def _compact_sweep_kept(sweep_decisions: list[dict]) -> list[dict]:
    """``engine``/``reason`` for every sanity-sweep decision that KEPT a tiny
    orphan engine (#589) -- a ``removed`` decision is dropped here, since its
    outcome already shows up in ``consolidations`` (``_compact_consolidations``
    above); without this, a KEPT engine was invisible to the approval gate,
    the one place #589 was raised to fix.
    """
    return [
        {"engine": sd["engine"], "reason": _truncate(sd.get("reason", ""))}
        for sd in sweep_decisions
        if sd.get("decision") == "kept"
    ]


def _schema_design_engines(store, job_id: str, db: str, version: int) -> list[str]:
    """The engines that get a schema design: triage's selection, filtered to
    the ones with an in-scope query at ``version`` -- owners (``assigned_engine``)
    *and* the cache-layer engine when it fronts an owner's hot reads
    (``cache_engine``, #296). Computed the same way as the state's
    ``selected_engines`` (``_surviving_engines``), so it never drifts from
    what schema design actually dispatches.

    Issue #329 review: ``after_distribution`` only counts owners -- the cache
    layer never owns a query by design (ADR #296's write gate), so a model
    reading only that dict for its engine list drops the cache layer and its
    schema design never gets dispatched. This gives the orchestrator an
    explicit, correct list instead of making it infer one.
    """
    from src.storage.assignment_versioning import schema_design_engines

    # The one rule, shared with the public ``assess()`` (#1387).
    return schema_design_engines(store, db, job_id, version)


def _reality_check_artifact(store, job_id: str, db: str) -> dict:
    """``artifact`` (the RC output), ``assignment_version`` (the effective
    assignment downstream phases use), and the final per-engine distribution
    for a reality_check status line.

    Issue #329: a chat-only approval gate had no numbers to show -- the
    orchestrator must never read reality-check/output.json or an assignment
    artifact itself, so without this the status line carried nothing besides
    a version number and a path. ``before_distribution``/``after_distribution``
    (the v1 -> vN per-engine query counts), a compact ``consolidations`` list
    (what moved, how many queries, why -- deduplicated, see
    ``_compact_consolidations``), a compact ``sweep_kept`` list (#589: why the
    sanity sweep KEPT a tiny orphan engine instead of removing it -- see
    ``_compact_sweep_kept``; a removed one is covered by ``consolidations``
    already), ``cache_overlay`` (the final assignment's cache coverage,
    trimmed to the three fields the gate names) and
    ``schema_design_engines`` (see ``_schema_design_engines``) are read here,
    deterministically, so the orchestrator can present them, and dispatch
    schema design, without opening either file. Review of #329: keep this
    line's growth in check -- the full cache_overlay object (owners,
    patterns, min/max) and repeated reason text are not needed to approve.
    """
    from src.storage.assignment_versioning import (
        assignment_artifact_path,
        reality_check_output_candidates,
        resolve_downstream_assignment_version,
    )

    version = resolve_downstream_assignment_version(store, db, job_id)
    out: dict = {"assignment_version": version}
    # The record of the run this version descends from (#193, #215), as synthesis,
    # the UI and the graph read it.
    key = next(
        (k for k in reality_check_output_candidates(store, db, job_id, version) if store.exists(k)),
        None,
    )
    if key is not None:
        out["artifact"] = _path(store, key)
        _log_artifact("reality-check", out["artifact"])
        rc = store.read_json(key)
        out["before_distribution"] = rc.get("before_distribution", {})
        out["after_distribution"] = rc.get("after_distribution", {})
        # #235: before/after_distribution count ANY query, in or out of scope, so
        # an engine can show a nonzero after_distribution entry and still be
        # eliminated -- eliminated_engines (contract 1.8+) is the settled answer
        # (src.shared.engine_elimination) a chat-only approval gate reads for its
        # survived/dropped labels instead of re-deriving one from the counts.
        out["eliminated_engines"] = rc.get("eliminated_engines", [])
        out["consolidations"] = _compact_consolidations(rc.get("consolidations", []))
        # #589: the sanity sweep's own reason for KEEPING a tiny orphan engine
        # (contract 1.10+) -- compacted the same way as consolidations, so a
        # kept engine's reason reaches the approval gate too, not only a
        # removed one's.
        out["sweep_kept"] = _compact_sweep_kept(rc.get("sweep_decisions", []))

    assignment_path = assignment_artifact_path(db, job_id, version)
    if store.exists(assignment_path):
        overlay = store.read_json(assignment_path).get("cache_overlay")
        if overlay:
            out["cache_overlay"] = {k: overlay[k] for k in _CACHE_OVERLAY_FIELDS if k in overlay}
            # #462: the hottest cached reads, named; the rest are counted.
            from src.storage.collector_for_models import read_collector_for_models

            collector = read_collector_for_models(store, db, job_id, missing_ok=True)
            queries = (collector.get("queries") or {}).get("query_patterns") or []
            reads = cache_candidate_reads(
                store.read_json(assignment_path).get("query_assignments") or [], queries
            )
            if reads:
                out["cache_reads"] = reads
    out["schema_design_engines"] = _schema_design_engines(store, job_id, db, version)
    out["pending_choices"] = _compact_pending_choices(store, job_id, db)
    return out


def _compact_pending_choices(store, job_id: str, db: str) -> list[dict]:
    """The effective assignment's pending engine choices, one per table (#1003),
    compact for the approval gate: per table its queries, each tied engine's
    whole-point and unrounded score and trade-off, why they tie, where the
    queries sit now, and what --decide-for-me would pick and why."""
    from src.agents.referee.assignment_overrides import pending_choices_for_job

    choices, _ = pending_choices_for_job(store, db, job_id)
    return [
        {
            "choice": c.choice_id,
            "table": c.table.rsplit(".", 1)[-1],
            "queries": len(c.query_ids),
            "writes": len(c.write_query_ids),
            "engines": [
                {
                    "engine": e.engine,
                    "score": e.score,
                    "unrounded_score": e.unrounded_score,
                    "trade_off": e.trade_off,
                }
                for e in c.options
            ],
            "why_tied": c.why_tied,
            "listed_under": c.current_engine,
            "decide_for_me": c.decide_for_me_engine,
            "decide_for_me_reason": c.decide_for_me_reason,
        }
        for c in choices
    ]


def phase_engine_choices(
    store, job_id: str, db: str, picks: list[str], decide_for_me: bool
) -> None:
    """Apply per-table engine picks (``--choose TABLE=ENGINE``) and, with
    ``--decide-for-me``, --auto's pick for every other pending choice, as one new
    assignment version through the customer gate (#1003). Prints one
    ``engine_choices`` status line: the version, what was picked and decided, the
    choices still pending and the engines schema design runs next."""
    from src.agents.referee.assignment_overrides import (
        AssignmentOverrideError,
        apply_engine_choices,
    )
    from src.agents.referee.pending_choices import EngineChoiceError
    from src.storage.assignment_versioning import resolve_downstream_assignment_version

    parsed: dict[str, str] = {}
    for item in picks:
        table, sep, engine = item.partition("=")
        if not sep or not table.strip() or not engine.strip():
            _error("engine_choices", f"--choose expects TABLE=ENGINE, got {item!r}")
        parsed[table.strip()] = engine.strip()
    try:
        result = apply_engine_choices(store, db, job_id, parsed, decide_for_me=decide_for_me)
    except (EngineChoiceError, AssignmentOverrideError) as exc:
        _error("engine_choices", str(exc))
        return  # unreachable
    version = resolve_downstream_assignment_version(store, db, job_id)
    data: dict = {
        "status": "complete" if result is not None else "unchanged",
        "assignment_version": version,
        "picked": result.picked if result is not None else {},
        "decided": [
            {
                "choice": p.choice_id,
                "table": p.table.rsplit(".", 1)[-1],
                "engine": p.engine,
                "alternatives": p.alternatives,
                "queries": len(p.query_ids),
                "reason": p.reason,
            }
            for p in (result.decided if result is not None else [])
        ],
        "pending_choices": _compact_pending_choices(store, job_id, db),
        "schema_design_engines": _schema_design_engines(store, job_id, db, version),
    }
    _output("engine_choices", data)


def phase_reality_check_finalize(
    store, job_id: str, db: str, assignment_version: int | None = None
) -> None:
    """Finalize reality check after LLM response has been written.

    ``assignment_version`` defaults to the same resolution the handler uses
    (``resolve_reality_check_input_version``), and the revision is written to
    ``next_assignment_version()`` (issue #189).
    """
    from src.agents.referee.reality_check_handler import finalize_reality_check
    from src.contracts.model_flag import ResponseContractError

    llm_response_path = f"{db}/{job_id}/llm_responses/reality_check.json"
    if not store.exists(llm_response_path):
        _error("reality_check", f"LLM response not found at {llm_response_path}")

    try:
        finalize_reality_check(
            store, job_id, db, store.read_json(llm_response_path), assignment_version
        )
    except ResponseContractError as exc:
        # The response is off its contract (RealityCheckModelResponse, #628).
        _error("reality_check", f"{llm_response_path}: {exc}")

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
def phase_schema_design(store, job_id: str, db: str, llm_mode: str) -> dict:
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

    status = _schema_design_status(store, job_id, db, llm_mode)
    _output("schema_design", status)
    return status


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
        from src.contracts.phase_models import SCHEMA_DESIGN_SKIP_REASON_NO_LLM

        data["status"] = "skipped"
        data["reason"] = (
            SCHEMA_DESIGN_SKIP_REASON_NO_LLM
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
            status: dict = {
                "status": "complete",
                "assignment_version": version,
                "artifact": artifact,
            }
            # #746: reality-check moves no model reviewed, stated in every
            # deliverable; on the status line too, so a run never hides them.
            facts = (store.read_json(key) or {}).get("facts") or {}
            if facts.get("unreviewed_move_count"):
                status["unreviewed_moves"] = facts["unreviewed_move_count"]
            return status
    return {
        "status": "error",
        "assignment_version": version,
        "message": "synthesis wrote no report.json",
    }


# ============================================================
# Phase: Report (render the deliverables)
# ============================================================
def phase_report(artifact_root: str, job_id: str, db: str, llm_mode: str) -> dict:
    """Render every deliverable, the same step ``/synthesize`` runs
    (``scripts/run_report.py``), into ``artifacts/<db>/<job>/deliverables/``.

    In ``external`` mode synthesis is not final yet (Claude Code writes the
    summary, then ``run_synthesis.py --finalize``), so rendering waits for
    ``/synthesize``. A failed render is an error; a partial one is reported and
    the run still completes.
    """
    _banner("REPORT")
    if llm_mode == "external":
        status = {
            "status": "skipped",
            "reason": (
                "llm_mode=external: synthesis awaits its summary; /synthesize "
                "finalizes it and runs scripts/run_report.py"
            ),
        }
        _output("report", status)
        return status
    from scripts import run_report

    result = run_report.run(job_id, db, artifact_root, None)
    if result["status"] == "error":
        _error("report", result["message"])
    status = {k: result[k] for k in ("status", "deliverables_dir", "errors") if k in result}
    if result.get("deliverables_dir"):
        status["deliverables_dir"] = display_path(result["deliverables_dir"])
        _log_artifact("report", status["deliverables_dir"])
    _output("report", status)
    return status


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
        "--db-from-file-name",
        action="store_true",
        help=(
            "With --file and no --db: name the database from the collection file name "
            "(the rule /modernize and /collect use) instead of preferring the file's own "
            "metadata name"
        ),
    )
    parser.add_argument(
        "--llm-mode",
        default="external",
        choices=["none", "bedrock", "external"],
        help="LLM mode for reality check and schema design (default: external)",
    )
    parser.add_argument(
        "--engines-in-use",
        default=None,
        metavar="ENGINES",
        help=(
            "Comma-separated engine ids of the other AWS databases the application already "
            "runs today (for example opensearch,elasticache). Recorded in the collection so "
            "retiring one shows a saving; added to any the file already lists"
        ),
    )
    parser.add_argument(
        "--target-databases",
        default=None,
        metavar="ENGINES",
        help=(
            "Comma-separated engine ids the plan may route to, the customer's engine scope "
            "(for example dynamodb,aurora_postgresql; #1328). Active engines only. Recorded "
            "as the job's decisions/engine_scope.json; left out, every engine is considered"
        ),
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
        "--choose",
        action="append",
        default=[],
        metavar="TABLE=ENGINE",
        help=(
            "With --job-id/--db: settle a pending engine choice (one per table) on one of "
            "its tied engines, for every query listed under it; repeatable (#1003)"
        ),
    )
    parser.add_argument(
        "--decide-for-me",
        action="store_true",
        help=(
            "With --job-id/--db: decide every pending engine choice not named by --choose "
            "the way --auto does (the higher score before rounding, then the engine the "
            "plan already uses more, then the tie rules), and record each decision (#1003)"
        ),
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
        default=os.environ.get("ARTIFACT_DIR", "./artifacts"),
        help="Root directory for local artifacts (default: $ARTIFACT_DIR or ./artifacts)",
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
    from src.shared.engines_in_use import parse_engines_in_use_option
    from src.shared.target_databases import parse_target_databases_option

    try:
        args.engines_in_use_ids = parse_engines_in_use_option(args.engines_in_use)
        args.target_database_ids = parse_target_databases_option(args.target_databases)
    except ValueError as exc:
        parser.error(str(exc))
    if args.llm_mode == "bedrock":
        exit_on_invalid_guardrail_config()

    if _want_verbose(args.verbose):
        sys.stdout = _ColorizedStream(sys.stdout)  # type: ignore[assignment]
        try:
            _run(args)
        except SystemExit:
            raise  # _error() already recorded the failure, if any (#517)
        except BaseException as exc:  # incl. KeyboardInterrupt: still mark the phase
            _mark_unhandled_failure(exc)
            raise
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
        _mark_unhandled_failure(exc)
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

    if args.db is not None and args.db_from_file_name:
        _error("init", "--db and --db-from-file-name cannot be used together")

    if args.db is not None:
        try:
            args.db = validate_database_name(args.db)
        except ValueError as exc:
            _error("init", str(exc))

    if args.job_id is not None:
        try:
            args.job_id = validate_job_id(args.job_id)
        except ValueError as exc:
            _error("init", str(exc))

    from src.storage.local_store import LocalArtifactStore

    store = LocalArtifactStore(base_dir=args.artifact_root)

    if getattr(args, "target_database_ids", None) is not None and (
        args.resume_reality_check or args.choose or args.decide_for_me
    ):
        # The scope is triage's input: a resumed or settled job keeps the one it started with.
        _error(
            "init",
            "--target-databases applies when a run starts at triage (--file, or --job-id "
            "with --db); it cannot change the scope of a job past triage",
        )

    # Resume reality check after LLM response has been written
    if args.resume_reality_check:
        if not args.job_id or not args.db:
            _error("init", "--resume-reality-check requires --job-id and --db")
        _attach_log(args.artifact_root, args.db, args.job_id)
        _set_job_context(store, args.db, args.job_id)
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

    # Settle pending engine choices at the approval gate (#1003)
    if args.choose or args.decide_for_me:
        if not args.job_id or not args.db or args.file:
            _error("init", "--choose/--decide-for-me require --job-id and --db (and no --file)")
        _attach_log(args.artifact_root, args.db, args.job_id)
        _set_job_context(store, args.db, args.job_id)
        _start_phase("engine_choices")
        phase_engine_choices(store, args.job_id, args.db, args.choose, args.decide_for_me)
        state = _read_state()
        if state.get("job_id") == args.job_id:
            state["selected_engines"] = _surviving_engines(
                store, args.job_id, args.db, state.get("selected_engines", [])
            )
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
    if args.llm_mode == "bedrock" and (guardrail := describe_active_guardrail()):
        print(f"  Guardrail: {guardrail}", file=sys.stderr)
    print("  Artifacts: ./artifacts/", file=sys.stderr)
    print(f"{'='*60}\n", file=sys.stderr, flush=True)

    # Determine starting point
    if args.file:
        # Full pipeline from collector file
        _start_phase("collect")
        declared = getattr(args, "engines_in_use_ids", None)
        collect_kwargs: dict = {"engines_in_use": declared} if declared else {}
        if args.db_from_file_name:
            collect_kwargs["from_file_name"] = True
        job_id, db_name = phase_collect(args.file, args.db, store, **collect_kwargs)
        _attach_log(args.artifact_root, db_name, job_id)
        _set_job_context(store, db_name, job_id)
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
        _set_job_context(store, db_name, job_id)
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

    # #1328: the customer's engine scope, recorded for the job; triage reads it.
    scope = getattr(args, "target_database_ids", None)
    if scope is not None:
        from src.agents.referee.engine_scope import record_engine_scope

        record_engine_scope(store, db_name, job_id, scope, via="cli")

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
        else:
            # -y is --auto: "decide for me" leaves no engine choice pending (#1003).
            _start_phase("engine_choices")
            phase_engine_choices(store, job_id, db_name, [], decide_for_me=True)
            state["selected_engines"] = _surviving_engines(
                store, job_id, db_name, state["selected_engines"]
            )
            _write_state(state)

        _start_phase("schema_design")
        schema_status = phase_schema_design(store, job_id, db_name, args.llm_mode)
        if schema_status["status"] == "skipped":
            # Same status and reason as the status line and the SKIPPED phase (#281).
            state["phase_status"]["schema_design"] = "skipped"
            state.setdefault("skip_reasons", {})["schema_design"] = schema_status["reason"]
        else:
            state["phase_status"]["schema_design"] = "complete"
        state["current_phase"] = "synthesis"
        _write_state(state)

        _start_phase("synthesis")
        phase_synthesis(store, job_id, db_name, args.llm_mode)
        state["phase_status"]["synthesis"] = "complete"
        _write_state(state)

        # The same render step /modernize runs, so a CLI or Bedrock run ends
        # with the deliverables, not only report.json.
        _start_phase("report")
        phase_report(args.artifact_root, job_id, db_name, args.llm_mode)
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
