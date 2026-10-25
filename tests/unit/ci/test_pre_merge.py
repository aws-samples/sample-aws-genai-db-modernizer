"""ci/pre_merge.py: the parsing and verdict logic behind ci/pre_merge.sh."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from ci.pre_merge import (
    check_contract,
    contract_versions,
    next_version,
    parse_merge_tree,
    pending_bumps,
    pick_remote,
)

PUBLIC = "https://github.com/aws-samples/sample-aws-genai-db-modernizer"


def _contract(version: str | None) -> str | None:
    if version is None:
        return None
    return f'class M:\n    contract_version: str = Field(\n        default="{version}", pattern=r"^\\d+\\.\\d+$"\n    )\n'


def test_pick_remote_prefers_the_public_repo():
    remotes = f"origin\tgit@host:me/fork.git (fetch)\nupstream\t{PUBLIC}.git (fetch)\n"
    assert pick_remote(remotes) == "upstream"


def test_pick_remote_prefers_origin_when_both_match():
    remotes = f"origin\t{PUBLIC} (fetch)\nupstream\t{PUBLIC} (fetch)\n"
    assert pick_remote(remotes) == "origin"


def test_pick_remote_falls_back_to_origin():
    assert pick_remote("mirror\thttps://example.com/x.git (fetch)\n") == "origin"
    assert pick_remote("") == "origin"


def test_parse_merge_tree_lists_conflicting_files_only():
    out = "deadbeef\nsrc/a.py\nsrc/b.py\n\nAuto-merging src/a.py\nCONFLICT (content): src/a.py\n"
    assert parse_merge_tree(out) == ["src/a.py", "src/b.py"]


def test_parse_merge_tree_clean_has_no_files():
    assert parse_merge_tree("deadbeef\n") == []


def test_contract_versions_reads_multi_line_defaults():
    assert contract_versions(_contract("2.10")) == [(2, 10)]
    assert contract_versions(None) == []
    assert contract_versions("x = 1") == []


def test_next_version_is_the_minor_bump():
    assert next_version((2, 9)) == "2.10"


@pytest.mark.parametrize(
    ("base", "main", "head", "level", "text"),
    [
        ("1.0", "1.0", "1.1", "ok", "higher"),
        ("1.0", "1.1", "1.2", "ok", "higher"),
        ("1.0", "1.1", "1.1", "error", "declare the bump instead"),
        ("1.0", "1.1", "1.1", "error", "or take 1.2 by hand"),
        ("1.0", "1.3", "1.1", "error", "lower than main's 1.3"),
        ("1.0", "1.0", "1.0", "warn", "still at 1.0"),
        ("1.0", "1.1", "1.0", "ok", "unchanged"),
        (None, None, "1.0", "ok", "higher"),
    ],
)
def test_check_contract(base, main, head, level, text):
    got_level, message = check_contract(
        "src/contracts/x.py", _contract(base), _contract(main), _contract(head)
    )
    assert got_level == level
    assert text in message


def test_check_contract_ignores_files_without_a_version():
    assert check_contract("src/contracts/x.py", "a", "b", "c")[0] == "ok"


def test_ci_workflow_reports_on_a_merge_group():
    workflow = Path(__file__).resolve().parents[3] / ".github/workflows/ci.yml"
    loaded = yaml.safe_load(workflow.read_text())
    triggers = loaded.get("on") or loaded[True]  # PyYAML reads a bare `on:` as True
    assert "merge_group" in triggers
    # Required checks must not read pull request context, which a merge group lacks.
    assert "github.event.pull_request" not in workflow.read_text().replace("\n#", "")


@pytest.mark.parametrize(
    ("base", "main", "head", "level", "text"),
    [
        ("1.0", "1.0", "1.0", "ok", "pending minor bump declared"),
        ("1.0", "1.1", "1.0", "ok", "pending minor bump declared"),
        ("1.0", "1.0", "1.1", "error", "keep one"),
    ],
)
def test_check_contract_with_a_pending_bump(base, main, head, level, text):
    """ADR-008 Decision 8 (#1158): a pending bump keeps main's version until it is
    resolved; a pending file next to a hand-written version is a mistake."""
    got_level, message = check_contract(
        "src/contracts/x.py", _contract(base), _contract(main), _contract(head), "minor"
    )
    assert got_level == level
    assert text in message


def test_pending_bumps_reads_each_file_and_flags_invalid_ones():
    texts = {
        "src/contracts/pending/a.C.x.toml": 'contract = "a.C"\nkind = "minor"\nreason = "r"\n',
        "src/contracts/pending/b.y.toml": 'contract = "b"\nkind = "major"\nreason = "r"\n',
        "src/contracts/pending/c.z.toml": 'contract = "c"\nkind = "patch"\n',
        "src/contracts/pending/d.z.toml": "contract = ",
    }
    assert pending_bumps(texts) == [
        ("a.C", "minor"),
        ("b", "major"),
        ("src/contracts/pending/c.z.toml", "invalid"),
        ("src/contracts/pending/d.z.toml", "invalid"),
    ]


# --- ADR touch rule (#1154) ---------------------------------------------------

REFEREE = "src/agents/referee/routing.py"
ADR = "docs/architecture/decisions/ADR-031-query-first-routing-and-engine-rule-catalog.md"


def test_adr_rule_ignores_branches_that_change_no_behaviour():
    from ci.pre_merge import check_adr

    changed = ["tests/unit/agents/referee/test_x.py", "ci/lib.sh", "src/agents/collector/a.py"]
    assert check_adr(changed, "")[0] == "ok"


def test_adr_rule_fails_on_referee_or_shared_behaviour_without_an_adr_or_a_statement():
    from ci.pre_merge import check_adr

    for path in (REFEREE, "src/shared/engine_names.py"):
        level, message = check_adr([path, "tests/unit/x.py"], "fix: something\n")
        assert level == "error", path
        assert path in message


def test_adr_rule_passes_when_an_adr_is_edited():
    from ci.pre_merge import check_adr

    assert check_adr([REFEREE, ADR], "")[0] == "ok"


def test_adr_rule_passes_on_a_stated_reason_in_text():
    from ci.pre_merge import check_adr

    assert check_adr([REFEREE], "body\n\nNo ADR change: pure refactor, same decisions\n")[0] == "ok"


def test_adr_rule_rejects_the_unfilled_template_placeholder_and_an_empty_reason():
    from ci.pre_merge import check_adr

    assert check_adr([REFEREE], "No ADR change: <reason>")[0] == "error"
    assert check_adr([REFEREE], "No ADR change:   \n")[0] == "error"


def test_adr_rule_does_not_count_non_python_files_under_the_paths():
    from ci.pre_merge import check_adr

    assert check_adr(["src/shared/README.md"], "")[0] == "ok"


def test_the_pr_template_has_an_adr_field_that_pre_merge_accepts():
    template = (
        Path(__file__).resolve().parents[3] / ".github" / "pull_request_template.md"
    ).read_text(encoding="utf-8")
    assert "## ADR" in template
    assert "No ADR change: <reason>" in template
