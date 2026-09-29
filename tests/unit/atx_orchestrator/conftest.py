"""Package marker for atx_orchestrator tests.

Intentionally does NOT guard the AWS Transform SDK (``agent_builder_sdk``) with a
``collect_ignore``. Two modules here (``test_discover_subagents``,
``test_subagent_result_contract``) import that SDK, which ships in the ATX
container image but is not a project dependency. A previous collect_ignore hid
them whenever the SDK was absent — so CI, which did not install it, silently
skipped them and let issue #150 (a stale PIPELINE_TOOLS registry) merge green.

The SDK is now installed in CI (see ``.github/workflows/ci.yml``) so these tests
RUN. If the SDK is missing they must fail loudly, not vanish: a hidden test is a
hidden failure. To run them locally, install the SDK:

    uv pip install "agent-builder-sdk-aws-transform>=1.0.0"
"""
