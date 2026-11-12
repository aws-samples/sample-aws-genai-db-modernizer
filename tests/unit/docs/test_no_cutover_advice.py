# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The modernizer never gives cutover advice and never claims more than it shows (#2062, #2107).

Step 1 shows that the full load is complete and that replication is in progress. Where the
cutover comes up, the text says only "The cutover is your responsibility." This guard scans
the shipped step 1 text: the data-movement README, ``/move``, the help page's step 1
section, the scripts' and the data-movement tools' source (their messages, comments and
templates), and every file the generators write, rendered here for both engines.

The assessment's deliverables follow the same rule (#2107), with the same words: the source
files that write the wave gates and the mitigations, the engineering and executive reports
rendered from reports the current code built, and the skills whose model text reaches a
deliverable. "in sync" is banned there too, with no exception: a CDC copy is "kept current
by CDC" from its owner (#2167). ``tests/e2e/test_no_cutover_advice.py`` runs the same check
on the samples' real deliverables, a model's included on an e2e-llm run.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from scripts import render_dms_step
from src.report.renderers import render_engineering_report_md
from src.shared.cutover_message import CUTOVER_DISCLAIMER, CUTOVER_MESSAGE, cutover_message_text
from src.tools.data_movement.cutover_scripts import CUTOVER_FILE, CUTOVER_INSTRUCTIONS
from src.tools.data_movement.dms_gate import REPLICATING_MESSAGE
from src.tools.data_movement.routine_scripts import DEPLOY_FILE, INSTRUCTIONS_FILE
from src.tools.data_movement.verify_scripts import write_verify_scripts
from tests.executive_pdf import executive_text, normalize
from tests.unit.report.test_cutover_responsibility import REPORTS

REPO = Path(__file__).resolve().parents[3]

# Words step 1 never uses, and phrases that tell the customer when or how to cut over.
BANNED = {
    "caught up": r"\b(?:caught|catch(?:es|ing)?)[ _-]?up\b",
    "in sync": r"\bin\s+sync\b",  # RDS's own apply status `in-sync` is a status value, not a claim
    "proven": r"\bproven\b",
    "ready": r"\bready\b",  # every use, unless READY_FORMS or a pinned line allows it
    "stop writes": r"\b(?:stop|stops|stopped|stopping|pause|freeze|freezing|froze)\s+"
    r"(?:(?:the|your|all|its|their)\s+)?(?:application'?s?'?\s+)?writ(?:es|ing)\b",
    "freeze writes": r"\bwrite[ -]freeze\b",
    "safe to cut over": r"\bsafe\s+to\s+cut\s*over\b",
    "applied the last change": r"\bapplied\s+the\s+last\s+change\b",
    "lag is zero": r"\blag\s+(?:is|reaches|hits|drops\s+to|gets\s+to)\s+zero\b",
    "cutover timing": r"\b(?:at|after|before|until|once|during|prior\s+to|ahead\s+of)\s+"
    r"(?:the\s+|your\s+|their\s+)?cut\s*over\b(?!-)"
    r"|\b(?:after|before|once|until|when|as\s+soon\s+as)\s+(?:you|they|the\s+customer)\s+"
    r"cut\s*over\b",
}
# Timing words model design text writes next to "in sync" about the plan's own waves or
# CDC, not the cutover (``_model_text_flags``): a later wave or its gate ("dual-read by OpenSearch queries until wave 4", "via
# CDC until then", "before this gate", "Before the Wave 3 gate", "before wave 3 starts",
# "remain on Aurora MySQL once later waves have moved their share", "once the cache wave has
# absorbed the read pressure") and replication's own
# delay ("sees a write only after CDC applies it").
CDC_TIMING = re.compile(
    "|".join(
        (
            r"\b(?:until|before|after)\s+wave\s+\d+(?:\s+starts)?\b",
            r"\bbefore\s+(?:this|the\s+Wave\s+\d+)\s+gate\b",
            r"\bonce\s+(?:every\s+)?later\s+waves?\b",
            r"\bonce\s+the\s+[\w-]+\s+wave\b",
            r"\bvia\s+CDC\s+until\s+then\b",
            r"\bonly\s+after\s+CDC\s+applies\s+it\b",
        )
    ),
    re.I,
)
TIMING_WORD = re.compile(r"\b(?:until|once|when|before|after|wait|switch|cut\s*over)\b", re.I)
# In the deliverables, wave 1's headline counts the whole workload it moves in one cutover ("all 107 queries at
# cutover"): what the wave moves, not when to cut over.
WAVE_ONE_COUNT = re.compile(r"\b(?:query|queries) at cutover$", re.I)
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
    # The assessment (#2107): a docstring, a comment and the no-wave line a report written
    # before #2107 stored, recognised so it is replaced.
    ("src/agents/referee/synthesis_report.py", "reason, ready to sit next to its excerpt."),
    ("src/agents/referee/synthesis_report.py", 'application change) catches up", the same'),
    ("src/shared/migration_risk_rules.py", '# "Before cutover, close" no-wave line a report'),
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
# Out of scope here: the samples (docs/examples/), which #1984 refreshes.
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


# The assessment's source files that write the wave gates, the mitigations and the contract
# text (#2107): scanned as deliverables, "in sync" included (#2167).
ASSESSMENT_SOURCES = (
    "src/agents/referee/migration_wave_builders.py",
    "src/report/waves.py",
    "src/agents/referee/synthesis_report.py",
    "src/contracts/transformation_contract.py",
    "src/shared/migration_risk_rules.py",
    # #1128: the slice plan's stage gates and the adoption paths, and the fixtures the
    # renderers are built against.
    "src/shared/adoption_facts.py",
    "tests/fixtures/adoption_paths/wordpress.json",
    "tests/fixtures/adoption_paths/discourse.json",
)


# Model-written design rationale and review text (a designer's note, a PE review, a
# trade-off) speaks of data, not of the move: "(user_id, name) is not proven unique" is a
# uniqueness statement. There only the cutover timing and readiness patterns apply:
# "proven" and "ready" only as a claim about the move, the target or the switch, and
# "in sync" only next to a timing word (#2107, the discourse e2e-llm run of #2111).
MODEL_TEXT_LABELS = (
    "caught up",
    "lag is zero",
    "cutover timing",
    "safe to cut over",
    "applied the last change",
    "in sync",
    "proven",
    "ready",
)
READINESS_CLAIM = re.compile(
    "|".join(
        (
            # "proven to work", "ready to switch", "ready for your applications"
            r"\b(?:proven|ready)\s+(?:to|for)\s+(?:(?:the|your|a|its|their)\s+)?(?:\w+\s+)?"
            r"(?:work|works|cut\s*over|cutover|switch|go\s+live|production|migrat\w*|"
            r"decommission\w*|retire\w*|traffic|applications?|users|use)\b",
            # "the move is proven.", "Aurora is ready", "is now ready"
            r"\b(?:is|are|was|were|has\s+been|have\s+been|now)\s+(?:now\s+)?(?:fully\s+)?"
            r"(?:proven|ready)\s*(?:[.,;:!?)]|$)",
            r"\b(?:proven|ready)\s+(?:in\s+production|end[- ]to[- ]end)\b",
        )
    ),
    re.I,
)


def _model_text_flags(label: str, line: str, start: int, end: int) -> bool:
    """Whether a match in model-written design text is a cutover or readiness claim."""
    if label in ("proven", "ready"):
        return any(m.start() <= start and end <= m.end() for m in READINESS_CLAIM.finditer(line))
    if label == "in sync":
        return bool(TIMING_WORD.search(CDC_TIMING.sub("", _sentence(line, end))))
    if label == "cutover timing":
        return not WAVE_ONE_COUNT.search(line[:end])
    return True


def _sentence(line: str, end: int) -> str:
    starts = [m.end() for m in re.finditer(r"[.!?]\s+", line[:end])]
    first = starts[-1] if starts else 0
    stop = re.search(r"[.!?](?:\s|$)", line[end:])
    return line[first : end + stop.start() + 1 if stop else len(line)]


def _allowed(
    name: str, label: str, line: str, start: int, end: int, deliverable: bool = False
) -> bool:
    if any(path == name and pinned in line for path, pinned in ALLOWED):
        return True
    if deliverable and label == "cutover timing":
        return bool(WAVE_ONE_COUNT.search(line[:end]))
    return label == "ready" and any(
        m.start() <= start and end <= m.end()
        for pattern in READY_FORMS
        for m in re.finditer(pattern, line)
    )


def _findings(
    name: str,
    text: str,
    skip: tuple[str, ...] = (),
    deliverable: bool = False,
    model_text: bool = False,
) -> list[str]:
    """Each banned match in ``text``. ``deliverable``: the assessment's text, where wave
    1's "all N queries at cutover" count is allowed (#2107); "in sync" never is (#2167). ``model_text``: model-written design rationale and
    review text, where only the cutover timing and readiness claims count
    (``MODEL_TEXT_LABELS``, ``READINESS_CLAIM``)."""
    found = []
    for label, pattern in BANNED.items():
        if label in skip or (model_text and label not in MODEL_TEXT_LABELS):
            continue
        for m in re.finditer(pattern, text, re.IGNORECASE):
            first = text.rfind("\n", 0, m.start()) + 1
            last = text.find("\n", m.end())
            line = text[first : None if last < 0 else last]
            start, end = m.start() - first, m.end() - first
            if model_text:
                if not _model_text_flags(label, line, start, end):
                    continue
            elif _allowed(name, label, line, start, end, deliverable):
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


# ---------------------------------------------------------------------------
# The assessment's deliverables (#2107)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ASSESSMENT_SOURCES)
def test_the_assessment_source_files_give_no_cutover_advice(name: str) -> None:
    assert _findings(name, (REPO / name).read_text(), deliverable=True) == []


@pytest.mark.parametrize("name", sorted(REPORTS))
def test_the_rendered_deliverables_give_no_cutover_advice(name: str) -> None:
    report = REPORTS[name]()
    found = _findings(f"{name} engineering", render_engineering_report_md(report), deliverable=True)
    found += _findings(f"{name} executive", normalize(executive_text(report)), deliverable=True)
    assert found == []


@pytest.mark.passes_on_main
@pytest.mark.parametrize(
    "advice",
    [
        "Once replication to Aurora MySQL has caught up, decommission the source.",
        "Decommission the legacy source once replication lag is zero.",
        "Aurora MySQL is in sync with the source.",
        "Wait until the copies are in sync, then switch.",
        "The move is proven.",
        "Aurora is ready for your applications.",
        "Load-test the queries before cutover.",
        "Rebuild it after cutover.",
        "Decommission the source once cutover completes.",
        "Run the sequences script at cutover.",
        # The review of #2111: readiness wording around "in sync".
        "Keep writes paused until the copies are in sync.",
        "Keep the source running until DynamoDB is in sync.",
        "Switch once Aurora is in sync via CDC.",
        "Cut over when Aurora MySQL is in sync from the source.",
        "Keep a DynamoDB copy in sync via CDC, then switch.",
        "3 tables kept in sync from their owners; cut over when they match.",
        # The review's timing phrasings.
        "Decommission the source after you cut over.",
        "Validate the schema prior to cutover.",
        "Freeze schema changes during cutover.",
        "Load-test ahead of the cutover.",
        "Retire the source when replication lag reaches zero.",
        "Wait for replication to catch up.",
    ],
)
def test_the_guard_catches_each_kind_of_advice_in_a_deliverable(advice: str) -> None:
    assert _findings("sample", advice, deliverable=True) != []
    assert _findings("src/agents/referee/synthesis_report.py", advice, deliverable=True) != []
    assert _findings("sample", advice) != []  # and in step 1's text


@pytest.mark.parametrize(
    "copy",
    [
        # The phrases the guard let through before #2167, and the summary model's paraphrase
        # of one (batch11112312's wordpress e2e-llm run).
        "3 tables kept in sync from their owners.",
        "52 of them read a copy of 13 tables that CDC keeps in sync from DynamoDB.",
        "DynamoDB reads served from copies that change data capture (CDC) keeps in sync.",
        "Build the consumer that keeps wp_posts and wp_postmeta in sync from DynamoDB.",
        "DocumentDB owns them; keep a DynamoDB copy of all of them in sync via CDC.",
        "Build the dual-write path and keep the copies in sync.",
        "22 tables: 15 migrate, 2 kept in sync, 5 stay on Aurora MySQL.",
    ],
)
@pytest.mark.passes_on_main
def test_a_deliverable_never_says_in_sync(copy: str) -> None:
    """#2167: no exception for a CDC copy's table role; a CDC copy is kept current."""
    assert _findings("sample", copy, deliverable=True) != []


@pytest.mark.passes_on_main
@pytest.mark.parametrize(
    "fine",
    [
        CUTOVER_MESSAGE,
        "Decommission the source MySQL database once you have completed your cutover.",
        "3 tables kept current by CDC from their owners.",
        "Build the consumer that keeps wp_posts and wp_postmeta current from DynamoDB.",
        "Every other engine that reads a table reads a copy kept current from the owner.",
        "DocumentDB owns them; keep a DynamoDB copy of all of them current via CDC.",
        "DynamoDB owns them, so keep the Aurora PostgreSQL, DocumentDB copy current from "
        "DynamoDB via CDC until then.",
        "Build the dual-write path that keeps the copies current.",
        "The main exposures are the tables kept current by change data capture (CDC): test the "
        "CDC replication before wave 3 starts.",
        "Close RISK-002 and RISK-004 before this gate (tables kept current by CDC and close "
        "calls).",
        "Before the Wave 3 gate, close RISK-001 and RISK-002 (tables kept current by CDC).",
        "17 source tables: 15 to move, 2 kept current by CDC from their owners Engines: DynamoDB "
        "Why: the smallest data migration available once the cache wave has absorbed the reads.",
        "17 source tables: 15 to move, 2 kept current by CDC from their owners. 5 queries remain "
        "on Aurora MySQL once later waves have moved their share.",
        "1 of the 3 tables stays dual-read by OpenSearch queries until wave 4; DynamoDB owns "
        "them, so keep the OpenSearch copy current from DynamoDB via CDC until then.",
        "A read on a replica reads a copy kept current from DynamoDB via CDC, so it sees a "
        "write only after CDC applies it.",
        "The cutover script creates the triggers.",
        "Wave 1 moves the whole database in one cutover.",
        "1,234 rows, all 107 queries at cutover; 5 remain on Aurora MySQL at the end.",
    ],
)
def test_the_guard_lets_the_deliverables_decided_wording_through(fine: str) -> None:
    assert _findings("sample", fine, deliverable=True) == []


# ---------------------------------------------------------------------------
# Model text: the skills that write customer prose carry the rule
# ---------------------------------------------------------------------------

SKILL_RULE = (
    "Cutover: the cutover is the customer's. The only sentence about it is \"The cutover is "
    'your responsibility." Never say when or how to cut over, switch or decommission (no '
    '"before cutover", "after cutover", "once replication has caught up", "when lag reaches '
    'zero"), and never use "caught up", "proven" or "ready" as readiness wording. Never write '
    '"in sync": a copy that CDC keeps current from its owner is "kept current by CDC".'
)
# Each skill whose text reaches a deliverable, and the section that holds the rule (the one
# its prompt loads).
CUSTOMER_PROSE_SKILLS = {
    "executive-summary.md": "Guardrails",
    "consolidation-review.md": "Executive Summary",
    **{
        f"{e}-data-modeling.md": "Customer-facing text"
        for e in ("aurora_mysql", "aurora_postgresql", "documentdb", "dynamodb", "elasticache")
    },
    "opensearch-index-modeling.md": "Customer-facing text",
    **{
        f"{e}-pe-review.md": "Output contract"
        for e in (
            "aurora_mysql",
            "aurora_postgresql",
            "documentdb",
            "dynamodb",
            "elasticache",
            "opensearch",
        )
    },
    "documentdb-analysis-advisor.md": "Rules",
    "dynamodb-analysis-advisor.md": "Rules",
}


@pytest.mark.parametrize("skill", sorted(CUSTOMER_PROSE_SKILLS))
def test_each_customer_prose_skill_carries_the_cutover_rule(skill: str) -> None:
    from src.shared.skill_sections import skill_sections

    assert SKILL_RULE in skill_sections(skill)[CUSTOMER_PROSE_SKILLS[skill]]


@pytest.mark.passes_on_main
def test_every_skill_is_listed() -> None:
    """A new skill under src/skills/ is placed here, so its prose gets the rule too."""
    assert sorted(p.name for p in (REPO / "src/skills").glob("*.md")) == sorted(
        CUSTOMER_PROSE_SKILLS
    )


def test_the_summary_prompts_carry_the_rule() -> None:
    """The executive summary's and the reality check's prompts, as each model sees them."""
    from src.agents.referee import synthesis_report
    from src.agents.referee.consolidation_validator import CONSOLIDATION_REVIEW_SKILL
    from src.agents.referee.reality_check_handler import REALITY_CHECK_SUMMARY_SECTION
    from src.shared.skill_sections import skill_sections

    assert SKILL_RULE in synthesis_report.executive_summary_prompt(["dynamodb"], [], {})
    assert SKILL_RULE in skill_sections(CONSOLIDATION_REVIEW_SKILL)[REALITY_CHECK_SUMMARY_SECTION]


@pytest.mark.passes_on_main
@pytest.mark.parametrize(
    "advice",
    [
        # The review's sentences, and readiness claims a model could write.
        "Keep writes paused until the copies are in sync.",
        "Keep the source running until DynamoDB is in sync.",
        "Switch once Aurora is in sync via CDC.",
        "Cut over when Aurora MySQL is in sync from the source.",
        "Once replication to Aurora MySQL has caught up, decommission the source.",
        "Retire the source when replication lag reaches zero.",
        "Load-test the queries before cutover.",
        "The design is proven to work, so the move can go ahead.",
        "The table is ready to switch.",
        "Aurora is ready for your applications.",
        "The move is proven.",
        "DynamoDB is now ready.",
    ],
)
def test_model_design_text_is_checked_for_cutover_and_readiness_claims(advice: str) -> None:
    assert _findings("design note", advice, model_text=True) != []


@pytest.mark.passes_on_main
@pytest.mark.parametrize(
    "fine",
    [
        # The discourse e2e-llm run of #2111: a designer's uniqueness statement.
        "The sort key is the id because the collector shows unique (name, user_id) indexes only "
        "for single names and the non-unique (user_id, name) index, so (user_id, name) is not "
        "proven unique and keying on it could overwrite a row.",
        "The access pattern is not proven by the collected queries alone.",
        "The GSI is ready-made for this lookup.",
        "A DynamoDB Streams consumer keeps the summary item in sync with the order items.",
        "This is a production-ready key design.",
        "1 of the 3 tables stays dual-read until wave 4.",
    ],
)
def test_model_design_text_keeps_its_data_statements(fine: str) -> None:
    assert _findings("design note", fine, model_text=True) == []


@pytest.mark.passes_on_main
def test_our_own_text_keeps_every_banned_word() -> None:
    """The deterministic text is checked strictly: the same uniqueness sentence fails."""
    note = "so (user_id, name) is not proven unique and keying on it could overwrite a row."
    assert _findings("ours", note, deliverable=True) != []
