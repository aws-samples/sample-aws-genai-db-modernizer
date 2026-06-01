"""Referee-Triage agent handler — reads collector output, selects analysis agents."""

from dataclasses import asdict
from datetime import UTC, datetime

from src.agents.referee.triage import triage
from src.contracts.triage_output import (
    DeferredAgent,
    SelectedAgent,
    SkippedAgent,
    TriageOutputContract,
    TriageSignalRecord,
)
from src.storage.artifact_store import ArtifactStore


def run_triage(job_id: str, database_name: str, store: ArtifactStore) -> None:
    """Run the triage agent. Reads collector output, writes triage decisions via ArtifactStore."""
    import time

    start_time = time.time()

    print(f"[triage] Starting triage for {database_name}")

    # Read collector output
    collector_key = f"{database_name}/{job_id}/collector/output.json"
    collector_output: dict = store.read_json(collector_key)
    tables = collector_output.get("database_schema", {}).get("tables", [])
    queries = collector_output.get("queries", {}).get("query_patterns", [])
    print(f"[triage] Loaded collector: {len(tables)} tables, {len(queries)} queries")

    # Run triage
    print("[triage] Analyzing workload signals...")
    result = triage(collector_output)

    print(f"[triage] Signals detected: {len(result.signals)}")
    for sig in result.signals:
        print(f"[triage]   {sig.signal} → {sig.targets} ({sig.evidence[:80]})")

    print(f"[triage] Selected: {list(result.selected.keys())}")
    print(f"[triage] Skipped: {list(result.skipped.keys())}")
    if result.deferred:
        print(f"[triage] Deferred: {list(result.deferred.keys())}")

    triage_output = TriageOutputContract(
        job_id=job_id,
        database_name=database_name,
        selected_agents=[
            SelectedAgent(agent_type=agent, reasons=list(reasons))
            for agent, reasons in result.selected.items()
        ],
        skipped_agents=[
            SkippedAgent(agent_type=agent, reason=reason)
            for agent, reason in result.skipped.items()
        ],
        baseline=dict(result.baseline),
        deferred_agents=[
            DeferredAgent(agent_type=agent, reasons=list(reasons))
            for agent, reasons in result.deferred.items()
        ],
        signals=[TriageSignalRecord(**asdict(s)) for s in result.signals],
        query_capabilities=result.query_capabilities,
        confidence_score=_compute_triage_confidence(result),
        timestamp=datetime.now(UTC),
    )

    key = f"{database_name}/{job_id}/referee-triage/triage.json"
    store.write_json(key, triage_output.model_dump(mode="json"))
    elapsed = time.time() - start_time
    confidence = _compute_triage_confidence(result)
    print(f"[triage] Confidence: {confidence}%")
    print(
        f"[triage] ✅ Complete in {elapsed:.1f}s — {len(result.selected)} selected, {len(result.skipped)} skipped"
    )


def _compute_triage_confidence(result) -> int:
    """Compute triage confidence (0-100) based on signal coverage.

    More signals with evidence = higher confidence in agent selection.
    Base confidence is 50 (we always have some basis for selection).
    Each signal adds up to 5 points, capped at 100.
    """
    base = 50
    signal_bonus = min(len(result.signals) * 5, 40)
    selection_bonus = min(len(result.selected) * 3, 10)
    return min(base + signal_bonus + selection_bonus, 100)
