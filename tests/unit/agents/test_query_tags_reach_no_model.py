"""No model input carries a query's tag fields (#1180).

``QueryPattern.query_tags`` is the caller's join key to its own code. A sample job
gets a marker tag on every query, then runs every step that talks to a model, both
ways: the external session's request files (analysis for DynamoDB and DocumentDB,
reality check, schema design, synthesis) and the Bedrock calls (the analysis
advisors, the reality check and its boto3 validator, every schema designer, the
redesign path, synthesis). The check sits at the model boundary, so a builder of any
name is covered: ``strands.Agent`` is a stand-in that records every prompt, every
botocore call to a Bedrock service is recorded and refused (no request leaves the
machine), and the designers' input files are recorded as they are handed over. The
marker must appear in no request file, prompt, Bedrock request or designer input.
Every read of ``collector/output.json`` made directly by a model-facing module during
the run must be ``read_collector_for_models``'s own.

``query_text`` keeps its leading comment (#1180), so the representative row's comment
text still reaches prompts inside it, as on main; this test is about the tag fields.

A name-based list (every ``prepare_*``/``build_*``/``render_*`` request or llm-input
builder, every ``*_prompt`` function, ``headless_text``, the designers' PE-review and
the validators' call sites) checks the run's coverage: each one must run, or be in
``NOT_RUN`` with a reason and then really not run.
"""

from __future__ import annotations

import json
import re
import sys
import threading
import zipfile
from pathlib import Path
from types import SimpleNamespace

import botocore.client
import pytest

from src.storage.local_store import LocalArtifactStore
from src.tools.database.collection_text import extract_json_object

REPO = Path(__file__).resolve().parents[3]
MARK = "Mark1180QueryTagSentinel"
SAMPLE = "trailgear"

_BUILDER_RE = re.compile(
    r"^\s*def ((?:prepare|build|render)_\w*(?:llm_input|input|request)"
    r"|\w*_prompt|_build_\w*prompt\w*|headless_text|_invoke_pe_reviewer|_try_\w*validator)\s*\(",
    re.MULTILINE,
)
# Modules whose direct reads of the collector output must be the choke point's.
sys.path.insert(0, str(REPO / "ci"))
from needs_llm_run import MODEL_FACING  # noqa: E402

FEEDERS = ("src/agents/analysis/handler.py", "src/agents/referee/synthesis_data.py")
# Builders the steps below do not run, and why none of them can see a query's tags.
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


def _builders() -> set[str]:
    found: set[str] = set()
    for path in sorted((REPO / "src").rglob("*.py")):
        for match in _BUILDER_RE.finditer(path.read_text(encoding="utf-8")):
            found.add(f"{path.relative_to(REPO)}::{match.group(1)}")
    return found


class _ModelStandIn:
    """``strands.Agent``: records each system prompt and prompt, answers nothing."""

    prompts: list[str] = []

    def __init__(self, *args, **kwargs) -> None:
        type(self).prompts.append(str(kwargs.get("system_prompt") or ""))

    def __call__(self, prompt, *args, **kwargs):
        type(self).prompts.append(str(prompt))
        return SimpleNamespace(structured_output=None, message={"content": [{"text": "{}"}]})


def _tagged_job(tmp: Path) -> tuple[LocalArtifactStore, str, str]:
    import scripts.run_assessment as run_assessment

    with zipfile.ZipFile(REPO / "docs" / "examples" / SAMPLE / f"{SAMPLE}.zip") as z:
        member = next(n for n in z.namelist() if n.endswith("-collection.json"))
        raw = json.loads(extract_json_object(z.read(member).decode("utf-8")))
    tmp.mkdir()
    collection = tmp / f"{SAMPLE}-collection.json"
    collection.write_text(json.dumps(raw), encoding="utf-8")
    store = LocalArtifactStore(base_dir=str(tmp / "artifacts"))
    job_id, db = run_assessment.phase_collect(str(collection), SAMPLE, store)
    key = f"{db}/{job_id}/collector/output.json"
    collector = store.read_json(key)
    for q in collector["queries"]["query_patterns"]:
        q["query_tags"] = [f"controller='{MARK}'"]
        q["query_tags_dropped"] = 1
    store.write_json(key, collector)
    return store, job_id, db


def _quietly(step, *args, **kwargs) -> None:
    """Run ``step``; a step that fails on the stand-in's empty answers still built its input."""
    try:
        step(*args, **kwargs)
    except (Exception, SystemExit):  # noqa: BLE001 -- the inputs are what is checked
        pass


@pytest.fixture(scope="module")
def runs(tmp_path_factory: pytest.TempPathFactory) -> dict:
    import scripts.run_analysis as run_analysis
    import scripts.run_assessment as run_assessment
    import scripts.run_schema_design as run_schema_design
    from src.agents.analysis.handler import run_analysis as analysis_handler
    from src.agents.schema_design import handler as schema_handler
    from src.agents.schema_design.revision_handler import _dispatch_llm_redesign
    from src.contracts.schema_revision_models import SchemaRevisionRequest

    tmp = tmp_path_factory.mktemp("tags-reach-no-model")
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
    names = {b.split("::")[1] for b in _builders()}
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
        ext_store, job, db = _tagged_job(tmp / "external")
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
        bed_store, bjob, bdb = _tagged_job(tmp / "bedrock")
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
            base_version=1, pattern_modifications=[], table_modifications=[], new_patterns=[]
        )
        _quietly(_dispatch_llm_redesign, bjob, bdb, "dynamodb", {}, request, bed_store)
        _quietly(run_assessment.phase_synthesis, bed_store, bjob, bdb, "bedrock")
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
        "handed": handed,
        "bedrock_requests": bedrock_requests,
        "collector_reads": [(str(Path(f).resolve()), n) for f, n in collector_reads],
        "called": {(str(Path(f).resolve()), n) for f, n in called},
    }


# The fixture's own check: the marker was planted.
@pytest.mark.passes_on_main
def test_the_marker_is_in_the_collector_output(runs: dict) -> None:
    hits = [name for name, text in runs["files"].items() if MARK in text]
    assert any(name.endswith("collector/output.json") for name in hits)


def test_no_request_file_carries_a_tag(runs: dict) -> None:
    requests = [n for n in runs["files"] if "llm_input" in n or "llm_requests/" in n]
    assert len(requests) >= 5, requests  # analysis x2, reality check, schema design, synthesis
    # The collector output (and the upload it came from) keep the tags for the caller.
    leaked = [
        name
        for name, text in runs["files"].items()
        if MARK in text and not name.endswith(("collector/output.json", "collector-output.json"))
    ]
    assert not leaked


def test_no_prompt_bedrock_request_or_designer_input_carries_a_tag(runs: dict) -> None:
    assert len(runs["prompts"]) >= 20 and runs["handed"]
    assert runs["bedrock_requests"]  # the reality check's boto3 validator ran, refused
    assert not [p[:200] for p in runs["prompts"] if MARK in p]
    assert not [r[:200] for r in runs["bedrock_requests"] if MARK in r]
    assert not [h[:200] for h in runs["handed"] if MARK in h]


def test_model_facing_modules_read_the_collector_only_through_the_choke_point(runs: dict) -> None:
    from fnmatch import fnmatchcase

    choke_point = str((REPO / "src/storage/collector_for_models.py").resolve())
    reads = runs["collector_reads"]
    assert any(f == choke_point for f, _ in reads)
    offenders = []
    for file, func in reads:
        rel = Path(file).relative_to(REPO) if file.startswith(str(REPO)) else None
        if rel is None or file == choke_point:
            continue
        if any(fnmatchcase(str(rel), p) for p in (*MODEL_FACING, *FEEDERS)):
            offenders.append(f"{rel}::{func}")
    assert not offenders, f"read the collector output with read_collector_for_models: {offenders}"


# Guards that the run covers every builder, as on main.
@pytest.mark.passes_on_main
def test_every_builder_runs_or_says_why_it_cannot_see_tags(runs: dict) -> None:
    builders = _builders()
    assert len(builders) >= 15, builders  # the pattern still finds them
    called = runs["called"]
    not_run, ran_anyway = [], []
    for builder in sorted(builders):
        path, name = builder.split("::")
        ran = (str((REPO / path).resolve()), name) in called
        if builder in NOT_RUN:
            if ran:
                ran_anyway.append(builder)
        elif not ran:
            not_run.append(builder)
    assert not ran_anyway, f"NOT_RUN says these do not run, but they did; cover them: {ran_anyway}"
    assert not not_run, (
        "these model-input builders did not run with a tagged job: run them here or "
        f"say in NOT_RUN why they cannot see a query's tags: {not_run}"
    )
    assert not sorted(set(NOT_RUN) - builders), "NOT_RUN names a builder that no longer exists"


# Guards NOT_RUN's no-caller reasons, which hold on main too.
@pytest.mark.passes_on_main
def test_the_unused_builders_have_no_caller() -> None:
    sources = [p for root in ("src", "scripts") for p in sorted((REPO / root).rglob("*.py"))]
    for name in ("prepare_aurora_pg_llm_input", "prepare_aurora_mysql_llm_input"):
        callers = [
            str(p.relative_to(REPO))
            for p in sources
            if re.search(rf"\b{name}\(", p.read_text(encoding="utf-8"))
            and not re.search(rf"^def {name}\(", p.read_text(encoding="utf-8"), re.MULTILINE)
        ]
        assert not callers, f"{name} has callers now; run it in this test: {callers}"
