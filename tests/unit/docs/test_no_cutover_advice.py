# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Step 1 never gives cutover advice and never claims more than it shows (#2062).

Step 1 shows that the full load is complete and that replication is in progress. Where the
cutover comes up, the text says only "The cutover is your responsibility." This guard scans
the shipped step 1 text: the data-movement README, ``/move``, the help page's step 1
section, the scripts' and the data-movement tools' source (their messages, comments and
templates), and every file the generators write, rendered here for both engines.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from scripts import render_dms_step
from src.shared.cutover_message import CUTOVER_DISCLAIMER, CUTOVER_MESSAGE, cutover_message_text
from src.tools.data_movement.cutover_scripts import CUTOVER_FILE, CUTOVER_INSTRUCTIONS
from src.tools.data_movement.dms_gate import REPLICATING_MESSAGE
from src.tools.data_movement.routine_scripts import DEPLOY_FILE, INSTRUCTIONS_FILE
from src.tools.data_movement.verify_scripts import write_verify_scripts

REPO = Path(__file__).resolve().parents[3]

# Words step 1 never uses, and phrases that tell the customer when or how to cut over.
BANNED = {
    "caught up": r"caught[ _-]?up",
    "in sync": r"\bin\s+sync\b",  # RDS's own apply status `in-sync` is a status value, not a claim
    "proven": r"\bproven\b",
    "ready": r"\bready\b",  # every use, unless READY_FORMS or a pinned line allows it
    "stop writes": r"\b(?:stop|stops|stopped|stopping|pause|freeze|freezing|froze)\s+"
    r"(?:(?:the|your|all|its|their)\s+)?(?:application'?s?'?\s+)?writ(?:es|ing)\b",
    "freeze writes": r"\bwrite[ -]freeze\b",
    "safe to cut over": r"\bsafe\s+to\s+cut\s*over\b",
    "applied the last change": r"\bapplied\s+the\s+last\s+change\b",
    "cutover timing": r"\b(?:at|after|before|until|once)\s+(?:the\s+|your\s+|their\s+)?"
    r"cut\s*over\b(?!-)",
}
# "ready" is allowed anywhere only in these exact forms, never as a readiness claim; every
# other use is pinned to its line in ALLOWED below.
READY_FORMS = (
    r"--until ready\b",  # /move's --until flag
    r"\breason=until ready\b",  # its MOVE_RESULT line
    r"\breason=--until must be ready or moving\b",  # the result line for a wrong value
    r"\bnot a production-ready configuration\b",  # the disclaimer
    r"\bnever started \(ready, not running\)",  # DMS's task status in words (dms_reports)
)
# Any other match a pattern may make: (file, a substring of the exact line), pinned, so a new
# line with the same word fails. Each must still be in its file, so a stale entry fails.
ALLOWED = {
    # The earlier --until value and phase, kept for back-compat (#2062).
    ("scripts/run_dms_step.py", 'LEGACY_PHASES = {"caught_up": REPLICATING}'),
    (
        "scripts/run_dms_step.py",
        "# status lines, and the earlier --until value, named it caught_up.",
    ),
    ("scripts/run_dms_step.py", "``caught_up`` is still accepted, and ``current_phase`` maps"),
    ("scripts/run_dms_step.py", "``--until`` value, says ``caught_up`` for ``replicating``"),
    # The "ready" status value: DMS's task status, and the scripts' "status": "ready" lines.
    ("scripts/run_dms_step.py", 'TASK_IDLE = TASK_STOPPED | {"ready"}'),
    ("scripts/run_dms_step.py", '"ready": "never started (ready, not running)"'),
    ("scripts/run_dms_step.py", 'return {"status": "ready", "stack_name": name'),
    ("scripts/run_dms_step.py", 'if task.get("Status") == "ready":'),
    ("scripts/run_dms_step.py", 'if current.get("task_status") != "ready":'),
    ("scripts/run_dms_step.py", 'pointer = {"ready": "a task that never ran: run start"'),
    ("scripts/run_dms_step.py", 'return {"status": "ready", "tables": made.tables'),
    ("scripts/run_dms_step.py", 'return {"status": "ready", **base'),
    ("scripts/run_dms_step.py", 'if scripts.status == "ready":'),
    ("src/tools/data_movement/routine_scripts.py", 'status: str  # "ready" or "nothing_to_deploy"'),
    ("src/tools/data_movement/routine_scripts.py", '        status="ready",'),
    ("src/tools/data_movement/cutover_scripts.py", 'status: str  # "ready" or "nothing_to_do"'),
    ("src/tools/data_movement/cutover_scripts.py", '            status="ready",'),
    # The disclaimer, split over two string literals in its source.
    (
        "src/tools/data_movement/dms_step.py",
        '"production-ready configuration: review every setting',
    ),
    # /move's --until flag value and the scripts' "status": "ready" lines, as move.md names them.
    (".claude/commands/move.md", "`ready`: once the preflight passes, before any AWS write"),
    (".claude/commands/move.md", "(Move or Prove for `ready`, Prove for `moving`)"),
    (".claude/commands/move.md", '`"status": "ready"`: show `carried`, `in_script`, `deferred`'),
    (".claude/commands/move.md", 'on `"status": "ready"`, show `expected`, `in_script`'),
    # The Debezium handoff of later waves (#1427), not step 1: its Readiness API.
    ("src/tools/data_movement/initial_load.py", "stay not ready (safe, but it stalls)"),
    ("src/tools/data_movement/initial_load.py", "    ready: bool"),
    (
        "src/tools/data_movement/initial_load.py",
        "connector has not passed the barrier it is not ready",
    ),
    ("src/tools/data_movement/initial_load.py", "return Readiness(ready=not reasons"),
}
DOCS = (
    "infrastructure/cloudformation/data-movement/README.md",
    ".claude/commands/move.md",
)
# The data-movement ADRs record design, and they discuss the cutover as an architecture
# event ("after the step 1 cutover"), so the cutover timing pattern does not apply to them;
# every banned word does.
ADRS = (
    "docs/architecture/decisions/ADR-033-data-movement-transformation-contract.md",
    "docs/architecture/decisions/ADR-034-carrying-database-routines-to-aurora.md",
)
# Out of scope here, owned elsewhere: the assessment report's wave gates and mitigations
# (src/agents/referee/, src/report/), the routine rule catalog (src/catalog/routine_checks/,
# rendered into docs/architecture/rule-catalog.md) and the samples (docs/examples/). They
# still say "caught up" and "before cutover"; #2107 (the follow-up to #2062) covers them.
SOURCES = (
    "scripts/run_dms_step.py",
    "scripts/check_dms_step_gate.py",
    "scripts/render_dms_step.py",
    "src/shared/cutover_message.py",
    "src/shared/row_counts.py",
    "src/catalog/dms_sizing.toml",
    *sorted(
        p.relative_to(REPO).as_posix() for p in (REPO / "src/tools/data_movement").glob("*.py")
    ),
)


def _allowed(name: str, label: str, line: str, start: int, end: int) -> bool:
    if any(path == name and pinned in line for path, pinned in ALLOWED):
        return True
    return label == "ready" and any(
        m.start() <= start and end <= m.end()
        for pattern in READY_FORMS
        for m in re.finditer(pattern, line)
    )


def _findings(name: str, text: str, skip: tuple[str, ...] = ()) -> list[str]:
    found = []
    for label, pattern in BANNED.items():
        if label in skip:
            continue
        for m in re.finditer(pattern, text, re.IGNORECASE):
            first = text.rfind("\n", 0, m.start()) + 1
            last = text.find("\n", m.end())
            line = text[first : None if last < 0 else last]
            if _allowed(name, label, line, m.start() - first, m.end() - first):
                continue
            number = text.count("\n", 0, m.start()) + 1
            found.append(f"{name}:{number}: {label}: {m.group(0)!r}")
    return found


def _help_page_step_1() -> str:
    page = (REPO / "docs/use-claude-code.md").read_text()
    return page.split("## Move step 1 (`/move`)", 1)[1].split("\n## ", 1)[0]


def _rendered(tmp_path: Path) -> dict[str, str]:
    """Every file the step 1 generators write, for both engines."""
    from tests.unit.tools.data_movement import test_cutover_scripts as cut
    from tests.unit.tools.data_movement import test_routine_scripts as rs
    from tests.unit.tools.data_movement.verify_fixtures import SHA, mysql_contract, pg_contract

    out: dict[str, str] = {}
    for example in render_dms_step.example_dirs():
        for name, text in render_dms_step.render_example(example).items():
            out[f"{example.name}/{name}"] = text
    pg_cut = cut._pg_case(tmp_path / "cut-pg", states={"public.orders.trg_b": "D"})
    mysql_cut = cut._mysql_case(tmp_path / "cut-mysql", events={"nightly": "ENABLE"})
    for engine, (store, contract) in (("pg", pg_cut), ("mysql", mysql_cut)):
        root = tmp_path / f"cut-{engine}"
        cut._cut(root, store, contract)
        for name in (CUTOVER_FILE, CUTOVER_INSTRUCTIONS):
            out[f"cutover-{engine}/{name}"] = cut._read(root, name)
    pg_rs = rs._pg_case(tmp_path / "routines-pg")
    mysql_rs = rs._mysql_case(tmp_path / "routines-mysql")
    for engine, (store, contract) in (("pg", pg_rs), ("mysql", mysql_rs)):
        root = tmp_path / f"routines-{engine}"
        rs._write(root, store, contract)
        for name in (DEPLOY_FILE, INSTRUCTIONS_FILE):
            out[f"routines-{engine}/{name}"] = (root / "step" / name).read_text()
    for engine, contract, source in (("pg", pg_contract(), "postgresql"),
                                     ("mysql", mysql_contract(), "mysql")):  # fmt: skip
        made = write_verify_scripts(
            contract, SHA, tmp_path / f"verify-{engine}", source_engine=source
        )
        for name, path in made.files.items():
            out[f"verify-{engine}/{name}"] = Path(path).read_text()
    return out


@pytest.mark.parametrize("name", DOCS)
def test_the_step_1_docs_give_no_cutover_advice(name):
    assert _findings(name, (REPO / name).read_text()) == []


@pytest.mark.parametrize("name", ADRS)
def test_the_data_movement_adrs_use_no_banned_word(name):
    assert _findings(name, (REPO / name).read_text(), skip=("cutover timing",)) == []


def test_the_help_pages_step_1_section_gives_no_cutover_advice():
    assert _findings("docs/use-claude-code.md (/move)", _help_page_step_1()) == []


@pytest.mark.parametrize("name", SOURCES)
def test_the_step_1_scripts_and_tools_give_no_cutover_advice(name):
    assert _findings(name, (REPO / name).read_text()) == []


def test_every_generated_step_1_file_gives_no_cutover_advice(tmp_path):
    rendered = _rendered(tmp_path)
    assert len(rendered) >= 16  # both examples, both cutover, routine and verify sets
    findings = [f for name, text in rendered.items() for f in _findings(name, text)]
    assert findings == []


def test_the_cutover_instructions_say_only_the_one_sentence_about_the_cutover(tmp_path):
    rendered = _rendered(tmp_path)
    for engine in ("pg", "mysql"):
        md = rendered[f"cutover-{engine}/{CUTOVER_INSTRUCTIONS}"]
        assert md.count(CUTOVER_MESSAGE) == 1
        assert "point your applications at Aurora" not in md


def test_each_cutover_facing_file_starts_with_the_disclaimer(tmp_path):
    """Maintainer decision of 2026-11-10 on #2062: the same sentence, before anything else."""
    assert CUTOVER_DISCLAIMER == (
        "The cutover is your responsibility. The modernizer does not run, schedule or advise on it."
    )
    rendered = _rendered(tmp_path)
    firsts = {
        name: text.splitlines()[0]
        for name, text in rendered.items()
        if name.rsplit("/", 1)[1] in (CUTOVER_INSTRUCTIONS, INSTRUCTIONS_FILE, "verify.md",
                                      CUTOVER_FILE, DEPLOY_FILE, "step-1.sequences.sql")
    }  # fmt: skip
    assert len(firsts) == 2 * 5 + 1  # both engines (sequences: Aurora PostgreSQL only)
    for name, first in firsts.items():
        expected = CUTOVER_DISCLAIMER if name.endswith(".md") else f"-- {CUTOVER_DISCLAIMER}"
        assert first == expected, name
    readme = (REPO / DOCS[0]).read_text()
    assert readme.split("### Cutover\n\n", 1)[1].startswith(CUTOVER_DISCLAIMER + "\n")


def test_the_fixed_messages_are_the_decided_wording():
    assert cutover_message_text() == "The cutover is your responsibility."
    assert REPLICATING_MESSAGE == "Full load complete. Replication is in progress."
    for text in (CUTOVER_MESSAGE, REPLICATING_MESSAGE):
        assert _findings("message", text) == []


def test_each_allowlisted_line_is_still_there():
    for path, pinned in ALLOWED:
        assert pinned in (REPO / path).read_text(), (path, pinned)


def test_a_new_line_with_an_allowlisted_word_still_fails():
    assert _findings("scripts/run_dms_step.py", 'message = "the task is caught_up"') != []
    assert _findings("scripts/run_dms_step.py", 'phase = "caught_up"') != []


@pytest.mark.parametrize(
    "advice",
    [
        "Stop your applications' writes to the source, then wait.",
        "Run it once the applications have stopped writing to the source.",
        "Once replication has caught up, decommission the source.",
        "The phase reads caught_up.",
        "This keeps your Aurora copy in sync with your source.",
        "The step is proven.",
        "It is now safe to cut over.",
        "You are ready to cut over.",
        "Run step-1.sequences.sql at cutover.",
        "Rebuild them after cutover.",
        "Wait until DMS has applied the last change.",
        "Freeze writes on the source first.",
        "Aurora is ready.",
        "The target is ready for your applications.",
        "Replication is ready.",
        "The migration is ready to go live.",
        "Your tables are ready.",
        "Poll until ready, then point your applications at Aurora.",
        "Wait until ready.",
        "Aurora is `ready`.",
        'Step 1 is "ready" for your applications.',
        "The target is production-ready.",
        "Aurora must be ready or moving before you switch.",
    ],
)
def test_the_guard_catches_each_kind_of_advice(advice):
    # In any file, including the ones with pinned exceptions.
    for name in ("sample", "scripts/run_dms_step.py", ".claude/commands/move.md"):
        assert _findings(name, advice) != [], name


@pytest.mark.parametrize(
    "fine",
    [
        "The cutover is your responsibility.",
        "Full load complete. Replication is in progress.",
        "Run cutover-scripts to write cutover-routines.sql.",
        "MySQL triggers and events are left to the cutover script.",
        "Every AWS write needs the customer's approval.",
        "`--until ready` stops once the preflight passes.",
        "MOVE_RESULT: stopped stage=check reason=until ready",
        "DMS reports the task never started (ready, not running).",
        "This is not a production-ready configuration.",
    ],
)
def test_the_guard_lets_the_decided_wording_and_file_names_through(fine):
    assert _findings("sample", fine) == []
