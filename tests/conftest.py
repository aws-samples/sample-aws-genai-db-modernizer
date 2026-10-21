"""Root test configuration with Hypothesis profiles."""

import os

from hypothesis import settings

# Selected with HYPOTHESIS_PROFILE, read here so that every entry point
# (ci/test.sh, make test, a plain `uv run pytest tests/`) honours it.
# ci: fewer examples, for pull request runs (ci/test.sh sets it on PRs; see
# ci/README.md). Tests that set their own max_examples use
# tests/hypothesis_examples.scaled(n) so they shrink with it.
settings.register_profile("ci", max_examples=20, deadline=None)

# Default: the full 100 examples, for local runs and main/nightly CI.
settings.register_profile("default", max_examples=100, deadline=None)

settings.load_profile(os.getenv("HYPOTHESIS_PROFILE", "default"))

# The local API only answers loopback host names; Starlette's TestClient sends
# Host: testserver, so allow it for the test session (set before src.api.main
# is imported).
os.environ.setdefault("MODERNIZER_ALLOWED_HOSTS", "localhost,127.0.0.1,::1,testserver")


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


# ---------------------------------------------------------------------------
# Tests never call a real model (AGENTS.md). Some unit tests reach a Bedrock call
# path their patches do not cover (the reality check's boto3 validator, an analysis
# advisor's or the synthesis summary's model): without credentials botocore fails
# before sending, but with credentials (a developer machine, a runner with vended
# credentials) the request would reach Bedrock. Every request to a Bedrock endpoint
# is refused here, before it is signed or sent. A botocore Stubber or a patched
# client never gets this far, so tests that build a Bedrock client and stub its
# answers are unaffected. Covered by tests/unit/test_no_real_model_calls.py.
# ---------------------------------------------------------------------------
import botocore.endpoint  # noqa: E402


class RealModelCallBlocked(RuntimeError):
    """A test tried to send a request to a real Bedrock endpoint."""


_real_make_request = botocore.endpoint.Endpoint.make_request

# Every refusal, as (test node id, message). Code under test often swallows the
# exception (a model failure falls back to the deterministic result), so the
# exception alone does not fail the test: the session-end check below does (#1318).
MODEL_REFUSALS: list[tuple[str, str]] = []


def _refuse_bedrock_requests(self, operation_model, request_dict):
    if "bedrock" in str(getattr(self, "host", "")):
        message = (
            f"a test tried to call {self.host} ({operation_model.name}); tests never call a "
            "real model: patch the model entry point"
        )
        current = os.environ.get("PYTEST_CURRENT_TEST", "<outside a test>")
        MODEL_REFUSALS.append((current.rsplit(" (", 1)[0], message))
        raise RealModelCallBlocked(message)
    return _real_make_request(self, operation_model, request_dict)


botocore.endpoint.Endpoint.make_request = _refuse_bedrock_requests  # type: ignore[method-assign]


@pytest.fixture(autouse=True)
def forget_expected_model_refusals(request):
    """A test marked `expects_model_refusal` tests the guard itself: its refusals do not count."""
    before = len(MODEL_REFUSALS)
    yield
    if request.node.get_closest_marker("expects_model_refusal"):
        del MODEL_REFUSALS[before:]


def pytest_sessionfinish(session, exitstatus):
    """Fail the run if any test reached the refusal above, even if it swallowed the error."""
    config = session.config
    if hasattr(config, "workeroutput"):  # an xdist worker: hand the list to the controller
        config.workeroutput["model_refusals"] = list(MODEL_REFUSALS)
        return
    offenders = list(MODEL_REFUSALS) + list(getattr(config, "_worker_model_refusals", []))
    if not offenders:
        return
    lines = sorted({f"  {node}" for node, _ in offenders})
    reporter = config.pluginmanager.get_plugin("terminalreporter")
    text = (
        f"\n{len(lines)} test(s) reached the Bedrock guard in tests/conftest.py "
        "(they depend on a model call failing; patch the model entry point instead):\n"
        + "\n".join(lines)
    )
    if reporter is not None:
        reporter.write_line(text, red=True)
    else:
        print(text)
    session.exitstatus = pytest.ExitCode.TESTS_FAILED


class _CollectWorkerModelRefusals:
    """xdist controller only: collect the refusals each worker recorded."""

    def pytest_testnodedown(self, node, error):
        refusals = getattr(node, "workeroutput", {}).get("model_refusals", [])
        node.config._worker_model_refusals = [
            *getattr(node.config, "_worker_model_refusals", []),
            *(tuple(r) for r in refusals),
        ]


def pytest_configure(config):
    if config.pluginmanager.hasplugin("xdist"):
        config.pluginmanager.register(_CollectWorkerModelRefusals(), "collect-worker-refusals")
