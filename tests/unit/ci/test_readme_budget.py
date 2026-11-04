# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""ci/README.md stays within its section length budget (#1851)."""

from __future__ import annotations

import subprocess  # nosec B404 -- builds a scratch git repository with fixed argv
from pathlib import Path

import pytest

from ci import readme_budget as rb

ROOT = Path(__file__).resolve().parents[3]


def page(*sections: tuple[str, str]) -> str:
    return "".join(f"{h}\n\n{body}\n\n" for h, body in sections)


def test_sections_measure_each_heading_and_its_largest_block():
    text = "Intro.\n\n## `a.sh`\n\nOne two.\n\n- item one\n- item two two\n\n### Sub\n\n| x | y |\n"
    got = {s.heading: (s.chars, s.largest_block) for s in rb.sections(text)}
    assert set(got) == {"", "## `a.sh`", "### Sub"}
    assert got["## `a.sh`"][1] == len("- item two two")
    assert got["### Sub"][1] == len("| x | y |")


def test_a_wrapped_paragraph_is_one_block():
    text = "## s\n\nline one\nline two\nline three\n"
    assert rb.sections(text)[-1].largest_block == len("line one line two line three")


def test_fenced_code_counts_toward_the_section_not_a_block_and_hides_headings():
    code = "```bash\n# not a heading\n" + "x" * 3000 + "\n```"
    (s,) = rb.sections(f"## s\n\nshort.\n\n{code}\n")
    assert s.heading == "## s" and s.chars > 3000 and s.largest_block == len("short.")


def test_claim_markers_are_not_counted():
    marker = "<!-- claim: tests/unit/ci/test_x.py::" + "t" * 2000 + " -->"
    (s,) = rb.sections(f"## s\n\nIt fails. {marker}\n")
    assert s.largest_block == len("It fails.") and s.chars < 100


def test_a_section_over_the_budget_fails():
    big = "word " * (rb.SECTION_CHARS // 5 + 10)
    found = rb.sections(
        page(("## `a.sh`", "\n\n".join([big[i : i + 900] for i in range(0, len(big), 900)])))
    )
    errs = rb.check(found, {})
    assert len(errs) == 1 and "over the 4000 budget" in errs[0] and "`a.sh`" in errs[0]


def test_a_block_over_the_budget_fails():
    found = rb.sections(page(("## `a.sh`", "x" * (rb.BLOCK_CHARS + 1))))
    errs = rb.check(found, {})
    assert len(errs) == 1 and "a block" in errs[0]


def test_a_section_within_the_budget_passes():
    assert rb.check(rb.sections(page(("## `a.sh`", "Short."))), {}) == []


def _over(chars: int, block: int = 100) -> list[rb.Section]:
    return [rb.Section("## big", chars, block)]


def test_a_section_cannot_grow_past_its_allowance_and_a_slack_one_must_be_lowered():
    allow = {"## big": (6000, 1200)}
    assert rb.check(_over(5900), allow) == []
    grown = rb.check(_over(6100), allow)
    assert len(grown) == 1 and "over its allowance of 6000" in grown[0]
    shrunk = rb.check(_over(5000), allow)
    assert len(shrunk) == 1 and "lower the allowance to 5000" in shrunk[0]


def _allowance_file(entries: dict[str, tuple[int, int]]) -> str:
    return "".join(
        f'[[allowance]]\nheading = "{h}"\nchars = {c}\nblock = {b}\n\n'
        for h, (c, b) in entries.items()
    )


def _git(repo: Path, *argv: str) -> None:
    subprocess.run(  # nosec B603 B607 -- fixed argv
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.org", *argv],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def _budget_repo(tmp_path: Path, base: dict[str, tuple[int, int]] | None, page: str) -> Path:
    """A scratch repo whose commit tagged ``base`` has ``base``'s allowances (or none)."""
    (tmp_path / "ci").mkdir()
    (tmp_path / "ci" / "README.md").write_text(page, encoding="utf-8")
    if base is not None:
        (tmp_path / "ci" / "readme_budget.toml").write_text(_allowance_file(base), "utf-8")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")
    _git(tmp_path, "tag", "base")
    return tmp_path


def _run_budget(
    repo: Path, head: dict[str, tuple[int, int]] | None, base: str | None = "base"
) -> tuple[int, str]:
    if head is not None:
        (repo / "ci" / "readme_budget.toml").write_text(_allowance_file(head), "utf-8")
    import contextlib
    import io

    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        rc = rb.main(
            (["--base", base] if base else [])
            + [
                "--page",
                str(repo / "ci" / "README.md"),
                "--allowances",
                str(repo / "ci" / "readme_budget.toml"),
            ]
        )
    return rc, out.getvalue()


BIG_PAGE = "## big\n\n" + "\n\n".join(["word " * 180] * 6) + "\n"  # about 5,400 characters


def test_an_allowance_only_goes_down_against_the_base(tmp_path: Path):
    size = rb.sections(BIG_PAGE)[-1].chars
    repo = _budget_repo(tmp_path, {"## big": (size + 100, 1200)}, BIG_PAGE)
    # The same or a lower allowance passes.
    assert _run_budget(repo, {"## big": (size + 100, 1200)})[0] == 0
    assert _run_budget(repo, {"## big": (size, 1200)})[0] == 0
    # Raising it fails, even though the page is within the raised allowance.
    rc, out = _run_budget(repo, {"## big": (size + 200, 1200)})
    assert rc == 1 and "above the base's" in out and "only goes down" in out
    rc, out = _run_budget(repo, {"## big": (size + 100, 1300)})
    assert rc == 1 and "block 1300 is above the base's 1200" in out


def test_a_new_or_renamed_heading_gets_the_default_budget(tmp_path: Path):
    size = rb.sections(BIG_PAGE)[-1].chars
    renamed = BIG_PAGE.replace("## big", "## bigger")
    repo = _budget_repo(tmp_path, {"## big": (size, 1200)}, renamed)
    rc, out = _run_budget(repo, {"## bigger": (size, 1200)})
    assert rc == 1 and "'## bigger' is not in the base" in out and "default budget" in out
    # Without the allowance the section is simply over the default budget.
    rc, out = _run_budget(repo, {})
    assert rc == 1 and "over the 4000 budget" in out


def _commit_all(repo: Path, message: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)


def test_a_base_that_predates_the_file_is_compared_with_the_copy_it_was_added_with(
    tmp_path: Path,
):
    """A base with neither the allowance file nor the script (the change that adds them,
    or a stale ref) is not a skip: the allowances cannot rise above the committed copy
    the file was added with."""
    size = rb.sections(BIG_PAGE)[-1].chars
    repo = _budget_repo(tmp_path, None, BIG_PAGE)
    (repo / "ci" / "readme_budget.toml").write_text(_allowance_file({"## big": (size, 1200)}))
    _commit_all(repo, "add the allowances")
    rc, out = _run_budget(repo, None)
    assert rc == 0 and "compared with the copy it was added with" in out
    rc, out = _run_budget(repo, {"## big": (size + 200, 1200)})
    assert rc == 1 and "above the base's" in out


def test_with_no_committed_copy_anywhere_the_comparison_says_it_was_skipped(tmp_path: Path):
    size = rb.sections(BIG_PAGE)[-1].chars
    repo = _budget_repo(tmp_path, None, BIG_PAGE)
    rc, out = _run_budget(repo, {"## big": (size, 1200)})
    assert rc == 0 and "not compared" in out and "no commit has it yet" in out


def test_a_base_with_the_script_but_no_allowance_file_is_an_error(tmp_path: Path):
    repo = _budget_repo(tmp_path, None, "## s\n\nshort\n")
    (repo / "ci" / "readme_budget.py").write_text("# the script\n", "utf-8")
    _commit_all(repo, "the script without its allowances")
    _git(repo, "tag", "-f", "base")
    rc, out = _run_budget(repo, {})
    assert rc == 2 and "moved or removed" in out


def test_a_base_copy_that_does_not_parse_is_an_error(tmp_path: Path):
    repo = _budget_repo(tmp_path, None, "## s\n\nshort\n")
    (repo / "ci" / "readme_budget.toml").write_text("[[allowance]\nbroken", "utf-8")
    _commit_all(repo, "broken allowances")
    _git(repo, "tag", "-f", "base")
    rc, out = _run_budget(repo, {})
    assert rc == 2 and "does not parse" in out


def test_a_missing_base_ref_warns_and_skips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A clone without the ref (single-branch CI clone, an upstream not named origin)."""
    monkeypatch.delenv("README_BUDGET_REQUIRE_BASE", raising=False)
    monkeypatch.delenv("README_BUDGET_BASE", raising=False)
    repo = _budget_repo(tmp_path, None, "## s\n\nshort\n")
    rc, out = _run_budget(repo, {}, base="no-such-ref")
    assert rc == 0 and "warning: the base ref 'no-such-ref' does not resolve" in out
    assert "not compared" in out


def test_a_missing_base_ref_is_an_error_when_the_comparison_is_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("README_BUDGET_REQUIRE_BASE", "1")
    repo = _budget_repo(tmp_path, None, "## s\n\nshort\n")
    rc, out = _run_budget(repo, {}, base="no-such-ref")
    assert rc == 2 and "README_BUDGET_REQUIRE_BASE=1 requires the comparison" in out


def test_the_base_ref_can_be_set_by_env_and_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    size = rb.sections(BIG_PAGE)[-1].chars
    repo = _budget_repo(tmp_path, {"## big": (size, 1200)}, BIG_PAGE)
    monkeypatch.setenv("README_BUDGET_REQUIRE_BASE", "1")
    monkeypatch.setenv("README_BUDGET_BASE", "base")
    rc, out = _run_budget(repo, {"## big": (size + 200, 1200)}, base=None)
    assert rc == 1 and "above the base's" in out  # compared with the env's ref
    monkeypatch.setenv("README_BUDGET_BASE", "no-such-ref")
    rc, out = _run_budget(repo, {"## big": (size, 1200)}, base="base")
    assert rc == 0 and "compared with base's copy" in out  # the flag wins over the env


def test_the_github_lint_job_requires_the_comparison():
    import yaml

    wf = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
    steps = [s for job in wf["jobs"].values() for s in job.get("steps", [])]
    lint = [s for s in steps if s.get("run", "").strip() == "./ci/lint.sh"]
    assert lint and all(s.get("env", {}).get("README_BUDGET_REQUIRE_BASE") == "1" for s in lint)


def test_an_allowance_no_longer_needed_or_for_a_missing_section_fails():
    assert "remove its allowance" in rb.check(_over(3000), {"## big": (6000, 1200)})[0]
    errs = rb.check(_over(3000), {"## gone": (6000, 1200)})
    assert errs == ["allowance for '## gone': no such section; remove it"]


def test_a_block_allowance_is_checked_on_its_own():
    allow = {"## big": (6000, 3000)}
    assert rb.check(_over(5900, 2900), allow) == []
    assert "largest block 3100, over its allowance" in rb.check(_over(5900, 3100), allow)[0]
    assert "lower the allowance to 1200" in rb.check(_over(5900, 900), allow)[0]


def test_the_repo_readme_is_within_its_budget(capsys: pytest.CaptureFixture[str]):
    assert rb.main(["--base", "HEAD"]) == 0, capsys.readouterr().out


def test_every_allowance_names_a_section_of_the_readme():
    headings = {s.heading for s in rb.sections((ROOT / "ci" / "README.md").read_text())}
    assert set(rb.load_allowances()) <= headings
