#!/usr/bin/env python3
"""
Test the full phased workflow locally — mimics the real UI flow.

Usage:
  python scripts/test_local_phased.py <collector-output-json> [--db <database_name>]

Example:
  python scripts/test_local_phased.py docs/examples/forum-db-analysis/collector-output-fresh.json --db forum_db
  python scripts/test_local_phased.py docs/examples/forum-db-analysis/collector-output-offline.json

If --db is not provided, the database name is auto-detected from the collection metadata.

The script follows the same flow as the UI:
  1. Prepare: create job ID + upload folder
  2. Upload: copy raw collection output into the artifact store
  3. Phase 1: Run collector (offline mode — parses raw JSON) + triage
  4. Phase 2: Run analysis for all selected engines
  5. PAUSE: Display assignment for review
  6. Phase 3: Run assignment resolution
  7. Phase 4: Run schema design for assigned engines
  8. Phase 5: Run load test (requires k6 + AWS credentials, skipped if unavailable)
  9. Phase 6: Run synthesis and produce final report

All artifacts land in ./artifacts/{db_name}/{job_id}/
"""

import json
import os
import sys
import uuid
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("RUNTIME_MODE", "local")
os.environ.setdefault("ARTIFACT_DIR", "./artifacts")


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Test the phased workflow locally")
    parser.add_argument("collector_file", help="Path to collector output JSON")
    parser.add_argument(
        "--db",
        dest="db_name",
        default=None,
        help="Database name (auto-detected from metadata if not provided)",
    )
    parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Skip interactive pause and auto-approve assignment",
    )
    parser.add_argument(
        "--llm-mode",
        default="none",
        choices=["none", "bedrock", "external"],
        help="LLM mode for reality check (default: none = deterministic only)",
    )
    args = parser.parse_args()

    collector_file = args.collector_file
    db_name_override = args.db_name

    if not os.path.exists(collector_file):
        print(f"ERROR: File not found: {collector_file}")
        sys.exit(1)

    with open(collector_file, encoding="utf-8") as f:
        content = f.read().strip()
        # MySQL outputs a column header (e.g. "collection_output") on the first
        # line when run without -N.  Strip it so we get valid JSON.
        if not content.startswith("{") and "\n" in content:
            content = content[content.index("\n") + 1 :]
        input_data = json.loads(content)

    # Detect format
    is_contract = isinstance(input_data.get("contract_version"), str) and input_data[
        "contract_version"
    ].startswith("3")
    is_raw = "collection_version" in input_data or isinstance(input_data.get("queries"), list)

    from src.contracts.phase_models import Phase, PhaseStatus
    from src.orchestrator.local_orchestrator import LocalOrchestrator
    from src.storage import create_artifact_store

    store = create_artifact_store()
    orch = LocalOrchestrator(store=store, llm_mode=args.llm_mode)
    job_id = str(uuid.uuid4())[:8]

    if is_contract:
        # Already processed — use directly
        collector_data = input_data
        db_name = db_name_override or (
            collector_data.get("metadata", {})
            .get("source_database", {})
            .get("database_name", "unknown_db")
        )
        tables = collector_data.get("database_schema", {}).get("tables", [])
        queries = collector_data.get("queries", {}).get("query_patterns", [])
    elif is_raw:
        # Raw collection script output — run through offline parser locally
        db_name = db_name_override or input_data.get("metadata", {}).get(
            "database_name", "unknown_db"
        )
        if not db_name_override and isinstance(input_data.get("metadata"), str):
            meta = json.loads(input_data["metadata"])
            db_name = meta.get("database_name", meta.get("schema_name", "unknown_db"))

        raw_tables = input_data.get("tables", [])
        raw_queries = input_data.get("queries", [])

        print(f"\n{'='*60}")
        print("  Database Modernizer — Local Phased Workflow")
        print(f"{'='*60}")
        print("  Input format: Raw collection script output")
        print(f"  Database:     {db_name}")
        print(f"  Job ID:       {job_id}")
        print(f"  Raw tables:   {len(raw_tables)}")
        print(f"  Raw queries:  {len(raw_queries)}")
        print(f"  Input:        {collector_file}")
        print(f"  Artifacts:    ./artifacts/{db_name}/{job_id}/")
        print(f"{'='*60}\n")

        # Step 1: Upload raw file to uploads path (mimics presigned URL upload)
        upload_path = f"{db_name}/{job_id}/uploads/collector-output.json"
        store.write_json(upload_path, input_data)
        print(f"[prepare] Raw collection uploaded to {upload_path}")

        # Step 2: Parse offline collection locally (same as collector agent offline mode)
        print("[collector] Parsing raw collection output (offline mode)...")
        from src.tools.database.offline_parser import parse_offline_collection

        parsed = parse_offline_collection(input_data)

        # Build collector contract output using the same builder functions
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

        # Detect source engine from metadata version string
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

        tables = collector_data.get("database_schema", {}).get("tables", [])
        queries = collector_data.get("queries", {}).get("query_patterns", [])
        print(f"[collector] Parsed: {len(tables)} tables, {len(queries)} queries")
    else:
        print("ERROR: Unrecognized file format.")
        sys.exit(1)

    if is_contract:
        print(f"\n{'='*60}")
        print("  Database Modernizer — Local Phased Workflow")
        print(f"{'='*60}")
        print("  Input format: Collector contract output")
        print(f"  Database:     {db_name}")
        print(f"  Job ID:       {job_id}")
        print(f"  Tables:       {len(tables)}")
        print(f"  Queries:      {len(queries)}")
        print(f"  Input:        {collector_file}")
        print(f"  Artifacts:    ./artifacts/{db_name}/{job_id}/")
        print(f"{'='*60}\n")

    # Write collector output to artifact store
    collector_path = f"{db_name}/{job_id}/collector/output.json"
    store.write_json(collector_path, collector_data)
    print(f"[collector] Output written to {collector_path}")

    # Materialize query journey files (source section) for each query
    from src.agents.query_journey_materializer import materialize_source

    materialize_source(collector_data, db_name, job_id, store)
    print(f"[collector] Query journey files materialized ({len(queries)} queries)")

    # ================================================================
    # Phase 1: TRIAGE
    # ================================================================
    print(f"\n{'='*60}")
    print(f"  PHASE 1: Triage  [{datetime.now().strftime('%H:%M:%S.%f')[:-3]}]")
    print(f"{'='*60}")
    from src.agents.referee.triage_handler import run_triage

    run_triage(job_id, db_name, store)

    triage = store.read_json(f"{db_name}/{job_id}/referee-triage/triage.json")
    selected = [a["agent_type"] for a in triage.get("selected_agents", [])]
    skipped = [a["agent_type"] for a in triage.get("skipped_agents", [])]
    print(f"\n[triage] Selected: {selected}")
    print(f"[triage] Skipped: {skipped}")

    # Seed progression
    progression = orch._new_progression(job_id)
    orch._set_phase_status(progression, Phase.COLLECT_TRIAGE, PhaseStatus.COMPLETED)
    orch._save_progression(progression)

    # ================================================================
    # Phase 2: ANALYSIS
    # ================================================================
    print(f"\n{'='*60}")
    print(f"  PHASE 2: Analysis (all engines)  [{datetime.now().strftime('%H:%M:%S.%f')[:-3]}]")
    print(f"{'='*60}")
    orch._set_phase_status(progression, Phase.ANALYSIS, PhaseStatus.IN_PROGRESS)
    orch._save_progression(progression)

    from src.agents.analysis.handler import run_analysis

    if len(selected) > 1:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        print(f"  Running {len(selected)} engines in parallel: {selected}")
        with ThreadPoolExecutor(max_workers=len(selected)) as pool:
            futures = {
                pool.submit(run_analysis, job_id, db_name, engine, store): engine
                for engine in selected
            }
            for future in as_completed(futures):
                engine = futures[future]
                future.result()
                print(
                    f"  [{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] {engine} analysis complete"
                )
    else:
        for engine in selected:
            print(f"\n--- Analyzing for {engine} ---")
            run_analysis(job_id, db_name, engine, store)

    orch._set_phase_status(progression, Phase.ANALYSIS, PhaseStatus.COMPLETED)
    orch._save_progression(progression)

    # ================================================================
    # Phase 3: ASSIGNMENT
    # ================================================================
    print(f"\n{'='*60}")
    print(f"  PHASE 3: Assignment Resolution  [{datetime.now().strftime('%H:%M:%S.%f')[:-3]}]")
    print(f"{'='*60}")
    orch.resume(job_id, Phase.ASSIGNMENT)

    assignment_path = f"{db_name}/{job_id}/assignment/v1/assignment.json"
    if store.exists(assignment_path):
        assignment = store.read_json(assignment_path)
        _print_assignment(assignment)

    # ================================================================
    # Phase 3b: REALITY CHECK
    # ================================================================
    print(f"\n{'='*60}")
    print(
        f"  PHASE 3b: Reality Check (CTO-level engine consolidation)  [{datetime.now().strftime('%H:%M:%S.%f')[:-3]}]"
    )
    print(f"{'='*60}")
    progression = orch.get_progression(job_id)
    progression.phases[Phase.ASSIGNMENT].status = PhaseStatus.COMPLETED
    orch._save_progression(progression)
    orch.resume(job_id, Phase.REALITY_CHECK)

    # Show reality check results
    rc_path = f"{db_name}/{job_id}/reality-check/output.json"
    if store.exists(rc_path):
        rc = store.read_json(rc_path)
        before = rc.get("before_distribution", {})
        after = rc.get("after_distribution", {})
        cons = rc.get("consolidations", [])
        patterns = rc.get("architectural_patterns", [])
        uva = rc.get("unique_value_assessment", [])
        recs = rc.get("recommendations", [])

        print("\n  Reality Check Summary")
        print(f"  {'-'*40}")
        print(f"  Before: {before}")
        print(f"  After:  {after}")

        if cons:
            print(f"\n  Consolidations ({len(cons)}):")
            for c in cons:
                print(
                    f"    {c['from_engine']} → {c['to_engine']}: "
                    f"{c['query_count']} queries moved (saves ~${c['saved_cost_estimate']}/mo)"
                )
        else:
            print("\n  No consolidations — all engines provide unique value")

        if uva:
            print("\n  Unique Value Assessment:")
            for engine_name, assessment in uva.items():
                unique = len(assessment.get("unique_queries", []))
                total = assessment.get("total_queries", 0)
                ratio = assessment.get("unique_ratio", 0) * 100
                delta = assessment.get("avg_delta", 0)
                blocked = assessment.get("consolidation_blocked", "")
                extra = f" [BLOCKED: {blocked}]" if blocked else ""
                print(
                    f"    {engine_name}: {unique}/{total} unique queries "
                    f"({ratio:.0f}% unique, avg delta {delta}){extra}"
                )

        if patterns:
            print("\n  Architectural Patterns:")
            for p in patterns:
                print(f"    {p['name']}: {p.get('description', '')}")

        if recs:
            print("\n  Recommendations:")
            for r in recs:
                print(f"    - {r}")

        print(f"\n  Artifact: ./artifacts/{rc_path}")

    # Check if assignment version was bumped
    av = orch._get_assignment_version(job_id, db_name)
    if av > 1:
        print(f"\n[reality-check] Assignment bumped to v{av} (consolidation applied)")
        revised = store.read_json(f"{db_name}/{job_id}/assignment/v{av}/assignment.json")
        _print_assignment(revised)
    else:
        print("\n[reality-check] Assignment stays at v1 (no consolidation)")

    # ================================================================
    # PAUSE — Assignment Review (human gate)
    # ================================================================
    if not args.yes:
        print(f"\n{'='*60}")
        print("  PAUSED: Review the assignment and reality check above.")
        print("  Press Enter to approve and continue to Schema Design.")
        print("  (Or Ctrl+C to stop and inspect artifacts)")
        print(f"{'='*60}")
        input()
    else:
        print("\n[--yes] Auto-approving assignment, continuing to Schema Design...")

    # ================================================================
    # Phase 3c: ASSIGNMENT REVIEW (approval)
    # ================================================================
    print(f"\n{'='*60}")
    print(
        f"  PHASE 3c: Assignment Review (approved)  [{datetime.now().strftime('%H:%M:%S.%f')[:-3]}]"
    )
    print(f"{'='*60}")
    progression = orch.get_progression(job_id)
    progression.phases[Phase.REALITY_CHECK].status = PhaseStatus.COMPLETED
    orch._set_phase_status(progression, Phase.ASSIGNMENT_REVIEW, PhaseStatus.COMPLETED)
    orch._save_progression(progression)
    print("[assignment-review] Approved — proceeding to schema design")

    # ================================================================
    # Phase 4: SCHEMA DESIGN
    # ================================================================
    print(f"\n{'='*60}")
    print(
        f"  PHASE 4: Schema Design (assigned queries only)  [{datetime.now().strftime('%H:%M:%S.%f')[:-3]}]"
    )
    print(f"{'='*60}")
    orch.resume(job_id, Phase.SCHEMA_DESIGN)

    # ================================================================
    # Phase 4b: POST-SCHEMA ROUTING (automatic cascade)
    # ================================================================
    print(f"\n{'='*60}")
    print(f"  PHASE 4b: Post-Schema Routing  [{datetime.now().strftime('%H:%M:%S.%f')[:-3]}]")
    print(f"{'='*60}")
    print("  Running deterministic router + cascade schema design...")
    orch._run_post_schema_routing(job_id, db_name)

    # ================================================================
    # Phase 5: LOAD TEST (requires k6 + AWS credentials)
    # ================================================================
    print(f"\n{'='*60}")
    print(f"  PHASE 5: Load Test  [{datetime.now().strftime('%H:%M:%S.%f')[:-3]}]")
    print(f"{'='*60}")
    try:
        orch._run_load_test(job_id, db_name)
        print("  Load test complete.")
    except Exception as e:
        print(f"  Load test skipped/failed: {e}")
        print("  (Requires k6 installed + AWS credentials. Continuing to synthesis.)")

    # ================================================================
    # Phase 6: SYNTHESIS
    # ================================================================
    print(f"\n{'='*60}")
    print(f"  PHASE 6: Synthesis  [{datetime.now().strftime('%H:%M:%S.%f')[:-3]}]")
    print(f"{'='*60}")
    progression = orch.get_progression(job_id)
    progression.phases[Phase.SCHEMA_DESIGN].status = PhaseStatus.COMPLETED
    orch._save_progression(progression)
    orch.resume(job_id, Phase.SYNTHESIS)

    # ================================================================
    # RESULTS
    # ================================================================
    av = orch._get_assignment_version(job_id, db_name)
    rp = (
        f"{db_name}/{job_id}/synthesis/v{av}/report.json"
        if av > 0
        else f"{db_name}/{job_id}/referee-synthesis/report.json"
    )
    if store.exists(rp):
        _print_report(store.read_json(rp), rp)
    else:
        print(f"\nReport not found at {rp}")

    print(f"\nAll artifacts: ./artifacts/{db_name}/{job_id}/")
    print("Done!")


def _print_assignment(a: dict) -> None:
    print(f"\nAssignment v{a['version']} — {a['status']}")
    print(f"  {len(a['query_assignments'])} queries assigned")
    print("-" * 50)
    by_engine: dict[str, list] = {}
    for qa in a["query_assignments"]:
        by_engine.setdefault(qa["assigned_engine"], []).append(qa)
    for engine, qas in sorted(by_engine.items()):
        ins = sum(1 for q in qas if q.get("in_scope", True))
        print(f"\n  {engine}: {len(qas)} queries ({ins} in scope)")
        for qa in qas[:5]:
            s = "" if qa.get("in_scope", True) else " [OUT]"
            t = ", ".join(qa.get("source_tables", [])[:3])
            print(f"    {qa['query_id'][:16]}: {qa['confidence']}% [{t}]{s}")
        if len(qas) > 5:
            print(f"    ... and {len(qas) - 5} more")
    multi = [
        t
        for t in a.get("table_assignments", [])
        if isinstance(t, dict) and len(t.get("engines", [])) > 1
    ]
    if multi:
        print(f"\n  Multi-engine tables ({len(multi)}):")
        for ta in multi[:5]:
            print(f"    {ta['table_id']}: {ta['engines']}")  # type: ignore[index]
    co_dep = a.get("co_dependency_groups")
    if co_dep:
        print(f"\n  Co-dependency groups: {len(co_dep)}")


def _print_report(r: dict, path: str) -> None:
    print(f"\n{'='*60}")
    print("  FINAL REPORT")
    print(f"{'='*60}")
    arch = r.get("recommended_architecture", {})
    print(f"\n  Architecture: {arch.get('architecture_type', 'N/A')}")
    print(f"  Table mappings: {len(r.get('table_mappings', []))}")
    print(f"  Query groups: {len(r.get('query_groups', []))}")
    print(f"  Risk level: {r.get('risk_assessment', {}).get('overall_risk_level', 'N/A')}")
    tco = r.get("tco_analysis", {})
    if tco.get("projected_monthly_cost"):
        print(f"  Projected cost: ${tco['projected_monthly_cost']:.2f}/mo")
    if r.get("ranking"):
        print("\n  Engine ranking:")
        for rk in r["ranking"]:
            print(
                f"    {rk['target']}: {rk['confidence_score']}% confidence, ${rk.get('monthly_cost_usd', 0):.2f}/mo"
            )
        # Show workload distribution if assignment data is present
        has_workload = any("assigned_queries" in rk for rk in r["ranking"])
        if has_workload:
            print("\n  Workload distribution:")
            for rk in r["ranking"]:
                aq = rk.get("assigned_queries", 0)
                aqs = rk.get("assigned_queries_in_scope", 0)
                wp = rk.get("workload_percent", 0)
                pt = rk.get("primary_tables", 0)
                reasons = rk.get("assignment_reason_summary", [])
                print(
                    f"    {rk['target']}: {aq} queries ({wp}%), {aqs} in scope, {pt} primary tables"
                )
                if reasons:
                    print(f"      reasons: {', '.join(reasons)}")
    if r.get("assignment_summary"):
        s = r["assignment_summary"]
        print(
            f"\n  Assignment: v{s.get('version')} — {s.get('in_scope_count')}/{s.get('query_count')} in scope"
        )
    summary: str = r.get("summary_deterministic", r.get("summary", ""))
    if summary:
        print(f"\n  Summary: {summary[:300]}...")
    print(f"\n  Full report: ./artifacts/{path}")


if __name__ == "__main__":
    main()
