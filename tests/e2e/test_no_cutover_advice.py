# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The samples' deliverables give no cutover advice (#2107), on the real pipeline output.

The words are the unit guard's (``tests/unit/docs/test_no_cutover_advice.py``, deliverables mode):
no "caught up", "in sync", "proven" or "ready" as a readiness claim, no cutover timing
("before", "after", "once", "during", "prior to" cutover; the rule-risk mitigations
included), and the wave plan of the engineering and executive reports states "The cutover
is your responsibility." under its opening sentence.

Both runs: the deterministic job, and on an e2e-llm run the headless job, whose executive
summary, design text and reviews a model wrote. The skills that write customer prose carry
the same rule (``test_each_customer_prose_skill_carries_the_cutover_rule``), so the whole
report is scanned, the model's sections with the deterministic ones.
"""

from __future__ import annotations

import pytest

from src.shared.cutover_message import CUTOVER_MESSAGE as SENTENCE
from tests.e2e.pipeline import PipelineResult
from tests.executive_pdf import body_text, normalize
from tests.unit.docs.test_no_cutover_advice import _findings

# The engineering report's sections that hold model-written design rationale and review
# text on an e2e-llm run (the schema designers' notes, the PE reviews, the trade-offs).
# There only the cutover timing and readiness claims count (``model_text``); the rest of the
# report is checked strictly (the discourse e2e-llm run of #2111 flagged a designer's
# "(user_id, name) is not proven unique").
MODEL_TEXT_SECTIONS = ("## Target schemas by engine", "## Migration trade-offs")


def _split(md: str) -> tuple[str, str]:
    """(the report with the model-text sections blanked, those sections with the rest
    blanked): line numbers stay the report's own."""
    ours: list[str] = []
    theirs: list[str] = []
    inside = False
    for line in md.split("\n"):
        if line.startswith("## "):
            inside = line.startswith(MODEL_TEXT_SECTIONS)
        (theirs if inside else ours).append(line)
        (ours if inside else theirs).append("")
    return "\n".join(ours), "\n".join(theirs)


def _summary(md: str) -> str:
    """The engineering report's executive summary: a model's text on an e2e-llm run."""
    return md.split("## Executive summary", 1)[1].split("\n## ", 1)[0]


# replay_stale: the replay's saved report.json was synthesized before #2107, so its stored
# wave gates and mitigations still carry the old cutover wording; a fresh e2e-llm run writes
# them anew and is checked.
@pytest.mark.replay_stale
def test_the_engineering_report_gives_no_cutover_advice(run: PipelineResult) -> None:
    """``replay_stale``: the saved replay report predates #2107's gates and mitigations."""
    md = run.deliverable("engineering-report.md").read_text()
    ours, model = _split(md)
    name = f"{run.db} engineering-report.md"
    assert _findings(name, ours, deliverable=True) == []
    assert _findings(f"{name} (design text)", model, model_text=True) == []
    roadmap = md.split("## Migration roadmap", 1)[1].split("\n## ", 1)[0]
    assert roadmap.count(f"*{SENTENCE}*") == 1, run.db


# replay_stale: the replay's saved summary was written before #2167 from a deterministic
# risk that said "in sync", and the model paraphrased it; a fresh e2e-llm run is checked.
@pytest.mark.replay_stale
def test_the_executive_summary_gives_no_cutover_advice(run: PipelineResult) -> None:
    """Named on its own so an e2e-llm failure points at the model's text. ``replay_stale``:
    the saved replay summary paraphrases the pre-#2167 "in sync" risk."""
    md = run.deliverable("engineering-report.md").read_text()
    summary = _summary(md)
    assert summary.strip(), run.db
    assert _findings(f"{run.db} executive summary", summary, deliverable=True) == []


@pytest.mark.replay_stale
def test_the_executive_report_gives_no_cutover_advice(run: PipelineResult) -> None:
    """``replay_stale``: the saved replay report predates #2107's gates and mitigations."""
    text = normalize(body_text(run.deliverable("executive-report.pdf"), run.db))
    assert _findings(f"{run.db} executive-report.pdf", text, deliverable=True) == []
    assert SENTENCE in text, run.db
