# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Contract bumps resolved at release, not in every pull request (#2176, ADR-008 Decision 8).

A pull request carries only its pending files. ``resolve_contract_versions.py --check
--against REF`` refuses a version, a Version History line or a snapshot entry the change
wrote by hand (``hand_set_problems``), and fails a contract change since REF's code that
no pending file of the change covers (``changes_without_own_bump``), even when main
already holds a pending file for that contract. The merge queue refuses a pending MAJOR
(``--refuse-pending-major``): a MAJOR is resolved in its own pull request. These run on
synthetic modules and snapshots; ``tests/unit/ci/test_release_resolve_scratch_repo.py``
runs the real scripts in a scratch clone.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from scripts import resolve_contract_versions as rcv
from scripts.contract_bumps import PENDING_RELDIR, PendingBump

MODULE = '''"""
A contract.

Version History:
- 1.0 (2026-01-01): Initial version.
- 1.2 (2026-03-01, #8): Added ``c``.
"""

from pydantic import BaseModel, Field


class Thing(BaseModel):
    """The thing."""

    contract_version: str = Field(default="1.2", pattern=r"^\\d+\\.\\d+$")


class Other(BaseModel):
    """Another.

    Version history:
    - 2.0: Initial version
    """

    contract_version: str = Field(default="2.0", pattern=r"^\\d+\\.\\d+$")
'''

# The resolver cannot edit Manual: no Version History has 3.0 as its newest entry, so
# it is bumped by hand (``hand_bumped_contracts``).
HAND = '''"""A contract bumped by hand."""

from pydantic import BaseModel, Field


class Manual(BaseModel):
    contract_version: str = Field(default="3.0", pattern=r"^\\d+\\.\\d+$")
'''


def _moved(text: str, version: str, entry: str | None = None) -> str:
    out = text.replace('default="1.2"', f'default="{version}"')
    if entry is not None:
        out = out.replace(
            "- 1.2 (2026-03-01, #8): Added ``c``.\n",
            f"- 1.2 (2026-03-01, #8): Added ``c``.\n- {version} (2026-11-12, #9): {entry}\n",
        )
    return out


def _bump(kind: str = "minor", name: str = "m.Thing.a.toml", contract: str = "m.Thing"):
    return PendingBump(name=name, contract=contract, kind=kind, reason=f"A {kind} change.")


def _entry(version: str, fields: str = "f") -> dict:
    return {"version": version, "schema_hash": fields, "fields": {fields: {}}}


SCHEMA = {"m.Thing": _entry("1.2"), "m.Other": _entry("2.0"), "h.Manual": _entry("3.0")}
VALUES = {"m.Kind": {"module": "m", "version": "2.0", "values": ["a"]}}


def _tree(module=MODULE, pending=(), schema=None, values=None, hand=HAND) -> rcv.Tree:
    return rcv.Tree(
        modules={"m": module, "h": hand},
        pending=tuple(pending),
        schema=SCHEMA if schema is None else schema,
        values=VALUES if values is None else values,
    )


# --- hand-set versions and Version History -----------------------------------------


def test_a_version_moved_by_hand_is_refused() -> None:
    """The per-PR resolution of a MINOR (or a hand bump) is what made parallel pull
    requests claim one number: the version stays at the last release's."""
    head = _tree(_moved(MODULE, "1.3", "Added ``d``."))
    (problem,) = rcv.hand_set_problems(_tree(), head)
    assert problem.startswith("m.Thing: contract_version moved 1.2 -> 1.3 by hand")
    assert "bump_contract.py" in problem


def test_an_unchanged_tree_and_a_pending_file_alone_pass() -> None:
    assert rcv.hand_set_problems(_tree(), _tree()) == []
    assert rcv.hand_set_problems(_tree(), _tree(pending=[_bump()])) == []


def test_the_release_resolve_of_mains_pending_files_passes() -> None:
    ref = _tree(pending=[_bump()])
    head = _tree(_moved(MODULE, "1.3", "A minor change."))
    assert rcv.hand_set_problems(ref, head) == []


def test_a_release_takes_the_strongest_pending_kind() -> None:
    """Two pending files for one contract make one bump of the strongest kind."""
    ref = _tree(pending=[_bump("minor"), _bump("major", name="m.Thing.b.toml")])
    (problem,) = rcv.hand_set_problems(ref, _tree(_moved(MODULE, "1.3", "x")))
    assert "1.2 -> 1.3" in problem and "2.0" in problem
    assert rcv.hand_set_problems(ref, _tree(_moved(MODULE, "2.0", "x"))) == []


def test_a_release_that_keeps_a_pending_file_of_that_contract_is_refused() -> None:
    ref = _tree(pending=[_bump()])
    head = _tree(_moved(MODULE, "1.3", "x"), pending=[_bump()])
    (problem,) = rcv.hand_set_problems(ref, head)
    assert "still has a pending file" in problem


def test_a_major_resolved_in_its_own_pull_request_passes() -> None:
    """A MAJOR never waits on main (the merge queue refuses a pending one), so the pull
    request that makes it resolves it, with its reader adapter."""
    assert rcv.hand_set_problems(_tree(), _tree(_moved(MODULE, "2.0", "Removed ``c``."))) == []


def test_a_version_history_edited_by_hand_is_refused() -> None:
    edited = MODULE.replace("Added ``c``.", "Added ``c`` and ``e``.")
    (problem,) = rcv.hand_set_problems(_tree(), _tree(edited))
    assert problem.startswith("m: the Version History changed by hand")


def test_a_new_model_with_its_own_history_passes() -> None:
    added = MODULE + (
        '\n\nclass New(BaseModel):\n    """New.\n\n    Version History:\n'
        '    - 1.0: Initial version\n    """\n\n'
        '    contract_version: str = Field(default="1.0", pattern=r"^\\\\d+\\\\.\\\\d+$")\n'
    )
    assert rcv.hand_set_problems(_tree(), _tree(added)) == []


def test_a_contract_the_resolver_cannot_edit_is_bumped_by_hand() -> None:
    bumped = HAND.replace('default="3.0"', 'default="3.1"')
    schema = {**SCHEMA, "h.Manual": _entry("3.1", "g")}
    assert rcv.hand_set_problems(_tree(), _tree(hand=bumped, schema=schema)) == []


# --- snapshots between releases ----------------------------------------------------


def test_a_snapshot_entry_of_a_pending_contract_written_in_a_pull_request_is_refused() -> None:
    """Regenerating it in every pull request is what conflicted; the release writes it."""
    schema = {**SCHEMA, "m.Thing": _entry("1.2", "g")}
    head = _tree(pending=[_bump()], schema=schema)
    (problem,) = rcv.hand_set_problems(_tree(), head)
    assert problem.startswith("tests/contract/schema_snapshot.json: m.Thing")
    assert "pending" in problem


def test_a_snapshot_version_written_by_hand_is_refused() -> None:
    schema = {**SCHEMA, "m.Thing": _entry("1.3")}
    (problem,) = rcv.hand_set_problems(_tree(), _tree(schema=schema))
    assert "records 1.2 -> 1.3" in problem


def test_a_regenerated_entry_with_no_pending_bump_and_no_version_move_passes() -> None:
    """A snapshot format change regenerates every entry; the snapshot test still fails a
    shape change with no bump."""
    schema = {**SCHEMA, "m.Thing": _entry("1.2", "g")}
    assert rcv.hand_set_problems(_tree(), _tree(schema=schema)) == []


def test_new_and_removed_snapshot_entries_pass() -> None:
    schema = {**{k: v for k, v in SCHEMA.items() if k != "m.Other"}, "m.New": _entry("1.0")}
    assert rcv.hand_set_problems(_tree(), _tree(schema=schema)) == []


def test_the_release_writes_the_snapshot_entries_of_what_it_resolves() -> None:
    ref = _tree(pending=[_bump()])
    head = _tree(
        _moved(MODULE, "1.3", "x"),
        schema={**SCHEMA, "m.Thing": _entry("1.3", "g")},
        values={"m.Kind": {"module": "m", "version": "2.0", "values": ["a", "b"]}},
    )
    assert rcv.hand_set_problems(ref, head) == []


def test_a_value_set_entry_of_a_pending_module_written_in_a_pull_request_is_refused() -> None:
    values = {"m.Kind": {"module": "m", "version": "2.0", "values": ["a", "b"]}}
    (problem,) = rcv.hand_set_problems(_tree(), _tree(pending=[_bump()], values=values))
    assert problem.startswith("tests/contract/value_set_snapshot.json: m.Kind")
    # The governing module decides (``query_trace`` takes ``synthesis_output``'s version).
    values = {"q.Kind": {"module": "q", "version": "2.0", "values": ["a", "b"]}}
    ref = _tree(values={"q.Kind": {"module": "q", "version": "2.0", "values": ["a"]}})
    head = _tree(pending=[_bump()], values=values)
    assert rcv.hand_set_problems(ref, head) == []
    assert rcv.hand_set_problems(ref, head, governing={"q": "m"}.get)


# --- a change since REF's code needs a pending file of its own ---------------------


class _Plain(BaseModel):
    name: str


class _More(BaseModel):
    name: str
    more: str | None = None


def _built(model: type[BaseModel], version: str = "1.0") -> dict:
    from scripts.contract_schema_snapshot import field_summary, schema_hash

    fields = field_summary(model)
    return {"version": version, "schema_hash": schema_hash(fields), "fields": fields}


def test_a_change_needs_its_own_pending_file_even_when_main_holds_one() -> None:
    """Main holds a pending MINOR of m.C from an earlier pull request, so the committed
    snapshot comparison is covered; this change adds another field and no pending file
    of its own, and the comparison with REF's code fails it (the missed bump)."""
    ref, head = {"m.C": _built(_Plain)}, {"m.C": _built(_More)}
    (problem,) = rcv.changes_without_own_bump(ref, head, {}, {}, {})
    assert problem.startswith("m.C: schema changed since main")
    assert rcv.changes_without_own_bump(ref, head, {}, {}, {"m.C": "minor"}) == []
    assert rcv.changes_without_own_bump(ref, head, {}, {}, {"m.Other": "minor"})


def test_a_value_change_needs_its_own_pending_file() -> None:
    ref = {"m.K": {"module": "m", "version": "1.0", "values": ["a"]}}
    head = {"m.K": {"module": "m", "version": "1.0", "values": ["a", "b"]}}
    (problem,) = rcv.changes_without_own_bump({}, {}, ref, head, {})
    assert "published values changed since main" in problem
    assert rcv.changes_without_own_bump({}, {}, ref, head, {"m.C": "minor"}) == []


def test_own_bumps_are_the_pending_files_ref_does_not_hold_as_they_are() -> None:
    mains = _bump("minor", name="m.Thing.a.toml")
    upgraded = _bump("major", name="m.Thing.a.toml")
    mine = _bump("minor", name="m.Thing.b.toml")
    assert rcv.own_bumps([mains], [mains, mine]) == [mine]
    assert rcv.own_bumps([mains], [upgraded]) == [upgraded]
    assert rcv.own_bumps([mains], []) == []


# --- the merge queue and the per-PR resolve ----------------------------------------


REAL = "synthesis_output.SynthesisOutputContract"


@pytest.mark.parametrize(("kind", "code"), [("minor", 0), ("major", 1)])
def test_the_merge_queue_refuses_only_a_pending_major(monkeypatch, capsys, kind, code) -> None:
    """A pending MINOR may reach main and waits for the release; a pending MAJOR would let
    artifacts of the new shape carry the old MAJOR, so it is refused."""
    bump = PendingBump(f"{REAL}.x.toml", REAL, kind, "r")
    monkeypatch.setattr(rcv, "load_pending", lambda: [bump])
    assert rcv.main(["--check"]) == 0
    assert rcv.main(["--check", "--refuse-pending-major"]) == code
    out = capsys.readouterr().out
    assert ("pending MAJOR" in out) is (code == 1)


def test_the_per_pr_resolve_takes_only_contracts_with_a_pending_major() -> None:
    """A MAJOR resolves every pending file of its contract (main's MINOR included);
    every other contract waits for the release."""
    minor_a = _bump("minor", name="m.Thing.a.toml")
    major_a = _bump("major", name="m.Thing.b.toml")
    minor_b = _bump("minor", name="m.Other.c.toml", contract="m.Other")
    assert rcv.for_this_pull_request([minor_a, major_a, minor_b]) == [minor_a, major_a]
    assert rcv.for_this_pull_request([minor_a, minor_b]) == []


def test_the_per_pr_resolve_leaves_minors_to_the_release(monkeypatch, capsys) -> None:
    def must_not_write(*args):
        raise AssertionError("the per-PR resolve wrote a MINOR")

    monkeypatch.setattr(
        rcv, "load_pending", lambda: [PendingBump(f"{REAL}.x.toml", REAL, "minor", "r")]
    )
    monkeypatch.setattr(rcv, "_apply", must_not_write)
    monkeypatch.setattr(rcv, "_write_snapshots", must_not_write)
    assert rcv.main(["--base", "HEAD"]) == 0
    assert "resolved at release" in capsys.readouterr().out


def test_the_release_needs_an_issue_to_commit(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        rcv, "load_pending", lambda: [PendingBump(f"{REAL}.x.toml", REAL, "minor", "r")]
    )
    assert rcv.main(["--release"]) == 1
    assert "--issue" in capsys.readouterr().err


def test_the_release_writes_nothing_when_a_contract_cannot_be_resolved(monkeypatch, capsys) -> None:
    """All or nothing: a release that resolved some contracts and not others would leave
    main with half its pending files."""
    good = PendingBump(f"{REAL}.x.toml", REAL, "minor", "r")
    bad = PendingBump(
        "assignment_models.ValidationResult.x.toml",
        "assignment_models.ValidationResult",
        "minor",
        "r",
    )

    class Proc:
        returncode, stdout, stderr = 0, "", ""

    def git(*args):
        if args[0] == "show":
            proc = Proc()
            proc.stdout = (rcv.REPO / args[1].split(":", 1)[1]).read_text(encoding="utf-8")
            return proc
        return Proc()

    def must_not_write(*args):
        raise AssertionError("the release wrote a contract")

    monkeypatch.setattr(rcv, "load_pending", lambda: [good, bad])
    monkeypatch.setattr(rcv, "_git", git)
    monkeypatch.setattr(rcv, "_apply", must_not_write)
    monkeypatch.setattr(rcv, "_write_snapshots", must_not_write)
    assert rcv.main(["--release", "--issue", "1"]) == 1
    err = capsys.readouterr().err
    assert "assignment_models.ValidationResult" in err and "nothing written" in err


def test_a_hand_set_problem_is_never_hidden_by_the_committed_snapshot_check(monkeypatch) -> None:
    """A change the committed-snapshot comparison already reported is not repeated by the
    comparison with REF's code; a hand-set problem of the same key still is."""
    from scripts import contract_schema_snapshot as schema
    from scripts import contract_value_snapshot as values

    monkeypatch.setattr(rcv, "_extract", lambda ref, root: None)
    monkeypatch.setattr(rcv, "_read_tree", lambda root: rcv.Tree({}))
    monkeypatch.setattr(rcv, "_built", lambda root: ({}, {}))
    monkeypatch.setattr(schema, "build_snapshot", lambda: {})
    monkeypatch.setattr(values, "build_snapshot", lambda: {})
    monkeypatch.setattr(rcv, "changes_without_own_bump", lambda *a: ["m.C: changed"])
    monkeypatch.setattr(rcv, "hand_set_problems", lambda *a: ["m.C: by hand"])
    assert rcv._since_ref("REF", [], seen={"m.C"}) == ["m.C: by hand"]
    assert rcv._since_ref("REF", []) == ["m.C: changed", "m.C: by hand"]


# --- review of #2183 ------------------------------------------------------------------


def test_an_unbumped_change_cannot_ride_on_a_release_resolve() -> None:
    """A PR commits a field with no pending file, then resolves main's pending MINOR with
    ``--release``: the version rises, so the move alone read as covering the field. A
    contract on the release path is compared at REF's version, so only a pure release
    (the shape REF's code already has) passes."""
    ref = {"m.C": _built(_Plain, "1.0")}
    rode = {"m.C": _built(_More, "1.1")}
    pure = {"m.C": _built(_Plain, "1.1")}
    assert rcv.changes_without_own_bump(ref, rode, {}, {}, {}) == []  # the hole
    (problem,) = rcv.changes_without_own_bump(ref, rode, {}, {}, {}, released={"m.C"})
    assert problem.startswith("m.C: schema changed since main")
    assert rcv.changes_without_own_bump(ref, pure, {}, {}, {}, released={"m.C"}) == []


def test_an_unbumped_value_change_cannot_ride_on_a_release_resolve() -> None:
    ref = {"m.K": {"module": "m", "version": "1.0", "values": ["a"]}}
    rode = {"m.K": {"module": "m", "version": "1.1", "values": ["a", "b"]}}
    (problem,) = rcv.changes_without_own_bump({}, {}, ref, rode, {}, released={"m.C"})
    assert "published values changed since main" in problem


def test_released_contracts_are_the_release_path_only() -> None:
    """A MAJOR resolved in its pull request is not on the release path: its own pending
    MAJOR covered every change of that contract."""
    ref = _tree(pending=[_bump()])
    assert rcv.released_contracts(ref, _tree(_moved(MODULE, "1.3", "x"))) == {"m.Thing"}
    assert rcv.released_contracts(_tree(), _tree(_moved(MODULE, "2.0", "x"))) == set()


def test_a_version_move_only_appends_to_the_version_history() -> None:
    """Once a version moves, the module's older Version History lines stay as they were:
    a resolve appends one entry, it never rewrites one."""
    released = _moved(MODULE, "1.3", "A minor change.")
    ref = _tree(pending=[_bump()])
    assert rcv.hand_set_problems(ref, _tree(released)) == []
    rewritten = released.replace("Added ``c``.", "Added ``c`` and more.")
    (problem,) = rcv.hand_set_problems(ref, _tree(rewritten))
    assert problem.startswith("m: a Version History line changed")
    major = _moved(MODULE, "2.0", "Removed ``c``.").replace("Initial version.", "First.")
    (problem,) = rcv.hand_set_problems(_tree(), _tree(major))
    assert problem.startswith("m: a Version History line changed")


def test_a_pull_request_never_edits_a_pending_file_main_holds() -> None:
    """An edit (one more space in the reason) would make main's pending file read as the
    PR's own and cover the PR's change; the release deletes it, nobody edits it."""
    mains = _bump()
    edited = PendingBump(mains.name, mains.contract, mains.kind, mains.reason + " ")
    (problem,) = rcv.hand_set_problems(_tree(pending=[mains]), _tree(pending=[edited]))
    assert problem.startswith(f"{PENDING_RELDIR}/{mains.name}: a pending file main holds")
    assert rcv.hand_set_problems(_tree(pending=[mains]), _tree(pending=[mains])) == []


def test_the_release_workflow_refuses_a_tag_with_pending_bumps() -> None:
    """A missed release resolve would ship two shapes under one released number."""
    import yaml

    workflow = rcv.REPO / ".github/workflows/release.yml"
    steps = yaml.safe_load(workflow.read_text())["jobs"]["release"]["steps"]
    runs = [s.get("run", "") for s in steps]
    (index,) = [
        i
        for i, r in enumerate(runs)
        if "resolve_contract_versions.py --check --require-resolved" in r
    ]
    publish = next(i for i, s in enumerate(steps) if "release" in str(s.get("uses", "")).lower())
    assert index < publish


FLAG = '''"""A module versioned by its history and a constant, bumped by hand.

Version History:
- 1.0 (2026-10-14): Initial version.
"""

FLAG_SCHEMA_VERSION = "1.0"
'''


def test_a_history_line_cannot_move_a_hand_bumped_module_alone() -> None:
    """Review round 2: ``model_flag`` is bumped by hand (its version is also a constant),
    so its Version History was not checked; a ``- 2.0`` line with the constant left at
    1.0 moved the value-set snapshot's version. Its newest entry must match a version the
    module declares."""
    lone = FLAG.replace("Initial version.\n", "Initial version.\n- 2.0 (2026-11-12): Changed.\n")
    ref, head = rcv.Tree({"f": FLAG}), rcv.Tree({"f": lone})
    (problem,) = rcv.hand_set_problems(ref, head)
    assert problem.startswith("f: the Version History's newest entry moved 1.0 -> 2.0")
    both = lone.replace('FLAG_SCHEMA_VERSION = "1.0"', 'FLAG_SCHEMA_VERSION = "2.0"')
    assert rcv.hand_set_problems(ref, rcv.Tree({"f": both})) == []
