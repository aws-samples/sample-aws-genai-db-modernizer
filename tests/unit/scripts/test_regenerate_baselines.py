"""One command regenerates every generated baseline and names the stale ones (#1163)."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import regenerate_baselines as rb
from scripts.hooks.block_baseline_edit import PROTECTED
from scripts.regenerate_baselines import Baseline

REPO = Path(__file__).resolve().parents[3]


def _hook_protected() -> dict[str, str]:
    return dict(PROTECTED)


def _fake(name: str, stale: bool, needs_reason: bool = False) -> Baseline:
    def write_argv(reason: str) -> list[str]:
        return ["write", name, reason]

    return Baseline(
        name=name,
        files=(f"{name}.json",),
        command=f"regen {name}",
        write_argv=write_argv,
        is_stale=lambda: stale,
        needs_reason=needs_reason,
    )


class _Runner:
    def __init__(self, codes: dict[str, int] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.codes = codes or {}

    def __call__(self, argv: list[str]) -> int:
        self.calls.append(argv)
        return self.codes.get(argv[1], 0)


def test_every_baseline_the_edit_hook_protects_is_regenerated_here() -> None:
    covered = {f for baseline in rb.BASELINES for f in baseline.files}
    protected = set(_hook_protected())
    # Recorded at a release with --write-released <tag>, never on a rebase.
    protected.discard("tests/contract/assess_released_snapshot.json")
    assert (
        protected <= covered
    ), f"not covered by regenerate_baselines.py: {sorted(protected - covered)}"


def test_every_file_exists_and_every_command_runs_a_real_script() -> None:
    for baseline in rb.BASELINES:
        for file in baseline.files:
            assert (REPO / file).is_file(), f"{baseline.name}: {file} is missing"
        argv = baseline.write_argv("why (#1)")
        if argv[1] == "-m":
            module = argv[2]
            assert (REPO / (module.replace(".", "/") + ".py")).is_file(), module
        else:
            assert (REPO / argv[1]).is_file(), argv
        assert baseline.command.startswith("uv run python "), baseline.command
        assert (baseline.needs_reason) == ("--reason" in argv), baseline.name


def test_the_baseline_commands_match_the_edit_hooks_commands() -> None:
    """The one place each baseline's regenerate command is documented stays in step."""
    protected = _hook_protected()
    for baseline in rb.BASELINES:
        documented = {protected[f].split(" --reason")[0] for f in baseline.files if f in protected}
        ours = baseline.command.split(" --reason")[0]
        assert documented <= {ours}, (baseline.name, documented, ours)


def test_check_names_each_stale_baseline_with_its_command(
    capsys: pytest.CaptureFixture[str],
) -> None:
    run = _Runner()
    code = rb.main(["--check"], [_fake("a", False), _fake("b", True), _fake("c", True)], run)
    out = capsys.readouterr().out
    assert code == 1 and run.calls == []
    assert "fresh: a" in out
    assert "stale: b (b.json)" in out and "regenerate with: regen b" in out
    assert "stale: c (c.json)" in out and "regenerate with: regen c" in out
    assert "stale baselines: b, c" in out
    assert 'scripts/regenerate_baselines.py --reason "' in out


def test_check_on_fresh_baselines_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    run = _Runner()
    assert rb.main(["--check"], [_fake("a", False)], run) == 0
    assert "every generated baseline is fresh" in capsys.readouterr().out
    assert run.calls == []


def test_write_regenerates_only_the_stale_ones_in_order() -> None:
    run = _Runner()
    baselines = [_fake("a", True), _fake("b", False), _fake("c", True)]
    assert rb.main([], baselines, run) == 0
    assert run.calls == [["write", "a", ""], ["write", "c", ""]]


def test_a_fresh_tree_writes_nothing() -> None:
    run = _Runner()
    assert rb.main([], [_fake("a", False), _fake("r", False, needs_reason=True)], run) == 0
    assert run.calls == []


def test_a_stale_routing_baseline_needs_a_reason_and_nothing_is_written(
    capsys: pytest.CaptureFixture[str],
) -> None:
    run = _Runner()
    baselines = [_fake("a", True), _fake("r", True, needs_reason=True)]
    assert rb.main([], baselines, run) == 2
    assert run.calls == []
    assert "--reason is required to regenerate r" in capsys.readouterr().err


def test_a_fresh_routing_baseline_does_not_need_a_reason() -> None:
    run = _Runner()
    assert rb.main([], [_fake("a", True), _fake("r", False, needs_reason=True)], run) == 0
    assert run.calls == [["write", "a", ""]]


def test_the_reason_reaches_the_routing_command() -> None:
    run = _Runner()
    assert rb.main(["--reason", " moved (#9) "], [_fake("r", True, needs_reason=True)], run) == 0
    assert run.calls == [["write", "r", "moved (#9)"]]


def test_a_failing_command_stops_the_rest(capsys: pytest.CaptureFixture[str]) -> None:
    run = _Runner({"a": 3})
    assert rb.main([], [_fake("a", True), _fake("b", True)], run) == 3
    assert run.calls == [["write", "a", ""]]
    assert "a failed (exit 3)" in capsys.readouterr().err


def test_only_limits_the_run_and_rejects_unknown_names() -> None:
    run = _Runner()
    assert rb.main(["--only", "b"], [_fake("a", True), _fake("b", True)], run) == 0
    assert run.calls == [["write", "b", ""]]
    with pytest.raises(SystemExit) as exc:
        rb.main(["--only", "nope"], [_fake("a", True)], run)
    assert exc.value.code == 2


def test_the_cheap_baselines_are_fresh_on_a_clean_checkout(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The real checks for the four baselines that need no pipeline run."""
    names = ["catalog", "assess-schemas", "contract-schema-snapshot", "value-set-snapshot"]
    args = ["--check", *[a for n in names for a in ("--only", n)]]
    assert rb.main(args) == 0, capsys.readouterr().out


def _junit(snapshot: str | None, baseline: str | None, *, error: bool = False) -> str:
    """A junit file with one case per routing guard; ``None`` is a pass, text a failure."""

    def case(cls: str, name: str, failure: str | None) -> str:
        body = ""
        if failure is not None:
            tag = "error" if error else "failure"
            body = f'<{tag} message="{failure}">{failure}</{tag}>'
        return f'<testcase classname="tests.e2e.{cls}" name="{name}[wordpress]">{body}</testcase>'

    return (
        "<testsuites><testsuite>"
        + case("test_routing_snapshot", "test_routing_snapshot_matches_the_baseline", snapshot)
        + case("test_routing_baseline", "test_routing_shape_matches_the_baseline", baseline)
        + "</testsuite></testsuites>"
    )


def test_a_routing_guard_that_fails_on_drift_means_stale() -> None:
    xml = _junit("diff\nThe snapshot is out of date, not necessarily wrong", None)
    assert rb.routing_staleness_from_junit(xml) == {
        "routing-snapshot": True,
        "routing-baseline": False,
    }
    xml = _junit(None, "wordpress: routing moved beyond the baseline:")
    assert rb.routing_staleness_from_junit(xml) == {
        "routing-snapshot": False,
        "routing-baseline": True,
    }


def test_a_routing_guard_that_passes_means_fresh() -> None:
    assert rb.routing_staleness_from_junit(_junit(None, None)) == {
        "routing-snapshot": False,
        "routing-baseline": False,
    }


@pytest.mark.parametrize("which", ["snapshot", "baseline"])
def test_a_routing_guard_failing_for_another_reason_is_an_error_not_stale(which: str) -> None:
    other = "KeyError: 'wordpress'"
    xml = _junit(other, None) if which == "snapshot" else _junit(None, other)
    with pytest.raises(RuntimeError, match="not with its drift message"):
        rb.routing_staleness_from_junit(xml)


def test_a_routing_guard_that_errors_or_is_missing_is_an_error() -> None:
    with pytest.raises(RuntimeError, match="could not run"):
        rb.routing_staleness_from_junit(_junit("boom", None, error=True))
    with pytest.raises(RuntimeError, match="could not run"):
        rb.routing_staleness_from_junit("<testsuites><testsuite/></testsuites>")


def test_the_drift_texts_are_still_in_the_guard_tests() -> None:
    """The texts this script looks for must stay in the tests that print them."""
    for file, _prefix, drift_text in rb._ROUTING_TESTS.values():
        assert drift_text in (REPO / file).read_text(), (file, drift_text)
