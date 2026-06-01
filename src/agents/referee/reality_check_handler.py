"""Referee-Reality-Check agent handler — CTO-level engine consolidation.

Reads assignment, triage, analysis, and collector artifacts, runs the reality
check logic, writes the revised assignment and reality check output via
ArtifactStore. Slots between assignment and schema design in the pipeline.
"""

import json
import logging
import os
from collections import defaultdict

from src.agents.referee.consolidation_validator import (
    apply_corrections,
    sanity_sweep,
    validate_consolidations,
)
from src.agents.referee.reality_check import _build_recommendations, run_reality_check
from src.contracts.reality_check_output import RealityCheckOutputContract
from src.storage.artifact_store import ArtifactStore

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Seam 1: Deterministic — reads artifacts and runs consolidation logic, no LLM
# ---------------------------------------------------------------------------


def run_reality_check_deterministic(
    job_id: str,
    database_name: str,
    store: ArtifactStore,
    assignment_version: int = 1,
) -> dict:
    """Run all deterministic reality-check logic without invoking any LLM.

    Reads assignment, triage, collector, and analysis artifacts from the store,
    runs the core consolidation logic, and builds recommendations.

    Returns a dict with keys:
        consolidations, recommendations, architectural_patterns,
        unique_value_assessment, before_distribution, after_distribution,
        executive_summary (always None), assignment, collector_output,
        analysis_outputs
    """
    # Read required artifacts
    assignment_key = f"{database_name}/{job_id}/assignment/v{assignment_version}/assignment.json"
    assignment = store.read_json(assignment_key)

    triage_key = f"{database_name}/{job_id}/referee-triage/triage.json"
    triage = store.read_json(triage_key)

    collector_key = f"{database_name}/{job_id}/collector/output.json"
    collector_output = store.read_json(collector_key)

    # Load analysis outputs for each engine in the triage
    selected_engines = [a["agent_type"] for a in triage.get("selected_agents", [])]
    analysis_outputs: dict[str, dict] = {}
    for engine in selected_engines:
        analysis_key = f"{database_name}/{job_id}/analysis-{engine}/analysis.json"
        if store.exists(analysis_key):
            analysis_outputs[engine] = store.read_json(analysis_key)

    # Extract query capabilities from triage
    query_capabilities = triage.get("query_capabilities", {})

    # Count queries per engine before reality check
    before_distribution: dict[str, int] = defaultdict(int)
    for qa in assignment.get("query_assignments", []):
        before_distribution[qa["assigned_engine"]] += 1

    # Run the core reality check (deterministic consolidation logic)
    result = run_reality_check(
        assignment, triage, analysis_outputs, collector_output, query_capabilities
    )

    # Count queries per engine after reality check
    after_distribution: dict[str, int] = defaultdict(int)
    for qa in result["revised_assignments"]:
        after_distribution[qa["assigned_engine"]] += 1

    return {
        "consolidations": result["consolidations"],
        "recommendations": result["recommendations"],
        "architectural_patterns": result["architectural_patterns"],
        "unique_value_assessment": result["unique_value_assessment"],
        "before_distribution": dict(before_distribution),
        "after_distribution": dict(after_distribution),
        "executive_summary": None,
        # Pass-through data needed by LLM and downstream writing steps
        "assignment": assignment,
        "triage": triage,
        "collector_output": collector_output,
        "analysis_outputs": analysis_outputs,
        # Keep revised_assignments and lightweight_recommendations for the write step
        "revised_assignments": result["revised_assignments"],
        "lightweight_recommendations": result.get("lightweight_recommendations", []),
    }


# ---------------------------------------------------------------------------
# Seam 2: Prepare — formats the LLM request payload from deterministic result
# ---------------------------------------------------------------------------


def prepare_reality_check_llm_input(deterministic_result: dict) -> dict:
    """Format the LLM input payload from a deterministic result dict.

    Returns a dict with two sub-keys:
        consolidation_validation: inputs for validate_consolidations()
        executive_summary: context for _generate_executive_summary()
    """
    triage = deterministic_result.get("triage", {})
    signals = triage.get("signals", [])
    query_signals_map: dict[str, list] = defaultdict(list)
    for signal in signals:
        for qid in signal.get("query_ids", []):
            query_signals_map[qid].append(signal.get("signal", ""))

    return {
        "consolidation_validation": {
            "consolidations": deterministic_result["consolidations"],
            "collector_output": deterministic_result["collector_output"],
            "analysis_outputs": deterministic_result["analysis_outputs"],
            "query_signals": dict(query_signals_map),
        },
        "executive_summary": {
            "before_distribution": deterministic_result["before_distribution"],
            "after_distribution": deterministic_result["after_distribution"],
            "consolidations": deterministic_result["consolidations"],
            "unique_value_assessment": deterministic_result["unique_value_assessment"],
            "architectural_patterns": deterministic_result["architectural_patterns"],
            "recommendations": deterministic_result["recommendations"],
        },
    }


# ---------------------------------------------------------------------------
# Seam 3: Apply — merges LLM output back into the deterministic result
# ---------------------------------------------------------------------------


def apply_reality_check_llm_output(deterministic_result: dict, llm_output: dict) -> dict:
    """Merge LLM output into the deterministic result dict.

    Handles two optional LLM contributions:
    - consolidation_corrections: queries the LLM says should NOT have been moved
    - executive_summary: CTO-facing narrative string

    Returns an updated copy of deterministic_result (mutated in place).
    """
    result = deterministic_result

    if "consolidation_corrections" in llm_output:
        corrections = llm_output["consolidation_corrections"]
        if corrections:
            surviving_engines = set(result["after_distribution"].keys())
            all_original_engines = set(result["before_distribution"].keys())
            result["revised_assignments"], result["consolidations"] = apply_corrections(
                corrections,
                result["revised_assignments"],
                result["consolidations"],
                surviving_engines=surviving_engines,
                all_original_engines=all_original_engines,
            )
            # Rebuild recommendations to reflect corrected consolidations
            result["recommendations"] = _build_recommendations(
                result["revised_assignments"],
                result["consolidations"],
                result["architectural_patterns"],
                {},  # engine_queries not needed for recommendation text
            )
            # Recompute after_distribution
            after_distribution: dict[str, int] = defaultdict(int)
            for qa in result["revised_assignments"]:
                after_distribution[qa["assigned_engine"]] += 1
            result["after_distribution"] = dict(after_distribution)

    if "executive_summary" in llm_output:
        result["executive_summary"] = llm_output["executive_summary"]

    return result


# ---------------------------------------------------------------------------
# Top-level handler — backward-compatible, delegates to seam functions
# ---------------------------------------------------------------------------


def run_reality_check_handler(
    job_id: str,
    database_name: str,
    store: ArtifactStore,
    assignment_version: int = 1,
    llm_mode: str = "bedrock",
) -> None:
    """Run the reality check agent.

    Reads assignment, triage, analysis, and collector artifacts.
    Writes:
      - reality-check/output.json (contract-validated)
      - assignment/v{N+1}/assignment.json (revised, if consolidation occurred)

    Args:
        llm_mode: "bedrock" (default) — validate consolidations + generate executive summary
                  "external" — write LLM input to store and mark as awaiting
                  "none" — skip all LLM calls, use deterministic result only
    """
    import time

    start_time = time.time()

    print(f"[reality-check] Starting for {database_name} (assignment v{assignment_version})")

    # Step 1: always run deterministic logic
    det = run_reality_check_deterministic(job_id, database_name, store, assignment_version)

    print(f"[reality-check] Before: {det['before_distribution']}")
    print(f"[reality-check] After:  {det['after_distribution']}")

    for c in det["consolidations"]:
        print(
            f"[reality-check] Consolidated {c['query_count']} queries: "
            f"{c['from_engine']} → {c['to_engine']} "
            f"(saves ~${c['saved_cost_estimate']}/mo)"
        )

    for p in det["architectural_patterns"]:
        print(f"[reality-check] Pattern: {p['name']}")

    for lw in det.get("lightweight_recommendations", []):
        print(
            f"[reality-check] LIGHTWEIGHT: {lw['service']} for "
            f"{len(lw['query_ids'])} queries (replaces {lw['replaces_engine']})"
        )

    # Step 2: LLM phase — determined by llm_mode
    if llm_mode == "bedrock":
        _run_bedrock_llm_phase(det, database_name)

    elif llm_mode == "external":
        llm_input = prepare_reality_check_llm_input(det)
        llm_input_key = f"{database_name}/{job_id}/reality-check/llm_input.json"
        store.write_json(llm_input_key, llm_input)
        awaiting_key = f"{database_name}/{job_id}/reality-check/awaiting_llm.json"
        store.write_json(awaiting_key, {"status": "awaiting_llm", "input_key": llm_input_key})
        print(f"[reality-check] LLM input written to {llm_input_key} — awaiting external LLM")

    # llm_mode == "none": skip LLM entirely, use det as-is

    # Step 3: Sanity sweep — catch orphan engines with trivial query counts
    query_capabilities = det.get("triage", {}).get("query_capabilities", {})
    det["revised_assignments"], det["consolidations"] = sanity_sweep(
        det["revised_assignments"],
        det["consolidations"],
        query_capabilities,
    )
    # Recompute after_distribution post-sweep
    sweep_distribution: dict[str, int] = defaultdict(int)
    for qa in det["revised_assignments"]:
        sweep_distribution[qa["assigned_engine"]] += 1
    det["after_distribution"] = dict(sweep_distribution)

    # Step 4: validate and write output
    assignment = det["assignment"]

    output = RealityCheckOutputContract.model_validate(
        {
            "source_assignment_version": assignment_version,
            "unique_value_assessment": det["unique_value_assessment"],
            "consolidations": det["consolidations"],
            "architectural_patterns": det["architectural_patterns"],
            "executive_summary": det["executive_summary"],
            "recommendations": det["recommendations"],
            "before_distribution": det["before_distribution"],
            "after_distribution": det["after_distribution"],
            "lightweight_recommendations": det.get("lightweight_recommendations", []),
        }
    )
    output_key = f"{database_name}/{job_id}/reality-check/output.json"
    store.write_json(output_key, output.model_dump(mode="json"))

    # If consolidation occurred, write a new assignment version
    if det["consolidations"]:
        new_version = assignment_version + 1
        revised_assignment = {
            **assignment,
            "version": new_version,
            "query_assignments": det["revised_assignments"],
            "reality_check_applied": True,
        }
        revised_key = f"{database_name}/{job_id}/assignment/v{new_version}/assignment.json"
        store.write_json(revised_key, revised_assignment)
        print(
            f"[reality-check] Revised assignment written to v{new_version} "
            f"({len(det['consolidations'])} consolidations)"
        )
    else:
        print("[reality-check] No consolidations — assignment unchanged")

    elapsed = time.time() - start_time
    print(f"[reality-check] Complete in {elapsed:.1f}s")


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _run_bedrock_llm_phase(det: dict, database_name: str) -> None:
    """Run the Bedrock LLM phase: validate consolidations + generate executive summary.

    Mutates det in place via apply_reality_check_llm_output.
    This is the original bedrock behavior extracted to a helper.
    """
    llm_output: dict = {}

    # LLM validation of consolidation decisions
    if det["consolidations"]:
        collector_output = det["collector_output"]
        revised_assignments = det["revised_assignments"]

        queries = collector_output.get("queries", {}).get("query_patterns", [])
        query_map = {q["query_id"]: q for q in queries}

        # Build signal map from triage — same logic as the original handler
        triage = det.get("triage", {})
        signals = triage.get("signals", [])
        query_signals_map: dict[str, list[str]] = defaultdict(list)
        for signal in signals:
            for qid in signal.get("query_ids", []):
                query_signals_map[qid].append(signal.get("signal", ""))

        corrections = validate_consolidations(
            consolidations=det["consolidations"],
            revised_assignments=revised_assignments,
            query_map=query_map,
            query_signals=dict(query_signals_map),
        )

        if corrections:
            print(
                f"[reality-check] LLM reversed {len(corrections)} queries — "
                f"applying corrections"
            )
            llm_output["consolidation_corrections"] = corrections

    # Generate executive summary
    executive_summary = _generate_executive_summary(
        database_name=database_name,
        collector_output=det["collector_output"],
        before_distribution=det["before_distribution"],
        after_distribution=det["after_distribution"],
        consolidations=det["consolidations"],
        unique_value_assessment=det["unique_value_assessment"],
        architectural_patterns=det["architectural_patterns"],
        recommendations=det["recommendations"],
        analysis_outputs=det["analysis_outputs"],
    )
    if executive_summary is not None:
        llm_output["executive_summary"] = executive_summary

    if llm_output:
        apply_reality_check_llm_output(det, llm_output)


def _generate_executive_summary(
    database_name: str,
    collector_output: dict,
    before_distribution: dict[str, int],
    after_distribution: dict[str, int],
    consolidations: list[dict],
    unique_value_assessment: dict,
    architectural_patterns: list[dict],
    recommendations: list[str],
    analysis_outputs: dict[str, dict],
) -> str | None:
    """Generate a CTO-facing executive summary using an LLM.

    Returns None if the LLM is unavailable — the UI will fall back
    to building a deterministic summary from the structured data.
    """
    try:
        from strands import Agent
        from strands.models.bedrock import BedrockModel
    except ImportError:
        logger.warning("Strands not available — skipping executive summary")
        return None

    # Gather context
    tables = collector_output.get("database_schema", {}).get("tables", [])
    queries = collector_output.get("queries", {}).get("query_patterns", [])
    total_tables = len(tables)
    total_queries = len(queries)

    # Only full consolidations count as eliminations
    eliminated_engines = [c["from_engine"] for c in consolidations if c.get("action") != "partial"]
    surviving_engines = list(after_distribution.keys())

    # Summarize what each engine analysis found
    engine_summaries = {}
    for engine, analysis in analysis_outputs.items():
        summary = {"tables_analyzed": 0, "top_signals": []}
        if "table_scores" in analysis:
            summary["tables_analyzed"] = len(analysis["table_scores"])
        if "signals" in analysis:
            summary["top_signals"] = [
                s.get("signal_name", s.get("name", "")) for s in analysis["signals"][:3]
            ]
        engine_summaries[engine] = summary

    # Build the context block
    context = {
        "database": database_name,
        "source_tables": total_tables,
        "query_patterns": total_queries,
        "engines_evaluated": list(before_distribution.keys()),
        "engines_remaining": surviving_engines,
        "before": before_distribution,
        "after": after_distribution,
        "engines_eliminated": list(set(eliminated_engines)),
        "consolidations": [
            {
                "from": c["from_engine"],
                "to": c["to_engine"],
                "queries_moved": c["query_count"],
                "queries_retained": len(c.get("queries_retained", [])),
                "action": c.get("action", "full"),
                "reason": c.get("reason", ""),
            }
            for c in consolidations
        ],
        "engine_analysis_summary": engine_summaries,
        "architectural_patterns": [p["name"] for p in architectural_patterns],
    }

    prompt = (
        "You just finished a deep analysis of a production database workload. "
        "You evaluated every query pattern against multiple target engines, "
        "and now you are briefing the CTO on what you found and what you recommend.\n\n"
        "Write 2-3 SHORT sentences. Your tone is a confident senior architect "
        "who owns this recommendation.\n\n"
        "CRITICAL GUARDRAILS (violating any of these is a failure):\n"
        "- NEVER use first person ('I analyzed', 'I found', 'I recommend'). "
        "Use second person ('Your workload...', 'The analysis shows...') or "
        "passive voice ('311 tables were analyzed...').\n"
        "- NEVER express doubt, uncertainty, or recommend collecting more data. "
        "You have the answer. Present it.\n"
        "- NEVER mention confidence scores or internal metrics.\n"
        "- If multiple engines are needed, present that as the RIGHT architecture "
        "(purpose-built databases matched to workload patterns), not as a complication.\n"
        "- Complexity is expected in modernization. Frame everything as 'here is the "
        "path forward' not 'here are the concerns.'\n"
        "- Your job is to give the CTO confidence that the migration is well-planned "
        "and the architecture is solid.\n\n"
        "What to convey:\n"
        "- What was analyzed (scope: tables, queries, engines evaluated)\n"
        "- The decision: which engines stay, which were dropped, and the "
        "technical WHY (e.g. 'the document queries are simple key lookups that "
        "DynamoDB handles natively', not 'to save money')\n"
        "- If engines were consolidated, explain what makes the remaining engines "
        "sufficient. Show you understand the workload.\n"
        "- IMPORTANT: If a consolidation has action='partial', the engine was NOT "
        "eliminated. Some queries moved but others stayed. Do NOT say the engine was "
        "dropped or eliminated. Instead explain that it was reduced in scope.\n"
        "- If no engines were eliminated, say why each one earns its place\n\n"
        "AWS MANAGED INTEGRATIONS (use these instead of generic pattern names):\n"
        "When the architecture involves data flowing between two engines, recommend "
        "the specific AWS managed integration instead of naming abstract patterns. "
        "For example, instead of saying 'CQRS pattern between DynamoDB and OpenSearch', "
        "say 'DynamoDB zero-ETL integration with OpenSearch keeps search indexes "
        "in sync automatically'. Here are the available zero-ETL integrations:\n"
        "- DynamoDB to OpenSearch: via OpenSearch Ingestion (fully managed, near real-time replication via DynamoDB Streams)\n"
        "- DocumentDB to OpenSearch: zero-ETL for full-text search over document collections\n"
        "- DynamoDB to Redshift: for analytics on transactional data without impacting production\n"
        "- Aurora MySQL/PostgreSQL to Redshift: near real-time analytics on relational data\n"
        "- Aurora MySQL to SageMaker Lakehouse: near real-time ML workloads\n"
        "- CloudWatch Logs to OpenSearch: direct log querying and visualization\n"
        "- S3 to OpenSearch: query operational logs in data lakes\n"
        "Only mention a zero-ETL integration if it directly applies to the engines "
        "in the recommendation. Do not list integrations that are irrelevant.\n\n"
        "STRICT STYLE RULES:\n"
        "- NEVER use em dashes (the long dash). Use commas, periods, or parentheses instead.\n"
        "- NEVER use the word 'straightforward', 'robust', 'leverage', 'comprehensive', "
        "'seamless', 'cutting-edge', 'holistic', 'synergy', 'paradigm', 'elevate', "
        "'landscape', 'realm', 'foster', 'delve', 'moreover', 'furthermore', 'notably'\n"
        "- Do not lead with or focus on cost savings\n"
        "- No buzzwords or marketing language\n"
        "- No markdown, bullet points, or headers\n"
        "- Write like a human engineer, not a language model\n"
        "- Keep it under 3 sentences total\n\n"
        f"Context:\n{json.dumps(context, indent=2)}\n\n"
        "Write the briefing now."
    )

    try:
        model = BedrockModel(
            model_id=os.environ.get(
                "SUMMARY_MODEL_ID",
                "us.anthropic.claude-sonnet-4-6",
            ),
            max_tokens=512,
            temperature=0.2,
        )
        agent = Agent(
            model=model,
            system_prompt=(
                "You are a senior database architect presenting the results of a "
                "completed modernization assessment. You speak with absolute authority "
                "because you have already done the work. You NEVER use first person "
                "('I analyzed', 'I found'). You address the reader's workload directly "
                "('Your workload presents...', 'The analysis identified...'). You NEVER "
                "express doubt or recommend going back for more data. Complexity is your "
                "job and you have handled it. Short, direct, confident, solution-oriented. "
                "No filler, no hedging."
            ),
            tools=[],
            callback_handler=None,
        )

        print("[reality-check] Generating executive summary with LLM...")
        result = agent(prompt)
        narrative = str(result).strip()

        if len(narrative) > 20:
            print(f"[reality-check] Executive summary generated ({len(narrative)} chars)")
            return narrative

    except Exception as exc:
        model_id = os.environ.get("SUMMARY_MODEL_ID", "us.anthropic.claude-sonnet-4-6")
        logger.error(
            "LLM executive summary failed (model=%s): %s",
            model_id,
            exc,
        )
        print(f"[reality-check] ERROR: LLM summary failed with model '{model_id}': {exc}")
        print("[reality-check] Set SUMMARY_MODEL_ID env var to override the model.")

    return None
