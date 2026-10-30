# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The optional Bedrock Guardrail (#144): off by default, on for every call.

Tests never call a real model: Bedrock clients are replaced by fakes and the
model entry points are patched.
"""

from __future__ import annotations

import ast
import contextlib
import json
import os
import subprocess  # nosec B404 -- git ls-files only
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from pydantic import BaseModel

from src.shared import bedrock_guardrail as bg
from src.shared.bedrock_guardrail import GuardrailConfigError, GuardrailIntervened
from src.shared.models import (
    Role,
    bedrock_model_kwargs,
    build_bedrock_model,
    converse_request_fields,
)

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in (bg.ENV_ID, bg.ENV_VERSION, bg.ENV_TRACE, bg.ENV_STREAM_MODE):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_REGION", "us-east-1")


@pytest.fixture
def guardrail_on(monkeypatch):
    monkeypatch.setenv(bg.ENV_ID, "gr-abc123")
    monkeypatch.setenv(bg.ENV_VERSION, "3")


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def test_off_when_neither_is_set():
    assert bg.guardrail_settings() is None
    assert bg.describe_active_guardrail() is None


def test_blank_values_count_as_unset(monkeypatch):
    monkeypatch.setenv(bg.ENV_ID, "  ")
    monkeypatch.setenv(bg.ENV_VERSION, "")
    assert bg.guardrail_settings() is None


def test_both_set(guardrail_on):
    settings = bg.guardrail_settings()
    assert (settings.identifier, settings.version, settings.trace) == ("gr-abc123", "3", "enabled")
    assert bg.describe_active_guardrail() == "gr-abc123 (version 3)"


@pytest.mark.parametrize(
    "name, missing", [(bg.ENV_ID, bg.ENV_VERSION), (bg.ENV_VERSION, bg.ENV_ID)]
)
def test_one_without_the_other_is_a_startup_error(monkeypatch, name, missing):
    monkeypatch.setenv(name, "x")
    with pytest.raises(GuardrailConfigError, match=missing):
        bg.guardrail_settings()
    with pytest.raises(GuardrailConfigError):
        build_bedrock_model(Role.SUMMARY)


def test_trace_is_optional_and_validated(guardrail_on, monkeypatch):
    monkeypatch.setenv(bg.ENV_TRACE, "Enabled_Full")
    assert bg.guardrail_settings().trace == "enabled_full"
    monkeypatch.setenv(bg.ENV_TRACE, "verbose")
    with pytest.raises(GuardrailConfigError, match=bg.ENV_TRACE):
        bg.guardrail_settings()


# ---------------------------------------------------------------------------
# Requests: unchanged when off, guarded when on
# ---------------------------------------------------------------------------


def _ok_stream() -> dict:
    return {
        "stream": [
            {"messageStart": {"role": "assistant"}},
            {"contentBlockDelta": {"delta": {"text": "hi"}, "contentBlockIndex": 0}},
            {"contentBlockStop": {"contentBlockIndex": 0}},
            {"messageStop": {"stopReason": "end_turn"}},
            {"metadata": {"usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2}}},
        ]
    }


def _request_for(role: Role) -> dict:
    from strands import Agent

    model = build_bedrock_model(role)
    seen: list[dict] = []
    model.client = MagicMock()

    def fake_converse_stream(**req):
        seen.append(req)
        return _ok_stream()

    model.client.converse_stream.side_effect = fake_converse_stream
    Agent(model=model, system_prompt="s", tools=[], callback_handler=None)("hello")
    return seen[0]


# ATX_AGENT is built by the AWS Transform SDK from the model ID, not by build_bedrock_model.
_STRANDS_ROLES = [r for r in Role if r is not Role.ATX_AGENT]


@pytest.mark.parametrize("role", _STRANDS_ROLES)
def test_off_adds_nothing_to_any_request(role):
    assert not [k for k in bedrock_model_kwargs(role) if k.startswith("guardrail")]
    assert "guardrailConfig" not in converse_request_fields(role)
    assert "guardrailConfig" not in _request_for(role)


@pytest.mark.parametrize("role", _STRANDS_ROLES)
def test_on_request_is_the_off_request_plus_the_guardrail(role, monkeypatch):
    off = _request_for(role)
    monkeypatch.setenv(bg.ENV_ID, "gr-abc123")
    monkeypatch.setenv(bg.ENV_VERSION, "3")
    on = _request_for(role)
    config = on.pop("guardrailConfig")
    assert config == {
        "guardrailIdentifier": "gr-abc123",
        "guardrailVersion": "3",
        "trace": "enabled",
    }
    assert on == off, "the guardrail must be the only difference"


def test_default_stream_mode_is_sync_so_masking_works(guardrail_on):
    """Async streaming cannot mask (PII anonymize), so it is opt-in (#1062)."""
    assert "guardrail_stream_processing_mode" not in bg.strands_guardrail_kwargs()
    assert "streamProcessingMode" not in str(_request_for(Role.SCHEMA_DESIGN))


def test_stream_mode_async_is_opt_in(guardrail_on, monkeypatch):
    monkeypatch.setenv(bg.ENV_STREAM_MODE, "ASYNC")
    assert bg.strands_guardrail_kwargs()["guardrail_stream_processing_mode"] == "async"
    assert _request_for(Role.SCHEMA_DESIGN)["guardrailConfig"]["streamProcessingMode"] == "async"
    monkeypatch.setenv(bg.ENV_STREAM_MODE, "sync")
    assert "guardrail_stream_processing_mode" not in bg.strands_guardrail_kwargs()


def test_stream_mode_is_validated(guardrail_on, monkeypatch):
    monkeypatch.setenv(bg.ENV_STREAM_MODE, "fast")
    with pytest.raises(GuardrailConfigError, match=bg.ENV_STREAM_MODE):
        bg.guardrail_settings()


def test_stream_mode_without_a_guardrail_adds_nothing(monkeypatch):
    monkeypatch.setenv(bg.ENV_STREAM_MODE, "async")
    assert bg.strands_guardrail_kwargs() == {}
    assert bg.guarded_boto_config() is None


def test_non_streamed_boto3_calls_carry_no_stream_mode(guardrail_on, monkeypatch):
    monkeypatch.setenv(bg.ENV_STREAM_MODE, "async")
    assert "streamProcessingMode" not in str(bg.converse_guardrail_fields())
    assert "streamProcessingMode" not in str(bg.invoke_model_guardrail_kwargs())


def test_guarded_model_gets_a_long_read_timeout(guardrail_on):
    from botocore.config import Config

    for build in _schema_builders():
        assert build().client.meta.config.read_timeout == bg.GUARDED_READ_TIMEOUT_SECONDS
    assert build_bedrock_model(Role.SUMMARY).client.meta.config.read_timeout == (
        bg.GUARDED_READ_TIMEOUT_SECONDS
    )
    keep = Config(read_timeout=2000, connect_timeout=7)
    assert bg.guarded_boto_config(keep) is keep
    merged = bg.guarded_boto_config(Config(read_timeout=300, connect_timeout=7))
    assert (merged.read_timeout, merged.connect_timeout) == (900, 7)


def test_read_timeout_is_unchanged_without_a_guardrail():
    for build in _schema_builders():
        assert build().client.meta.config.read_timeout == 300
    assert build_bedrock_model(Role.SUMMARY).client.meta.config.read_timeout != (
        bg.GUARDED_READ_TIMEOUT_SECONDS
    )


def test_converse_and_invoke_helpers(guardrail_on):
    assert converse_request_fields(Role.VALIDATOR)["guardrailConfig"]["guardrailIdentifier"] == (
        "gr-abc123"
    )
    assert bg.invoke_model_guardrail_kwargs() == {
        "guardrailIdentifier": "gr-abc123",
        "guardrailVersion": "3",
        "trace": "ENABLED",
    }


def test_helpers_are_empty_when_off():
    assert bg.strands_guardrail_kwargs() == {}
    assert bg.converse_guardrail_fields() == {}
    assert bg.invoke_model_guardrail_kwargs() == {}


def _schema_builders():
    from src.tools.schema import (
        aurora_mysql_schema_agent,
        aurora_postgresql_schema_agent,
        documentdb_schema_agent,
        dynamodb_schema_agent,
        elasticache_schema_agent,
        opensearch_schema_agent,
    )

    return [
        m._build_model
        for m in (
            aurora_mysql_schema_agent,
            aurora_postgresql_schema_agent,
            documentdb_schema_agent,
            dynamodb_schema_agent,
            elasticache_schema_agent,
            opensearch_schema_agent,
        )
    ]


def test_every_schema_designer_model_carries_the_guardrail(guardrail_on):
    builders = _schema_builders()
    assert len(builders) == 6
    for build in builders:
        assert build().config["guardrail_id"] == "gr-abc123"
        assert build().config["guardrail_version"] == "3"


def test_advisor_summary_and_validator_agents_carry_it(guardrail_on):
    """The three other Strands call sites build their model with the role builder."""
    from src.agents.referee import consolidation_validator, reality_check_handler
    from src.tools.analysis.llm_advisor_base import LlmAdvisorBase

    built: list = []
    real = build_bedrock_model

    def spy(*args, **kwargs):
        model = real(*args, **kwargs)
        built.append(model)
        return model

    class _Agent:
        def __init__(self, **kw):
            pass

        def __call__(self, prompt):
            raise RuntimeError("stop here")

    with (
        patch("src.tools.analysis.llm_advisor_base.build_bedrock_model", spy),
        patch.object(reality_check_handler, "build_bedrock_model", spy),
        patch.object(consolidation_validator, "build_bedrock_model", spy),
        patch("strands.Agent", _Agent),
    ):
        advisor = types.SimpleNamespace(system_prompt="s", _output_model=lambda: _Out)
        LlmAdvisorBase._get_agent(advisor)  # type: ignore[arg-type]
        reality_check_handler._generate_executive_summary(
            {
                "collector_output": {"database_schema": {"tables": []}, "queries": {}},
                "assignment": {"query_assignments": []},
                "revised_assignments": [],
                "before_distribution": {},
                "after_distribution": {},
                "consolidations": [],
                "unique_value_assessment": {},
                "architectural_patterns": [],
                "recommendations": [],
            }
        )
        with pytest.raises(consolidation_validator.ValidationFailed):
            consolidation_validator._try_strands_validator("documentdb", "dynamodb", [])
    assert len(built) == 3
    assert all(m.config["guardrail_id"] == "gr-abc123" for m in built)


# ---------------------------------------------------------------------------
# Guard: no model is built, no Bedrock client is called, outside the helpers
# ---------------------------------------------------------------------------


def _tracked_py() -> list[Path]:
    out = subprocess.run(  # nosec B603 B607 -- fixed argv
        ["git", "ls-files", "-z", "--", "src", "scripts"],
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
    ).stdout.decode()
    return [REPO_ROOT / p for p in out.split("\0") if p.endswith(".py")]


_BUILDER = "build_bedrock_model"
#: The type ``build_bedrock_model`` returns; a parameter annotated with it is trusted.
_BUILDER_RETURN_TYPE = "RoleBedrockModel"
_STRANDS_MODEL_CLASSES = {
    "strands.models.bedrock.BedrockModel",
    "strands.models.BedrockModel",
}
_STRANDS_AGENTS = {"strands.Agent", "strands.agent.Agent", "strands.agent.agent.Agent"}
#: Files that may construct a model class directly: the role builder only.
_MODEL_CONSTRUCTION_ALLOWED = {"src/shared/models.py"}


def _aliases(tree: ast.AST) -> dict[str, str]:
    """Local name -> dotted import path, for every import in the file (any scope)."""
    found: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                found[alias.asname or alias.name] = f"{node.module}.{alias.name}"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                found[alias.asname or alias.name.split(".")[0]] = (
                    alias.name if alias.asname else alias.name.split(".")[0]
                )
    return found


def _dotted(node: ast.AST, aliases: dict[str, str]) -> str | None:
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value, aliases)
        return f"{base}.{node.attr}" if base else None
    return None


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    return {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}


def _call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Call):
        name: str | None = getattr(node.func, "id", getattr(node.func, "attr", None))
        return name
    return None


def _builder_functions(tree: ast.AST) -> set[str]:
    """Functions whose every ``return`` is a ``build_bedrock_model(...)`` call.

    Trust follows the value, not the name: a function called ``_build_model``
    that returns anything else is not a builder.
    """
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            returns = [n for n in ast.walk(node) if isinstance(n, ast.Return)]
            if returns and all(_call_name(r.value) == _BUILDER for r in returns if r.value):
                found.add(node.name)
    return found


def _annotation_name(annotation: ast.AST | None) -> str | None:
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        return annotation.value.rsplit(".", 1)[-1]
    if isinstance(annotation, ast.Name):
        return annotation.id
    if isinstance(annotation, ast.Attribute):
        return annotation.attr
    return None


#: ``RoleBedrockModel``'s method returning the same model with its conversation cached.
_CONVERSATION_SIBLING = "with_conversation_cache"


def _model_comes_from_the_builder(
    value: ast.AST, call: ast.Call, parents: dict, builders: set[str]
) -> bool:
    """A ``model=`` value is ``build_bedrock_model``'s result, directly or through a
    local name assigned only from it (or from a function that only returns it), or a
    parameter annotated with its return type; or such a model's
    ``with_conversation_cache()`` (#1675), the same model for a multi-turn agent."""

    def is_builder_call(node: ast.AST) -> bool:
        return _call_name(node) == _BUILDER or _call_name(node) in builders

    if is_builder_call(value):
        return True
    if (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Attribute)
        and value.func.attr == _CONVERSATION_SIBLING
        and not value.args
        and not value.keywords
    ):
        return _model_comes_from_the_builder(value.func.value, call, parents, builders)
    if not isinstance(value, ast.Name):
        return False
    scope: ast.AST | None = call
    while scope is not None and not isinstance(scope, ast.FunctionDef | ast.AsyncFunctionDef):
        scope = parents.get(scope)
    if scope is None:
        return False
    args = scope.args  # type: ignore[attr-defined]
    typed_params = {
        a.arg
        for a in [*args.posonlyargs, *args.args, *args.kwonlyargs]
        if _annotation_name(a.annotation) == _BUILDER_RETURN_TYPE
    }
    assigned = [
        n.value
        for n in ast.walk(scope)
        if isinstance(n, ast.Assign) and any(getattr(t, "id", None) == value.id for t in n.targets)
    ]
    if assigned:
        return all(is_builder_call(v) for v in assigned)
    return value.id in typed_params


def strands_model_offenders(source: str, path: str = "<probe>") -> list[str]:
    """Every way a Bedrock model gets built outside the role builder.

    * a direct ``BedrockModel(...)`` / ``RoleBedrockModel(...)`` call, under any alias;
    * ``Agent(...)`` with no ``model=`` (Strands then builds a plain ``BedrockModel``);
    * ``Agent(model=...)`` whose model is not the role builder's result (a model ID
      string, ``resolve_model_id(...)``, an env lookup, ...).

    A model is trusted only by origin: ``build_bedrock_model(...)``, a function whose
    every return is that call, a local name assigned only from either, or a parameter
    annotated ``RoleBedrockModel``. Names (``_build_model``) and unannotated parameters
    earn no trust.

    Known limits (forms this check does not follow): an ``Agent`` subclass, a
    ``functools.partial`` or stored reference to ``Agent`` or a model class, a name
    rebound after its assignment, a method reference stored and called later, and
    construction through ``getattr``/``importlib``. A new form like these needs a
    reviewer's eye; the request-level tests above still show what each role sends.
    """
    tree = ast.parse(source)
    aliases = _aliases(tree)
    parents = _parents(tree)
    builders = _builder_functions(tree)
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = _dotted(node.func, aliases) or ""
        leaf = target.rsplit(".", 1)[-1]
        where = f"{path}:{node.lineno}"
        if target in _STRANDS_MODEL_CLASSES or leaf in {"BedrockModel", "RoleBedrockModel"}:
            if path not in _MODEL_CONSTRUCTION_ALLOWED:
                offenders.append(f"{where} builds {leaf} directly")
        elif target in _STRANDS_AGENTS or target == "Agent":
            model = next((k.value for k in node.keywords if k.arg == "model"), None)
            if model is None:
                offenders.append(f"{where} Agent() without model= (Strands defaults to Bedrock)")
            elif not _model_comes_from_the_builder(model, node, parents, builders):
                offenders.append(f"{where} Agent(model=...) not from build_bedrock_model")
    return offenders


_PROBES = {
    "model id from resolve_model_id": (
        "from strands import Agent\n" "Agent(model=resolve_model_id(Role.SUMMARY))\n"
    ),
    "Agent with no model": "from strands import Agent\nAgent(system_prompt='x')\n",
    "aliased BedrockModel": (
        "from strands.models.bedrock import BedrockModel as BM\nBM(model_id='x')\n"
    ),
    "module-qualified Agent with a string": "import strands\nstrands.Agent(model='some-id')\n",
    "model variable from an env lookup": (
        "from strands import Agent\n"
        "def f():\n    m = os.environ['MODEL']\n    return Agent(model=m)\n"
    ),
    "unannotated parameter": (
        "from strands import Agent\ndef f(model):\n    return Agent(model=model)\n"
    ),
    "a function named _build_model that is not the builder": (
        "from strands import Agent\n"
        "def _build_model():\n    return os.environ['M']\n"
        "def f():\n    return Agent(model=_build_model())\n"
    ),
    "aliased Agent": "from strands import Agent as A\nA(tools=[])\n",
    "the conversation sibling of a model that is not the builder's": (
        "from strands import Agent\n"
        "def f():\n    m = os.environ['M']\n    return Agent(model=m.with_conversation_cache())\n"
    ),
}


@pytest.mark.parametrize("name", sorted(_PROBES))
def test_the_strands_guard_catches_each_probe(name):
    assert strands_model_offenders(_PROBES[name]), name


@pytest.mark.passes_on_main  # a guard: main already behaves this way
def test_the_strands_guard_allows_the_role_builder():
    ok = (
        "from strands import Agent\n"
        "from src.shared.models import build_bedrock_model\n"
        "def f():\n    model = build_bedrock_model(Role.SUMMARY)\n    return Agent(model=model)\n"
        "def g(model: RoleBedrockModel):\n    return Agent(model=model, tools=[])\n"
        "def _build_model():\n    return build_bedrock_model(Role.X)\n"
        "def h():\n    return Agent(model=_build_model())\n"
        "def k():\n    model = _build_model()\n"
        "    return Agent(model=model.with_conversation_cache())\n"
    )
    assert strands_model_offenders(ok) == []


def test_every_bedrock_model_is_built_through_the_guardrail_aware_builder():
    """No ``Agent`` or ``BedrockModel`` in ``src/`` or ``scripts/`` gets a model any
    other way than ``build_bedrock_model`` (which adds the guardrail), and
    ``bedrock_model_kwargs`` / ``converse_request_fields`` keep adding it."""
    offenders = []
    for path in _tracked_py():
        rel = path.relative_to(REPO_ROOT).as_posix()
        offenders += strands_model_offenders(path.read_text(encoding="utf-8"), rel)
    assert not offenders, "Build models with shared.models.build_bedrock_model: " + "; ".join(
        offenders
    )
    models_src = (REPO_ROOT / "src" / "shared" / "models.py").read_text(encoding="utf-8")
    assert "strands_guardrail_kwargs()" in models_src
    assert "converse_guardrail_fields()" in models_src


_RUNTIME_METHODS = {
    "converse",
    "converse_stream",
    "invoke_model",
    "invoke_model_with_response_stream",
}
_GUARDRAIL_FIELD_HELPERS = {
    "converse_request_fields",
    "converse_guardrail_fields",
    "invoke_model_guardrail_kwargs",
}


def runtime_call_offenders(source: str, path: str = "<probe>") -> list[str]:
    """Every Bedrock runtime call that does not splat a guardrail-field helper."""
    offenders = []
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _RUNTIME_METHODS
        ):
            splats = {_call_name(k.value) for k in node.keywords if k.arg is None}
            if not splats & _GUARDRAIL_FIELD_HELPERS:
                offenders.append(f"{path}:{node.lineno} .{node.func.attr}(...)")
    return offenders


def test_the_runtime_call_guard_catches_an_unguarded_second_call():
    guarded = (
        "c.converse(**converse_request_fields(Role.VALIDATOR), messages=[])\n"
        "c.invoke_model(modelId='m', **invoke_model_guardrail_kwargs())\n"
    )
    assert runtime_call_offenders(guarded) == []
    for method in sorted(_RUNTIME_METHODS):
        assert runtime_call_offenders(guarded + f"c.{method}(modelId='m', messages=[])\n")
    assert runtime_call_offenders("c.converse(**{'modelId': 'm'})\n")


def test_every_bedrock_runtime_call_passes_the_guardrail_fields():
    offenders = []
    seen = 0
    for path in _tracked_py():
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(REPO_ROOT).as_posix()
        found = runtime_call_offenders(text, rel)
        seen += len(
            [
                n
                for n in ast.walk(ast.parse(text))
                if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr in _RUNTIME_METHODS
            ]
        )
        offenders += found
    assert seen == 2, "the two known runtime calls (validator converse, answer check invoke_model)"
    assert not offenders, "Bedrock runtime call without the guardrail fields: " + "; ".join(
        offenders
    )


# ---------------------------------------------------------------------------
# Guard: no handler turns a guardrail error into a fallback
# ---------------------------------------------------------------------------

_SWALLOWING = {"Exception", "BaseException", "<bare>", "RuntimeError", "ValueError"}

#: Handlers that catch a broad class but cannot see a guardrail error, keyed by
#: ``path::function::exception types``, each with the reason.
_HANDLERS_THAT_NEVER_SEE_A_MODEL_CALL = {
    "src/tools/schema/base_schema_agent.py::_throttle_max_retries::ValueError": "parses an int from an env var",
    "src/tools/schema/base_schema_agent.py::_invoke_designer::JSONDecodeError+TypeError+ValueError": (
        "salvages text the designer already returned; no model call in the try"
    ),
}


def _handler_names(node: ast.AST | None) -> set[str]:
    if node is None:
        return {"<bare>"}
    if isinstance(node, ast.Tuple):
        return set().union(*(_handler_names(e) for e in node.elts))
    return {getattr(node, "id", getattr(node, "attr", ""))}


def _reaches_a_bedrock_call(tree: ast.AST, text: str) -> bool:
    """The module builds a Strands model or agent, opens a runtime client, or already
    handles the guardrail errors. (``strands.tools`` is a decorator, not a model.)"""
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith("strands") and not node.module.startswith("strands.tools"):
                return True
        elif isinstance(node, ast.Import):
            if any(a.name.startswith("strands") and a.name != "strands.tools" for a in node.names):
                return True
    return any(m in text for m in (_BUILDER, '"bedrock-runtime"', "GuardrailIntervened"))


def swallowing_handlers(source: str, path: str = "<probe>") -> list[str]:
    """Broad handlers (``except Exception``, bare, ``RuntimeError``, ``ValueError``)
    not preceded, in the same ``try``, by handlers for both guardrail errors.

    ``GuardrailConfigError`` must be re-raised. ``GuardrailIntervened`` must be
    re-raised or handled by its own clause (the phases with a deterministic fallback
    log it by phase). A handler whose body always re-raises is fine.

    Known limits: it looks at ``try`` statements in modules that reach a Bedrock call,
    not at callers further up (those see the error as an exception and report it), and
    it does not follow a swallowing handler in a helper that is called from the ``try``.
    """
    tree = ast.parse(source)
    parents = _parents(tree)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        config_raised = intervened_seen = False
        for handler in node.handlers:
            names = _handler_names(handler.type)
            raises = any(isinstance(x, ast.Raise) for x in ast.walk(handler))
            config_raised |= "GuardrailConfigError" in names and raises
            intervened_seen |= "GuardrailIntervened" in names
            if names & _SWALLOWING and not raises and not (config_raised and intervened_seen):
                scope = parents.get(handler)
                while scope is not None and not isinstance(
                    scope, ast.FunctionDef | ast.AsyncFunctionDef
                ):
                    scope = parents.get(scope)
                fn = scope.name if scope is not None else "<module>"  # type: ignore[attr-defined]
                found.append(f"{path}::{fn}::{'+'.join(sorted(names))}")
    return found


def test_the_handler_guard_catches_the_opensearch_shape():
    swallowing = "def f():\n    try:\n        run()\n    except Exception:\n        pass\n"
    assert swallowing_handlers(swallowing)
    only_config = (
        "def f():\n    try:\n        run()\n"
        "    except GuardrailConfigError:\n        raise\n"
        "    except Exception:\n        pass\n"
    )
    assert swallowing_handlers(only_config)
    for broad in ("RuntimeError", "ValueError", "(OSError, Exception)"):
        assert swallowing_handlers(
            f"def f():\n    try:\n        run()\n    except {broad}:\n        pass\n"
        )
    assert swallowing_handlers("def f():\n    try:\n        run()\n    except:\n        pass\n")
    ok = (
        "def f():\n    try:\n        run()\n"
        "    except (GuardrailIntervened, GuardrailConfigError):\n        raise\n"
        "    except Exception:\n        pass\n"
    )
    assert swallowing_handlers(ok) == []
    handled = (
        "def f():\n    try:\n        run()\n"
        "    except GuardrailConfigError:\n        raise\n"
        "    except GuardrailIntervened as e:\n        log(e)\n"
        "    except Exception:\n        pass\n"
    )
    assert swallowing_handlers(handled) == []


def test_no_handler_in_a_bedrock_module_swallows_a_guardrail_error():
    offenders = []
    for path in _tracked_py():
        rel = path.relative_to(REPO_ROOT).as_posix()
        text = path.read_text(encoding="utf-8")
        if rel.startswith("src/ui") or not _reaches_a_bedrock_call(ast.parse(text), text):
            continue
        offenders += swallowing_handlers(text, rel)
    unexplained = sorted(set(offenders) - set(_HANDLERS_THAT_NEVER_SEE_A_MODEL_CALL))
    assert not unexplained, (
        "Re-raise GuardrailIntervened and GuardrailConfigError before the broad handler, "
        "or list it with a reason: " + ", ".join(unexplained)
    )
    stale = sorted(set(_HANDLERS_THAT_NEVER_SEE_A_MODEL_CALL) - set(offenders))
    assert not stale, "Allowlist entries that no longer match a handler: " + ", ".join(stale)


# ---------------------------------------------------------------------------
# Interventions
# ---------------------------------------------------------------------------


class _Out(BaseModel):
    ok: bool = True


def _guardrail_stream(action: str | None, *, trace: bool = True) -> dict:
    metadata: dict = {"usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2}}
    if trace and action:
        metadata["trace"] = {
            "guardrail": {
                "outputAssessments": {
                    "gr-abc123": [
                        {"sensitiveInformationPolicy": {"piiEntities": [{"action": action}]}}
                    ]
                }
            }
        }
    return {
        "stream": [
            {"messageStart": {"role": "assistant"}},
            {"contentBlockDelta": {"delta": {"text": "x"}, "contentBlockIndex": 0}},
            {"contentBlockStop": {"contentBlockIndex": 0}},
            {"messageStop": {"stopReason": "guardrail_intervened"}},
            {"metadata": metadata},
        ]
    }


def _run_agent(stream: dict, role: Role = Role.SCHEMA_DESIGN):
    from strands import Agent

    model = build_bedrock_model(role)
    model.client = MagicMock()
    model.client.converse_stream.return_value = stream
    return Agent(model=model, system_prompt="s", tools=[], callback_handler=None)("hello"), model


def test_a_block_raises_naming_the_phase(guardrail_on):
    with pytest.raises(GuardrailIntervened) as caught:
        _run_agent(_guardrail_stream("BLOCKED"), Role.SUMMARY)
    assert caught.value.phase == "summary"
    assert "summary phase" in str(caught.value)
    assert "not retried" in str(caught.value)


def test_a_block_without_a_trace_still_raises(guardrail_on):
    with pytest.raises(GuardrailIntervened):
        _run_agent(_guardrail_stream("BLOCKED", trace=False))


def test_masking_only_is_not_an_error(guardrail_on):
    result, _ = _run_agent(_guardrail_stream("ANONYMIZED"))
    assert result is not None


def test_a_normal_reply_is_untouched(guardrail_on):
    result, _ = _run_agent(_ok_stream())
    assert "hi" in str(result)


def test_blocked_by_guardrail_truth_table():
    blocked = {"a": [{"action": "BLOCKED"}]}
    masked = {"a": [{"action": "ANONYMIZED"}]}
    assert bg.blocked_by_guardrail("guardrail_intervened", blocked)
    assert not bg.blocked_by_guardrail("guardrail_intervened", masked)
    assert bg.blocked_by_guardrail("guardrail_intervened", None)
    assert bg.blocked_by_guardrail("guardrail_intervened", {})
    assert not bg.blocked_by_guardrail("end_turn", blocked)
    assert not bg.blocked_by_guardrail(None, blocked)


def test_designer_does_not_retry_a_block():
    from src.tools.schema import base_schema_agent as bsa

    designer = MagicMock(side_effect=GuardrailIntervened("schema_design"))
    runner = types.SimpleNamespace(target_type="dynamodb", output_model=_Out, designer=designer)
    with (
        patch.object(bsa.time, "sleep") as sleep,
        pytest.raises(GuardrailIntervened, match="schema_design"),
    ):
        bsa.SchemaDesignRunner._invoke_designer(runner, "prompt")  # type: ignore[arg-type]
    assert designer.call_count == 1
    sleep.assert_not_called()


def test_designer_block_is_not_hidden_by_the_previous_output_fallback():
    from src.tools.schema import base_schema_agent as bsa

    designer = MagicMock(side_effect=GuardrailIntervened("schema_design"))
    runner = types.SimpleNamespace(target_type="dynamodb", output_model=_Out, designer=designer)
    with pytest.raises(GuardrailIntervened):
        bsa.SchemaDesignRunner._invoke_designer(runner, "prompt", previous_output=_Out())  # type: ignore[arg-type]


def test_a_failing_ordinary_error_still_retries():
    """Control: other errors keep their retry budget."""
    from src.tools.schema import base_schema_agent as bsa

    designer = MagicMock(side_effect=ValueError("boom"))
    runner = types.SimpleNamespace(target_type="dynamodb", output_model=_Out, designer=designer)
    with patch.object(bsa.time, "sleep"), pytest.raises(RuntimeError, match="after 3 attempts"):
        bsa.SchemaDesignRunner._invoke_designer(runner, "prompt")  # type: ignore[arg-type]
    assert designer.call_count == 3


def test_pe_review_block_ends_the_run():
    from src.tools.schema import base_schema_agent as bsa

    runner = bsa.SchemaDesignRunner(
        target_type="dynamodb",
        output_model=_Out,
        model=MagicMock(),
        designer_agent=MagicMock(return_value=types.SimpleNamespace(structured_output=_Out())),
        pe_skill_path="x",
        pe_reviewer_fn=MagicMock(side_effect=GuardrailIntervened("schema_design")),
        format_pe_feedback_fn=lambda review: "",
    )
    with pytest.raises(GuardrailIntervened):
        runner.run("prompt", {})


def test_advisor_does_not_retry_a_block_and_degrades_to_none(caplog):
    from src.tools.analysis.llm_advisor_base import LlmAdvisorBase

    class Advisor(LlmAdvisorBase):
        def _output_model(self):
            return _Out

        def _build_prompt(self, schema, queries, **kw):
            return ""

        def _parse_result(self, result):
            return None

        def _merge_results(self, results):
            return None

        def _filter_kwargs_for_group(self, group_queries, referenced_tables, **kw):
            return {}

        def _call_llm(self, schema, queries, **kw):
            calls.append(1)
            raise GuardrailIntervened("analysis_advisor")

    calls: list[int] = []
    advisor = Advisor("s", enabled=True)
    with patch("time.sleep") as sleep:
        assert advisor.advise({"tables": []}, [{"query_id": "q"}]) is None
        many = [{"query_id": f"q{i}"} for i in range(advisor.MAX_LLM_QUERIES + 1)]
        assert advisor.advise({"tables": []}, many) is None
    assert len(calls) == 2, "one call per entry point, no retries"
    sleep.assert_not_called()
    assert "analysis_advisor phase" in caplog.text


def test_summary_does_not_retry_a_block(caplog):
    from src.agents.referee import reality_check_handler

    agent = MagicMock(side_effect=GuardrailIntervened("summary"))
    det = {
        "collector_output": {"database_schema": {"tables": []}, "queries": {}},
        "assignment": {"query_assignments": []},
        "revised_assignments": [],
        "before_distribution": {},
        "after_distribution": {},
        "consolidations": [],
        "unique_value_assessment": {},
        "architectural_patterns": [],
        "recommendations": [],
    }
    with (
        patch("strands.Agent", return_value=agent),
        patch.object(reality_check_handler, "build_bedrock_model", return_value=object()),
    ):
        assert reality_check_handler._generate_executive_summary(det) is None
    assert agent.call_count == 1
    assert "summary phase" in caplog.text


def test_validator_block_skips_the_boto3_fallback():
    from src.agents.referee import consolidation_validator as cv

    boto3_try = MagicMock(return_value=[])
    with (
        patch.object(cv, "_try_strands_validator", side_effect=GuardrailIntervened("validator")),
        patch.object(cv, "_try_boto3_validator", boto3_try),
        pytest.raises(GuardrailIntervened, match="validator"),
    ):
        cv._call_llm_validator("documentdb", "dynamodb", [])
    boto3_try.assert_not_called()


def test_boto3_validator_passes_and_checks_the_guardrail(guardrail_on):
    from src.agents.referee import consolidation_validator as cv

    with patch("boto3.client") as client:
        client.return_value.converse.return_value = {
            "stopReason": "guardrail_intervened",
            "trace": {
                "guardrail": {
                    "inputAssessment": {
                        "gr": {"contentPolicy": {"filters": [{"action": "BLOCKED"}]}}
                    }
                }
            },
            "output": {"message": {"content": [{"text": "x"}]}},
        }
        with pytest.raises(GuardrailIntervened, match="validator phase"):
            cv._try_boto3_validator("documentdb", "dynamodb", [])
        request = client.return_value.converse.call_args.kwargs
    assert request["guardrailConfig"]["guardrailIdentifier"] == "gr-abc123"


def test_boto3_validator_off_sends_no_guardrail():
    from src.agents.referee import consolidation_validator as cv

    with patch("boto3.client") as client:
        client.return_value.converse.return_value = {
            "output": {"message": {"content": [{"text": "[]"}]}}
        }
        with contextlib.suppress(cv.ValidationFailed):  # the reply's shape is not under test
            cv._try_boto3_validator("documentdb", "dynamodb", [])
        assert "guardrailConfig" not in client.return_value.converse.call_args.kwargs


def _free_text_question():
    from src.contracts.agent_interaction_models import AgentQuestion

    return AgentQuestion(question_id="q1", category="c", question="Why?", options=None)


def _invoke_response(body: dict) -> dict:
    return {"body": MagicMock(read=lambda: json.dumps(body).encode())}


def test_answer_check_passes_the_guardrail_and_accepts_with_a_warning_on_block(
    guardrail_on, caplog
):
    from src.agents.answer_validation import validate_answer

    blocked = {
        "amazon-bedrock-guardrailAction": "INTERVENED",
        "amazon-bedrock-trace": {
            "guardrail": {"input": {"gr": {"contentPolicy": {"filters": [{"action": "BLOCKED"}]}}}}
        },
        "content": [],
    }
    with patch("boto3.client") as client:
        client.return_value.invoke_model.return_value = _invoke_response(blocked)
        result = validate_answer(_free_text_question(), "because")
        kwargs = client.return_value.invoke_model.call_args.kwargs
    assert client.return_value.invoke_model.call_count == 1
    assert kwargs["guardrailIdentifier"] == "gr-abc123" and kwargs["guardrailVersion"] == "3"
    assert result.valid and "guardrail blocked" in result.warnings[0]
    assert "free-text answer check phase" in caplog.text


def test_answer_check_off_sends_no_guardrail():
    from src.agents.answer_validation import validate_answer

    with patch("boto3.client") as client:
        client.return_value.invoke_model.return_value = _invoke_response(
            {"content": [{"type": "text", "text": "yes"}]}
        )
        assert validate_answer(_free_text_question(), "because").valid
        kwargs = client.return_value.invoke_model.call_args.kwargs
    assert not [k for k in kwargs if k.lower().startswith(("guardrail", "trace"))]


def test_a_blocked_validation_batch_is_recorded_unreviewed_with_the_phase():
    from src.agents.referee.consolidation_validator import (
        MAX_QUERIES_PER_CALL,
        validate_consolidations,
    )
    from tests.unit.agents.referee.test_reality_check_request import _bedrock_case, _flag

    original, revised, query_map, consolidations = _bedrock_case(MAX_QUERIES_PER_CALL + 5)
    calls = iter([GuardrailIntervened("validator"), [_flag(["q31"])]])

    def fake(**kwargs):
        outcome = next(calls)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    incomplete: list[dict] = []
    with patch("src.agents.referee.consolidation_validator._call_llm_validator", side_effect=fake):
        corrections = validate_consolidations(
            consolidations, revised, query_map, {}, original, incomplete
        )
    assert [c.query_ids for c in corrections] == [["q31"]]
    assert len(incomplete) == 1 and "validator phase" in incomplete[0]["error"]


# ---------------------------------------------------------------------------
# Partial configuration: one clear error at start, never swallowed
# ---------------------------------------------------------------------------

_PARTIAL = {bg.ENV_ID: "gr-abc123"}

_SCRIPT_ARGS = {
    "run_assessment.py": ["--file", "x.json", "--all", "-y"],
    "run_analysis.py": ["--job-id", "j", "--db", "d", "--engine", "dynamodb"],
    "run_reality_check.py": ["--job-id", "j", "--db", "d"],
    "run_schema_design.py": ["--job-id", "j", "--db", "d", "--engine", "dynamodb"],
    "run_synthesis.py": ["--job-id", "j", "--db", "d"],
    "test_local_phased.py": ["x.json"],
}


def test_every_script_with_an_llm_mode_is_covered_by_the_startup_check():
    with_mode = {
        p.name
        for p in (REPO_ROOT / "scripts").glob("*.py")
        if '"--llm-mode"' in p.read_text(encoding="utf-8")
    }
    # build_docs_sample.py runs the pipeline with --llm-mode none only.
    assert with_mode - {"build_docs_sample.py"} == set(_SCRIPT_ARGS)


@pytest.mark.parametrize("script", sorted(_SCRIPT_ARGS))
def test_a_partial_setting_stops_each_script_before_any_work(script, tmp_path):
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("BEDROCK_GUARDRAIL")},
        **_PARTIAL,
        "ARTIFACT_DIR": str(tmp_path),
    }
    done = subprocess.run(  # nosec B603 -- fixed argv, this repo's own script
        (
            [
                sys.executable,
                str(REPO_ROOT / "scripts" / script),
                *_SCRIPT_ARGS[script],
                "--llm-mode",
                "bedrock",
                "--artifact-root",
                str(tmp_path),
            ]
            if script != "test_local_phased.py"
            else [
                sys.executable,
                str(REPO_ROOT / "scripts" / script),
                *_SCRIPT_ARGS[script],
                "--llm-mode",
                "bedrock",
            ]
        ),
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=env,
        timeout=120,
    )
    assert done.returncode == 2, (done.returncode, done.stderr[-400:])
    assert f"{bg.ENV_ID} and {bg.ENV_VERSION} must be set together" in done.stderr
    assert not list(tmp_path.iterdir()), "nothing may be written before the check"


def test_a_complete_or_absent_setting_does_not_stop_the_script(tmp_path):
    for env_extra in ({}, {bg.ENV_ID: "gr", bg.ENV_VERSION: "1"}):
        env = {
            **{k: v for k, v in os.environ.items() if not k.startswith("BEDROCK_GUARDRAIL")},
            **env_extra,
        }
        done = subprocess.run(  # nosec B603 -- fixed argv, this repo's own script
            [
                sys.executable,
                str(REPO_ROOT / "scripts" / "run_reality_check.py"),
                "--job-id",
                "no-such-job",
                "--db",
                "d",
                "--llm-mode",
                "bedrock",
                "--artifact-root",
                str(tmp_path),
            ],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            env=env,
            timeout=120,
        )
        assert "must be set together" not in done.stderr


def test_the_api_refuses_to_start_with_a_partial_setting(monkeypatch):
    import asyncio

    from src.api import main as api_main

    monkeypatch.setenv(bg.ENV_VERSION, "1")

    async def enter():
        async with api_main._lifespan(api_main.app):
            pass

    with pytest.raises(GuardrailConfigError, match=bg.ENV_ID):
        asyncio.run(enter())


def test_the_atx_entrypoint_exits_non_zero(monkeypatch):
    from src.atx_orchestrator import atx_entrypoint

    monkeypatch.setenv("AGENT_TYPE", "orchestrator")
    monkeypatch.setenv(bg.ENV_ID, "gr")
    with pytest.raises(SystemExit) as caught:
        atx_entrypoint.main()
    assert caught.value.code == 1


def test_handlers_check_before_touching_the_store(monkeypatch):
    from src.agents.referee.reality_check_handler import run_reality_check_handler
    from tests.unit.agents.referee.test_reality_check_llm_seam import _mock_store

    monkeypatch.setenv(bg.ENV_ID, "gr")
    with (
        patch("src.agents.referee.reality_check_handler._run_bedrock_llm_phase") as phase,
        pytest.raises(GuardrailConfigError),
    ):
        run_reality_check_handler("job-1", "mydb", _mock_store(), llm_mode="bedrock")
    phase.assert_not_called()


def _partial(monkeypatch):
    monkeypatch.setenv(bg.ENV_ID, "gr-abc123")


def test_no_fallback_turns_a_partial_setting_into_a_retry_or_a_default(monkeypatch):
    """Each generic handler re-raises the configuration error."""
    from src.agents.answer_validation import validate_answer
    from src.agents.referee import consolidation_validator as cv
    from src.agents.referee import reality_check_handler, synthesis_report
    from src.tools.analysis.llm_advisor_base import LlmAdvisorBase

    _partial(monkeypatch)
    det = {
        "collector_output": {"database_schema": {"tables": []}, "queries": {}},
        "assignment": {"query_assignments": []},
        "revised_assignments": [],
        "before_distribution": {},
        "after_distribution": {},
        "consolidations": [],
        "unique_value_assessment": {},
        "architectural_patterns": [],
        "recommendations": [],
    }
    with pytest.raises(GuardrailConfigError):
        reality_check_handler._generate_executive_summary(det)
    with pytest.raises(GuardrailConfigError):
        synthesis_report.generate_executive_summary(
            {"schema_design_status": {"designed": [], "not_designed": []}}
        )
    with pytest.raises(GuardrailConfigError):
        cv._try_strands_validator("documentdb", "dynamodb", [])
    with patch("boto3.client"), pytest.raises(GuardrailConfigError):
        cv._try_boto3_validator("documentdb", "dynamodb", [])
    with patch("boto3.client"), pytest.raises(GuardrailConfigError):
        validate_answer(_free_text_question(), "because")

    class Advisor(LlmAdvisorBase):
        def _output_model(self):
            return _Out

        def _build_prompt(self, schema, queries, **kw):
            return ""

        def _parse_result(self, result):
            return None

        def _merge_results(self, results):
            return None

        def _filter_kwargs_for_group(self, group_queries, referenced_tables, **kw):
            return {}

        def _call_llm(self, schema, queries, **kw):
            calls.append(1)
            raise GuardrailConfigError("partial")

    calls: list[int] = []
    with patch("time.sleep") as sleep, pytest.raises(GuardrailConfigError):
        Advisor("s", enabled=True).advise({"tables": []}, [{"query_id": "q"}])
    many = [{"query_id": f"q{i}"} for i in range(Advisor("s", enabled=True).MAX_LLM_QUERIES + 1)]
    with patch("time.sleep"), pytest.raises(GuardrailConfigError):
        Advisor("s", enabled=True).advise({"tables": []}, many)
    assert len(calls) == 2
    sleep.assert_not_called()


def test_designer_and_pe_review_do_not_swallow_a_config_error():
    from src.tools.schema import base_schema_agent as bsa

    designer = MagicMock(side_effect=GuardrailConfigError("partial"))
    runner = types.SimpleNamespace(target_type="dynamodb", output_model=_Out, designer=designer)
    with patch.object(bsa.time, "sleep"), pytest.raises(GuardrailConfigError):
        bsa.SchemaDesignRunner._invoke_designer(runner, "p")  # type: ignore[arg-type]
    assert designer.call_count == 1


def test_a_block_on_the_aurora_delta_correction_propagates():
    from src.tools.schema.aurora_common import bedrock_delta

    merged = types.SimpleNamespace(errors=["bad ref"], summary="", warnings=[], output={})
    runner = types.SimpleNamespace(
        _invoke_designer=MagicMock(side_effect=GuardrailIntervened("schema_design"))
    )
    with (
        patch.object(bedrock_delta, "merge_design_delta", return_value=merged),
        pytest.raises(GuardrailIntervened),
    ):
        bedrock_delta.merge_or_correct("aurora-postgresql", None, None, runner, _Out, {})  # type: ignore[arg-type]
    runner._invoke_designer.assert_called_once()


def test_a_block_on_the_synthesis_summary_does_not_say_to_set_the_model_id(capsys, caplog):
    from src.agents.referee import synthesis_report

    class Agent:
        def __init__(self, **kw):
            pass

        def __call__(self, prompt):
            raise GuardrailIntervened("summary")

    with (
        patch("strands.Agent", Agent),
        patch.object(synthesis_report, "build_bedrock_model", return_value=object()),
    ):
        result = synthesis_report.generate_executive_summary(
            {"schema_design_status": {"designed": [], "not_designed": []}}
        )
    assert result is None
    out = capsys.readouterr().out
    assert "summary phase" in out and "SUMMARY_MODEL_ID" not in out
    assert "summary phase" in caplog.text


# ---------------------------------------------------------------------------
# The CloudFormation template
# ---------------------------------------------------------------------------

TEMPLATE = REPO_ROOT / "infrastructure" / "cloudformation" / "bedrock-guardrail.yaml"


def _template_pattern(parameter: str) -> str:
    import re

    text = TEMPLATE.read_text(encoding="utf-8")
    block = text.split(f"  {parameter}:\n", 1)[1].split("\n\n", 1)[0]
    return re.search(r"^\s+AllowedPattern: '(.*)'$", block, re.M).group(1)  # type: ignore[union-attr]


def _botocore_profile_arn_pattern() -> str:
    import botocore.session

    model = botocore.session.get_session().get_service_model("bedrock")
    return str(model.shape_for("GuardrailCrossRegionGuardrailProfileArn").metadata["pattern"])


def test_profile_arn_parameter_uses_the_api_pattern():
    assert _template_pattern("CrossRegionGuardrailProfileArn") == (
        "^$|^" + _botocore_profile_arn_pattern() + "$"
    )


@pytest.mark.parametrize(
    "arn, ok",
    [
        ("", True),
        ("arn:aws:bedrock:us-east-1:111122223333:guardrail-profile/us.guardrail.v1:0", True),
        ("arn:aws:bedrock:us-east-1:111122223333:guardrail-profile/us.guardrail.v1", True),
        (
            "arn:aws-us-gov:bedrock:us-gov-west-1:111122223333:guardrail-profile/us-gov.guardrail.v1:0",
            True,
        ),
        ("arn:aws:bedrock:us-east-1:111122223333:guardrail-profile/us.guardrail", False),
        ("arn:aws:bedrock:us-east-1:111122223333:guardrail/abc123", False),
        ("arn:aws:bedrock:us-east-1:1111:guardrail-profile/us.guardrail.v1:0", False),
        ("us.guardrail.v1:0", False),
    ],
)
def test_profile_arn_pattern_accepts_what_the_api_accepts(arn, ok):
    import re

    template = re.fullmatch(_template_pattern("CrossRegionGuardrailProfileArn"), arn)
    api = re.fullmatch(_botocore_profile_arn_pattern(), arn) or arn == ""
    assert bool(template) is ok
    assert bool(api) is ok


def test_the_answer_is_tagged_for_the_prompt_attack_filter_only_when_on(guardrail_on):
    """InvokeModel evaluates only tagged content, so an untagged answer would skip
    the prompt-attack filter (seen against a deployed guardrail)."""
    from src.agents.answer_validation import validate_answer

    with patch("boto3.client") as client:
        client.return_value.invoke_model.return_value = _invoke_response(
            {"content": [{"type": "text", "text": "yes"}]}
        )
        validate_answer(_free_text_question(), "my answer")
        body = json.loads(client.return_value.invoke_model.call_args.kwargs["body"])
    suffix = body["amazon-bedrock-guardrailConfig"]["tagSuffix"]
    prompt = body["messages"][0]["content"]
    tag = f"amazon-bedrock-guardrails-guardContent_{suffix}"
    assert f"<{tag}>my answer</{tag}>" in prompt
    assert "Why?" in prompt.split(f"<{tag}>")[0], "only the answer is tagged"


def test_the_answer_is_not_tagged_when_off():
    from src.agents.answer_validation import validate_answer

    with patch("boto3.client") as client:
        client.return_value.invoke_model.return_value = _invoke_response(
            {"content": [{"type": "text", "text": "yes"}]}
        )
        validate_answer(_free_text_question(), "my answer")
        body = json.loads(client.return_value.invoke_model.call_args.kwargs["body"])
    assert "amazon-bedrock-guardrailConfig" not in body
    assert "guardContent" not in body["messages"][0]["content"]
    assert "Answer: my answer" in body["messages"][0]["content"]


# ---------------------------------------------------------------------------
# Customer data is the guarded content (live finding: the framing text itself
# trips the prompt-attack filter, so only the data inside it may be evaluated)
# ---------------------------------------------------------------------------


def _framed_prompt(data: str) -> str:
    from src.agents.prompt_framing import frame_untrusted

    return "Task line.\n" + frame_untrusted(data, label="schema") + "\nClosing line."


def test_split_guard_content_marks_only_the_data(guardrail_on):
    blocks = bg.split_guard_content(_framed_prompt("orders(id, total)"))
    guarded = [b["guardContent"]["text"]["text"] for b in blocks if "guardContent" in b]
    assert guarded == ["orders(id, total)"]
    text = "".join(b["text"] for b in blocks if "text" in b)
    assert "Task line." in text and "Closing line." in text
    assert "orders(id, total)" not in text
    rebuilt = "".join(
        b["text"] if "text" in b else b["guardContent"]["text"]["text"] for b in blocks
    )
    assert rebuilt == _framed_prompt("orders(id, total)"), "no text is lost or reordered"


def test_split_guard_content_handles_several_blocks_and_none(guardrail_on):
    two = _framed_prompt("a(x)") + "\n" + _framed_prompt("b(y)")
    assert [
        b["guardContent"]["text"]["text"]
        for b in bg.split_guard_content(two)
        if "guardContent" in b
    ] == ["a(x)", "b(y)"]
    assert bg.split_guard_content("no data here") == [{"text": "no data here"}]


def test_guard_messages_is_the_identity_when_off():
    messages = [{"role": "user", "content": [{"text": _framed_prompt("t(a)")}]}]
    assert bg.guard_messages(messages) is messages


def test_the_strands_request_guards_the_data_only_when_on(monkeypatch):
    from strands import Agent

    def sent(prompt: str) -> list:
        model = build_bedrock_model(Role.SUMMARY)
        seen: list[dict] = []
        model.client = MagicMock()

        def fake(**req):
            seen.append(req)
            return _ok_stream()

        model.client.converse_stream.side_effect = fake
        Agent(model=model, system_prompt="s", tools=[], callback_handler=None)(prompt)
        content: list = seen[0]["messages"][0]["content"]
        return content

    prompt = _framed_prompt("orders(id, total)")
    assert sent(prompt) == [{"text": prompt}], "off: one text block, as before"
    monkeypatch.setenv(bg.ENV_ID, "gr-abc123")
    monkeypatch.setenv(bg.ENV_VERSION, "3")
    on = sent(prompt)
    assert [b["guardContent"]["text"]["text"] for b in on if "guardContent" in b] == [
        "orders(id, total)"
    ]


def test_the_validator_converse_messages_guard_the_data(guardrail_on):
    from src.agents.referee import consolidation_validator as cv

    with (
        patch("boto3.client") as client,
        patch.object(cv, "_build_validation_prompt", return_value=_framed_prompt("orders(id)")),
    ):
        client.return_value.converse.return_value = {
            "output": {"message": {"content": [{"text": "[]"}]}}
        }
        with contextlib.suppress(cv.ValidationFailed):
            cv._try_boto3_validator("documentdb", "dynamodb", [])
        content = client.return_value.converse.call_args.kwargs["messages"][0]["content"]
    assert any("guardContent" in b for b in content)
