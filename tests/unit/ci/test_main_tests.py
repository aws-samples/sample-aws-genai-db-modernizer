"""ci/main_tests.py: which new and changed tests ci/pre_merge.sh runs on main's code (#1155)."""

from __future__ import annotations

from ci.main_tests import (
    changed_tests,
    functions_in,
    is_test_file,
    main,
    module_marked,
    not_run_reason,
    outcomes,
    parse_name_status,
)

BASE = """
import pytest


def helper():
    return 1


def test_same():
    assert helper() == 1


def test_edited():
    assert 1 == 1


@pytest.mark.parametrize("x", [1, 2])
def test_params(x):
    assert x


class TestGroup:
    def test_kept(self):
        assert True
"""

HEAD = """
import pytest


def helper():
    return 2


def test_same():
    assert helper() == 1


def test_edited():
    assert 1 == 2


@pytest.mark.parametrize("x", [1, 2, 3])
def test_params(x):
    assert x


def test_new():
    assert True


@pytest.mark.passes_on_main
def test_new_guard():
    assert True


class TestGroup:
    def test_kept(self):
        assert True

    async def test_added(self):
        assert True


@pytest.mark.passes_on_main
class TestGuards:
    def test_one(self):
        assert True
"""


def test_only_new_and_changed_test_functions_are_listed():
    """An unchanged test (even one whose helper changed) is not listed; a changed
    parametrize list counts as a change; methods get their class in the node id."""
    got = changed_tests("tests/unit/test_x.py", BASE, HEAD)
    assert got == [
        ("run", "tests/unit/test_x.py::test_edited"),
        ("run", "tests/unit/test_x.py::test_params"),
        ("run", "tests/unit/test_x.py::test_new"),
        ("marked", "tests/unit/test_x.py::test_new_guard"),
        ("run", "tests/unit/test_x.py::TestGroup::test_added"),
        ("marked", "tests/unit/test_x.py::TestGuards::test_one"),
    ]


def test_a_new_file_lists_every_test():
    got = changed_tests("tests/unit/test_y.py", None, BASE)
    assert [node for _, node in got] == [
        "tests/unit/test_y.py::test_same",
        "tests/unit/test_y.py::test_edited",
        "tests/unit/test_y.py::test_params",
        "tests/unit/test_y.py::TestGroup::test_kept",
    ]


def test_an_unchanged_file_lists_nothing():
    assert changed_tests("tests/unit/test_x.py", BASE, BASE) == []


def test_a_module_level_marker_is_not_honoured():
    """One ``pytestmark`` line must not exempt a whole new file: its tests run, and
    the file is reported."""
    source = (
        "import pytest\n\npytestmark = [pytest.mark.passes_on_main]\n\n\ndef test_a():\n    pass\n"
    )
    assert changed_tests("tests/t/test_m.py", None, source) == [
        ("run", "tests/t/test_m.py::test_a")
    ]
    assert module_marked(source)
    assert module_marked("import pytest\npytestmark = pytest.mark.passes_on_main\n")
    assert not module_marked("import pytest\npytestmark = pytest.mark.slow\n")


def test_only_the_real_marker_decorator_counts():
    """The name inside a string, another marker or another object is not the marker."""
    source = """
import pytest


@pytest.mark.skipif(False, reason="not passes_on_main")
def test_reason():
    pass


@other.mark.passes_on_main
def test_other_object():
    pass


@pytest.mark.passes_on_main(reason="guards existing behaviour")
def test_called():
    pass
"""
    assert changed_tests("tests/t/test_d.py", None, source) == [
        ("run", "tests/t/test_d.py::test_reason"),
        ("run", "tests/t/test_d.py::test_other_object"),
        ("marked", "tests/t/test_d.py::test_called"),
    ]


def test_nested_classes_are_listed_and_inherit_the_marker():
    source = """
import pytest


class TestOuter:
    class TestInner:
        def test_x(self):
            pass


@pytest.mark.passes_on_main
class TestMarked:
    class TestDeep:
        def test_y(self):
            pass
"""
    assert changed_tests("tests/t/test_n.py", None, source) == [
        ("run", "tests/t/test_n.py::TestOuter::TestInner::test_x"),
        ("marked", "tests/t/test_n.py::TestMarked::TestDeep::test_y"),
    ]


def test_a_renamed_file_is_compared_with_its_old_path():
    out = "M\ttests/a/test_m.py\nA\ttests/a/test_new.py\nR095\ttests/a/test_old.py\ttests/b/test_moved.py\nD\ttests/a/test_gone.py\n"
    assert parse_name_status(out) == [
        ("tests/a/test_m.py", "tests/a/test_m.py"),
        ("tests/a/test_new.py", None),
        ("tests/b/test_moved.py", "tests/a/test_old.py"),
    ]


def test_a_file_that_does_not_parse_lists_nothing():
    assert functions_in("def test_(:\n") == {}


def test_test_files_and_the_suites_not_run():
    assert is_test_file("tests/unit/ci/test_pre_merge.py")
    assert not is_test_file("tests/unit/ci/conftest.py")
    assert not is_test_file("tests/unit/ci/fixtures/data.json")
    assert not is_test_file("src/test_thing.py")
    assert not_run_reason("tests/e2e/test_pipeline.py")
    assert not_run_reason("tests/integration/test_api.py")
    assert not_run_reason("tests/e2e_extra/test_x.py") is None
    assert not_run_reason("tests/unit/test_x.py") is None


JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest">
<testcase classname="tests.unit.test_x" name="test_new" time="0.01"/>
<testcase classname="tests.unit.test_x" name="test_edited" time="0.01"><failure message="boom"/></testcase>
<testcase classname="tests.unit.test_x" name="test_params[1]" time="0.01"/>
<testcase classname="tests.unit.test_x" name="test_params[2]" time="0.01"/>
<testcase classname="tests.unit.test_x" name="test_params[3]" time="0.01"><failure message="new case"/></testcase>
<testcase classname="tests.unit.test_x" name="test_all_params[a]" time="0.01"/>
<testcase classname="tests.unit.test_x" name="test_all_params[b]" time="0.01"/>
<testcase classname="tests.unit.test_x.TestGroup" name="test_added" time="0.01"/>
<testcase classname="tests.unit.test_x" name="test_skipped" time="0.01"><skipped message="s"/></testcase>
<testcase classname="tests.unit.test_x" name="test_errored" time="0.01"><error message="e"/></testcase>
<testcase classname="" name="tests.unit.test_z" time="0.01"><error message="collection failure"/></testcase>
</testsuite></testsuites>
"""


def test_outcomes_separate_pass_fail_skip_and_no_import():
    """A parametrized test whose new case fails on main fails; a test whose file does
    not import on main is reported as such, not as an ordinary failure; a skip is a
    skip; a test that was never collected fails."""
    ids = [
        "tests/unit/test_x.py::test_new",
        "tests/unit/test_x.py::test_edited",
        "tests/unit/test_x.py::test_params",
        "tests/unit/test_x.py::test_all_params",
        "tests/unit/test_x.py::TestGroup::test_added",
        "tests/unit/test_x.py::test_skipped",
        "tests/unit/test_x.py::test_errored",
        "tests/unit/test_x.py::test_not_collected",
        "tests/unit/test_z.py::test_import_fails",
    ]
    assert outcomes([JUNIT], ids) == [
        ("pass", "tests/unit/test_x.py::test_new"),
        ("fail", "tests/unit/test_x.py::test_edited"),
        ("fail", "tests/unit/test_x.py::test_params"),
        ("pass", "tests/unit/test_x.py::test_all_params"),
        ("pass", "tests/unit/test_x.py::TestGroup::test_added"),
        ("skipped", "tests/unit/test_x.py::test_skipped"),
        ("fail", "tests/unit/test_x.py::test_errored"),
        ("fail", "tests/unit/test_x.py::test_not_collected"),
        ("noimport", "tests/unit/test_z.py::test_import_fails"),
    ]


def test_outcomes_read_every_report(tmp_path, capsys):
    """pre_merge.sh runs one pytest per file, so the cases of one run are spread
    over several reports."""
    first = '<testsuites><testsuite><testcase classname="tests.a" name="test_p[1]"/></testsuite></testsuites>'
    second = '<testsuites><testsuite><testcase classname="tests.a" name="test_p[2]"><failure/></testcase>'
    second += '<testcase classname="tests.b" name="test_q"/></testsuite></testsuites>'
    assert outcomes([first, second], ["tests/a.py::test_p", "tests/b.py::test_q"]) == [
        ("fail", "tests/a.py::test_p"),
        ("pass", "tests/b.py::test_q"),
    ]
    (tmp_path / "1.xml").write_text(first)
    (tmp_path / "2.xml").write_text(second)
    assert main(["report", str(tmp_path), "tests/a.py::test_p", "tests/b.py::test_q"]) == 0
    assert capsys.readouterr().out == "fail tests/a.py::test_p\npass tests/b.py::test_q\n"
