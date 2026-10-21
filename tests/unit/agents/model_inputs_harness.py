"""Run every step that talks to a model on a sample job and record what it hands over.

Shared by the model-boundary marker tests (``test_query_tags_reach_no_model.py``,
#1180, and ``test_collector_text_is_framed.py``, #1317). A sample job is built from
an edited collection, then every step that builds a model input runs both ways: the
external session's request files (analysis for DynamoDB and DocumentDB, reality
check, schema design, synthesis) and the Bedrock calls (the analysis advisors, the
reality check and its boto3 validator, every schema designer, the redesign path,
synthesis, the free-text answer check). The AWS Transform orchestrator's report and
routing-summary tools run on the same job. The check sits at the model boundary, so
a builder of any name is covered: ``strands.Agent`` is a stand-in that records every
prompt, every botocore call to a Bedrock service is recorded and refused (no request
leaves the machine), and the designers' input files are recorded as they are handed
over.

A name-based list (every ``prepare_*``/``build_*``/``render_*`` request or llm-input
builder, every ``*_prompt`` function, ``headless_text``, the designers' PE-review,
the validators' call sites, the answer check and those two tools) checks the run's
coverage: each one must run, or be in ``NOT_RUN`` with a reason and then really not
run.
"""

from __future__ import annotations

import json
import re
import sys
import threading
import zipfile
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import botocore.client
import pytest

from src.storage.local_store import LocalArtifactStore
from src.tools.database.collection_text import extract_json_object

REPO = Path(__file__).resolve().parents[3]
SAMPLE = "trailgear"

_BUILDER_RE = re.compile(
    r"^\s*def ((?:prepare|build|render)_\w*(?:llm_input|input|request)"
    r"|\w*_prompt|_build_\w*prompt\w*|headless_text|_invoke_pe_reviewer|_try_\w*validator"
    # The free-text answer check, and the AWS Transform orchestrator's tools whose
    # results carry the report and the routing summary to its model (#1317).
    r"|_bedrock_validate|get_synthesis_report|present_assignment_review)\s*\(",
    re.MULTILINE,
)

# Builders the steps do not run, and why none of them is reached by the run.
_PE_REVIEW = (
    "runs only after a design passes, which the stand-in's empty answers never do; it "
    "reviews the design output with the input summary, whose queries are the "
    "AgentQueryPattern projection (test_query_tags_not_in_model_inputs.py)"
)
NOT_RUN: dict[str, str] = {
    "src/tools/schema/aurora_mysql_schema_agent.py::_invoke_pe_reviewer": _PE_REVIEW,
    "src/tools/schema/aurora_postgresql_schema_agent.py::_invoke_pe_reviewer": _PE_REVIEW,
    "src/tools/schema/documentdb_schema_agent.py::_invoke_pe_reviewer": _PE_REVIEW,
    "src/tools/schema/dynamodb_schema_agent.py::_invoke_pe_reviewer": _PE_REVIEW,
    "src/tools/schema/elasticache_schema_agent.py::_invoke_pe_reviewer": _PE_REVIEW,
    "src/tools/schema/opensearch_schema_agent.py::_invoke_pe_reviewer": _PE_REVIEW,
    "src/tools/analysis/llm_advisor_base.py::_build_prompt": "abstract; each advisor's own runs",
    "src/agents/analysis/aurora_pg_analysis_agent.py::prepare_aurora_pg_llm_input": (
        "no caller (test_the_unused_builders_have_no_caller); its AnalysisInput would come "
        "from read_collector_for_models"
    ),
    "src/agents/analysis/aurora_mysql_analysis_agent.py::prepare_aurora_mysql_llm_input": (
        "no caller (test_the_unused_builders_have_no_caller); its AnalysisInput would come "
        "from read_collector_for_models"
    ),
    "src/agents/schema_design/pe_review_external.py::prepare_pe_review_llm_input": (
        "needs a finished design; reads the design and its group input, which "
        "build_group_input limits to the agent contract's query fields "
        "(test_query_tags_not_in_model_inputs.py)"
    ),
    "src/tools/schema/aurora_common/bedrock_delta.py::pe_review_prompt": (
        "needs a finished design delta; takes the delta and the input summary's counts"
    ),
    "src/tools/schema/base_schema_agent.py::numbers_correction_prompt": "takes error strings only",
}


def builders() -> set[str]:
    """Every model-input builder in ``src/``, as ``path::name``."""
    found: set[str] = set()
    for path in sorted((REPO / "src").rglob("*.py")):
        for match in _BUILDER_RE.finditer(path.read_text(encoding="utf-8")):
            found.add(f"{path.relative_to(REPO)}::{match.group(1)}")
    return found


def coverage_problems(called: set[tuple[str, str]]) -> list[str]:
    """What is wrong with a run's builder coverage; empty when every builder is accounted for."""
    found = builders()
    problems: list[str] = []
    if len(found) < 15:  # the pattern still finds them
        problems.append(f"the builder pattern found only {sorted(found)}")
    not_run, ran_anyway = [], []
    for builder in sorted(found):
        path, name = builder.split("::")
        ran = (str((REPO / path).resolve()), name) in called
        if builder in NOT_RUN:
            if ran:
                ran_anyway.append(builder)
        elif not ran:
            not_run.append(builder)
    if ran_anyway:
        problems.append(f"NOT_RUN says these do not run, but they did; cover them: {ran_anyway}")
    if not_run:
        problems.append(
            "these model-input builders did not run: run them in the harness or say in "
            f"NOT_RUN why they cannot be reached: {not_run}"
        )
    if sorted(set(NOT_RUN) - found):
        problems.append("NOT_RUN names a builder that no longer exists")
    return problems


class _ModelStandIn:
    """``strands.Agent``: records each system prompt and prompt, answers nothing."""

    prompts: list[str] = []
    system_prompts: list[str] = []

    def __init__(self, *args, **kwargs) -> None:
        system = str(kwargs.get("system_prompt") or "")
        type(self).prompts.append(system)
        type(self).system_prompts.append(system)

    def __call__(self, prompt, *args, **kwargs):
        type(self).prompts.append(str(prompt))
        return SimpleNamespace(structured_output=None, message={"content": [{"text": "{}"}]})


EditRaw = Callable[[dict], dict]
EditCollector = Callable[[dict], None]


def _job(
    tmp: Path,
    edit_raw: EditRaw | None,
    edit_collector: EditCollector | None,
    sample: str = SAMPLE,
) -> tuple[LocalArtifactStore, str, str]:
    import scripts.run_assessment as run_assessment

    with zipfile.ZipFile(REPO / "docs" / "examples" / sample / f"{sample}.zip") as z:
        member = next(n for n in z.namelist() if n.endswith("-collection.json"))
        raw = json.loads(extract_json_object(z.read(member).decode("utf-8")))
    if edit_raw is not None:
        raw = edit_raw(raw)
    tmp.mkdir()
    collection = tmp / f"{sample}-collection.json"
    collection.write_text(json.dumps(raw), encoding="utf-8")
    store = LocalArtifactStore(base_dir=str(tmp / "artifacts"))
    job_id, db = run_assessment.phase_collect(str(collection), sample, store)
    if edit_collector is not None:
        key = f"{db}/{job_id}/collector/output.json"
        collector = store.read_json(key)
        edit_collector(collector)
        store.write_json(key, collector)
    return store, job_id, db


def _quietly(step, *args, **kwargs) -> None:
    """Run ``step``; a step that fails on the stand-in's empty answers still built its input."""
    try:
        step(*args, **kwargs)
    except (Exception, SystemExit):  # noqa: BLE001 -- the inputs are what is checked
        pass


def run_every_model_input(
    tmp: Path,
    *,
    edit_raw: EditRaw | None = None,
    edit_collector: EditCollector | None = None,
    sample: str = SAMPLE,
    free_text: str = "Keep the order history in one place.",
) -> dict:
    """Build a job from the edited sample and run every model-input step, both ways.

    ``sample`` names a bundled sample under ``docs/examples/``. ``edit_raw`` edits
    the uploaded collection before it is collected; ``edit_collector`` edits the
    collector output after. ``free_text`` is the customer's own text: the revision
    request's note and new pattern, and the free-text question and answer the answer
    check sees. Returns the artifact files, the prompts and system prompts the
    stand-in model saw, the designers' handed inputs, the refused Bedrock requests,
    the AWS Transform tool results, the collector reads and the functions that ran.
    """
    import scripts.run_analysis as run_analysis
    import scripts.run_assessment as run_assessment
    import scripts.run_schema_design as run_schema_design
    from src.agents.analysis.handler import run_analysis as analysis_handler
    from src.agents.answer_validation import validate_answer
    from src.agents.schema_design import handler as schema_handler
    from src.agents.schema_design.revision_handler import _dispatch_llm_redesign
    from src.atx_orchestrator import tools as atx_tools
    from src.atx_orchestrator.runtime.store import upgrade_store
    from src.contracts.agent_interaction_models import AgentQuestion
    from src.contracts.schema_revision_models import (
        NewPattern,
        PatternAction,
        PatternModification,
        SchemaRevisionRequest,
    )

    tool_results: list[str] = []

    called: set[tuple[str, str]] = set()

    def profile(frame, event, _arg):
        if event == "call":
            called.add((frame.f_code.co_filename, frame.f_code.co_name))

    handed: list[str] = []
    real_dispatch = schema_handler._dispatch_schema_agent
    bedrock_requests: list[str] = []
    real_api_call = botocore.client.BaseClient._make_api_call

    def recording_api_call(client, operation_name, api_params):
        if client.meta.service_model.service_name.startswith("bedrock"):
            bedrock_requests.append(json.dumps(api_params, default=str))
            raise RuntimeError("stand-in: no real model in tests")
        return real_api_call(client, operation_name, api_params)

    collector_reads: list[tuple[str, str]] = []
    real_read_json = LocalArtifactStore.read_json

    def spying_read_json(store, path):
        if str(path).endswith("collector/output.json"):
            caller = sys._getframe(1).f_code
            collector_reads.append((caller.co_filename, caller.co_name))
        return real_read_json(store, path)

    def recording_dispatch(target_type, collector_path=None, analysis_path=None, **kwargs):
        for path in (collector_path, analysis_path, *kwargs.values()):
            if path:
                handed.append(Path(path).read_text(encoding="utf-8"))
        return real_dispatch(target_type, collector_path, analysis_path, **kwargs)

    _ModelStandIn.prompts = []
    _ModelStandIn.system_prompts = []
    mp = pytest.MonkeyPatch()
    mp.setattr(botocore.client.BaseClient, "_make_api_call", recording_api_call)
    # Fake credentials and a region: a boto3 client is built the same way on every
    # machine, and its calls reach the recorder (which refuses them) instead of failing
    # on a missing region; real credentials in the environment are never used.
    for name, value in (
        ("AWS_DEFAULT_REGION", "us-east-1"),
        ("AWS_ACCESS_KEY_ID", "testing"),
        ("AWS_SECRET_ACCESS_KEY", "testing"),  # pragma: allowlist secret
        ("AWS_SESSION_TOKEN", "testing"),
    ):
        mp.setenv(name, value)
    mp.delenv("AWS_PROFILE", raising=False)
    mp.setattr(LocalArtifactStore, "read_json", spying_read_json)
    # A cached builder (``functools.cache``) that an earlier test in this process
    # called would not run again; clear its cache so the run reaches it.
    names = {b.split("::")[1] for b in builders()}
    for module in list(sys.modules.values()):
        if (getattr(module, "__name__", "") or "").startswith("src."):
            for name in names:
                cached = getattr(module, name, None)
                if callable(getattr(cached, "cache_clear", None)):
                    cached.cache_clear()
    # Every thread, including pool threads an earlier test started.
    threading.setprofile_all_threads(profile)
    try:
        # The external session's request files.
        ext_store, job, db = _job(tmp / "external", edit_raw, edit_collector, sample)
        mp.chdir(tmp / "external")
        selected = run_assessment.phase_triage(ext_store, job, db)
        run_assessment.phase_analysis(ext_store, job, db, selected)
        for engine in ("dynamodb", "documentdb"):
            if engine in selected:
                run_analysis.run_external(ext_store, job, db, engine)
        run_assessment.phase_assignment(ext_store, job, db)
        _quietly(run_assessment.phase_reality_check, ext_store, job, db, "external")
        _quietly(run_assessment.phase_schema_design, ext_store, job, db, "external")
        for engine in ("dynamodb", "documentdb", "opensearch", "elasticache", "aurora_postgresql"):
            _quietly(run_schema_design.run_external, ext_store, job, db, engine, 1)
        _quietly(run_schema_design.run_split, ext_store, job, db, "dynamodb", 1)
        _quietly(run_assessment.phase_synthesis, ext_store, job, db, "external")

        # The Bedrock calls, with a stand-in model.
        bed_store, bjob, bdb = _job(tmp / "bedrock", edit_raw, edit_collector, sample)
        mp.chdir(tmp / "bedrock")
        import strands

        real_agent = strands.Agent
        mp.setattr("strands.Agent", _ModelStandIn)
        # A module that already ran ``from strands import Agent`` holds the real one.
        for module in list(sys.modules.values()):
            if (module.__name__ or "").startswith("src.") and getattr(
                module, "Agent", None
            ) is real_agent:
                mp.setattr(module, "Agent", _ModelStandIn)
        mp.setattr("src.shared.role_bedrock_model.RoleBedrockModel", lambda *a, **k: object())
        mp.setattr("time.sleep", lambda _s: None)
        mp.setattr(schema_handler, "_dispatch_schema_agent", recording_dispatch)
        selected = run_assessment.phase_triage(bed_store, bjob, bdb)
        for engine in selected:
            _quietly(analysis_handler, bjob, bdb, engine, bed_store, llm_mode="bedrock")
        run_assessment.phase_assignment(bed_store, bjob, bdb)
        _quietly(run_assessment.phase_reality_check, bed_store, bjob, bdb, "bedrock")
        for engine in ("dynamodb", "documentdb", "opensearch", "elasticache", "aurora_postgresql"):
            _quietly(
                schema_handler.run_schema_design_auto,
                bjob,
                bdb,
                engine,
                bed_store,
                llm_mode="bedrock",
                assignment_version=1,
            )
        request = SchemaRevisionRequest(
            base_version=1,
            pattern_modifications=[
                PatternModification(
                    pattern_id="DDB-AP-1", action=PatternAction.NOTE, note=free_text
                )
            ],
            table_modifications=[],
            new_patterns=[
                NewPattern(
                    description=free_text,
                    target_engine="dynamodb",
                    source_tables=[],
                    context=free_text,
                )
            ],
        )
        _quietly(_dispatch_llm_redesign, bjob, bdb, "dynamodb", {}, request, bed_store)
        _quietly(run_assessment.phase_synthesis, bed_store, bjob, bdb, "bedrock")
        # The free-text answer check (boto3, refused by the recorder).
        question = AgentQuestion(
            question_id="q-free-text", category="design", question=free_text, options=None
        )
        _quietly(validate_answer, question, free_text)
        # The AWS Transform orchestrator's tools, reading the Bedrock job's report
        # and assignment from the same store.
        mp.setattr(atx_tools, "_make_store", lambda: upgrade_store(bed_store))
        for atx_tool in (atx_tools.get_synthesis_report, atx_tools.present_assignment_review):
            try:
                tool_results.append(str(atx_tool(bjob, bdb)))
            except Exception:  # noqa: BLE001 -- recorded as missing by the caller's checks
                pass
    finally:
        threading.setprofile_all_threads(None)  # type: ignore[arg-type]
        mp.undo()

    files = {}
    for root in (tmp / "external" / "artifacts", tmp / "bedrock" / "artifacts"):
        for path in sorted(root.rglob("*")):
            if path.is_file() and path.suffix in (".json", ".md", ".txt", ".mmd"):
                files[str(path.relative_to(root))] = path.read_text(
                    encoding="utf-8", errors="replace"
                )
    return {
        "files": files,
        "prompts": list(_ModelStandIn.prompts),
        "system_prompts": list(_ModelStandIn.system_prompts),
        "handed": handed,
        "tool_results": tool_results,
        "bedrock_requests": bedrock_requests,
        "collector_reads": [(str(Path(f).resolve()), n) for f, n in collector_reads],
        "called": {(str(Path(f).resolve()), n) for f, n in called},
    }
