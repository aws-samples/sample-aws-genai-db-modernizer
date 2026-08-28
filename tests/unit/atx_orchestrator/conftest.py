"""Collection guard for atx_orchestrator tests that need the AWS Transform SDK.

``agent_builder_sdk`` ships in the container image and the toolkit environment,
but it is not a project dependency, so CI (``uv sync --extra dev``) runs without
it. Two modules import the SDK at module load and would fail collection there.
Ignore only those when the SDK is absent; the rest of the atx tests run either
way because they import the atx package lazily (the SDK imports are deferred).
"""

from importlib.util import find_spec

collect_ignore: list[str] = []
if find_spec("agent_builder_sdk") is None:
    collect_ignore = [
        "test_discover_subagents.py",
        "test_subagent_result_contract.py",
    ]
