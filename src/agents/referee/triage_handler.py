"""Referee-Triage agent handler — reads collector output, selects analysis agents."""

from collections.abc import Iterable
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
from src.shared.progress import say
from src.storage.artifact_store import ArtifactStore
from src.tools.sql.query_facts import remember_collected_facts


def run_triage(
    job_id: str,
    database_name: str,
    store: ArtifactStore,
    engine_scope: Iterable[str] | None = None,
) -> None:
    """Run the triage agent. Reads collector output, writes triage decisions via ArtifactStore.

    ``engine_scope`` is the customer's engine scope (#1327, ADR-031 Decision 2
    Amendment 14), recorded in ``triage.json``. ``None`` takes the scope the job was
    started with (``decisions/engine_scope.json``, #1328: the API's, the CLI's or the
    orchestrator's ``target_databases``), and with no such decision every engine.
    """
    import time

    start_time = time.time()

    say(f"[triage] Starting triage for {database_name}")

    # Read collector output
    collector_key = f"{database_name}/{job_id}/collector/output.json"
    collector_output: dict = store.read_json(collector_key)
    remember_collected_facts(collector_output)
    tables = collector_output.get("database_schema", {}).get("tables", [])
    queries = collector_output.get("queries", {}).get("query_patterns", [])
    say(f"[triage] Loaded collector: {len(tables)} tables, {len(queries)} queries")

    if engine_scope is None:
        from src.agents.referee.engine_scope import recorded_engine_scope

        engine_scope = recorded_engine_scope(store, database_name, job_id)
        if engine_scope is not None:
            say(f"[triage] Customer's engine scope: {', '.join(engine_scope)}")

    # Run triage
    say("[triage] Analyzing workload signals...")
    result = triage(collector_output, engine_scope=engine_scope)

    say(f"[triage] Signals detected: {len(result.signals)}")
    for sig in result.signals:
        say(f"[triage]   {sig.signal} → {sig.targets} ({sig.evidence[:80]})")

    say(f"[triage] Selected: {list(result.selected.keys())}")
    say(f"[triage] Skipped: {list(result.skipped.keys())}")
    if result.deferred:
        say(f"[triage] Deferred: {list(result.deferred.keys())}")

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
        engine_scope=result.engine_scope,
    )

    key = f"{database_name}/{job_id}/referee-triage/triage.json"
    store.write_json(key, triage_output.model_dump(mode="json"))
    elapsed = time.time() - start_time
    confidence = _compute_triage_confidence(result)
    say(f"[triage] Confidence: {confidence}%")
    say(
        f"[triage] ✅ Complete in {elapsed:.1f}s — {len(result.selected)} selected, {len(result.skipped)} skipped"
    )


def _compute_triage_confidence(result) -> int:
    """Triage confidence (0-100) from evidence: the workload signals that fired.

    Base confidence is 50 (the schema and query statistics are always some basis
    for selection); each signal that fired adds 5 points, capped at 50, so ten
    signals reach 100. How many engines are selected is not evidence (#927): every
    query-owning engine is analysed whatever the workload shows (#922), so a
    workload with no signals stays at the base.
    """
    base = 50
    signal_bonus = min(len(result.signals) * 5, 50)
    return min(base + signal_bonus, 100)
