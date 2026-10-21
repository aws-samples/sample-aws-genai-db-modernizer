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
text still reaches prompts inside it; this test is about the tag fields, and
``test_collector_text_is_framed.py`` shows that text is framed as data (#1317). The run
itself is ``model_inputs_harness.run_every_model_input``, shared by both tests.

A name-based list (every ``prepare_*``/``build_*``/``render_*`` request or llm-input
builder, every ``*_prompt`` function, ``headless_text``, the designers' PE-review and
the validators' call sites) checks the run's coverage: each one must run, or be in
``NOT_RUN`` with a reason and then really not run.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

from tests.unit.agents.model_inputs_harness import (
    NOT_RUN,
    REPO,
    coverage_problems,
    run_every_model_input,
)

MARK = "Mark1180QueryTagSentinel"

# Modules whose direct reads of the collector output must be the choke point's.
sys.path.insert(0, str(REPO / "ci"))
from needs_llm_run import MODEL_FACING  # noqa: E402

FEEDERS = ("src/agents/analysis/handler.py", "src/agents/referee/synthesis_data.py")


def _tag_every_query(collector: dict) -> None:
    for q in collector["queries"]["query_patterns"]:
        q["query_tags"] = [f"controller='{MARK}'"]
        q["query_tags_dropped"] = 1


@pytest.fixture(scope="module")
def runs(tmp_path_factory: pytest.TempPathFactory) -> dict:
    tmp = tmp_path_factory.mktemp("tags-reach-no-model")
    return run_every_model_input(tmp, edit_collector=_tag_every_query)


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
    assert not coverage_problems(runs["called"])
    assert NOT_RUN  # the harness's reasons, shared with test_collector_text_is_framed.py


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
