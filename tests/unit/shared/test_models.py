"""Named-role model config and per-family request shapes (#624).

Every model call in ``src/`` resolves its model through a role in
``src.shared.models`` and builds its request settings from that role plus the
model family. These tests pin three things:

* no literal model ID lives in any tracked text file under ``src/``,
  ``scripts/`` or ``pipeline/`` outside ``src/shared/models.py``;
* the defaults are the 5.5 family, in the ``us.`` inference-profile form that
  ``pipeline/iam-roles.yaml`` allows;
* a 4.x model (still reachable through the env overrides) keeps exactly the
  request settings the call sites sent before the role config existed;
* a 5.5-family model gets a valid request: no sampling parameters, no
  ``budget_tokens``, an explicit ``effort``, ``max_tokens`` above a floor, strict
  tools on structured-output roles, and never a forced ``tool_choice``.

Tests never call a real model: Bedrock clients are built offline and their
``converse_stream`` entry point is patched.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess  # nosec B404 -- git ls-files only
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from pydantic import BaseModel

from src.shared import models
from src.shared.models import (
    ADAPTIVE_MAX_TOKENS_FLOOR,
    LEGACY_MAX_TOKENS_FLOOR,
    MODEL_IDS,
    Role,
    bedrock_model_kwargs,
    build_bedrock_model,
    converse_request_fields,
    invoke_model_body,
    profile_for,
    resolve_model_id,
)

REPO_ROOT = Path(__file__).resolve().parents[3]

FIVE_FIVE_IDS = [MODEL_IDS["opus-5.5"], MODEL_IDS["sonnet-5.5"], MODEL_IDS["haiku-5.5"]]
# The 5.5-family models that reject a forced tool_choice (Haiku 5.5 accepts it).
FORCED_REJECTED_IDS = [MODEL_IDS["opus-5.5"], MODEL_IDS["sonnet-5.5"]]

# Every env var a role reads; cleared so the developer's shell cannot leak in.
_ALL_ROLE_ENV = sorted({name for role in Role for name in models.ROLE_SPECS[role].env_vars})


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in _ALL_ROLE_ENV + ["SCHEMA_AGENT_MAX_TOKENS"]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_REGION", "us-east-1")


# ---------------------------------------------------------------------------
# Guard: one place for model IDs
# ---------------------------------------------------------------------------

# Any spelled-out model ID (code, docs, config), quoted or not.
_MODEL_ID_PATTERN = re.compile(r"anthropic\.claude|claude-(opus|sonnet|haiku|fable|mythos|\d)-\d")


# Files allowed to spell a model ID: the role module itself, and the IAM policy,
# whose resource patterns follow the role defaults (a follow-up of #624).
_ALLOWED = {
    REPO_ROOT / "src" / "shared" / "models.py",
    REPO_ROOT / "pipeline" / "iam-roles.yaml",
}
_SCANNED_TOPS = ("src", "scripts", "pipeline")


def _tracked_text_files() -> list[tuple[Path, str]]:
    """Every git-tracked text file under the scanned trees, with its content.

    Tracked files (not a suffix allowlist), so templates such as ``.j2`` are
    covered too. Only a file with a NUL byte counts as binary; anything else
    is decoded with replacement characters, so a stray non-UTF-8 byte cannot
    hide a model ID from the guard.
    """
    out = subprocess.run(  # nosec B603 B607 -- fixed argv
        ["git", "ls-files", "-z", "--", *_SCANNED_TOPS],
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
    ).stdout.decode()
    files = []
    for rel in filter(None, out.split("\0")):
        path = REPO_ROOT / rel
        if path in _ALLOWED or not path.is_file():
            continue
        raw = path.read_bytes()
        if b"\0" in raw:
            continue
        files.append((path, raw.decode("utf-8", errors="replace")))
    return files


def test_guard_scans_templates_and_every_tracked_tree():
    scanned = {path.relative_to(REPO_ROOT).parts[0] for path, _ in _tracked_text_files()}
    assert scanned == set(_SCANNED_TOPS)
    assert any(path.suffix == ".j2" for path, _ in _tracked_text_files())


def test_no_literal_model_id_outside_models_module():
    offenders = []
    for path, text in _tracked_text_files():
        for lineno, line in enumerate(text.splitlines(), 1):
            if _MODEL_ID_PATTERN.search(line):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {line.strip()}")
    assert (
        not offenders
    ), "Model IDs belong in src/shared/models.py; use a Role instead:\n" + "\n".join(offenders)


def test_every_strands_agent_gets_an_explicit_model():
    """An ``Agent()`` without ``model=`` silently uses the Strands default model,
    which no role (and no literal-ID guard) can see."""
    offenders = []
    for path in (REPO_ROOT / "src").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", getattr(node.func, "attr", None))
            if name == "Agent" and not any(kw.arg == "model" for kw in node.keywords):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
    assert not offenders, "Agent() without model=: " + ", ".join(offenders)


_STRING_SOURCES = {"resolve_model_id", "getenv", "get"}


def _is_model_id_string(node: ast.AST, string_names: set[str]) -> bool:
    """A string literal, f-string, ``resolve_model_id(...)``/env lookup, or a
    name bound to one of those."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return True
    if isinstance(node, ast.JoinedStr):
        return True
    if isinstance(node, ast.Call):
        name = getattr(node.func, "id", getattr(node.func, "attr", None))
        return name in _STRING_SOURCES
    if isinstance(node, ast.Subscript):  # os.environ["..."]
        return bool(getattr(node.value, "attr", None) == "environ")
    return isinstance(node, ast.Name) and node.id in string_names


def _agent_model_string_offenders(tree: ast.AST) -> list[int]:
    string_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and _is_model_id_string(node.value, set()):
            string_names.update(t.id for t in node.targets if isinstance(t, ast.Name))
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if getattr(node.func, "id", getattr(node.func, "attr", None)) != "Agent":
            continue
        for kw in node.keywords:
            if kw.arg == "model" and _is_model_id_string(kw.value, string_names):
                offenders.append(node.lineno)
    return offenders


def test_every_strands_agent_gets_a_role_model_object():
    """``Agent(model="<id>")`` or ``Agent(model=resolve_model_id(...))`` makes
    Strands build a plain ``BedrockModel`` from the string, without the role's
    request settings or the forced-choice guard. Pass ``build_bedrock_model(...)``."""
    offenders = []
    for path, text in _tracked_text_files():
        if path.suffix == ".py" and path.parts[len(REPO_ROOT.parts)] == "src":
            offenders += [
                f"{path.relative_to(REPO_ROOT)}:{line}"
                for line in _agent_model_string_offenders(ast.parse(text))
            ]
    assert not offenders, "Agent(model=<model ID string>): " + ", ".join(offenders)


@pytest.mark.parametrize(
    "source",
    [
        'Agent(model="us.x")',
        "Agent(model=resolve_model_id(Role.SUMMARY))",
        "m = resolve_model_id(Role.SUMMARY)\nAgent(model=m)",
        'm = os.environ.get("X")\nAgent(model=m)',
        'Agent(model=f"{p}.x")',
    ],
)
def test_agent_model_string_guard_catches(source):
    assert _agent_model_string_offenders(ast.parse(source))


def test_agent_model_string_guard_allows_role_models():
    source = (
        "model = build_bedrock_model(Role.SUMMARY)\nAgent(model=model)\nAgent(model=_build_model())"
    )
    assert not _agent_model_string_offenders(ast.parse(source))


def test_bedrock_model_is_built_only_by_the_role_builder():
    """A direct ``BedrockModel(...)`` skips the role's family-valid settings
    (and the forced-choice guard); only ``RoleBedrockModel`` may construct one."""
    allowed = REPO_ROOT / "src" / "shared" / "role_bedrock_model.py"
    offenders = []
    for path, text in _tracked_text_files():
        if path.suffix != ".py" or path == allowed:
            continue
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "id", getattr(node.func, "attr", None))
                if name == "BedrockModel":
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
    assert not offenders, "Build models with shared.models.build_bedrock_model: " + ", ".join(
        offenders
    )


# ---------------------------------------------------------------------------
# Role resolution
# ---------------------------------------------------------------------------


def test_defaults_are_the_five_five_family():
    assert resolve_model_id(Role.SCHEMA_DESIGN) == "us.anthropic.claude-opus-5-5"
    assert resolve_model_id(Role.SUMMARY) == "us.anthropic.claude-sonnet-5-5"
    assert resolve_model_id(Role.VALIDATOR) == "us.anthropic.claude-sonnet-5-5"
    assert resolve_model_id(Role.ANALYSIS_ADVISOR) == "us.anthropic.claude-sonnet-5-5"
    assert resolve_model_id(Role.ATX_AGENT) == "us.anthropic.claude-sonnet-5-5"
    assert resolve_model_id(Role.LIGHT_CHECK) == "us.anthropic.claude-haiku-5-5"


def test_defaults_fit_todays_iam_policy():
    """``pipeline/iam-roles.yaml`` allows in-region inference profiles and
    ``us-*`` foundation models. A ``us.`` profile routes only to ``us-*``
    regions; a ``global.`` one does not, so it needs a policy change first."""
    policy = (REPO_ROOT / "pipeline" / "iam-roles.yaml").read_text(encoding="utf-8")
    assert '"arn:aws:bedrock:us-*::foundation-model/*"' in policy
    assert "inference-profile/*" in policy
    for role in Role:
        assert models.ROLE_SPECS[role].default_model.startswith("us.anthropic."), role


@pytest.mark.parametrize("env_var", ["SCHEMA_AGENT_MODEL_ID", "BEDROCK_MODEL_ID"])
def test_schema_design_reads_both_historical_env_vars(monkeypatch, env_var):
    monkeypatch.setenv(env_var, "custom-model")
    assert resolve_model_id(Role.SCHEMA_DESIGN) == "custom-model"


def test_first_set_env_var_wins_and_empty_is_unset(monkeypatch):
    monkeypatch.setenv("SUMMARY_MODEL_ID", "summary-model")
    assert resolve_model_id(Role.VALIDATOR) == "summary-model"
    monkeypatch.setenv("VALIDATOR_MODEL_ID", "validator-model")
    assert resolve_model_id(Role.VALIDATOR) == "validator-model"
    monkeypatch.setenv("VALIDATOR_MODEL_ID", "")
    assert resolve_model_id(Role.VALIDATOR) == "summary-model"


@pytest.mark.parametrize(
    "role,env_var",
    [
        (Role.SUMMARY, "SUMMARY_MODEL_ID"),
        (Role.LIGHT_CHECK, "LIGHT_CHECK_MODEL_ID"),
        (Role.ANALYSIS_ADVISOR, "ANALYSIS_MODEL_ID"),
        (Role.ATX_AGENT, "MODEL_ID"),
    ],
)
def test_each_role_has_an_env_override(monkeypatch, role, env_var):
    monkeypatch.setenv(env_var, "override")
    assert resolve_model_id(role) == "override"


# ---------------------------------------------------------------------------
# Family profiles
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model_id,sampling,budget,forced,effort",
    [
        ("us.anthropic.claude-opus-4-6-v1", True, True, "accepted", False),
        ("us.anthropic.claude-sonnet-4-6", True, True, "accepted", False),
        ("us.anthropic.claude-sonnet-4-5-v1", True, True, "accepted", False),
        ("us.anthropic.claude-haiku-4-5-20251001-v1:0", True, True, "accepted", False),
        ("anthropic.claude-3-haiku-20240307-v1:0", True, True, "accepted", False),
        ("anthropic.claude-opus-4-8", False, False, "accepted", True),
        ("anthropic.claude-sonnet-5", False, False, "needs_thinking_disabled", True),
        ("anthropic.claude-opus-5", False, False, "rejected", True),
        ("global.anthropic.claude-opus-5-5", False, False, "rejected", True),
        ("global.anthropic.claude-sonnet-5-5", False, False, "rejected", True),
        # Haiku 5.5 accepts a forced tool_choice (the forced turn skips thinking).
        ("global.anthropic.claude-haiku-5-5", False, False, "accepted", True),
        ("anthropic.claude-fable-5-1", False, False, "rejected", True),
        ("anthropic.claude-mythos-5-1", False, False, "rejected", True),
    ],
)
def test_profile_for(model_id, sampling, budget, forced, effort):
    profile = profile_for(model_id)
    assert profile.accepts_sampling is sampling
    assert profile.accepts_budget_tokens is budget
    assert profile.forced_tool_choice == forced
    assert profile.accepts_effort is effort


def test_unknown_model_gets_the_minimal_request_and_a_warning(caplog):
    arn = "arn:aws:bedrock:us-east-1:111122223333:application-inference-profile/abc"
    with caplog.at_level("WARNING", logger="src.shared.models"):
        profile = profile_for(arn)
    assert not profile.accepts_sampling
    assert not profile.accepts_budget_tokens
    assert not profile.accepts_forced_tool_choice
    assert not profile.accepts_effort
    assert any(arn in record.getMessage() for record in caplog.records)


# ---------------------------------------------------------------------------
# Request shape: a 4.x model keeps the pre-#624 request
# ---------------------------------------------------------------------------

#: A 4.x model per role (reachable through the role's env override).
_FOUR_X = {
    Role.SCHEMA_DESIGN: MODEL_IDS["opus-4.6"],
    Role.SUMMARY: MODEL_IDS["sonnet-4.6"],
    Role.VALIDATOR: MODEL_IDS["sonnet-4.6"],
    Role.LIGHT_CHECK: MODEL_IDS["haiku-4.5"],
    Role.ANALYSIS_ADVISOR: MODEL_IDS["sonnet-4.6"],
    Role.ATX_AGENT: MODEL_IDS["sonnet-4.6"],
}


def test_schema_design_4x_shape_unchanged():
    kwargs = bedrock_model_kwargs(Role.SCHEMA_DESIGN, model_id=MODEL_IDS["opus-4.6"])
    kwargs.pop("cache_config")  # prompt caching (#626), pinned in test_prompt_cache.py
    assert kwargs == {
        "model_id": "us.anthropic.claude-opus-4-6-v1",
        "max_tokens": 65536,
        "temperature": 1.0,
        "additional_request_fields": {"thinking": {"type": "enabled", "budget_tokens": 6000}},
    }


def test_schema_design_non_opus_4x_has_no_thinking(monkeypatch):
    monkeypatch.setenv("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-5-v1")
    kwargs = bedrock_model_kwargs(Role.SCHEMA_DESIGN, max_tokens=8192)
    assert kwargs["max_tokens"] == 8192
    assert kwargs["temperature"] == 1.0
    assert kwargs["additional_request_fields"] == {}


def test_schema_design_call_site_cap_is_raised_only_on_adaptive_families(monkeypatch):
    five_five = bedrock_model_kwargs(Role.SCHEMA_DESIGN, max_tokens=32768)
    assert five_five["max_tokens"] == 65536
    four_x = bedrock_model_kwargs(
        Role.SCHEMA_DESIGN, model_id=MODEL_IDS["opus-4.6"], max_tokens=32768
    )
    assert four_x["max_tokens"] == 32768
    monkeypatch.setenv("SCHEMA_AGENT_MAX_TOKENS", "24000")
    assert bedrock_model_kwargs(Role.SCHEMA_DESIGN, max_tokens=32768)["max_tokens"] == 24000


def test_schema_design_max_tokens_env(monkeypatch):
    monkeypatch.setenv("SCHEMA_AGENT_MAX_TOKENS", "16384")
    assert bedrock_model_kwargs(Role.SCHEMA_DESIGN)["max_tokens"] == 16384
    # A call-site default (ElastiCache uses 32768) yields to the env var too.
    assert bedrock_model_kwargs(Role.SCHEMA_DESIGN, max_tokens=32768)["max_tokens"] == 16384


def test_summary_4x_shape_unchanged():
    assert bedrock_model_kwargs(Role.SUMMARY, model_id=MODEL_IDS["sonnet-4.6"]) == {
        "model_id": "us.anthropic.claude-sonnet-4-6",
        "max_tokens": 512,
        "temperature": 0.2,
    }


def test_validator_4x_shape_unchanged():
    four_x = MODEL_IDS["sonnet-4.6"]
    assert bedrock_model_kwargs(Role.VALIDATOR, model_id=four_x) == {
        "model_id": "us.anthropic.claude-sonnet-4-6",
        "max_tokens": 4096,
        "temperature": 0.0,
    }
    assert converse_request_fields(Role.VALIDATOR, model_id=four_x) == {
        "modelId": "us.anthropic.claude-sonnet-4-6",
        "inferenceConfig": {"maxTokens": 4096, "temperature": 0.0},
    }


def test_analysis_advisor_4x_shape_unchanged():
    assert bedrock_model_kwargs(Role.ANALYSIS_ADVISOR, model_id=MODEL_IDS["sonnet-4.6"]) == {
        "model_id": "us.anthropic.claude-sonnet-4-6",
    }


def test_light_check_4x_body_has_a_sane_max_tokens():
    body = invoke_model_body(
        Role.LIGHT_CHECK, [{"role": "user", "content": "q"}], model_id=MODEL_IDS["haiku-4.5"]
    )
    assert body["anthropic_version"] == "bedrock-2023-05-31"
    assert body["max_tokens"] >= LEGACY_MAX_TOKENS_FLOOR
    assert "output_config" not in body  # Haiku 4.5 rejects effort
    assert "temperature" not in body


@pytest.mark.parametrize("role", list(Role))
def test_4x_builders_send_exactly_the_plain_bedrock_request(role):
    """On a 4.x model the role model formats the same request as a plain
    ``BedrockModel`` with the same settings, forced tool_choice included."""
    from strands.models.bedrock import BedrockModel

    model = build_bedrock_model(role, model_id=_FOUR_X[role])
    plain = BedrockModel(**bedrock_model_kwargs(role, model_id=_FOUR_X[role]))
    messages = [{"role": "user", "content": [{"text": "hi"}]}]
    spec = {"name": "out", "description": "d", "inputSchema": {"json": {"type": "object"}}}
    for choice in (None, {"auto": {}}, {"tool": {"name": "out"}}):
        expected = plain.format_request(messages, [spec], None, choice)
        # The one intended difference (#626): with prompt caching on, the role
        # model caches the stable prefix only, never the last user turn.
        for message in expected["messages"]:
            message["content"] = [b for b in message["content"] if "cachePoint" not in b]
        assert model.format_request(messages, [spec], None, choice) == expected


# ---------------------------------------------------------------------------
# Request shape: 5.5 family (the defaults)
# ---------------------------------------------------------------------------


def test_default_requests_exactly():
    """The exact settings each default sends (measured on Bedrock, #624)."""
    schema = bedrock_model_kwargs(Role.SCHEMA_DESIGN)
    cache = schema.pop("cache_config")  # prompt caching (#626), pinned in test_prompt_cache.py
    assert (cache.strategy, cache.tools_ttl) == ("auto", True)
    assert models.min_cacheable_tokens(schema["model_id"]) == 512
    assert schema == {
        "model_id": "us.anthropic.claude-opus-5-5",
        "max_tokens": 65536,
        "additional_request_fields": {"output_config": {"effort": "high"}},
    }
    assert bedrock_model_kwargs(Role.SUMMARY) == {
        "model_id": "us.anthropic.claude-sonnet-5-5",
        "max_tokens": ADAPTIVE_MAX_TOKENS_FLOOR,
        "additional_request_fields": {"output_config": {"effort": "low"}},
    }
    assert bedrock_model_kwargs(Role.VALIDATOR) == {
        "model_id": "us.anthropic.claude-sonnet-5-5",
        "max_tokens": ADAPTIVE_MAX_TOKENS_FLOOR,
        "additional_request_fields": {"output_config": {"effort": "medium"}},
    }
    assert converse_request_fields(Role.VALIDATOR) == {
        "modelId": "us.anthropic.claude-sonnet-5-5",
        "inferenceConfig": {"maxTokens": ADAPTIVE_MAX_TOKENS_FLOOR},
        "additionalModelRequestFields": {"output_config": {"effort": "medium"}},
    }
    assert bedrock_model_kwargs(Role.ANALYSIS_ADVISOR) == {
        "model_id": "us.anthropic.claude-sonnet-5-5",
        "max_tokens": ADAPTIVE_MAX_TOKENS_FLOOR,
        "additional_request_fields": {"output_config": {"effort": "medium"}},
        "strict_tools": True,
    }
    body = invoke_model_body(Role.LIGHT_CHECK, [{"role": "user", "content": "q"}])
    assert {k: v for k, v in body.items() if k != "messages"} == {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": ADAPTIVE_MAX_TOKENS_FLOOR,
        "output_config": {"effort": "low"},
    }


def _walk(value: Any):
    if isinstance(value, dict):
        for key, inner in value.items():
            yield key, inner
            yield from _walk(inner)
    elif isinstance(value, list):
        for inner in value:
            yield from _walk(inner)


_BEDROCK_ROLES = [Role.SCHEMA_DESIGN, Role.SUMMARY, Role.VALIDATOR, Role.ANALYSIS_ADVISOR]


@pytest.mark.parametrize("model_id", FIVE_FIVE_IDS)
@pytest.mark.parametrize("role", _BEDROCK_ROLES)
def test_five_five_strands_shape(role, model_id):
    kwargs = bedrock_model_kwargs(role, model_id=model_id)
    assert kwargs["model_id"] == model_id
    assert "temperature" not in kwargs and "top_p" not in kwargs
    keys = {key for key, _ in _walk(kwargs)}
    assert "budget_tokens" not in keys
    thinking = kwargs.get("additional_request_fields", {}).get("thinking")
    assert thinking is None  # adaptive is the default (and the only mode) on 5.5
    effort = kwargs["additional_request_fields"]["output_config"]["effort"]
    assert effort == models.ROLE_SPECS[role].effort
    assert kwargs["max_tokens"] >= ADAPTIVE_MAX_TOKENS_FLOOR
    expected_strict = (
        models.ROLE_SPECS[role].strict_tools
        and not profile_for(model_id).accepts_forced_tool_choice
    )
    assert kwargs.get("strict_tools", False) is expected_strict


@pytest.mark.parametrize("model_id", FIVE_FIVE_IDS)
def test_five_five_converse_shape(model_id):
    fields = converse_request_fields(Role.VALIDATOR, model_id=model_id)
    assert "temperature" not in fields["inferenceConfig"]
    assert fields["inferenceConfig"]["maxTokens"] >= ADAPTIVE_MAX_TOKENS_FLOOR
    assert fields["additionalModelRequestFields"] == {"output_config": {"effort": "medium"}}


@pytest.mark.parametrize("model_id", FIVE_FIVE_IDS)
def test_five_five_invoke_model_body(model_id):
    body = invoke_model_body(
        Role.LIGHT_CHECK, [{"role": "user", "content": "q"}], model_id=model_id
    )
    assert "temperature" not in body and "thinking" not in body
    assert body["output_config"] == {"effort": "low"}
    assert body["max_tokens"] >= ADAPTIVE_MAX_TOKENS_FLOOR


def test_opus_4_8_schema_design_opts_into_adaptive_thinking():
    kwargs = bedrock_model_kwargs(Role.SCHEMA_DESIGN, model_id="anthropic.claude-opus-4-8")
    assert kwargs["additional_request_fields"]["thinking"] == {"type": "adaptive"}
    assert "temperature" not in kwargs


def test_every_role_has_a_spec():
    assert set(models.ROLE_SPECS) == set(Role)


def test_strict_tools_only_on_structured_output_roles():
    for role, spec in models.ROLE_SPECS.items():
        assert not spec.strict_tools or spec.structured_output, role


# ---------------------------------------------------------------------------
# The forced tool_choice retry never reaches a 5.5 model
# ---------------------------------------------------------------------------


class _Verdict(BaseModel):
    ok: bool


def _stream_text(text: str) -> dict:
    return {
        "stream": [
            {"messageStart": {"role": "assistant"}},
            {"contentBlockDelta": {"delta": {"text": text}, "contentBlockIndex": 0}},
            {"contentBlockStop": {"contentBlockIndex": 0}},
            {"messageStop": {"stopReason": "end_turn"}},
            {"metadata": {"usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2}}},
        ]
    }


def _stream_tool_use(name: str, payload: dict) -> dict:
    return {
        "stream": [
            {"messageStart": {"role": "assistant"}},
            {
                "contentBlockStart": {
                    "start": {"toolUse": {"toolUseId": "tool-1", "name": name}},
                    "contentBlockIndex": 0,
                }
            },
            {
                "contentBlockDelta": {
                    "delta": {"toolUse": {"input": json.dumps(payload)}},
                    "contentBlockIndex": 0,
                }
            },
            {"contentBlockStop": {"contentBlockIndex": 0}},
            {"messageStop": {"stopReason": "tool_use"}},
            {"metadata": {"usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2}}},
        ]
    }


def _run_structured_agent(model_id: str) -> list[dict]:
    """Run a structured-output Strands agent whose first turn skips the tool.

    That is exactly the case Strands answers with a forced ``tool_choice``
    retry. Returns every request the (patched) Bedrock client received.
    """
    from strands import Agent

    model = build_bedrock_model(Role.ANALYSIS_ADVISOR, model_id=model_id)
    requests: list[dict] = []
    responses = [_stream_text("thinking out loud"), _stream_tool_use("_Verdict", {"ok": True})]

    def fake_converse_stream(**request):
        requests.append(request)
        return responses.pop(0) if responses else _stream_text("done")

    model.client = MagicMock()
    model.client.converse_stream.side_effect = fake_converse_stream
    agent = Agent(
        model=model,
        system_prompt="Answer.",
        tools=[],
        structured_output_model=_Verdict,
        callback_handler=None,
    )
    result = agent("Is this fine?")
    assert result.structured_output == _Verdict(ok=True)
    return requests


def _tool_choices(requests: list[dict]) -> list[dict]:
    return [r["toolConfig"].get("toolChoice") for r in requests if "toolConfig" in r]


@pytest.mark.parametrize("model_id", FORCED_REJECTED_IDS)
def test_forced_tool_choice_never_sent_where_rejected(model_id):
    requests = _run_structured_agent(model_id)
    assert len(requests) >= 2, "the end_turn-without-tool retry path was not exercised"
    for choice in _tool_choices(requests):
        assert "any" not in choice and "tool" not in choice, choice
    for request in requests:
        for tool in request.get("toolConfig", {}).get("tools", []):
            if "toolSpec" in tool:
                assert tool["toolSpec"].get("strict") is True
        assert "temperature" not in request.get("inferenceConfig", {})


def test_forced_tool_choice_still_used_on_4x():
    """Control: the same scenario on today's default does force the tool, so
    the test above really exercises the retry path."""
    requests = _run_structured_agent("us.anthropic.claude-sonnet-4-6")
    assert any("tool" in choice for choice in _tool_choices(requests))


def test_haiku_five_five_keeps_the_forced_retry():
    requests = _run_structured_agent(MODEL_IDS["haiku-5.5"])
    assert any("tool" in choice for choice in _tool_choices(requests))
    assert all("temperature" not in r.get("inferenceConfig", {}) for r in requests)


def test_sonnet_five_forced_retry_disables_thinking():
    """Sonnet 5 on Bedrock takes a forced choice only with thinking disabled;
    dropping the key (Strands' default) would leave adaptive thinking on."""
    requests = _run_structured_agent("anthropic.claude-sonnet-5")
    forced = [r for r in requests if "tool" in r.get("toolConfig", {}).get("toolChoice", {})]
    assert forced, "the forced retry was not exercised"
    for request in forced:
        assert request["additionalModelRequestFields"]["thinking"] == {"type": "disabled"}
        assert request["additionalModelRequestFields"]["output_config"] == {"effort": "medium"}
    unforced = [r for r in requests if r not in forced]
    for request in unforced:
        assert "thinking" not in request.get("additionalModelRequestFields", {})


def test_safe_tool_choice_downgrades_only_forced_choices():
    model = build_bedrock_model(Role.SUMMARY, model_id=MODEL_IDS["sonnet-5.5"])
    assert model.safe_tool_choice({"any": {}}) == {"auto": {}}
    assert model.safe_tool_choice({"tool": {"name": "x"}}) == {"auto": {}}
    assert model.safe_tool_choice({"auto": {}}) == {"auto": {}}
    assert model.safe_tool_choice(None) is None
    legacy = build_bedrock_model(Role.SUMMARY, model_id=MODEL_IDS["sonnet-4.6"])
    assert legacy.safe_tool_choice({"tool": {"name": "x"}}) == {"tool": {"name": "x"}}


# ---------------------------------------------------------------------------
# Call sites go through the roles
# ---------------------------------------------------------------------------


def _schema_agent(engine: str):
    from src.tools.schema import (
        aurora_mysql_schema_agent,
        aurora_postgresql_schema_agent,
        documentdb_schema_agent,
        dynamodb_schema_agent,
        elasticache_schema_agent,
        opensearch_schema_agent,
    )

    return {
        "dynamodb": dynamodb_schema_agent,
        "documentdb": documentdb_schema_agent,
        "opensearch": opensearch_schema_agent,
        "elasticache": elasticache_schema_agent,
        "aurora_mysql": aurora_mysql_schema_agent,
        "aurora_postgresql": aurora_postgresql_schema_agent,
    }[engine]


_SCHEMA_AGENTS = {
    "dynamodb": 65536,
    "documentdb": 65536,
    "opensearch": 65536,
    "elasticache": 32768,
    "aurora_mysql": 65536,
    "aurora_postgresql": 65536,
}


@pytest.mark.parametrize("engine,max_tokens", sorted(_SCHEMA_AGENTS.items()))
def test_schema_agents_keep_the_4x_request(monkeypatch, engine, max_tokens):
    monkeypatch.setenv("SCHEMA_AGENT_MODEL_ID", MODEL_IDS["opus-4.6"])
    module = _schema_agent(engine)
    with patch("src.shared.role_bedrock_model.RoleBedrockModel") as bedrock_model:
        module._build_model()
    kwargs = bedrock_model.call_args.kwargs
    assert kwargs["model_id"] == "us.anthropic.claude-opus-4-6-v1"
    assert kwargs["max_tokens"] == max_tokens
    assert kwargs["temperature"] == 1.0
    assert kwargs["additional_request_fields"] == {
        "thinking": {"type": "enabled", "budget_tokens": 6000}
    }
    assert "strict_tools" not in kwargs
    assert kwargs["boto_client_config"].read_timeout == 300


@pytest.mark.parametrize("engine,max_tokens", sorted(_SCHEMA_AGENTS.items()))
def test_schema_agents_default_to_opus_five_five(engine, max_tokens):
    module = _schema_agent(engine)
    with patch("src.shared.role_bedrock_model.RoleBedrockModel") as bedrock_model:
        module._build_model()
    kwargs = bedrock_model.call_args.kwargs
    assert kwargs["model_id"] == MODEL_IDS["opus-5.5"]
    assert "temperature" not in kwargs
    assert kwargs["additional_request_fields"] == {"output_config": {"effort": "high"}}
    # ElastiCache's 32768 is raised: on Opus 5.5 thinking shares the budget,
    # and 32768 overran on the wordpress sample (#624).
    assert kwargs["max_tokens"] == 65536
    # The structured-output retry is guarded instead (see the forced-choice tests).
    assert "strict_tools" not in kwargs


def test_summaries_and_validator_use_their_roles(monkeypatch):
    from src.agents.referee import consolidation_validator as cv

    monkeypatch.setenv("VALIDATOR_MODEL_ID", MODEL_IDS["sonnet-5.5"])
    with patch("boto3.client") as client:
        client.return_value.converse.return_value = {
            "output": {
                "message": {
                    "content": [
                        {"reasoningContent": {}},
                        {"text": '{"consolidation_corrections": []}'},
                    ]
                }
            }
        }
        assert cv._try_boto3_validator("documentdb", "dynamodb", []) == []
    kwargs = client.return_value.converse.call_args.kwargs
    assert kwargs["modelId"] == MODEL_IDS["sonnet-5.5"]
    assert "temperature" not in kwargs["inferenceConfig"]
    assert kwargs["additionalModelRequestFields"] == {"output_config": {"effort": "medium"}}


def test_light_check_reads_the_text_block(monkeypatch):
    from src.agents import answer_validation
    from src.contracts.agent_interaction_models import AgentQuestion

    monkeypatch.setenv("LIGHT_CHECK_MODEL_ID", MODEL_IDS["haiku-5.5"])
    body = MagicMock()
    body.read.return_value = json.dumps(
        {"content": [{"type": "thinking", "thinking": ""}, {"type": "text", "text": "Yes"}]}
    )
    question = MagicMock(spec=AgentQuestion)
    question.question = "Why?"
    with patch("boto3.client") as client:
        client.return_value.invoke_model.return_value = {"body": body}
        result = answer_validation._bedrock_validate(question, "Because.")
    assert result.valid
    sent = client.return_value.invoke_model.call_args.kwargs
    assert sent["modelId"] == MODEL_IDS["haiku-5.5"]
    assert "temperature" not in json.loads(sent["body"])


def _light_check(response: dict) -> Any:
    from src.agents import answer_validation
    from src.contracts.agent_interaction_models import AgentQuestion

    body = MagicMock()
    body.read.return_value = json.dumps(response)
    question = MagicMock(spec=AgentQuestion)
    question.question = "Why?"
    question.question_id = "q-1"
    question.options = None
    with patch("boto3.client") as client:
        client.return_value.invoke_model.return_value = {"body": body}
        return answer_validation.validate_answer(question, "Because.")


@pytest.mark.parametrize(
    "response",
    [
        # Haiku 5.5 safety decline: HTTP 200, stop_reason refusal, no text block.
        {"stop_reason": "refusal", "content": []},
        {"stop_reason": "refusal", "content": [{"type": "thinking", "thinking": ""}]},
        {"stop_reason": "end_turn", "content": [{"type": "text", "text": "  "}]},
    ],
)
def test_light_check_without_a_verdict_is_could_not_validate(response):
    """A refusal or empty answer says nothing about the user's answer, so it is
    accepted with a warning (like an unavailable model), never rejected."""
    result = _light_check(response)
    assert result.valid
    assert result.warnings and "could not be validated" in result.warnings[0]
    assert not result.errors


def test_light_check_no_still_rejects():
    result = _light_check({"stop_reason": "end_turn", "content": [{"type": "text", "text": "No"}]})
    assert not result.valid
    assert result.errors


def test_adaptive_floor_follows_thinking_not_just_the_family():
    """Opus 4.7/4.8 think only when asked; schema design asks, so its 32768
    call-site cap is raised like on 5.x. Roles without thinking keep theirs."""
    for model_id in ("anthropic.claude-opus-4-8", "anthropic.claude-opus-4-7"):
        kwargs = bedrock_model_kwargs(Role.SCHEMA_DESIGN, model_id=model_id, max_tokens=32768)
        assert kwargs["additional_request_fields"]["thinking"] == {"type": "adaptive"}
        assert kwargs["max_tokens"] == 65536
    summary = bedrock_model_kwargs(Role.SUMMARY, model_id="anthropic.claude-opus-4-8")
    assert summary["max_tokens"] == 512


def test_max_tokens_env_is_still_raised_to_the_adaptive_floor(monkeypatch):
    monkeypatch.setenv("SCHEMA_AGENT_MAX_TOKENS", "8000")
    assert bedrock_model_kwargs(Role.SCHEMA_DESIGN)["max_tokens"] == ADAPTIVE_MAX_TOKENS_FLOOR
