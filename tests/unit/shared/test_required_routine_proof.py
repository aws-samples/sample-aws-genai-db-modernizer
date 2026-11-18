# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""What every deliverable says about the routine test (#2411, the maintainer decision of
2026-11-17 on #1925): before a test on the job's routines file, the routines are checked,
not tested yet, and must be tested on a test Aurora before they are deployed; after one, the
line gives its counts and never more than no difference on the calls made. One sentence,
built in ``src/shared/routine_facts.py``, read by the engineering report, the executive
report, the ``/modernize`` summary and the UI."""

from __future__ import annotations

import json
from pathlib import Path

from src.agents.collector.routine_sources import routine_sources_digest, write_routine_sources
from src.contracts.routine_migration import RoutineMigrationContract
from src.report.printed_summary import render_final_summary
from src.report.renderers import render_engineering_report_md
from src.shared.report_facts import compute_report_facts
from src.shared.routine_facts import CUTOVER_SENTENCE, read_routine_facts
from src.storage.local_store import LocalArtifactStore
from src.tools.routines.migration import routine_migration_path
from tests.e2e.deliverable_figures import md_plain
from tests.executive_pdf import executive_text
from tests.unit.report.test_wave_readiness_deliverables import _report as _plain_report
from tests.unit.shared.test_routine_facts import _artifact, _row
from tests.unit.tools.data_movement.routine_fixtures import pg_source, with_test_result

REPO = Path(__file__).resolve().parents[3]
NOT_TESTED = (
    "The 2 carried routines are checked, not tested yet; test them on a test Aurora with "
    "/test-routines before they are deployed."
)
ARTIFACT = _artifact(_row("public.a()"), _row("public.b()"), source="mysql", target="aurora_mysql")


def _store(tmp_path: Path, artifact: RoutineMigrationContract | None = None) -> LocalArtifactStore:
    store = LocalArtifactStore(base_dir=str(tmp_path))
    write_routine_sources(store, "shop", "j1", {"metadata": {"source_database": {"engine": "mysql"}}},
                          [pg_source("a", "CREATE PROCEDURE a() SELECT 1", kind="procedure")])  # fmt: skip
    data = (artifact or ARTIFACT).model_dump(mode="json")
    store.write_json(routine_migration_path("shop", "j1", 1), data)
    return store


def _tested(store, outcomes=None, digest: str | None = "current") -> None:
    found = routine_sources_digest(store, "shop", "j1") if digest == "current" else digest
    done = with_test_result(ARTIFACT, found, outcomes)
    store.write_json(routine_migration_path("shop", "j1", 1), done.model_dump(mode="json"))


def _deliverables(store) -> dict[str, str]:
    """The routines' words in each Python deliverable, as the customer reads them."""
    report = _plain_report("MEDIUM")
    routines = read_routine_facts(store, "shop", "j1", 1, report["migration_waves"])
    report["facts"] = compute_report_facts(report, routines=routines)
    summary = render_final_summary(report, database_name="shop", job_id="j1",
                                   deliverables_dir="d/", deliverables=[])  # fmt: skip
    return {
        "facts": str((routines or {}).get("line")),
        "engineering report": " ".join(md_plain(render_engineering_report_md(report)).split()),
        "executive report": " ".join(executive_text(report).split()),
        "/modernize summary": " ".join(md_plain(summary).split()),
    }


#: The executive report's short form of the same state (#2411, the maintainer's direction).
EXECUTIVE_NOT_TESTED = (
    "Before they are deployed, test them on a throwaway Aurora with the /test-routines command."
)


def test_no_test_says_checked_not_tested_yet_in_every_deliverable(tmp_path) -> None:
    for where, text in _deliverables(_store(tmp_path)).items():
        expected = EXECUTIVE_NOT_TESTED if where == "executive report" else NOT_TESTED
        assert expected in text, where
        assert "showed no difference" not in text, where


def test_the_ui_reads_the_same_sentence() -> None:
    """RoutineMigrationSection renders ``facts.routines.line`` as it is; its fixture is the
    Python facts (``test_routine_fact_words.py``), so it carries the sentence too."""
    fixture = REPO / "src/ui/src/components/__tests__/fixtures/routineFacts.json"
    line = json.loads(fixture.read_text(encoding="utf-8"))["line"]
    assert line.endswith(
        "The 3 carried routines are not tested yet; test them on a test Aurora with "
        "/test-routines before they are deployed."
    )


def test_after_a_test_the_line_gives_its_counts_and_never_more(tmp_path) -> None:
    store = _store(tmp_path)
    _tested(store, {"public.b()": "needs_attention"})
    for where, text in _deliverables(store).items():
        expected = (
            "Tested on a throwaway Aurora: step 1 deploys 1; 1 is held back."
            if where == "executive report"
            else "On a test Aurora, of 2 carried routines: 1 showed no difference on the calls "
            "made; 1 needs attention. Step 1 deploys only that one; the other one is held back."
        )
        assert expected in text, where
        assert "not tested yet" not in text and "/test-routines command" not in text, where
    line = _deliverables(store)["facts"]
    for claim in ("proved", "works", "correct", "identical", "same as", "every input"):
        assert claim not in line.lower(), claim


def test_a_test_with_no_difference_on_any_routine_holds_none_back(tmp_path) -> None:
    store = _store(tmp_path)
    _tested(store)
    line = _deliverables(store)["facts"]
    assert line.endswith(
        "On a test Aurora, of 2 carried routines: 2 showed no difference on the calls made."
    )


def test_a_stale_or_undated_test_reads_as_not_tested(tmp_path) -> None:
    store = _store(tmp_path)
    for digest in ("e" * 64, None):  # another routines file; a test before #2411 named none
        _tested(store, digest=digest)
        facts = read_routine_facts(store, "shop", "j1", 1)
        assert facts is not None and facts["proof"] == "checked, not tested yet"
        assert facts["proof_counts"] is None and NOT_TESTED in facts["line"]


def test_an_old_artifact_with_no_proof_fields_still_loads_as_not_tested(tmp_path) -> None:
    store = _store(tmp_path)
    data = ARTIFACT.model_dump(mode="json")
    data.pop("proof_run")
    for r in data["routines"]:
        r.pop("proof")
    store.write_json(routine_migration_path("shop", "j1", 1), data)
    facts = read_routine_facts(store, "shop", "j1", 1)
    assert facts is not None and NOT_TESTED in facts["line"]


def test_the_sentence_is_built_once_and_gives_no_cutover_advice() -> None:
    hits = [
        str(p.relative_to(REPO))
        for p in (REPO / "src").rglob("*.py")
        if "on a test Aurora with" in p.read_text(encoding="utf-8")
    ]
    assert hits == ["src/shared/routine_facts.py"]
    assert CUTOVER_SENTENCE == "the cutover is your responsibility"
    assert "cutover" not in NOT_TESTED.lower()


def test_a_test_with_no_digest_on_a_job_with_no_routines_file_is_not_current() -> None:
    """A test recorded before #2411 names no routines file, and a job with no routines index
    has no digest: None never matches None."""
    from src.shared.routine_facts import proof_is_current

    assert proof_is_current({"routine_sources_sha256": None}, None) is False
    assert proof_is_current({"routine_sources_sha256": "a" * 64}, None) is False
    assert proof_is_current({"routine_sources_sha256": "a" * 64}, "a" * 64) is True


def test_a_routine_that_calls_a_held_one_is_counted_as_held_in_the_line() -> None:
    from src.shared.routine_facts import proof_run_sentence

    assert proof_run_sentence({"proved": 3, "needs_attention": 1}, 1).endswith(
        "Step 1 deploys only 2 of them; the other 2 are held back, 1 because a routine it "
        "calls is held back."
    )
