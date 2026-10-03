"""Root test configuration with Hypothesis profiles."""

import os

from hypothesis import settings

# CI profile: fewer examples for faster pipeline runs
settings.register_profile("ci", max_examples=20, deadline=None)

# Default: full 100 examples for local development
settings.register_profile("default", max_examples=100, deadline=None)

settings.load_profile(os.getenv("HYPOTHESIS_PROFILE", "default"))


# ---------------------------------------------------------------------------
# --fail-on-skip: turn skipped tests into failures.
#
# A skipped test is a hidden failure with better manners. Three separate bugs in
# this repo hid behind silent skips/ignores (a collect_ignore that dropped the
# SDK tests, a fixture-gated skip on a fixture that was never committed, and an
# SDK skipif), so CI now runs the gated suite with this flag: any test that
# skips fails the build. Genuinely environment-dependent tests are marked
# `integration` and DESELECTED (`-m "not integration"`) rather than skipped, so
# they never reach this hook in the gated run. The separate integration run does
# NOT pass this flag, so its legitimate skips are allowed.
# ---------------------------------------------------------------------------
import pytest  # noqa: E402


# ---------------------------------------------------------------------------
# Keep plain `uv run pytest tests/` from trying to COLLECT tests/e2e/*.
#
# Those modules import playwright/pypdf (the `e2e` extra), which a bare `uv
# sync` (no `--extra e2e`) does not install -- `-m "not integration and not
# e2e"` alone can't help, because pytest has to IMPORT a module before it can
# even see its markers (see ci/test.sh, which also passes --ignore=tests/e2e
# for this exact reason).
#
# A blanket `--ignore=tests/e2e` in addopts would be simplest, but it would
# also apply when tests/e2e IS named explicitly -- breaking ci/e2e.sh, which
# runs `pytest tests/e2e ...` and `pytest tests/e2e/test_ui.py ...` directly.
# So: ignore tests/e2e by default, but not when it was asked for, either by
# naming it on the command line or by selecting it via `-m e2e`.
#
# IMPORTANT: pytest_ignore_collect is a firstresult hook -- pytest's own core
# hookimpl (which implements `--ignore=`/`--ignore-glob=`) is just another
# registered impl of the same hook, and the chain stops at the first non-None
# return. So when tests/e2e WAS asked for, this must return None (not False)
# to defer to that remaining chain -- including core's own --ignore= handling,
# which is how ci/e2e.sh's first invocation excludes test_ui.py (via
# `--ignore=tests/e2e/test_ui.py`) while still naming `tests/e2e` on the
# command line. Returning False here would short-circuit the chain and defeat
# that --ignore=.
# ---------------------------------------------------------------------------
def pytest_ignore_collect(collection_path, config):
    try:
        rel = collection_path.relative_to(config.rootpath)
    except ValueError:
        return None
    if rel.parts[:2] != ("tests", "e2e"):
        return None  # not under tests/e2e -- no opinion, let pytest decide
    args = [str(a) for a in config.invocation_params.args]
    positional = [a for a in args if not a.startswith("-")]
    if any("tests/e2e" in a for a in positional):
        return None  # explicitly named -- defer to pytest's normal handling
    if "e2e" in (config.getoption("markexpr", default="") or ""):
        return None  # explicitly selected via -m e2e -- same deferral
    return True  # bare `pytest tests/` (or similar): skip tests/e2e by default


def pytest_addoption(parser):
    parser.addoption(
        "--fail-on-skip",
        action="store_true",
        default=False,
        help="Treat any skipped test as a failure (used by the gated CI run).",
    )


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    if not item.config.getoption("--fail-on-skip"):
        return
    report = outcome.get_result()
    # xfail is an intentional, declared expectation — leave it alone. Only plain
    # skips (pytest.skip, skipif that fired, importorskip) are turned into failures.
    if report.skipped and not hasattr(report, "wasxfail"):
        report.outcome = "failed"
        reason = ""
        if isinstance(report.longrepr, tuple) and len(report.longrepr) == 3:
            reason = report.longrepr[2]
        report.longrepr = (
            f"Test was SKIPPED but --fail-on-skip is set: {reason}\n"
            "A skipped test is a hidden failure. Either make it run, or mark it "
            "`integration` so it is explicitly deselected from the gated run "
            "(-m 'not integration') rather than silently skipped."
        )
