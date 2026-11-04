# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Claim markers name a test that exists (#1851)."""

from __future__ import annotations

import subprocess  # nosec B404 -- builds a scratch git repository with fixed argv
import sys
from pathlib import Path

import pytest

from ci import claims

ROOT = Path(__file__).resolve().parents[3]

TEST_FILE = """
import pytest

def helper():
    pass

def test_plain():
    pass

async def test_async():
    pass

class TestGroup:
    def test_method(self):
        pass

    class TestInner:
        def test_deep(self):
            pass

class Helper:
    def test_hidden(self):
        pass

@pytest.mark.skip(reason='x')
def test_skipped():
    pass

@pytest.mark.skipif(True, reason='x')
def test_skipped_if():
    pass

@pytest.mark.xfail
def test_expected_to_fail():
    pass

@pytest.mark.parametrize('x', [1])
def test_cases(x):
    pass

@pytest.mark.xfail(strict=True)
class TestFailing:
    def test_inside(self):
        pass
"""


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text(TEST_FILE, encoding="utf-8")
    (tmp_path / "tests" / "test_broken.py").write_text("def test_(:\n", encoding="utf-8")
    (tmp_path / "tests" / "helpers.py").write_text("def test_helper():\n    pass\n", "utf-8")
    (tmp_path / "tests" / "test_marked.py").write_text(
        "import pytest\npytestmark = [pytest.mark.skip(reason='x')]\ndef test_any():\n    pass\n",
        encoding="utf-8",
    )
    (tmp_path / "tests" / "test_forms.py").write_text(
        "import pytest\n"
        "from pytest import mark\n"
        "@mark.skip(reason='x')\n"
        "def test_bare_mark():\n    pass\n"
        "class TestClassMarked:\n"
        "    pytestmark = pytest.mark.skip(reason='x')\n"
        "    def test_in(self):\n        pass\n"
        "class TestClassListMarked:\n"
        "    pytestmark = [pytest.mark.xfail]\n"
        "    def test_in(self):\n        pass\n",
        encoding="utf-8",
    )
    (tmp_path / "tests" / "test_annotated.py").write_text(
        "import pytest\npytestmark: list = [pytest.mark.skip(reason='x')]\n"
        "def test_any():\n    pass\n",
        encoding="utf-8",
    )
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "test_y.py").write_text("def test_y():\n    pass\n", "utf-8")
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths = ["tests"]\npython_files = ["test_*.py"]\n',
        encoding="utf-8",
    )
    claims._pytest_config.cache_clear()
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
    claims._tree.cache_clear()
    return tmp_path


def ids(text: str) -> list[str]:
    return [m.node_id for m in claims.markers(text)]


def test_markers_read_each_id_and_its_line():
    text = "Intro.\n\nIt fails. <!-- claim: tests/a.py::test_one, tests/b.py::test_two -->\n"
    found = claims.markers(text, "page.md")
    assert [(m.page, m.line, m.node_id) for m in found] == [
        ("page.md", 3, "tests/a.py::test_one"),
        ("page.md", 3, "tests/b.py::test_two"),
    ]


def test_markers_accept_spaces_newlines_and_no_space_after_the_colon():
    assert ids("<!--claim:tests/a.py::test_a\n  tests/b.py::test_b-->") == [
        "tests/a.py::test_a",
        "tests/b.py::test_b",
    ]


def test_markers_in_code_are_examples_and_are_not_read():
    text = (
        "Use `<!-- claim: tests/a.py::test_span -->` like this:\n\n"
        "```markdown\nIt fails. <!-- claim: tests/a.py::test_fenced -->\n```\n\n"
        "~~~\n<!-- claim: tests/a.py::test_tilde -->\n~~~\n"
        "Real. <!-- claim: tests/a.py::test_real -->\n"
    )
    found = claims.markers(text)
    assert [(m.line, m.node_id) for m in found] == [(10, "tests/a.py::test_real")]


def test_an_empty_marker_is_reported():
    assert ids("x <!-- claim: -->") == [""]
    errs = claims.check(claims.markers("x <!-- claim: -->", "p.md"))
    assert errs == ["p.md:1: empty claim marker; name the test's node id"]


@pytest.mark.parametrize(
    "comment", ["<!-- Claim: t -->", "<!-- claims: t -->", "<!--claim : t -->", "<!-- CLAIM: t -->"]
)
def test_a_misspelt_marker_is_an_error(comment: str):
    errs = claims.near_misses(f"Intro.\n\nIt fails. {comment}\n", "p.md")
    assert len(errs) == 1 and errs[0].startswith("p.md:3: ") and "is not a claim marker" in errs[0]


def test_a_correct_marker_or_one_in_code_is_no_near_miss():
    text = "x <!-- claim: t --> `<!-- Claim: t -->`\n```\n<!-- claims: t -->\n```\n"
    assert claims.near_misses(text) == []


def test_check_fails_on_a_misspelt_marker(repo: Path, capsys: pytest.CaptureFixture[str]):
    (repo / "a.md").write_text("A. <!-- Claim: tests/test_x.py::test_plain -->\n", "utf-8")
    _git_repo(repo)
    assert claims.main(["--root", str(repo), "check"]) == 1
    assert "a.md:1: '<!-- Claim:' is not a claim marker" in capsys.readouterr().out


def test_other_comments_are_not_markers():
    assert ids("<!-- a note --> <!-- claims are good -->") == []


@pytest.mark.parametrize(
    "node_id",
    [
        "tests/test_x.py::test_plain",
        "tests/test_x.py::test_async",
        "tests/test_x.py::TestGroup",
        "tests/test_x.py::TestGroup::test_method",
        "tests/test_x.py::TestGroup::TestInner::test_deep",
        "tests/test_x.py::test_cases",
    ],
)
def test_resolve_accepts_collectable_tests(repo: Path, node_id: str):
    assert claims.resolve(node_id, repo) is None


@pytest.mark.parametrize(
    ("node_id", "why"),
    [
        ("tests/test_x.py::test_missing", "defines no 'test_missing'"),
        ("tests/test_gone.py::test_plain", "tests/test_gone.py does not exist"),
        ("tests/test_x.py::helper", "not a test function"),
        ("tests/test_x.py::Helper::test_hidden", "not a test class"),
        ("tests/test_x.py::TestGroup::test_nope", "defines no 'test_nope' there"),
        ("tests/test_x.py::test_plain::test_more", "nothing is collected inside it"),
        ("tests/test_broken.py::test_", "does not parse"),
        ("tests/test_x.py", "not a pytest node id"),
        ("notes.txt::test_plain", "not a pytest node id"),
        ("../tests/test_x.py::test_plain", "no '..'"),
        ("/tests/test_x.py::test_plain", "repo-relative"),
        ("tests/test_x.py::test_plain extra", "not a pytest node id"),
        ("tests/helpers.py::test_helper", "pytest does not collect helpers.py"),
        ("other/test_y.py::test_y", "not under pytest's testpaths (tests)"),
        ("tests/test_x.py::test_cases[1]", "parametrize id [1] cannot be checked"),
        ("tests/test_x.py::test_plain[case-1]", "parametrize id"),
        ("tests/test_x.py::test_skipped", "pytest.mark.skip: a test that does not run"),
        ("tests/test_x.py::test_skipped_if", "pytest.mark.skipif"),
        ("tests/test_x.py::test_expected_to_fail", "pytest.mark.xfail"),
        ("tests/test_x.py::TestFailing::test_inside", "'TestFailing' carries pytest.mark.xfail"),
        ("tests/test_marked.py::test_any", "pytestmark carries pytest.mark.skip"),
        ("tests/test_forms.py::test_bare_mark", "carries pytest.mark.skip"),
        ("tests/test_forms.py::TestClassMarked::test_in", "'TestClassMarked''s pytestmark"),
        ("tests/test_forms.py::TestClassListMarked::test_in", "pytest.mark.xfail"),
        ("tests/test_annotated.py::test_any", "pytestmark carries pytest.mark.skip"),
    ],
)
def test_resolve_rejects_what_names_no_test(repo: Path, node_id: str, why: str):
    got = claims.resolve(node_id, repo)
    assert got is not None and why in got, got


def _git_repo(repo: Path) -> None:
    for argv in (["git", "init", "-q"], ["git", "add", "-A"]):
        subprocess.run(argv, cwd=repo, check=True)  # nosec B603 B607 -- fixed argv


def test_check_names_the_page_line_and_reason(repo: Path, capsys: pytest.CaptureFixture[str]):
    (repo / "docs").mkdir()
    (repo / "docs" / "page.md").write_text(
        "Fine. <!-- claim: tests/test_x.py::test_plain -->\n\n"
        "Overclaim. <!-- claim: tests/test_x.py::test_renamed -->\n",
        encoding="utf-8",
    )
    _git_repo(repo)
    rc = claims.main(["--root", str(repo), "check"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "docs/page.md:3: claim tests/test_x.py::test_renamed: " in out
    assert out.count(": claim ") == 1, out


def test_check_passes_and_counts_when_every_marker_resolves(
    repo: Path, capsys: pytest.CaptureFixture[str]
):
    (repo / "a.md").write_text("A. <!-- claim: tests/test_x.py::test_plain -->\n", "utf-8")
    (repo / "b.md").write_text("B. <!-- claim: tests/test_x.py::test_async -->\n", "utf-8")
    _git_repo(repo)
    assert claims.main(["--root", str(repo), "check"]) == 0
    assert "2 test reference(s) on 2 page(s), all resolve" in capsys.readouterr().out


def test_check_reads_only_tracked_markdown_by_default(repo: Path):
    (repo / "tracked.md").write_text("ok\n", encoding="utf-8")
    _git_repo(repo)
    (repo / "untracked.md").write_text("<!-- claim: tests/test_x.py::nope -->\n", "utf-8")
    assert claims.tracked_pages(repo) == ["tracked.md"]
    assert claims.main(["--root", str(repo), "check"]) == 0
    assert claims.main(["--root", str(repo), "check", "untracked.md"]) == 1


def test_a_ui_test_file_resolves_when_it_defines_a_case(repo: Path):
    ui = repo / "src" / "ui" / "__tests__"
    ui.mkdir(parents=True)
    (ui / "a.test.js").write_text("describe('x', () => { it('works', () => {}); });\n", "utf-8")
    (ui / "empty.test.js").write_text("// nothing here\n", "utf-8")
    assert claims.resolve("src/ui/__tests__/a.test.js", repo) is None
    assert "defines no test() or it() case" in (
        claims.resolve("src/ui/__tests__/empty.test.js", repo) or ""
    )
    assert "does not exist" in (claims.resolve("src/ui/__tests__/gone.test.js", repo) or "")
    assert "no '..'" in (claims.resolve("../ui/a.test.js", repo) or "")
    assert "not a pytest node id" in (claims.resolve("src/ui/a.js", repo) or "")
    for name, suite in (("s.test.js", "describe.skip"), ("x.test.js", "xdescribe")):
        (ui / name).write_text(f"{suite}('x', () => {{ it('works', () => {{}}); }});\n", "utf-8")
        got = claims.resolve(f"src/ui/__tests__/{name}", repo) or ""
        assert "has a skipped suite" in got, name
    (ui / "only.test.js").write_text("it.skip('a', () => {});\nxit('b', () => {});\n", "utf-8")
    assert "defines no test() or it() case" in (
        claims.resolve("src/ui/__tests__/only.test.js", repo) or ""
    )


def test_run_mode_runs_each_marked_test_once():
    found = [
        claims.Marker("a.md", 1, "tests/a.py::test_a"),
        claims.Marker("b.md", 2, "tests/a.py::test_a"),
        claims.Marker("b.md", 2, "src/ui/__tests__/a.test.js"),
        claims.Marker("b.md", 3, "tests/b.py::test_b"),
    ]
    argv = claims.pytest_argv(found)
    assert argv[:3] == [sys.executable, "-m", "pytest"]
    assert argv[-2:] == ["tests/a.py::test_a", "tests/b.py::test_b"]


def test_impact_entries_map_each_cited_test_file_to_its_pages():
    found = [
        claims.Marker("docs/a.md", 1, "tests/t1.py::test_a"),
        claims.Marker("AGENTS.md", 5, "tests/t1.py::TestX::test_b"),
        claims.Marker("docs/b.md", 2, "tests/t2.py::test_c[x]"),
        claims.Marker("docs/c.md", 2, ""),
    ]
    entries = claims.impact_entries(found)
    assert [(e["paths"], e["docs"]) for e in entries] == [
        (["tests/t1.py"], ["AGENTS.md", "docs/a.md"]),
        (["tests/t2.py"], ["docs/b.md"]),
    ]
    assert all(e["name"].startswith("claims:") and e["why"] for e in entries)


def test_every_marker_in_the_repo_names_a_test():
    found = claims.scan(claims.tracked_pages(ROOT), ROOT)
    assert claims.check(found, ROOT) == []


# The pages review flagged most in the v0.4.0 cycle carry markers (#1851). Removing the
# markers from one of them should be a decision, not an accident of an edit.
SEEDED_PAGES = (
    "AGENTS.md",
    "CONTRIBUTING.md",
    "ci/README.md",
    "docs/architecture/decisions/ADR-031-query-first-routing-and-engine-rule-catalog.md",
    "docs/architecture/decisions/ADR-032-one-facts-layer-renderers-only-format.md",
    "docs/understand-the-results.md",
)


@pytest.mark.parametrize("page", SEEDED_PAGES)
def test_the_seeded_pages_carry_markers(page: str):
    assert claims.markers((ROOT / page).read_text(encoding="utf-8"), page), page


def test_lint_and_pre_commit_run_the_check():
    assert "uv run python ci/claims.py check" in (ROOT / "ci" / "lint.sh").read_text()
    config = (ROOT / ".pre-commit-config.yaml").read_text()
    assert "entry: uv run python ci/claims.py check" in config
    assert "uv run python ci/readme_budget.py" in (ROOT / "ci" / "lint.sh").read_text()
    assert "entry: uv run python ci/readme_budget.py" in config
