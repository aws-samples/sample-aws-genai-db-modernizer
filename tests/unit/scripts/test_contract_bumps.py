"""Pending contract version bumps (#1158, ADR-008 Decision 8).

``scripts/bump_contract.py`` writes a pending file that declares a bump kind;
``scripts/resolve_contract_versions.py`` assigns the number above main's after the
last rebase. These cover the parsing, the text edits and the resolution on synthetic
modules, so no git repository is needed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import bump_contract
from scripts import resolve_contract_versions as rcv
from scripts.contract_bumps import (
    BumpError,
    PendingBump,
    already_recorded,
    bump_version,
    contract_default,
    fragment_name,
    fragment_text,
    insert_history_entry,
    kind_covers,
    load_pending,
    parse_fragment,
    pending_kinds,
    pending_module_kinds,
    set_contract_default,
)
from scripts.resolve_contract_versions import resolve_texts

MODULE = '''"""
A contract.

Version History:
- 1.0 (2026-01-01): Initial version.
- 1.1 (2026-02-01, #7): Added ``b``.

  A second paragraph of the same entry.
- 1.2 (2026-03-01, #8): Added ``c``.
"""

from pydantic import BaseModel, Field


class Thing(BaseModel):
    """The thing."""

    contract_version: str = Field(
        default="1.2",
        pattern=r"^\\d+\\.\\d+$",
    )


class Other(BaseModel):
    """Another.

    Version history:
    - 2.0: Initial version
    """

    contract_version: str = Field(default="2.0", pattern=r"^\\d+\\.\\d+$")
'''


def _bump(contract: str = "m.Thing", kind: str = "minor", reason: str = "Added ``d``.", issue=9):
    return PendingBump(
        name=f"{contract}.x.toml", contract=contract, kind=kind, reason=reason, issue=issue
    )


def _resolve(bumps, head=MODULE, base=MODULE):
    texts, done, failed = resolve_texts(bumps, lambda m: head, lambda m: base, "2026-10-19")
    assert failed == []
    return texts, done


def _failures(bumps, head=MODULE, base=MODULE):
    return resolve_texts(bumps, lambda m: head, lambda m: base, "2026-10-19")[2]


# --- versions and kinds ------------------------------------------------------------


def test_bump_version_minor_and_major() -> None:
    assert bump_version("1.41", "minor") == "1.42"
    assert bump_version("1.41", "major") == "2.0"
    with pytest.raises(BumpError):
        bump_version("1.41", "patch")
    with pytest.raises(BumpError):
        bump_version("1", "minor")


def test_kind_covers_orders_none_minor_major() -> None:
    assert kind_covers("major", "minor") and kind_covers("minor", "minor")
    assert kind_covers("minor", "none")
    assert not kind_covers("minor", "major")


def test_pending_kinds_keep_the_strongest_per_contract_and_module() -> None:
    bumps = [
        _bump(kind="minor"),
        _bump(kind="major"),
        _bump("m.Other", "minor"),
        _bump("n", "minor"),
    ]
    kinds = pending_kinds(bumps)
    assert kinds == {"m.Thing": "major", "m.Other": "minor", "n": "minor"}
    assert pending_module_kinds(kinds) == {"m": "major", "n": "minor"}


# --- the pending file --------------------------------------------------------------


def test_a_fragment_round_trips() -> None:
    text = fragment_text("m.Thing", "minor", 'Added "d"\n  and e.', 12)
    bump = parse_fragment("m.Thing.my-branch.toml", text)
    assert (bump.contract, bump.kind, bump.reason, bump.issue) == (
        "m.Thing",
        "minor",
        'Added "d" and e.',
        12,
    )
    assert (
        parse_fragment("m.Thing.b.toml", fragment_text("m.Thing", "major", "r", None)).issue is None
    )


@pytest.mark.parametrize(
    ("name", "text", "error"),
    [
        ("m.Thing.a.toml", 'contract = "m.Thing"\nkind = "patch"\nreason = "r"\n', "`kind`"),
        ("m.Thing.a.toml", 'contract = "m.Thing"\nkind = "minor"\nreason = " "\n', "`reason`"),
        ("m.Thing.a.toml", 'kind = "minor"\nreason = "r"\n', "`contract`"),
        (
            "m.Thing.a.toml",
            'contract = "m.Thing"\nkind = "minor"\nreason = "r"\nissue = "9"\n',
            "`issue`",
        ),
        (
            "m.Thing.a.toml",
            'contract = "m.Thing"\nkind = "minor"\nreason = "r"\nversion = "1.3"\n',
            "unknown keys",
        ),
        ("other.a.toml", 'contract = "m.Thing"\nkind = "minor"\nreason = "r"\n', "file name"),
        ("m.Thing.a.toml", "contract = ", "not valid TOML"),
    ],
)
def test_a_malformed_fragment_is_refused(name: str, text: str, error: str) -> None:
    with pytest.raises(BumpError, match=error):
        parse_fragment(name, text)


def test_fragment_text_refuses_an_empty_reason_or_unknown_kind() -> None:
    with pytest.raises(BumpError):
        fragment_text("m.Thing", "minor", "  ", None)
    with pytest.raises(BumpError):
        fragment_text("m.Thing", "patch", "r", None)


def test_fragment_names_are_unique_per_branch() -> None:
    assert fragment_name("m.Thing", "feat/Add D!") == "m.Thing.feat-add-d.toml"
    assert fragment_name("m.Thing", "feat/a") != fragment_name("m.Thing", "feat/b")
    with pytest.raises(BumpError):
        fragment_name("m.Thing", "")


def test_load_pending_reads_toml_files_only(tmp_path: Path) -> None:
    assert load_pending(tmp_path / "missing") == []
    (tmp_path / "m.Thing.a.toml").write_text(fragment_text("m.Thing", "minor", "r", 1))
    (tmp_path / "notes.md").write_text("ignored")
    assert [b.name for b in load_pending(tmp_path)] == ["m.Thing.a.toml"]


# --- text edits --------------------------------------------------------------------


def test_set_contract_default_edits_only_that_class() -> None:
    edited = set_contract_default(MODULE, "Thing", "1.3")
    assert contract_default(edited, "Thing") == "1.3"
    assert contract_default(edited, "Other") == "2.0"
    with pytest.raises(BumpError):
        contract_default(MODULE, "Missing")


def test_an_entry_is_appended_after_a_multi_paragraph_history() -> None:
    edited = insert_history_entry(MODULE, "Thing", "1.2", "1.3", "2026-10-19", "#9", "Added ``d``.")
    lines = edited.splitlines()
    at = lines.index("- 1.3 (2026-10-19, #9): Added ``d``.")
    assert lines[at - 1].startswith("- 1.2 ") and lines[at + 1] == '"""'


def test_a_class_docstring_history_is_preferred_when_it_holds_the_version() -> None:
    edited = insert_history_entry(MODULE, "Other", "2.0", "2.1", "2026-10-19", "", "Added x.")
    assert "    - 2.0: Initial version\n    - 2.1 (2026-10-19): Added x.\n" in edited


def test_a_newest_first_history_with_a_v_prefix_keeps_its_style() -> None:
    source = '"""\nVersion History:\n- v2.0 (2026-02-02): Two\n- v1.0 (2026-01-21): One\n"""\n'
    edited = insert_history_entry(source, None, "2.0", "2.1", "2026-10-19", "#3", "Three")
    assert edited.startswith('"""\nVersion History:\n- v2.1 (2026-10-19, #3): Three\n- v2.0')


def test_a_long_entry_wraps_with_a_two_space_continuation() -> None:
    reason = "word " * 40
    edited = insert_history_entry(MODULE, "Thing", "1.2", "1.3", "2026-10-19", "#9", reason)
    new = edited.split("- 1.3 ", 1)[1].split('"""', 1)[0].splitlines()
    assert len(new) > 1 and all(line.startswith("  ") for line in new[1:])
    assert all(len(line) <= 88 for line in edited.splitlines())


def test_no_history_with_the_current_version_is_refused() -> None:
    with pytest.raises(BumpError, match="newest entry"):
        insert_history_entry(MODULE, "Thing", "1.1", "1.2", "2026-10-19", "", "r")


def test_a_history_closed_on_its_last_line_is_refused() -> None:
    source = '"""\nVersion History:\n- 1.0: One"""\n'
    with pytest.raises(BumpError, match="closing quotes"):
        insert_history_entry(source, None, "1.0", "1.1", "2026-10-19", "", "r")


# --- resolution --------------------------------------------------------------------


def test_resolution_takes_the_next_number_above_mains() -> None:
    texts, done = _resolve([_bump()])
    assert contract_default(texts["m"], "Thing") == "1.3"
    assert "- 1.3 (2026-10-19, #9): Added ``d``." in texts["m"]
    assert [(d.contract, d.before, d.after, d.kind) for d in done] == [
        ("m.Thing", "1.2", "1.3", "minor")
    ]


def test_resolution_counts_from_main_after_another_pr_took_the_number() -> None:
    """Two branches both declared a minor bump of the same contract: whichever is
    resolved second, after rebasing onto the first, gets the next number."""
    first = _resolve([_bump(reason="First.")])[0]["m"]
    texts, done = _resolve([_bump(reason="Second.")], head=first, base=first)
    second = texts["m"]
    assert contract_default(second, "Thing") == "1.4"
    assert second.index("- 1.3 (2026-10-19, #9): First.") < second.index(
        "- 1.4 (2026-10-19, #9): Second."
    )
    assert done[0].before == "1.3"


def test_several_pending_files_for_one_contract_make_one_bump() -> None:
    texts, done = _resolve(
        [_bump(kind="minor", reason="A.", issue=2), _bump(kind="major", reason="B.", issue=1)]
    )
    assert contract_default(texts["m"], "Thing") == "2.0"
    assert "- 2.0 (2026-10-19, #1, #2): A. B." in texts["m"]
    assert len(done) == 1


def test_two_contracts_in_one_module_are_both_resolved() -> None:
    texts, done = _resolve([_bump(), _bump("m.Other", "minor", "Added y.", None)])
    assert contract_default(texts["m"], "Thing") == "1.3"
    assert contract_default(texts["m"], "Other") == "2.1"
    assert [d.contract for d in done] == ["m.Other", "m.Thing"]


def test_a_hand_bump_with_a_pending_file_is_refused() -> None:
    hand = set_contract_default(MODULE, "Thing", "1.3")
    (failure,) = _failures([_bump()], head=hand)
    assert "by hand (1.2 on main, 1.3 here)" in failure.message and "--base" in failure.message


def test_a_contract_new_on_the_branch_needs_no_pending_bump() -> None:
    (failure,) = _failures([_bump()], base=None)
    assert "not on main" in failure.message


def test_a_history_only_module_bumps_its_history() -> None:
    source = '"""\nVersion History:\n- 1.0 (2026-01-01): One.\n"""\n'
    texts, done = _resolve([_bump("h", reason="Two.")], head=source, base=source)
    assert texts["h"].endswith('- 1.1 (2026-10-19, #9): Two.\n"""\n')
    assert done[0].after == "1.1"


def test_a_module_with_a_version_constant_is_bumped_by_hand() -> None:
    source = '"""\nVersion History:\n- 1.0 (2026-01-01): One.\n"""\nFLAG_SCHEMA_VERSION = "1.0"\n'
    (failure,) = _failures([_bump("h")], head=source, base=source)
    assert "constant" in failure.message


def test_a_contract_it_cannot_edit_does_not_block_the_others() -> None:
    """Reviewer case (#1209): one unresolvable contract used to stop the resolver
    before the PR's other bumps; now it is reported with the manual steps and the
    rest are resolved."""
    no_history = MODULE.replace("Version History:", "Notes:")
    texts, done, failed = resolve_texts(
        [_bump("m.Thing"), _bump("m.Other", reason="Added y.")],
        lambda m: no_history,
        lambda m: no_history,
        "2026-10-19",
    )
    assert [d.contract for d in done] == ["m.Other"]
    assert contract_default(texts["m"], "Other") == "2.1"
    assert [f.contract for f in failed] == ["m.Thing"]
    line, steps = rcv._failure_lines(failed)
    assert line.startswith("error: m.Thing:") and "bump this contract by hand" in steps
    assert "delete src/contracts/pending/m.Thing.x.toml" in steps


def test_a_pending_file_whose_bump_already_landed_is_refused() -> None:
    """A stacked branch keeps the original commit of the branch below it; after that
    one resolved and merged, rebasing re-adds its pending file. Its reason is then
    already in the Version History, and resolving again would bump twice."""
    landed = _resolve([_bump(reason="Added the long field ``d`` " + "word " * 20)])[0]["m"]
    (failure,) = _failures(
        [_bump(reason="Added the long field ``d`` " + "word " * 20)], head=landed, base=landed
    )
    assert "already landed" in failure.message
    assert _resolve([_bump(reason="Something new.")], head=landed, base=landed)[1][0].after == "1.4"


def test_already_recorded_reads_only_the_version_history() -> None:
    assert already_recorded(MODULE, "Added ``c``.")
    assert already_recorded(MODULE, "Added ``b``. A second paragraph of the same entry.")
    assert already_recorded(MODULE, "Initial version")  # a class docstring's history
    assert not already_recorded(MODULE, "The thing.")  # a docstring, not a history entry
    assert not already_recorded(MODULE, "Added")  # part of a word sequence, not an entry part
    assert not already_recorded(MODULE, "  ")
    two = _resolve([_bump(reason="First part."), _bump(reason="Second part.")])[0]["m"]
    assert already_recorded(two, "First part.") and already_recorded(two, "Second part.")


# --- the two scripts' checks -------------------------------------------------------


def test_bump_contract_names_one_model_or_asks_which() -> None:
    keys = ["m.Thing", "m.Other", "n.Only"]
    assert bump_contract.resolve_contract("n", keys) == "n.Only"
    assert bump_contract.resolve_contract("m.Thing", keys) == "m.Thing"
    with pytest.raises(BumpError, match="name one"):
        bump_contract.resolve_contract("m", keys)
    with pytest.raises(BumpError, match="not a versioned contract"):
        bump_contract.resolve_contract("m.Missing", keys)


def test_bump_contract_accepts_the_real_synthesis_output_module() -> None:
    assert bump_contract.resolve_contract("synthesis_output", bump_contract.versioned_keys()) == (
        "synthesis_output.SynthesisOutputContract"
    )
    with pytest.raises(BumpError, match="constant"):
        bump_contract.resolve_contract("model_flag", bump_contract.versioned_keys())
    assert (
        bump_contract.resolve_contract("move_ledger", bump_contract.versioned_keys())
        == "move_ledger"
    )


def test_bump_contract_writes_only_the_pending_file(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(bump_contract, "PENDING_DIR", tmp_path / "pending")
    code = bump_contract.main(
        ["synthesis_output", "minor", "--reason", "Added x.", "--issue", "5", "--slug", "feat/x"]
    )
    assert code == 0
    (written,) = (tmp_path / "pending").iterdir()
    assert written.name == "synthesis_output.SynthesisOutputContract.feat-x.toml"
    assert parse_fragment(written.name, written.read_text()).kind == "minor"
    assert "resolve_contract_versions.py" in capsys.readouterr().out


def test_check_accepts_pending_files_and_require_resolved_refuses_them(monkeypatch, capsys) -> None:
    real = PendingBump(
        "synthesis_output.SynthesisOutputContract.x.toml",
        "synthesis_output.SynthesisOutputContract",
        "minor",
        "r",
    )
    monkeypatch.setattr(rcv, "load_pending", lambda: [real])
    assert rcv.main(["--check"]) == 0
    assert rcv.main(["--check", "--require-resolved"]) == 1
    assert "pending contract bump" in capsys.readouterr().out
    monkeypatch.setattr(rcv, "load_pending", lambda: [])
    assert rcv.main(["--require-resolved"]) == 0


def test_bump_contract_refuses_a_contract_it_cannot_resolve(tmp_path, monkeypatch, capsys) -> None:
    """Reviewer case (#1209): the Aurora design outputs have no Version History, so the
    pending file is refused when it is declared, not after review."""
    monkeypatch.setattr(bump_contract, "PENDING_DIR", tmp_path / "pending")
    code = bump_contract.main(
        ["aurora_mysql_model_output", "minor", "--reason", "x", "--slug", "s"]
    )
    assert code == 1
    assert not (tmp_path / "pending").exists()
    assert "bump this contract by hand" in capsys.readouterr().err


def test_check_refuses_a_pending_file_it_could_not_resolve(monkeypatch, capsys) -> None:
    bad = PendingBump(
        "aurora_mysql_model_output.AuroraMySQLModelOutputContract.x.toml",
        "aurora_mysql_model_output.AuroraMySQLModelOutputContract",
        "minor",
        "r",
    )
    monkeypatch.setattr(rcv, "load_pending", lambda: [bad])
    assert rcv.main(["--check"]) == 1
    assert "bump this contract by hand" in capsys.readouterr().out


def test_the_default_base_is_the_public_repos_remote(monkeypatch) -> None:
    """On a fork ``origin`` is the fork: the default base is the remote that points at
    the public repository, as in ci/pre_merge.sh."""
    remotes = (
        "origin\tgit@example.com:me/fork.git (fetch)\n"
        "upstream\thttps://github.com/aws-samples/sample-aws-genai-db-modernizer.git (fetch)\n"
    )

    class Proc:
        stdout = remotes

    monkeypatch.setattr(rcv, "_git", lambda *a: Proc())
    assert rcv.default_base() == "upstream/main"


@pytest.mark.passes_on_main
def test_check_against_head_finds_nothing_on_a_clean_tree() -> None:
    """``--against`` compares with a ref's committed snapshots; this tree's own
    snapshots match it, so there is no problem. A branch that changes a contract
    keeps main's snapshots and declares the bump (ADR-008 Decision 8), so the
    comparison reads this tree's pending bumps, as ``--check`` does (#377)."""
    assert rcv._against("HEAD", pending_kinds(load_pending())) == []


def test_check_refuses_a_pending_file_for_an_unknown_contract(monkeypatch, capsys) -> None:
    monkeypatch.setattr(rcv, "load_pending", lambda: [_bump("synthesis_output.Nope")])
    assert rcv.main(["--check"]) == 1
    assert "not a versioned contract" in capsys.readouterr().out


def test_the_merge_queue_refuses_a_pending_bump() -> None:
    """The validate-contracts job (a required check) runs the check with
    --require-resolved on every event but a pull request, so a pending file never
    reaches main."""
    import yaml

    workflow = Path(__file__).resolve().parents[3] / ".github/workflows/ci.yml"
    if not workflow.exists():  # trees without .github/workflows have nothing to check
        return
    loaded = yaml.safe_load(workflow.read_text())
    assert "merge_group" in (loaded.get("on") or loaded[True])  # PyYAML reads `on:` as True
    job = loaded["jobs"]["validate-contracts"]
    (step,) = [
        s for s in job["steps"] if "resolve_contract_versions.py --check" in s.get("run", "")
    ]
    assert "$REQUIRE_RESOLVED" in step["run"]
    assert step["env"]["REQUIRE_RESOLVED"] == (
        "${{ github.event_name != 'pull_request' && '--require-resolved' || '' }}"
    )
    # ... and compares with main's snapshots (HEAD^1 of the PR merge needs depth 2).
    assert '--against "$base"' in step["run"] and "HEAD^1" in step["run"]
    assert "merge_group.base_sha" in step["env"]["MERGE_GROUP_BASE"]
    assert job["steps"][0]["with"]["fetch-depth"] == 2
