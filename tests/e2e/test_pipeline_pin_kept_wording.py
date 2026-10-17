"""The deliverables describe a consolidation the reality check kept partial (#861).

A consolidation whose source engine stays in the reality check's own result
-- a partial consolidation, or an engine a customer pin holds (#852) -- still
applies: every deliverable shows it, and none says "an assignment edit moved
queries back" (that is only true when an edit after the check put queries
back on an engine the check emptied, #546).

Two customer-pin flows on copies of the deterministic jobs, each ending with
the reality check, synthesis and the reports:

- ``before_check``: two queries per first-assignment engine pinned on that
  engine before the reality check runs (the #852 review's case).
- ``after_check``: one query of the engine the reality check removed pinned
  back on it, then the check runs again on the customer's version (#860's
  case).

Both run on every bundled sample (wordpress, discourse, trailgear).

Which flow produces a partial consolidation depends on how the reality check
treats pinned engines, so the checks hold for every consolidation either flow
produces, and at least one flow on some sample must produce a partial one.
"""

from __future__ import annotations

import json
import shutil
import subprocess  # nosec B404 -- runs this repo's own scripts/ entry points with fixed argv
import sys
from collections import defaultdict
from pathlib import Path

import pytest

from tests.e2e.pipeline import REPO, PipelineResult, _env

# Every flow copies the deterministic sample jobs, so an external (e2e-llm) job has none to copy.
pytestmark = pytest.mark.deterministic

JOB = "pinwording"


def _script(*args: str) -> dict:
    proc = subprocess.run(  # nosec B603 -- fixed interpreter plus this repo's own script args
        [sys.executable, *args], cwd=REPO, capture_output=True, text=True, env=_env(), timeout=600
    )
    assert proc.returncode == 0, f"{args[0]} failed:\n{proc.stderr[-3000:]}"
    payload: dict = json.loads(proc.stdout.strip().splitlines()[-1])
    return payload


def _json(path: Path) -> dict:
    data: dict = json.loads(path.read_text())
    return data


def _pdf_text(paths: list[Path]) -> str:
    from tests.e2e.deliverable_figures import pdf_page_texts

    pdf = next(p for p in paths if p.name == "executive-report.pdf")
    return " ".join(pdf_page_texts(str(pdf)))


def _copy(run: PipelineResult, root: Path) -> Path:
    job = root / run.db / JOB
    shutil.copytree(run.job_dir(), job)
    return job


def _finish(run: PipelineResult, root: Path, rc_args: list[str]) -> dict:
    common = ["--job-id", JOB, "--db", run.db, "--artifact-root", str(root)]
    _script("scripts/run_reality_check.py", *common, "--llm-mode", "none", *rc_args)
    _script("scripts/run_synthesis.py", *common, "--llm-mode", "none")
    step = _script("scripts/run_report.py", *common)
    job = root / run.db / JOB
    return {
        "rc": _json(job / "reality-check" / "output.json"),
        "report": _json(root / step["report"]),
        "paths": [Path(p) for p in step["deliverables"]],
        "root": root,
        "db": run.db,
    }


def _before_check(run: PipelineResult, root: Path) -> dict:
    from src.agents.referee.assignment_overrides import (
        QueryOverrideInput,
        apply_assignment_overrides,
    )
    from src.storage.local_store import LocalArtifactStore

    job = _copy(run, root)
    # Back to the first assignment, as if the customer edited it before the check.
    for version in (job / "assignment").glob("v*"):
        if version.name != "v1":
            shutil.rmtree(version)
    for later in ("reality-check", "synthesis", "decisions"):
        shutil.rmtree(job / later, ignore_errors=True)
    by_engine: dict[str, list[str]] = defaultdict(list)
    for qa in _json(job / "assignment" / "v1" / "assignment.json")["query_assignments"]:
        by_engine[qa["assigned_engine"]].append(str(qa["query_id"]))
    picks = [
        QueryOverrideInput(query_id=q, assigned_engine=e)
        for e, qids in sorted(by_engine.items())
        for q in sorted(qids)[:2]
    ]
    apply_assignment_overrides(LocalArtifactStore(base_dir=str(root)), run.db, JOB, picks)
    return _finish(run, root, [])


def _after_check(run: PipelineResult, root: Path) -> dict:
    from src.agents.referee.assignment_overrides import (
        QueryOverrideInput,
        apply_assignment_overrides,
    )
    from src.storage.local_store import LocalArtifactStore

    job = _copy(run, root)
    first = _json(job / "assignment" / "v1" / "assignment.json")["query_assignments"]
    removed = sorted(_json(job / "reality-check" / "output.json")["eliminated_engines"])
    assert removed, "the reality check removes an engine on this sample"
    qids = sorted(str(qa["query_id"]) for qa in first if qa["assigned_engine"] == removed[0])
    result = apply_assignment_overrides(
        LocalArtifactStore(base_dir=str(root)),
        run.db,
        JOB,
        [QueryOverrideInput(query_id=qids[-1], assigned_engine=removed[0])],
    )
    return _finish(run, root, ["--assignment-version", str(result.assignment.version)])


SAMPLES = ["wordpress", "discourse", "trailgear"]
_FLOWS: dict[str, dict[str, dict]] = {}
_PLAIN: dict[str, dict] = {}


def _sample_run(e2e_root: Path, sample: str) -> PipelineResult:
    from tests.e2e.conftest import _external_env, _run_pipeline_cached

    if _external_env() is not None:
        pytest.skip("needs the deterministic sample jobs")
    return _run_pipeline_cached(sample, e2e_root / "artifacts", job_id=f"e2e-{sample[:4]}")


@pytest.fixture(scope="module")
def flows_of(e2e_root: Path, tmp_path_factory: pytest.TempPathFactory):  # type: ignore[no-untyped-def]
    """``flows_of(sample)``: the two pin flows on a copy of that sample's job, built once."""

    def build(sample: str) -> dict[str, dict]:
        if sample not in _FLOWS:
            run = _sample_run(e2e_root, sample)
            _FLOWS[sample] = {
                "before_check": _before_check(run, tmp_path_factory.mktemp(f"before-{sample}")),
                "after_check": _after_check(run, tmp_path_factory.mktemp(f"after-{sample}")),
            }
        return _FLOWS[sample]

    return build


@pytest.fixture(scope="module")
def plain_of(e2e_root: Path, tmp_path_factory: pytest.TempPathFactory):  # type: ignore[no-untyped-def]
    """``plain_of(sample)``: that sample's job with no pin, synthesized on a copy."""

    def build(sample: str) -> dict:
        if sample not in _PLAIN:
            run = _sample_run(e2e_root, sample)
            root = Path(tmp_path_factory.mktemp(f"plain-{sample}"))
            shutil.copytree(run.job_dir(), root / run.db / JOB)
            common = ["--job-id", JOB, "--db", run.db, "--artifact-root", str(root)]
            _script("scripts/run_synthesis.py", *common, "--llm-mode", "none")
            step = _script("scripts/run_report.py", *common)
            _PLAIN[sample] = {"report": _json(root / step["report"]), "root": root, "db": run.db}
        return _PLAIN[sample]

    return build


def _kept_by_check(rc: dict) -> set[str]:
    return {e for e, n in (rc.get("after_distribution") or {}).items() if n}


@pytest.mark.parametrize("sample", SAMPLES)
@pytest.mark.parametrize("name", ["before_check", "after_check"])
def test_a_kept_consolidation_is_shown_and_never_called_reversed(
    flows_of, sample: str, name: str  # type: ignore[no-untyped-def]
) -> None:
    flow = flows_of(sample)[name]
    rc, facts = flow["rc"], flow["report"]["facts"]
    kept = _kept_by_check(rc)
    for t in facts["reality_check_trade_offs"]:
        if t["from_engine"] in kept:
            assert not t["reversed"], t
    shown = {d["index"] for d in facts["reality_check_decisions"]}
    for t in facts["reality_check_trade_offs"]:
        if t["from_engine"] in kept:
            assert t["index"] in shown, t

    md = next(p for p in flow["paths"] if p.name == "engineering-report.md")
    if not any(t["reversed"] for t in facts["reality_check_trade_offs"]):
        assert "moved queries back" not in md.read_text()
    pdf = _pdf_text(flow["paths"])
    for d in facts["reality_check_decisions"]:
        # The PDF's fonts lack the arrow, so it reads "to".
        assert " ".join(d["title"].replace(" → ", " to ").split()) in pdf, d


def test_at_least_one_flow_kept_a_consolidation_partial(flows_of) -> None:  # type: ignore[no-untyped-def]
    partial = {
        (sample, name): [
            c
            for c in flow["rc"]["consolidations"]
            if c["from_engine"] in _kept_by_check(flow["rc"])
        ]
        for sample in SAMPLES
        for name, flow in flows_of(sample).items()
    }
    assert any(partial.values()), "no pin flow produced a partial consolidation"


# ---------------------------------------------------------------------------
# An edit after the check that undoes a consolidation, wholly or in part
# (#891 review): per query, a line is reversed when every query it moved is
# back, and restated for what stayed moved when only some are.
# ---------------------------------------------------------------------------


def _undo(flow: dict, tmp: Path, pick: str) -> dict | None:
    """On a copy of ``flow``'s job, move queries of its largest consolidation
    back to the engine they left (``pick``: "all" or "one"), then synthesis
    and the reports, without running the reality check again."""
    from src.agents.referee.assignment_overrides import (
        QueryOverrideInput,
        apply_assignment_overrides,
    )
    from src.storage.local_store import LocalArtifactStore

    lines = [
        c for c in flow["report"]["reality_check"]["consolidations"] if c.get("moved_query_ids")
    ]
    lines = [c for c in lines if pick == "all" or len(c["moved_query_ids"]) >= 2]
    if not lines:
        return None
    line = max(lines, key=lambda c: (len(c["moved_query_ids"]), c["to_engine"]))
    ids = line["moved_query_ids"] if pick == "all" else line["moved_query_ids"][:1]
    root = tmp / "artifacts"
    shutil.copytree(flow["root"] / flow["db"] / JOB, root / flow["db"] / JOB)
    apply_assignment_overrides(
        LocalArtifactStore(base_dir=str(root)),
        flow["db"],
        JOB,
        [QueryOverrideInput(query_id=q, assigned_engine=line["from_engine"]) for q in ids],
    )
    common = ["--job-id", JOB, "--db", flow["db"], "--artifact-root", str(root)]
    _script("scripts/run_synthesis.py", *common, "--llm-mode", "none")
    step = _script("scripts/run_report.py", *common)
    report = _json(root / step["report"])
    return {
        "line": line,
        "moved_back": len(ids),
        "report": report,
        "paths": [Path(p) for p in step["deliverables"]],
    }


def _row(report: dict, line: dict) -> dict:
    rows = report["facts"]["reality_check_trade_offs"]
    return next(
        t
        for t in rows
        if (t["from_engine"], t["to_engine"]) == (line["from_engine"], line["to_engine"])
    )


def _undone_flows(plain: dict, flows: dict[str, dict], tmp: Path, pick: str) -> dict[str, dict]:
    """``_undo`` on every flow where it applies. The plain job always has a
    consolidation to undo; the pin flows do or not, depending on how the
    reality check treats pinned engines (#860)."""
    out: dict[str, dict] = {}
    for name, flow in {"plain": plain, **flows}.items():
        done = _undo(flow, tmp / name, pick)
        if done is not None:
            out[name] = done
    assert "plain" in out, "the plain check moves queries on every sample"
    return out


@pytest.mark.parametrize("sample", SAMPLES)
def test_an_edit_that_undoes_every_move_of_a_line_reverses_it(
    plain_of, flows_of, sample: str, tmp_path: Path  # type: ignore[no-untyped-def]
) -> None:
    undone_flows = _undone_flows(plain_of(sample), flows_of(sample), tmp_path, "all")
    for name, undone in undone_flows.items():
        row = _row(undone["report"], undone["line"])
        assert row["reversed"], (name, row)
        assert row["queries_still_moved"] == 0, name
        shown = {d["index"] for d in undone["report"]["facts"]["reality_check_decisions"]}
        assert row["index"] not in shown, name
        md = next(p for p in undone["paths"] if p.name == "engineering-report.md")
        assert "### Reversed by a later assignment edit" in md.read_text(), name


@pytest.mark.parametrize("sample", SAMPLES)
def test_an_edit_that_undoes_part_of_a_line_states_what_stayed_moved(
    plain_of, flows_of, sample: str, tmp_path: Path  # type: ignore[no-untyped-def]
) -> None:
    undone_flows = _undone_flows(plain_of(sample), flows_of(sample), tmp_path, "one")
    for name, undone in undone_flows.items():
        row = _row(undone["report"], undone["line"])
        moved = len(undone["line"]["moved_query_ids"])
        assert not row["reversed"], name
        assert (row["queries_moved_back"], row["queries_still_moved"]) == (1, moved - 1), name
        now = row["description_now"]
        assert now and "a later assignment edit moved 1 back to" in now, name
        title = now.split(":", 1)[0]
        md = next(p for p in undone["paths"] if p.name == "engineering-report.md")
        assert title in md.read_text(), name
        assert " ".join(title.replace(" → ", " to ").split()) in _pdf_text(undone["paths"]), name
