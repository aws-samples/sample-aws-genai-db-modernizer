"""Every text and SQL heuristic over one adversarial corpus (#1411).

Most of the Critical review findings on detector and prose-rewriter work were a
hostile input a reviewer wrote by hand: a keyword in a string, a comment, a
dollar-quoted body, a quoted identifier, a negation, a range, a code fence. The
corpus (``tests/corpus/adversarial_inputs.py``) keeps those inputs in one place and
this test runs every registered detector (``tests/corpus/detectors.py``) over all of
them, in each dialect an entry applies to:

- each detector answers what it declares, or what it answers on the entry's twin;
- a wrong answer not fixed yet is an ``xfail(strict=True)`` naming its issue;
- every public function of the declared fact and prose modules is registered or
  listed as not a detector, so a new detector declares its expectations;
- each detector's cost on a very long input grows linearly, measured against the
  same detector on a shorter input in this process (#1230).

Every test guards behaviour main already has (``passes_on_main``): the corpus pins
what each detector answers today, and its known wrong answers are the strict xfails.
"""

from __future__ import annotations

import importlib
import inspect

import pytest

from tests.corpus.adversarial_inputs import (
    LONG_PROSE_UNIT,
    LONG_SQL_UNIT,
    PROSE_ENTRIES,
    SQL_ENTRIES,
    long_text,
)
from tests.corpus.detectors import (
    NOT_PROSE_DETECTORS,
    NOT_SQL_DETECTORS,
    PROSE_DETECTORS,
    PROSE_MODULES,
    SQL_DETECTORS,
    entry_keys,
)
from tests.unit.tools.sql._timing import assert_linear
from tests.unit.tools.sql.test_one_definition_per_fact import FACT_MODULES, _public_facts


def _marks(bugs: dict, *keys: str) -> list:
    issue = next((bugs[k] for k in keys if k in bugs), None)
    if issue is None:
        return []
    return [pytest.mark.xfail(strict=True, reason=f"wrong answer, tracked in #{issue}")]


def _not_linear(issue: int | None) -> list:
    """A known non-linear detector is not run: its quadratic case runs every sampling
    round to the end, seconds each, and would dominate the unit suite. The fix for
    ``issue`` drops the detector's ``linear_bug`` so the check runs again."""
    if issue is None:
        return []
    return [
        pytest.mark.xfail(
            strict=True, run=False, reason=f"not linear on long text, tracked in #{issue}"
        )
    ]


def _sql_cases() -> list:
    cases = []
    for detector in SQL_DETECTORS:
        for entry in SQL_ENTRIES:
            for dialect in entry.dialects:
                key = f"{entry.id}@{dialect}"
                cases.append(
                    pytest.param(
                        detector,
                        entry,
                        dialect,
                        id=f"{detector.name.rsplit('/', 1)[-1]}-{key}",
                        marks=_marks(dict(detector.bugs), key, entry.id),
                    )
                )
    return cases


def _prose_cases() -> list:
    return [
        pytest.param(
            detector,
            entry,
            id=f"{detector.name.rsplit('/', 1)[-1]}-{entry.id}",
            marks=_marks(dict(detector.bugs), entry.id),
        )
        for detector in PROSE_DETECTORS
        for entry in PROSE_ENTRIES
    ]


@pytest.mark.passes_on_main
@pytest.mark.parametrize(("detector", "entry", "dialect"), _sql_cases())
def test_sql_detector_answers_as_declared(detector, entry, dialect):
    key = f"{entry.id}@{dialect}"
    if key in detector.expect:
        expected = detector.expect[key]
    elif entry.id in detector.expect:
        expected = detector.expect[entry.id]
    else:
        expected = detector.read(entry.twin, dialect)
    assert detector.read(entry.text, dialect) == expected, (detector.name, key, entry.why)


@pytest.mark.passes_on_main
@pytest.mark.parametrize(("detector", "entry"), _prose_cases())
def test_prose_detector_answers_as_declared(detector, entry):
    if entry.id in detector.expect:
        expected = detector.expect[entry.id]
    else:
        assert (
            entry.twin is not None
        ), f"{detector.name} declares no answer for {entry.id!r}, which has no twin"
        expected = detector.read(entry.twin)
    assert detector.read(entry.text) == expected, (detector.name, entry.id, entry.why)


# ---------------------------------------------------------------------------
# The registry: every detector declares its expectations
# ---------------------------------------------------------------------------


def _module_functions(path: str) -> set[str]:
    module = importlib.import_module(path.removesuffix(".py").replace("/", "."))
    return {
        f"{path}:{name}"
        for name, value in inspect.getmembers(module, inspect.isfunction)
        if not name.startswith("_") and value.__module__ == module.__name__
    }


@pytest.mark.passes_on_main
def test_every_sql_detector_in_the_fact_modules_is_registered():
    found = {f"QueryFacts.{name}" for name in _public_facts()}
    for path in FACT_MODULES:
        found |= _module_functions(path)
    registered = {d.name for d in SQL_DETECTORS}
    assert not registered & set(NOT_SQL_DETECTORS)
    declared = registered | set(NOT_SQL_DETECTORS)
    assert not found - declared, (
        "A detector in the declared fact modules is not in the adversarial corpus: register "
        "it in tests/corpus/detectors.py with its expectations, or list it in "
        f"NOT_SQL_DETECTORS with why. {sorted(found - declared)}"
    )
    assert not declared - found, f"Registered but gone: {sorted(declared - found)}"


@pytest.mark.passes_on_main
def test_every_prose_rewriter_and_guard_is_registered():
    found: set[str] = set()
    for path in PROSE_MODULES:
        found |= _module_functions(path)
    registered = {d.name for d in PROSE_DETECTORS}
    assert not registered & set(NOT_PROSE_DETECTORS)
    declared = registered | set(NOT_PROSE_DETECTORS)
    assert not found - declared, (
        "A prose rewriter or guard is not in the adversarial corpus: register it in "
        "tests/corpus/detectors.py with its expectations, or list it in "
        f"NOT_PROSE_DETECTORS with why. {sorted(found - declared)}"
    )
    assert not declared - found, f"Registered but gone: {sorted(declared - found)}"


@pytest.mark.passes_on_main
def test_every_declared_answer_and_bug_names_a_corpus_entry():
    sql_keys, prose_ids = entry_keys()
    sql_ids = {key.split("@")[0] for key in sql_keys}
    for detector in SQL_DETECTORS:
        for key in (*detector.expect, *detector.bugs):
            assert key in sql_keys or key in sql_ids, (detector.name, key)
        assert all(issue > 0 for issue in detector.bugs.values()), detector.name
    for detector in PROSE_DETECTORS:
        for key in (*detector.expect, *detector.bugs):
            assert key in prose_ids, (detector.name, key)
        assert all(issue > 0 for issue in detector.bugs.values()), detector.name


@pytest.mark.passes_on_main
def test_each_entry_applies_to_a_dialect_and_differs_from_its_twin():
    assert len({e.id for e in SQL_ENTRIES}) == len(SQL_ENTRIES)
    assert len({e.id for e in PROSE_ENTRIES}) == len(PROSE_ENTRIES)
    for entry in SQL_ENTRIES:
        assert entry.dialects and entry.text != entry.twin, entry.id
    for prose in PROSE_ENTRIES:
        assert prose.text != prose.twin, prose.id


# ---------------------------------------------------------------------------
# Very long input: cost grows linearly (#1230)
# ---------------------------------------------------------------------------

# Both sizes are past the facade's parse limit (MAX_SQL_CHARS), so both take the
# same path and the ratio isolates the growth of the detector itself.
_SQL_SIZES = (120_000, 960_000)
_PROSE_SIZES = (20_000, 160_000)


@pytest.mark.passes_on_main
@pytest.mark.parametrize(
    "detector", SQL_DETECTORS, ids=[d.name.rsplit("/", 1)[-1] for d in SQL_DETECTORS]
)
def test_sql_detector_is_linear_on_a_very_long_statement(detector):
    assert_linear(
        lambda n: long_text(LONG_SQL_UNIT, n),
        lambda text: detector.read(text, "postgresql"),
        sizes=_SQL_SIZES,
    )


@pytest.mark.passes_on_main
@pytest.mark.parametrize(
    "detector",
    [
        pytest.param(
            d,
            id=d.name.rsplit("/", 1)[-1],
            marks=_not_linear(d.linear_bug),
        )
        for d in PROSE_DETECTORS
    ],
)
def test_prose_detector_is_linear_on_a_very_long_text(detector):
    assert_linear(
        lambda n: long_text(LONG_PROSE_UNIT, n),
        detector.read,
        sizes=_PROSE_SIZES,
    )
