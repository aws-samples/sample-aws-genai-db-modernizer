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
