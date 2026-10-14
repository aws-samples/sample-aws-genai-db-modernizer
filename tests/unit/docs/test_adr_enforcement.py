"""Every numbered ADR decision names the test that guards it (#623).

An ADR that is only read does not stop the code drifting from it (ADR-031 was
accepted while table-first scoping and name-based tie rules lived on). So an
ADR that numbers its decisions (``### N. Title`` under ``## Decision(s)``) also
carries an ``## Enforcement`` table with one row per decision:

    | Decision | Enforced by |
    |---|---|
    | 1. The query is the unit | `tests/unit/x/test_y.py::TestClass::test_name`<br>`tests/unit/z.py` |
    | 5. Cost is a dimension | none yet -- #638 |

Lettered (``### A.``), ``### Decision N:`` and ``### 1 - Title`` headings count too, and
so do the decisions an ``## Amendment`` section adds (ADR-028's D, E, F). Each ``Enforced by`` cell lists guard tests separated by ``<br>``. An entry is a
path under ``tests/``, optionally followed by ``::Class`` and ``::function``
names that must exist in that file. A decision with no guard says
``none yet -- #N`` (the follow-up issue that adds one) instead of inventing a
weak test; it is never left blank. A decision a later ADR replaced says
``superseded by ADR-NNN Decision N``: that ADR must exist and number that decision, and the
superseding decision carries the guard (its own Enforcement row). When no later ADR covers
it, ``superseded -- #N (reason)`` or ``superseded -- <what changed and the code path>`` says so (the decision text stays). An amendment that adds no lettered or numbered decision (a prose ``## Amendment 2 (date)``
section, or a dated ``**Amendment (date, ...)**`` paragraph inside a decision) is a unit
of its own: it needs a row labelled ``Amendment 2``, ``Amendment 2026-10-14`` or
``Amendment #273`` (the number, else the date, else the issue in its heading) naming a guard
test, ``none yet -- #N``, or ``no rule -- <reason>`` when it is prose only and states no
rule. ``no rule`` is valid on amendment rows only; a decision always has a rule. Sections of ADRs that predate this rule are
listed in ``PENDING_ADOPTION`` (#641) and leave that list when their table is
added.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
TESTS_DIR = REPO_ROOT / "tests"
ADR_DIR = REPO_ROOT / "docs" / "architecture" / "decisions"

# ADRs with numbered decisions that have no Enforcement table yet (#641 finished the first
# batch; the set stays so a future ADR written before it has a table can be listed).
# Remove an entry when its table lands; the test below fails if you forget.
PENDING_ADOPTION: frozenset[str] = frozenset()

# A label (``1``, ``A``) then ``.``, ``:``, ``)`` or a spaced dash: ``### 1. T``, ``### A: T``,
# ``### Decision 2 — T``, ``### 1 - T``.
LABEL = r"(?:Decision\s+)?(\d+|[A-Z])(?:[.:)]|\s+[-\u2013\u2014])"
DECISION_HEADING = re.compile(rf"^### {LABEL}\s+\S", re.MULTILINE)
# A heading that starts like a decision (``### 1 Title``, ``### Decision 3``) but is not in a
# recognised style: reported rather than silently skipped.
DECISION_LOOKALIKE = re.compile(r"^### (?:Decision\s+\d+|\d+\b|[A-Z]\b(?!\s+[a-z]))", re.MULTILINE)
FENCE = re.compile(r"^(```|~~~).*?^\1[^\n]*$", re.MULTILINE | re.DOTALL)
DECISION_SECTIONS = ("Decision", "Amendment")
SECTION_HEADING = re.compile(r"^## (.+?)\s*$", re.MULTILINE)
ROW = re.compile(rf"^\|\s*{LABEL}[^|]*\|(.*)\|\s*$")
NONE_YET = re.compile(r"^none yet\s+(?:--|—)\s+#\d+(?:\s+\(.*\))?$")
SUPERSEDED_NOTE = re.compile(
    r"^superseded\s+(?:--|—)\s+(?:#\d+(?:\s+\(.*\))?|[^#\s].*)$", re.IGNORECASE
)
SUPERSEDED = re.compile(
    r"^superseded by ADR-(\d{3}) Decision (\d+|[A-Z])(?:\s+\(.*\))?$", re.IGNORECASE
)
NO_RULE = re.compile(r"^no rule\s+(?:--|—)\s+\S.*$", re.IGNORECASE)
# An amendment row label: ``Amendment 1``, ``Amendment 2026-10-02`` or ``Amendment #273``,
# optionally followed by free text before the ``|``.
AMENDMENT_KEY = r"Amendment\s+(?:\d{4}-\d{2}-\d{2}|\d+(?![\d-])|#\d+)"
AMENDMENT_ROW = re.compile(rf"^\|\s*({AMENDMENT_KEY})[^|]*\|(.*)\|\s*$")
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
# A dated amendment note inside a decision: ``**Amendment (2026-10-14, ...)**`` or ``*Amended ...*``.
INLINE_AMENDMENT = re.compile(r"^\*{1,2}Amend(?:ment|ed)\b[^\n]*", re.MULTILINE)
TEST_ENTRY = re.compile(r"^`(tests/[\w./-]+\.py)((?:::\w+)*)`$")


def _sections(text: str) -> list[tuple[str, str]]:
    """Top-level (``##``) ``(heading, body)`` pairs, in document order.

    A list, not a dict: an ADR may repeat a heading (two ``## Decision`` parts, several
    ``## Amendment`` sections) and none of them may be dropped. Fenced code blocks are
    blanked first so a ``## `` line inside one does not cut a section short.
    """
    text = FENCE.sub(lambda m: "\n" * m.group(0).count("\n"), text)
    matches = list(SECTION_HEADING.finditer(text))
    out: list[tuple[str, str]] = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        out.append((m.group(1), text[m.end() : end]))
    return out


def _decision_body(text: str) -> str:
    """Every section that can carry decisions: ``## Decision*`` and ``## Amendment*``."""
    return "\n".join(b for title, b in _sections(text) if title.startswith(DECISION_SECTIONS))


def decision_numbers(text: str) -> list[str]:
    """Labels (``1``, ``A``) of the decision headings in every decision or amendment section."""
    return DECISION_HEADING.findall(_decision_body(text))


def unrecognised_decision_headings(text: str) -> list[str]:
    """Headings that look like decisions but are in a style the parser does not read."""
    return [
        line
        for line in re.findall(r"^### .*$", _decision_body(text), re.MULTILINE)
        if DECISION_LOOKALIKE.match(line) and not DECISION_HEADING.match(line)
    ]


def _amendment_key(heading: str) -> str | None:
    """``Amendment 1`` / ``Amendment 2026-10-02`` / ``Amendment #273`` for a section heading."""
    num = re.match(r"Amendment\s+(\d+)(?![\d-])", heading)
    if num:
        return f"Amendment {num.group(1)}"
    date = DATE.search(heading)
    if date:
        return f"Amendment {date.group(0)}"
    issue = re.search(r"#\d+", heading)
    return f"Amendment {issue.group(0)}" if issue else None


def _amendment_units(text: str) -> list[tuple[str, str]]:
    """``(key, own text)`` of each amendment that adds no decision heading of its own."""
    units: list[tuple[str, str]] = []
    for title, body in _sections(text):
        if title.startswith("Amendment"):
            if not DECISION_HEADING.search(body):
                units.append((_amendment_key(title) or f"Amendment ?{title}", body))
        elif title.startswith("Decision"):
            for m in INLINE_AMENDMENT.finditer(body):
                note = body[m.start() :].split("\n\n", 1)[0]
                date = DATE.search(m.group(0))
                units.append(
                    (f"Amendment {date.group(0)}" if date else f"Amendment ?{note[:40]}", note)
                )
    return units


def amendment_keys(text: str) -> list[str]:
    """Row keys of the amendments that add no decision heading of their own.

    Two shapes: a ``## Amendment ...`` section with no ``### A.``/``### 1.`` decision inside
    (its decisions, when it has them, are checked as decisions), and a dated
    ``**Amendment (date ...)**`` paragraph inside a decision. A heading the key cannot be read
    from (no number, date or issue) gives ``Amendment ?<heading>`` so the check reports it.
    """
    return [k for k, _ in _amendment_units(text)]


AMENDMENT_MENTION = re.compile(r"^\s*(?:#{1,6}\s+|[>\-*]+\s*)+(?:Amend(?:ment|ed)|Addendum)\b")
NO_RULE_NOTE = re.compile(r"^\*No enforceable rule\b", re.MULTILINE)


def unrecognised_amendments(text: str) -> list[str]:
    """Lines that open an amendment (or addendum) in a shape that produced no key.

    A line starting with ``Amendment``/``Amended``/``Addendum`` after a ``#``, ``>``, ``-`` or
    ``*`` marker counts (plain wrapped prose that begins with the word does not). Read shapes are ``## Amendment ...`` sections and a
    ``**Amendment ...**`` paragraph at the start of a line inside a ``## Decision`` section;
    anything else (``### Amendment`` under a decision, a ``> **Amendment`` quote, a note under
    ``## Consequences``, ``## Amended`` or ``## Addendum``) would be skipped silently, so it is
    reported, as ``unrecognised_decision_headings`` does.
    """
    out: list[str] = []
    for title, body in _sections(text):
        if title.startswith("Amendment"):
            continue  # the heading is read; a line inside is its own prose
        if AMENDMENT_MENTION.match(f"## {title}"):
            out.append(f"## {title}")
        read = title.startswith("Decision")
        for line in body.splitlines():
            if AMENDMENT_MENTION.match(line) and not (read and INLINE_AMENDMENT.match(line)):
                out.append(line.strip())
    return out


def no_rule_problems(text: str, key: str, cell: str) -> list[str]:
    """``no rule`` needs the amendment's own italic ``*No enforceable rule: ...*`` note.

    Without it, one edit to a row could silence an amendment that states a rule.
    """
    if not any(NO_RULE.match(e.strip()) for e in cell.split("<br>")):
        return []
    own = next((body for k, body in _amendment_units(text) if k == key), "")
    if NO_RULE_NOTE.search(own):
        return []
    return ["the amendment has no italic `*No enforceable rule: ...*` note in its own text"]


def enforcement_rows(text: str) -> dict[str, str]:
    """Decision number or amendment key -> raw ``Enforced by`` cell, from the Enforcement table."""
    body = "\n".join(b for title, b in _sections(text) if title == "Enforcement")
    rows: dict[str, str] = {}
    for line in body.splitlines():
        line = line.strip()
        m = ROW.match(line)
        if m:
            key, cell = m.group(1), m.group(2)
        else:
            m = AMENDMENT_ROW.match(line)
            if not m:
                continue
            key, cell = re.sub(r"\s+", " ", m.group(1)), m.group(2)
        assert key not in rows, f"{key} has two Enforcement rows"
        rows[key] = cell.strip()
    return rows


def _members(node: ast.AST) -> dict[str, ast.AST]:
    return {
        n.name: n
        for n in getattr(node, "body", [])
        if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _status(text: str) -> str:
    m = re.search(r"\*\*Status:\*\*\s*(.*)", text)
    return m.group(1).strip() if m else ""


def superseding_problems(adr_number: str, label: str, current: str | None = None) -> list[str]:
    """Why ``superseded by ADR-NNN Decision N`` does not point at a real, guarded decision.

    The target must be another ADR (not ``current``) that is itself in force, and its row must
    name at least one test, so a superseded decision cannot point at a decision with no guard.
    """
    files = sorted(ADR_DIR.glob(f"ADR-{adr_number}-*.md"))
    if not files:
        return [f"superseded by ADR-{adr_number}: no such ADR"]
    if all(f.name == current for f in files):
        return [f"superseded by ADR-{adr_number}: an ADR cannot supersede its own decision"]
    for f in files:
        if f.name == current:
            continue
        text = f.read_text()
        if label in decision_numbers(text):
            if re.match(r"(?:Superseded|Deprecated|Rejected)\b", _status(text)):
                return [f"{f.name} is itself {_status(text)!r}: point at its successor"]
            cell = enforcement_rows(text).get(label)
            if cell is None:
                return [f"{f.name} decision {label} has no Enforcement row to carry the guard"]
            if not any(TEST_ENTRY.match(e.strip()) for e in cell.split("<br>")):
                return [f"{f.name} decision {label} names no test, so it cannot carry the guard"]
            return []
    return [f"superseded by ADR-{adr_number}: no decision {label!r} in it"]


def decision_text(text: str, label: str) -> str:
    """The body of one decision: from its heading to the next ``###``/``##`` heading."""
    body = _decision_body(text)
    for m in DECISION_HEADING.finditer(body):
        if m.group(1) == label:
            nxt = re.search(r"^##+ ", body[m.end() :], re.MULTILINE)
            return body[m.start() : m.end() + (nxt.start() if nxt else len(body))]
    return ""


SUPERSEDED_STATUS = re.compile(r"(?:Partially\s+)?(?:superseded|deprecated)", re.IGNORECASE)
SUPERSEDED_BODY_NOTE = re.compile(r"^\*(?:Superseded|Deprecated)\b", re.MULTILINE)


def superseded_context_problems(text: str, label: str, cell: str) -> list[str]:
    """A superseded marker needs the ADR's status line and the decision's own italic note.

    Without both, one edit to a row could replace a live guard and stay green.
    """
    entries = [e.strip() for e in cell.split("<br>")]
    if not any(SUPERSEDED_NOTE.match(e) or SUPERSEDED.match(e) for e in entries):
        return []
    problems = []
    if not SUPERSEDED_STATUS.match(_status(text)):
        problems.append(
            "the Status line does not say Superseded, Partially superseded or Deprecated"
        )
    if not SUPERSEDED_BODY_NOTE.search(decision_text(text, label)):
        problems.append("the decision has no italic `*Superseded ...*` note")
    return problems


def entry_problems(entry: str, current: str | None = None, *, amendment: bool = False) -> list[str]:
    """Why one cell entry is not a valid guard reference (empty when valid).

    ``amendment`` allows ``no rule -- <reason>``, which only a prose-only amendment may say.
    """
    if NONE_YET.match(entry):
        return []
    if amendment and NO_RULE.match(entry):
        return []
    if SUPERSEDED_NOTE.match(entry):
        return []
    sup = SUPERSEDED.match(entry)
    if sup:
        return superseding_problems(sup.group(1), sup.group(2), current)
    m = TEST_ENTRY.match(entry)
    if not m:
        return [f"{entry!r} is neither a `tests/...py[::Name]` reference nor 'none yet -- #N'"]
    rel = m.group(1)
    if ".." in Path(rel).parts:
        return [f"{rel} must not contain '..'"]
    path = (REPO_ROOT / rel).resolve()
    if not path.is_relative_to(TESTS_DIR):
        return [f"{rel} is outside tests/"]
    if not (path.name.startswith("test_") or path.stem.endswith("_test")):
        return [f"{rel} is not a test module (test_*.py or *_test.py)"]
    if not path.is_file():
        return [f"{rel} does not exist"]
    names = [n for n in m.group(2).split("::") if n]
    if len(names) > 2:
        return [f"{entry}: use at most Class::function"]
    scope = _members(ast.parse(path.read_text()))
    walked = rel
    for depth, name in enumerate(names):
        node = scope.get(name)
        if node is None:
            return [f"{walked} has no {name!r}"]
        is_class = isinstance(node, ast.ClassDef)
        collected = (is_class and name.startswith("Test")) or (
            not is_class and name.startswith("test")
        )
        if (
            not collected
            or (depth == 1 and is_class)
            or (depth == 0 and not is_class and len(names) > 1)
        ):
            return [f"{walked}::{name} is not a collected test (Test* class or test_* function)"]
        walked += f"::{name}"
        scope = _members(node)
    return []


def cell_problems(cell: str, current: str | None = None, *, amendment: bool = False) -> list[str]:
    entries = [e.strip() for e in cell.split("<br>") if e.strip()]
    if not entries:
        return ["the Enforced by cell is empty"]
    if amendment and len(entries) > 1 and any(NO_RULE.match(e) for e in entries):
        return ["`no rule` cannot be mixed with other entries: it says the amendment has no rule"]
    return [p for e in entries for p in entry_problems(e, current, amendment=amendment)]


def _adrs() -> list[Path]:
    return sorted(ADR_DIR.glob("ADR-*.md"))


def _in_force(text: str) -> bool:
    status = re.search(r"\*\*Status:\*\*\s*(\w+)", text)
    # Deprecated ADRs stay checked: their decisions are marked, not dropped.
    return not status or status.group(1) not in {"Superseded", "Rejected"}


def _has_checkable_units(text: str) -> bool:
    return bool(decision_numbers(text) or amendment_keys(text))


def _checked_adrs() -> list[Path]:
    """ADRs whose numbered decisions must each have a guard (not pending)."""
    return [
        p
        for p in _adrs()
        if p.name not in PENDING_ADOPTION
        and _has_checkable_units(p.read_text())
        and _in_force(p.read_text())
    ]


def unparsed_decisions(text: str) -> bool:
    """True when an ADR claims enforcement (a table) but the parser reads no decision.

    That is how a renamed ``## Decision`` or a ``### Part A.`` heading would drop an ADR out
    of the check unnoticed.
    """
    return bool(enforcement_rows(text)) and not _has_checkable_units(text)


@pytest.mark.parametrize("adr", _adrs(), ids=lambda p: p.name)
def test_an_adr_with_an_enforcement_table_has_decisions_the_parser_reads(adr: Path) -> None:
    assert not unparsed_decisions(adr.read_text()), (
        f"{adr.name} has an Enforcement table but no decision heading the parser reads "
        "(a renamed `## Decision` section or an unsupported heading style?)"
    )


def test_checker_notices_a_renamed_decision_section_and_an_unsupported_heading() -> None:
    table = "## Enforcement\n\n| 1. A | none yet -- #1 |\n"
    assert not unparsed_decisions(f"## Decision\n\n### 1. A\n\n{table}")
    assert unparsed_decisions(f"## What we decided\n\n### 1. A\n\n{table}")
    assert unparsed_decisions(f"## Decision\n\n### Part A. First\n\n{table.replace('1.', 'A.')}")


def test_adr_031_is_checked() -> None:
    """The rule must actually cover the ADR it was written for."""
    assert "ADR-031-query-first-routing-and-engine-rule-catalog.md" in [
        p.name for p in _checked_adrs()
    ]


@pytest.mark.parametrize("adr", _checked_adrs(), ids=lambda p: p.name)
def test_every_numbered_decision_names_an_existing_guard(adr: Path) -> None:
    text = adr.read_text()
    numbers = decision_numbers(text)
    amendments = amendment_keys(text)
    rows = enforcement_rows(text)
    problems = [
        f"decision {n} has no row in the Enforcement table" for n in numbers if n not in rows
    ]
    problems += [
        f"{a} has no row in the Enforcement table (a guard test, `none yet -- #N` or "
        "`no rule -- <reason>`)"
        for a in amendments
        if a not in rows
    ]
    problems += [
        f"{a} appears twice; give each amendment its own number"
        for a in set(amendments)
        if amendments.count(a) > 1
    ]
    problems += [
        f"Enforcement row {n} matches no numbered decision or amendment"
        for n in rows
        if n not in numbers and n not in amendments
    ]
    for n, cell in rows.items():
        is_amendment = n.startswith("Amendment")
        label = n if is_amendment else f"decision {n}"
        problems += [f"{label}: {p}" for p in cell_problems(cell, adr.name, amendment=is_amendment)]
        if not is_amendment:
            problems += [f"{label}: {p}" for p in superseded_context_problems(text, n, cell)]
        else:
            problems += [f"{label}: {p}" for p in no_rule_problems(text, n, cell)]
    assert not problems, f"{adr.name}:\n" + "\n".join(problems)


def test_pending_list_only_holds_adrs_that_still_need_a_table() -> None:
    by_name = {p.name: p for p in _adrs()}
    stale = []
    for name in sorted(PENDING_ADOPTION):
        if name not in by_name:
            stale.append(f"{name} does not exist")
            continue
        text = by_name[name].read_text()
        if not _has_checkable_units(text):
            stale.append(f"{name} has no numbered decisions or amendments")
        elif enforcement_rows(text):
            stale.append(f"{name} has an Enforcement table: remove it from PENDING_ADOPTION")
    assert not stale, "\n".join(stale)


def test_every_adr_with_numbered_decisions_is_checked_or_pending() -> None:
    """A new ADR cannot dodge the rule by simply omitting the table."""
    unchecked = [
        p.name
        for p in _adrs()
        if _has_checkable_units(p.read_text())
        and _in_force(p.read_text())
        and not enforcement_rows(p.read_text())
        and p.name not in PENDING_ADOPTION
    ]
    assert not unchecked, f"no Enforcement table: {unchecked}"


# --- the checker itself must reject the failures it exists to catch ----------


def _adr(decisions: int, table: str) -> str:
    heads = "\n\n".join(f"### {i}. Title {i}\n\nbody" for i in range(1, decisions + 1))
    return f"# ADR\n\n**Status:** Accepted\n\n## Decision\n\n{heads}\n\n## Enforcement\n\n{table}\n"


def test_checker_finds_a_decision_with_no_row() -> None:
    text = _adr(2, "| Decision | Enforced by |\n|---|---|\n| 1. A | none yet -- #1 |")
    assert decision_numbers(text) == ["1", "2"]
    assert set(enforcement_rows(text)) == {"1"}


def test_checker_rejects_blank_and_unparseable_cells() -> None:
    assert cell_problems("") == ["the Enforced by cell is empty"]
    assert cell_problems("TBD")
    assert cell_problems("none yet")


def test_checker_rejects_missing_file_class_and_function() -> None:
    here = "tests/unit/docs/test_adr_enforcement.py"
    assert cell_problems(f"`{here}`") == []
    assert cell_problems(f"`{here}::test_checker_rejects_blank_and_unparseable_cells`") == []
    assert cell_problems("`tests/unit/docs/test_does_not_exist.py`")
    assert cell_problems(f"`{here}::TestNope`")
    assert cell_problems(f"`{here}::test_nope`")


def test_checker_accepts_the_explicit_marker_only_with_an_issue_number() -> None:
    assert cell_problems("none yet -- #638") == []
    assert cell_problems("none yet — #638") == []
    assert cell_problems("none yet -- later")


def test_checker_accepts_a_superseded_marker_only_when_the_target_decision_exists() -> None:
    assert cell_problems("superseded by ADR-031 Decision 6") == []
    assert cell_problems("Superseded by ADR-031 Decision 6 (one engine per source)") == []
    assert cell_problems("superseded by ADR-028 Decision A") == []  # two ADR-028 files
    assert cell_problems("superseded by ADR-031 Decision 99")
    assert cell_problems("superseded by ADR-999 Decision 1")
    assert cell_problems("superseded by ADR-031")
    assert cell_problems("superseded by ADR-031 Decision 6<br>`tests/nope.py`")


def test_checker_accepts_a_superseded_note_only_with_a_reason() -> None:
    assert cell_problems("superseded -- groups are 20 queries (`group_splitter.py`)") == []
    assert cell_problems("Superseded — the stack was removed") == []
    assert cell_problems("superseded")
    assert cell_problems("superseded --")
    assert cell_problems("superseded -- #701") == []
    assert cell_problems("superseded -- #701 (weights table is stale)") == []
    assert cell_problems("superseded -- #") and cell_problems("superseded -- #abc")
    assert cell_problems("superseded -- #701 no parentheses")


def test_checker_rejects_self_superseded_and_unguarded_targets() -> None:
    gate = "ADR-018-reality-check-and-human-gate.md"
    assert superseding_problems("018", "3", gate)  # self-reference
    assert superseding_problems("017", "2")  # target row is only `none yet`
    assert superseding_problems("001", "1")  # target ADR is itself Superseded
    assert superseding_problems("031", "6", gate) == []


def test_a_superseded_marker_needs_the_status_line_and_the_decision_note() -> None:
    live = "# A\n\n**Status:** Accepted\n\n## Decision\n\n### 1. One\n\nbody\n"
    marked = (
        "# A\n\n**Status:** Partially superseded: Decision 1\n\n## Decision\n\n### 1. One\n\n"
        "body\n\n*Superseded (#1): the code changed.*\n\n### 2. Two\n"
    )
    cell = "superseded -- #1 (the code changed)"
    assert superseded_context_problems(live, "1", cell)  # silences a live guard
    assert superseded_context_problems(marked.replace("*Superseded", "Superseded"), "1", cell)
    assert superseded_context_problems(marked, "2", cell)  # the note belongs to decision 1
    assert superseded_context_problems(marked, "1", cell) == []
    assert superseded_context_problems(live, "1", "`tests/unit/docs/x.py`") == []


def test_checker_accepts_several_entries_in_one_cell() -> None:
    here = "tests/unit/docs/test_adr_enforcement.py"
    assert cell_problems(f"`{here}`<br>none yet -- #1") == []
    assert cell_problems(f"`{here}`<br>`tests/nope.py`")


def test_checker_rejects_paths_that_escape_tests() -> None:
    assert cell_problems("`tests/../src/agents/referee/tie_break.py::break_tie`")
    assert cell_problems("`tests/unit/../../src/agents/referee/tie_break.py`")


def test_checker_rejects_helpers_conftest_and_non_test_modules() -> None:
    assert cell_problems("`tests/e2e/routing_snapshot.py`")
    assert cell_problems("`tests/conftest.py`")
    assert cell_problems("`tests/e2e/conftest.py::run`")


def test_checker_rejects_names_that_are_not_collected_tests() -> None:
    mod = "tests/unit/test_assignment_resolver.py"
    assert cell_problems(f"`{mod}::_make_triage`")
    assert cell_problems(f"`{mod}::TestOpenSearchNeverOwnsAWrite::_helper`")
    assert cell_problems(f"`{mod}::TestOpenSearchNeverOwnsAWrite::TestNested`")
    assert cell_problems(f"`{mod}::TestOpenSearchNeverOwnsAWrite::test_a::extra`")
    assert cell_problems(f"`{mod}::TestOpenSearchNeverOwnsAWrite`") == []


def test_checker_finds_decisions_in_other_heading_styles() -> None:
    outcome = "# A\n\n## Decision Outcome\n\n### Decision 1: One\n\nx\n\n### Decision 2: Two\n"
    assert decision_numbers(outcome) == ["1", "2"]
    lettered = "# A\n\n## Decision\n\n### A. First\n\n### B. Second\n"
    assert decision_numbers(lettered) == ["A", "B"]
    row = "| Decision A: First | none yet -- #1 |"
    assert set(enforcement_rows(f"## Enforcement\n\n{row}\n")) == {"A"}


def test_checker_ignores_a_heading_line_inside_a_code_fence() -> None:
    text = "## Decision\n\n### 1. One\n\n```text\n## not a heading\n```\n\n### 2. Two\n"
    assert decision_numbers(text) == ["1", "2"]


def test_checker_reads_decisions_added_under_an_amendment_section() -> None:
    text = (
        "## Decision\n\n### A. First\n\n### B. Second\n\n"
        "## Amendment (2026-08-27): new transport\n\n### D. Two-step\n\n### E. Table\n"
    )
    assert decision_numbers(text) == ["A", "B", "D", "E"]


def test_checker_keeps_every_part_of_a_repeated_section_heading() -> None:
    text = (
        "## Decision\n\n### 1. One\n\n## Other\n\nx\n\n"
        "## Decision\n\n### 2. Two\n\n"
        "## Enforcement\n\n| 1. One | none yet -- #1 |\n\n## Notes\n\ny\n\n"
        "## Enforcement\n\n| 2. Two | none yet -- #2 |\n"
    )
    assert decision_numbers(text) == ["1", "2"]
    assert set(enforcement_rows(text)) == {"1", "2"}


def test_checker_reads_dash_and_paren_heading_styles() -> None:
    text = "## Decision\n\n### 1 - One\n\n### Decision 2 — Two\n\n### 3) Three\n\n### C: Four\n"
    assert decision_numbers(text) == ["1", "2", "3", "C"]
    assert set(enforcement_rows("## Enforcement\n\n| 1 - One | none yet -- #1 |\n")) == {"1"}


def test_checker_reports_a_decision_heading_in_an_unreadable_style() -> None:
    text = "## Decision\n\n### 1. Fine\n\n### 2 Missing the dot\n\n### Decision 3\n\n### Notes\n"
    assert unrecognised_decision_headings(text) == ["### 2 Missing the dot", "### Decision 3"]


@pytest.mark.parametrize("adr", _adrs(), ids=lambda p: p.name)
def test_no_decision_heading_goes_unchecked(adr: Path) -> None:
    assert unrecognised_decision_headings(adr.read_text()) == []


# --- amendments that add no decision heading (#749) --------------------------

_PROSE_ADR = (
    "# A\n\n**Status:** Accepted\n\n## Decision\n\n### 1. One\n\nbody\n\n"
    "**Amendment (2026-10-14, maintainer decision, #9): inline note.** text\n\n"
    "## Amendment 1 (2026-09-17): first\n\nprose\n\n"
    "## Amendment (2026-10-02): second (issue #189)\n\nprose\n\n"
    "## Amendment: third (issue #273)\n\nprose\n\n"
    "## Amendment (2026-08-27): has decisions\n\n### D. Two-step\n\nx\n"
)


def test_checker_sees_every_amendment_that_adds_no_decision() -> None:
    assert amendment_keys(_PROSE_ADR) == [
        "Amendment 2026-10-14",  # the dated note inside Decision 1
        "Amendment 1",
        "Amendment 2026-10-02",
        "Amendment #273",
    ]
    assert decision_numbers(_PROSE_ADR) == [
        "1",
        "D",
    ]  # the decision-bearing amendment is not a unit


def test_checker_reports_an_amendment_heading_it_cannot_key() -> None:
    assert amendment_keys("## Amendment: no number\n\nprose\n") == [
        "Amendment ?Amendment: no number"
    ]
    assert amendment_keys("## Decision\n\n**Amendment: undated.** text\n")[0].startswith(
        "Amendment ?"
    )


def test_checker_ignores_an_amendment_heading_inside_a_code_fence() -> None:
    assert amendment_keys("## Decision\n\n```md\n## Amendment 1 (2026-01-01): x\n```\n") == []


def test_checker_reads_amendment_rows() -> None:
    table = (
        "## Enforcement\n\n"
        "| Amendment 1 (2026-09-17): first | none yet -- #1 |\n"
        "| Amendment 2026-10-02 | no rule -- prose only |\n"
        "| Amendment #273 | `tests/unit/docs/test_adr_enforcement.py` |\n"
        "| 1. One | none yet -- #2 |\n"
    )
    assert set(enforcement_rows(table)) == {
        "Amendment 1",
        "Amendment 2026-10-02",
        "Amendment #273",
        "1",
    }
    with pytest.raises(AssertionError):
        enforcement_rows(
            "## Enforcement\n\n| Amendment 1 | none yet -- #1 |\n| Amendment  1 | none yet -- #1 |\n"
        )


def test_no_rule_marker_is_for_amendments_only_and_needs_a_reason() -> None:
    assert cell_problems("no rule -- the amendment only records why", amendment=True) == []
    assert cell_problems("No rule — prose only", amendment=True) == []
    assert cell_problems("no rule -- prose only")  # a decision always has a rule
    assert cell_problems("no rule", amendment=True)
    assert cell_problems("no rule --", amendment=True)
    assert cell_problems("no rule -- prose<br>none yet -- #1", amendment=True)


def test_an_adr_with_only_amendments_is_checked() -> None:
    prose = (
        "# A\n\n**Status:** Accepted\n\n## Decision\n\nprose\n\n## Amendment 1 (2026-01-01): x\n"
    )
    assert _has_checkable_units(prose)
    assert not unparsed_decisions(prose + "\n## Enforcement\n\n| Amendment 1 | no rule -- p |\n")
    assert unparsed_decisions(
        "# A\n\n## Decision\n\nprose\n\n## Enforcement\n\n| Amendment 1 | no rule -- p |\n"
    )


def test_checker_reports_amendment_shapes_it_does_not_read() -> None:
    shapes = {
        "### Amendment (2026-10-15): x": "## Decision\n\n### 1. One\n\n### Amendment (2026-10-15): x\n",
        "> **Amendment (2026-10-15): x**": "## Decision\n\n### 1. One\n\n> **Amendment (2026-10-15): x**\n",
        "- **Amendment (2026-10-15): x**": "## Decision\n\n### 1. One\n\n- **Amendment (2026-10-15): x**\n",
        "**Amendment (2026-10-15): x**": "## Consequences\n\n**Amendment (2026-10-15): x**\n",
        "## Amended 2026-10-15": "## Amended 2026-10-15\n\nprose\n",
        "## Addendum": "## Addendum\n\nprose\n",
    }
    for expected, text in shapes.items():
        found = unrecognised_amendments(text)
        assert any(expected in f for f in found), (expected, found)


def test_checker_does_not_report_the_amendment_shapes_it_reads() -> None:
    assert unrecognised_amendments(_PROSE_ADR) == []
    # prose that merely mentions an amendment is not an amendment
    assert unrecognised_amendments("## Decision\n\nSee the Decision 6 amendment.\n") == []
    assert unrecognised_amendments("## Decision\n\nthe text\namendment to ADR-027.\n") == []
    assert unrecognised_amendments("## Enforcement\n\n| Amendment 1 | none yet -- #1 |\n") == []


@pytest.mark.parametrize("adr", _adrs(), ids=lambda p: p.name)
def test_no_amendment_goes_unrecognised(adr: Path) -> None:
    assert unrecognised_amendments(adr.read_text()) == []


def test_no_rule_needs_the_amendments_own_italic_note() -> None:
    base = "# A\n\n**Status:** Accepted\n\n## Decision\n\nx\n\n## Amendment 1 (2026-01-01): p\n\nprose\n"
    cell = "no rule -- prose only"
    assert no_rule_problems(base, "Amendment 1", cell)
    noted = base + "\n*No enforceable rule: prose only.*\n"
    assert no_rule_problems(noted, "Amendment 1", cell) == []
    assert no_rule_problems(noted, "Amendment 2", cell)  # the note belongs to amendment 1
    assert no_rule_problems(base, "Amendment 1", "none yet -- #1") == []
    inline = "## Decision\n\n### 1. A\n\n*Amendment (2026-01-01): x*\n\n*No enforceable rule: x.*\n"
    assert no_rule_problems(
        inline, "Amendment 2026-01-01", cell
    )  # a later paragraph is not its own


def test_no_rule_cannot_replace_a_row_for_an_amendment_that_states_a_rule() -> None:
    adr = next(p for p in _adrs() if p.name.startswith("ADR-029"))
    text = adr.read_text()
    row = re.search(r"^\| Amendment 2 [^\n]*$", text, re.MULTILINE)
    assert row
    edited = text.replace(
        row.group(0), "| Amendment 2 (2026-09-18): advisory | no rule -- prose only |"
    )
    assert no_rule_problems(edited, "Amendment 2", enforcement_rows(edited)["Amendment 2"])
