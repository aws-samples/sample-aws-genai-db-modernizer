"""The test session refuses every request to a real Bedrock endpoint (tests/conftest.py).

AGENTS.md: tests never call a real model. The guard sits at botocore's endpoint, so a
request is refused whatever built the client (boto3, a Strands BedrockModel), before
it is signed or sent, while a stubbed client still answers.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import boto3
import pytest
from botocore.stub import Stubber

from tests.conftest import RealModelCallBlocked

# These test tests/conftest.py itself, not src/: run against main's src/ with this
# branch's tests/ they pass, which is the point (the guard is in the test tree).


def _client(service: str = "bedrock-runtime"):
    return boto3.client(
        service,
        region_name="us-east-1",
        aws_access_key_id="testing",  # pragma: allowlist secret
        aws_secret_access_key="testing",  # pragma: allowlist secret  # nosec B106
    )


@pytest.mark.passes_on_main
@pytest.mark.expects_model_refusal
def test_a_real_bedrock_request_is_refused() -> None:
    with pytest.raises(RealModelCallBlocked):
        _client().converse(modelId="m", messages=[{"role": "user", "content": [{"text": "x"}]}])


@pytest.mark.passes_on_main
def test_a_stubbed_bedrock_client_still_answers() -> None:
    client = _client()
    with Stubber(client) as stub:
        stub.add_response("list_async_invokes", {"asyncInvokeSummaries": []})
        assert client.list_async_invokes()["asyncInvokeSummaries"] == []


@pytest.mark.passes_on_main
def test_other_services_are_not_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    import tests.conftest as conftest

    sent: list[tuple] = []

    def real(*args):
        sent.append(args)
        return "ok"

    monkeypatch.setattr(conftest, "_real_make_request", real)
    endpoint = SimpleNamespace(host="https://sts.us-east-1.amazonaws.com")
    operation = SimpleNamespace(name="GetCallerIdentity")
    assert conftest._refuse_bedrock_requests(endpoint, operation, {}) == "ok"
    assert len(sent) == 1


# The session-end check (#1318): code under test swallows the refusal, so only the
# recorded refusals can show a test that depends on a model call failing.
_CONFTEST = "from tests.conftest import *  # noqa: F401,F403\n"
_SWALLOWING_TEST = """
import boto3, pytest

def _call():
    client = boto3.client("bedrock-runtime", region_name="us-east-1",
        aws_access_key_id="x", aws_secret_access_key="y")  # pragma: allowlist secret
    try:
        client.converse(modelId="m", messages=[{"role": "user", "content": [{"text": "x"}]}])
    except Exception:
        pass

def test_swallows_the_refusal():
    _call()

@pytest.mark.expects_model_refusal
def test_marked_as_testing_the_guard():
    _call()

def test_clean():
    assert True
"""


ROOT = str(Path(__file__).resolve().parents[2])


def _run(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, *args: str):
    pytester.makeconftest(_CONFTEST)
    pytester.makepyfile(test_swallow=_SWALLOWING_TEST)
    pytester.makeini("[pytest]\nmarkers =\n    expects_model_refusal: x\n")
    monkeypatch.setenv("PYTHONPATH", ROOT)
    return pytester.runpytest_subprocess(*args, "-p", "no:cacheprovider")


pytest_plugins = ["pytester"]


@pytest.mark.parametrize("workers", [[], ["-n", "2"]])
@pytest.mark.passes_on_main
def test_a_swallowed_refusal_fails_the_session(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, workers
) -> None:
    result = _run(pytester, monkeypatch, *workers)
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    result.stdout.fnmatch_lines(["*1 test(s) reached the Bedrock guard*"])
    result.stdout.fnmatch_lines(["*test_swallow.py::test_swallows_the_refusal"])
    assert (
        "test_marked_as_testing_the_guard\n"
        not in result.stdout.str().split("reached the Bedrock")[-1]
    )
    assert "test_clean\n" not in result.stdout.str().split("reached the Bedrock")[-1]


@pytest.mark.passes_on_main
def test_a_session_without_refusals_passes(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytester.makeconftest(_CONFTEST)
    pytester.makepyfile(test_ok="def test_ok():\n    assert True\n")
    pytester.makeini("[pytest]\nmarkers =\n    expects_model_refusal: x\n")
    monkeypatch.setenv("PYTHONPATH", ROOT)
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider")
    assert result.ret == pytest.ExitCode.OK


# `expects_model_refusal` switches the session-end check off for a test, so it may
# only mark the guard's own test, not any other test, class or module.
_ALLOWED_REFUSAL_MARKS = {
    ("tests/unit/test_no_real_model_calls.py", "test_a_real_bedrock_request_is_refused")
}


def _refusal_mark_uses(source: str, path: str) -> list[tuple[str, int]]:
    """Uses of the marker in `source` (decorators, pytestmark, any attribute access) that are not allowed."""
    tree = ast.parse(source)
    allowed: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and (path, node.name) in _ALLOWED_REFUSAL_MARKS:
            for decorator in node.decorator_list:
                allowed.update(id(n) for n in ast.walk(decorator))
    return [
        (path, node.lineno)
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and node.attr == "expects_model_refusal"
        and id(node) not in allowed
    ]


@pytest.mark.passes_on_main
def test_expects_model_refusal_marks_only_the_guards_own_test() -> None:
    root = Path(ROOT)
    found: list[tuple[str, int]] = []
    for file in sorted((root / "tests").rglob("*.py")):
        rel = file.relative_to(root).as_posix()
        found += _refusal_mark_uses(file.read_text(encoding="utf-8"), rel)
    assert not found, f"expects_model_refusal marks a test other than the guard's own: {found}"


@pytest.mark.parametrize(
    "source",
    [
        "import pytest\n@pytest.mark.expects_model_refusal\ndef test_other(): ...\n",
        "import pytest\n@pytest.mark.expects_model_refusal\nclass TestX:\n    def test_a(self): ...\n",
        "import pytest\npytestmark = pytest.mark.expects_model_refusal\n",
        "import pytest\npytestmark = [pytest.mark.slow, pytest.mark.expects_model_refusal]\n",
    ],
)
@pytest.mark.passes_on_main
def test_the_marker_scan_flags_function_class_and_module_marks(source: str) -> None:
    assert _refusal_mark_uses(source, "tests/unit/test_other.py")


@pytest.mark.passes_on_main
def test_the_marker_scan_allows_the_guards_own_test() -> None:
    source = (
        "import pytest\n@pytest.mark.expects_model_refusal\n"
        "def test_a_real_bedrock_request_is_refused(): ...\n"
    )
    assert not _refusal_mark_uses(source, "tests/unit/test_no_real_model_calls.py")
