"""One place for which model each LLM call uses, and how to ask it (#624).

Every model call in ``src/`` names a :class:`Role`, never a literal model ID.
A role says which model to use (an env override, else a default) and what the
call needs (output length, sampling, thinking, effort, structured output). The
model *family* then decides which of those settings the request may carry,
because the families reject different fields:

==========================  ============  ===============  =======================  ========
Family                      sampling      ``budget_tokens``  forced ``tool_choice``   effort
==========================  ============  ===============  =======================  ========
Claude 3.x, 4.0-4.6         accepted      accepted         accepted                 not sent
Opus 4.7/4.8                rejected      rejected         accepted                 sent
Sonnet 5                    rejected      rejected         with thinking disabled   sent
Opus 5, Fable 5             rejected      rejected         sent as ``auto``         sent
Haiku 5.5                   rejected      rejected         accepted                 sent
Opus/Sonnet 5.5, 5.1 tier   rejected      rejected         rejected (``auto``)      sent
Unrecognised ID             not sent      not sent         sent as ``auto``         not sent
==========================  ============  ===============  =======================  ========

Today's 4.x request is reproduced exactly (the role's ``legacy_*`` settings), so
switching a default to a newer family is a model-ID change only. On families
where adaptive thinking is on by default, ``max_tokens`` covers thinking plus
the answer, so it is raised to :data:`ADAPTIVE_MAX_TOKENS_FLOOR`. Where forced
``tool_choice`` is rejected, the Strands model never sends a forced choice: the
structured-output retry goes out as ``auto`` with Strands' own "use the tool"
instruction instead of a 400 (``src/shared/role_bedrock_model.py``), and roles whose tool schemas compile under Bedrock
strict mode also turn on ``strict_tools`` (schema-valid tool input on the first
try).

Roles with ``prompt_cache`` (schema design, #626) send Bedrock cache points
after the tool definitions and after the system prompt, the prefix every
designer and PE turn repeats. A prefix shorter than the model's minimum
(:func:`min_cacheable_tokens`) is simply not cached; nothing fails.

The default switch to the 5.5 family is a separate, measured change; the 5.5
IDs below are here so tests (and env overrides) can exercise them.
"""

from __future__ import annotations

import functools
import logging
import os
import re
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from src.shared.role_bedrock_model import RoleBedrockModel

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Model IDs (the only place in src/ that spells one)
# ---------------------------------------------------------------------------

#: Bedrock model / cross-region inference profile IDs.
MODEL_IDS: dict[str, str] = {
    "opus-4.6": "us.anthropic.claude-opus-4-6-v1",
    "sonnet-4.6": "us.anthropic.claude-sonnet-4-6",
    "haiku-4.5": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    "opus-5.5": "global.anthropic.claude-opus-5-5",
    "sonnet-5.5": "global.anthropic.claude-sonnet-5-5",
    "haiku-5.5": "global.anthropic.claude-haiku-5-5",
}

#: ``max_tokens`` floor on families with adaptive thinking on by default:
#: thinking and the answer share the budget, so a 512-token cap would truncate.
ADAPTIVE_MAX_TOKENS_FLOOR = 16000
#: ``max_tokens`` floor everywhere else (a short classification answer).
LEGACY_MAX_TOKENS_FLOOR = 256

Effort = Literal["low", "medium", "high", "xhigh", "max"]


class Role(str, Enum):
    """What a model call is for. Each role has one entry in :data:`ROLE_SPECS`."""

    SCHEMA_DESIGN = "schema_design"
    SUMMARY = "summary"
    VALIDATOR = "validator"
    LIGHT_CHECK = "light_check"
    ANALYSIS_ADVISOR = "analysis_advisor"
    ATX_AGENT = "atx_agent"


@dataclass(frozen=True)
class RoleSpec:
    """What a role needs from its model call.

    ``env_vars`` are read in order; the first non-empty one wins. ``legacy_*``
    settings are sent only to families that accept them (they reproduce the
    pre-#624 request). ``effort`` is sent only to families that accept it.
    """

    env_vars: tuple[str, ...]
    default_model: str
    max_tokens: int | None = None
    max_tokens_env: str | None = None
    legacy_temperature: float | None = None
    #: Extended thinking wanted. 4.x: ``budget_tokens`` on Opus only (as before).
    thinking: bool = False
    legacy_thinking_budget: int | None = None
    effort: Effort | None = None
    #: The call uses Strands structured output (a tool the model must call).
    structured_output: bool = False
    #: Send ``strict_tools`` where forced ``tool_choice`` is rejected. Needs
    #: every tool schema in the call to compile under Bedrock strict mode.
    strict_tools: bool = False
    #: Bedrock prompt caching (#626): cache points after the tool definitions
    #: and after the system prompt, so the stable prefix every turn resends
    #: (output-contract tool schema, skill text) bills at the cache-read rate.
    #: The conversation itself is not cached. Claude model IDs only.
    prompt_cache: bool = False


ROLE_SPECS: dict[Role, RoleSpec] = {
    # Six schema designers + PE reviewers. Before #624 DynamoDB read
    # SCHEMA_AGENT_MODEL_ID and the other five BEDROCK_MODEL_ID; both now
    # apply to all six.
    Role.SCHEMA_DESIGN: RoleSpec(
        env_vars=("SCHEMA_AGENT_MODEL_ID", "BEDROCK_MODEL_ID"),
        default_model=MODEL_IDS["opus-4.6"],
        max_tokens=65536,
        max_tokens_env="SCHEMA_AGENT_MAX_TOKENS",
        legacy_temperature=1.0,
        thinking=True,
        legacy_thinking_budget=6000,
        effort="high",
        structured_output=True,
        # Not yet. Bedrock strict mode rejects parts of the designer schemas:
        # the DynamoDB output contract uses ``oneOf`` (join/calculation),
        # ``minimum`` (x6) and ``maxItems``, and has ~35 optional properties;
        # the designers also carry many other tools whose schemas are compiled.
        strict_tools=False,
        # Every designer and PE turn resends the same skill text and contract
        # tool schema (#626). Parallel groups start together, so each group's
        # first call writes the entry; later turns and later groups read it.
        prompt_cache=True,
    ),
    # Reality-check and synthesis executive summaries (a few sentences).
    Role.SUMMARY: RoleSpec(
        env_vars=("SUMMARY_MODEL_ID",),
        default_model=MODEL_IDS["sonnet-4.6"],
        max_tokens=512,
        legacy_temperature=0.2,
        effort="low",
    ),
    # Consolidation validator (a JSON array verdict per moved query batch).
    Role.VALIDATOR: RoleSpec(
        env_vars=("VALIDATOR_MODEL_ID", "SUMMARY_MODEL_ID"),
        default_model=MODEL_IDS["sonnet-4.6"],
        max_tokens=4096,
        legacy_temperature=0.0,
        effort="medium",
    ),
    # Free-text answer check (yes/no). Claude Haiku 3 is retired, so the
    # default is the closest still-served Haiku with the same 4.x request.
    Role.LIGHT_CHECK: RoleSpec(
        env_vars=("LIGHT_CHECK_MODEL_ID",),
        default_model=MODEL_IDS["haiku-4.5"],
        max_tokens=LEGACY_MAX_TOKENS_FLOOR,
        effort="low",
    ),
    # Analysis-phase LLM advisor (Strands structured output per query group).
    Role.ANALYSIS_ADVISOR: RoleSpec(
        env_vars=("ANALYSIS_MODEL_ID",),
        default_model=MODEL_IDS["sonnet-4.6"],
        effort="medium",
        structured_output=True,
        strict_tools=True,
    ),
    # AWS Transform orchestrator and subagents. The SDK builds the model from
    # the ID alone (no sampling or thinking fields), so only the ID is ours.
    Role.ATX_AGENT: RoleSpec(
        env_vars=("MODEL_ID",),
        default_model=MODEL_IDS["sonnet-4.6"],
    ),
}


def resolve_model_id(role: Role) -> str:
    """The model ID for ``role``: the first non-empty env override, else the default."""
    spec = ROLE_SPECS[role]
    for name in spec.env_vars:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return spec.default_model


# ---------------------------------------------------------------------------
# Family profiles
# ---------------------------------------------------------------------------


ForcedToolChoice = Literal["accepted", "needs_thinking_disabled", "rejected"]


@dataclass(frozen=True)
class ModelProfile:
    """Which request settings a model family accepts."""

    family: str
    tier: str | None
    accepts_sampling: bool
    accepts_budget_tokens: bool
    #: How a forced ``tool_choice`` (``any``/``tool``) may be sent:
    #: as is, only with ``thinking: {type: "disabled"}`` (Sonnet 5 on Bedrock),
    #: or not at all (sent as ``auto`` instead).
    forced_tool_choice: ForcedToolChoice
    accepts_effort: bool
    #: Adaptive thinking runs when ``thinking`` is omitted.
    adaptive_by_default: bool

    @property
    def accepts_forced_tool_choice(self) -> bool:
        return self.forced_tool_choice != "rejected"


_NAMED = re.compile(
    r"claude-(?P<tier>opus|sonnet|haiku|fable|mythos)-(?P<major>\d+)(?:-(?P<minor>\d{1,2}))?(?=$|[-:.@\[])"
)
_LEGACY_NAMED = re.compile(r"claude-(?P<major>\d)(?:-\d)?-(?P<tier>opus|sonnet|haiku)")


def profile_for(model_id: str) -> ModelProfile:
    """The request profile for ``model_id`` (Bedrock or first-party form)."""
    legacy = _LEGACY_NAMED.search(model_id)
    if legacy:
        return _budget_profile(f"claude-{legacy['major']}", legacy["tier"])
    match = _NAMED.search(model_id)
    if not match:
        _warn_unknown(model_id)
        # Not a Claude ID we recognise (e.g. an inference-profile ARN): send the
        # smallest request every model takes, and never force a tool.
        return ModelProfile("unknown", None, False, False, "rejected", False, False)
    tier = match["tier"]
    version = (int(match["major"]), int(match["minor"] or 0))
    family = f"claude-{version[0]}.{version[1]}"
    if version <= (4, 6) or (tier == "haiku" and version < (5, 0)):
        return _budget_profile(family, tier)
    if tier == "haiku" and version == (5, 5):
        # Haiku 5.5 accepts a forced tool_choice (the forced turn skips thinking).
        return ModelProfile(family, tier, False, False, "accepted", True, True)
    if version >= (5, 5) or (tier in ("fable", "mythos") and version >= (5, 1)):
        # Opus/Sonnet 5.5, Fable/Mythos 5.1 (and anything newer): forced is a 400.
        return ModelProfile(family, tier, False, False, "rejected", True, True)
    if version < (5, 0):
        # Opus 4.7/4.8: no thinking unless asked; Strands drops ``thinking`` on a
        # forced turn, which leaves a valid thinking-off request.
        return ModelProfile(family, tier, False, False, "accepted", True, False)
    if tier == "sonnet":
        # Sonnet 5 on Bedrock: a forced choice needs ``thinking: disabled``;
        # dropping the key would leave adaptive thinking on.
        return ModelProfile(family, tier, False, False, "needs_thinking_disabled", True, True)
    # Opus 5, Fable 5: thinking is on by default, and the Bedrock rule for a
    # forced choice with thinking on is not documented, so the retry goes out
    # as ``auto``, which every model accepts.
    return ModelProfile(family, tier, False, False, "rejected", True, True)


@functools.lru_cache(maxsize=32)
def _warn_unknown(model_id: str) -> None:
    logger.warning(
        "Model ID %r is not a recognised Claude model ID; sending a minimal request "
        "(no temperature, thinking or effort, no forced tool_choice).",
        model_id,
    )


def _budget_profile(family: str, tier: str) -> ModelProfile:
    return ModelProfile(family, tier, True, True, "accepted", False, False)


# ---------------------------------------------------------------------------
# Prompt caching
# ---------------------------------------------------------------------------


def min_cacheable_tokens(model_id: str) -> int | None:
    """The shortest prefix (tokens) ``model_id`` caches, or None if unknown.

    A cache point on a shorter prefix is silently ignored: no error, no cache
    write, no cache read. Values from Anthropic's prompt-caching reference; the
    minimum is not monotonic across generations (Opus 4.6 needs 4096, 5.x 512).
    """
    legacy = _LEGACY_NAMED.search(model_id)
    if legacy:
        return 2048 if legacy["tier"] == "haiku" else 1024
    match = _NAMED.search(model_id)
    if not match:
        return None
    tier, version = match["tier"], (int(match["major"]), int(match["minor"] or 0))
    if version >= (5, 0):
        return 1024 if (tier, version) == ("sonnet", (5, 0)) else 512
    if tier == "haiku":
        return 4096 if version >= (4, 5) else 2048
    if tier == "opus":
        if version in ((4, 5), (4, 6)):
            return 4096
        if version == (4, 7):
            return 2048
    return 1024


def _cache_config(spec: RoleSpec, profile: ModelProfile) -> Any:
    """The Strands ``CacheConfig`` for ``spec``, or None when the role or model has none.

    ``strategy="auto"`` places cache points only on Claude model IDs; with
    ``tools_ttl=True`` Strands adds one after the tool definitions and one after
    the system prompt (Bedrock renders tools, then system, then messages, so the
    second covers both). Strands would also put one on the last user turn;
    :class:`~src.shared.role_bedrock_model.RoleBedrockModel` drops that one,
    because a designer prompt or a PE review input is sent once and a cache
    write on it is a 25% surcharge nothing reads back. The TTL is Bedrock's
    default (5 minutes), refreshed by every read.
    """
    if not spec.prompt_cache or profile.family == "unknown":
        return None
    try:
        from strands.models.model import CacheConfig
    except ImportError:  # a Strands release older than CacheConfig: no caching
        logger.warning("strands has no CacheConfig; schema-design prompt caching is off")
        return None
    try:
        return CacheConfig(strategy="auto", tools_ttl=True)  # type: ignore[call-arg]
    except TypeError:  # a Strands release older than tools_ttl: system + messages only
        return CacheConfig(strategy="auto")


# ---------------------------------------------------------------------------
# Request builders
# ---------------------------------------------------------------------------


def _max_tokens(spec: RoleSpec, profile: ModelProfile, override: int | None) -> int | None:
    value = override if override is not None else spec.max_tokens
    if spec.max_tokens_env and os.environ.get(spec.max_tokens_env, "").strip():
        value = int(os.environ[spec.max_tokens_env])
    if profile.adaptive_by_default:
        return max(value or 0, ADAPTIVE_MAX_TOKENS_FLOOR)
    if value is None:
        return None
    return max(value, LEGACY_MAX_TOKENS_FLOOR)


def _thinking(spec: RoleSpec, profile: ModelProfile) -> dict | None:
    if not spec.thinking:
        return None
    if profile.accepts_budget_tokens:
        # Unchanged 4.x behaviour: budgeted thinking on Opus only.
        if profile.tier == "opus" and spec.legacy_thinking_budget:
            return {"type": "enabled", "budget_tokens": spec.legacy_thinking_budget}
        return None
    if profile.adaptive_by_default or profile.family == "unknown":
        return None  # omitted = adaptive (and the only valid mode on 5.5)
    return {"type": "adaptive"}  # Opus 4.7/4.8 think only when asked


def _model_fields(spec: RoleSpec, profile: ModelProfile) -> dict[str, Any]:
    """Native Anthropic body fields (``thinking``, ``output_config``)."""
    fields: dict[str, Any] = {}
    thinking = _thinking(spec, profile)
    if thinking:
        fields["thinking"] = thinking
    if spec.effort and profile.accepts_effort:
        fields["output_config"] = {"effort": spec.effort}
    return fields


def _temperature(spec: RoleSpec, profile: ModelProfile) -> float | None:
    return spec.legacy_temperature if profile.accepts_sampling else None


def bedrock_model_kwargs(
    role: Role, *, model_id: str | None = None, max_tokens: int | None = None
) -> dict[str, Any]:
    """Keyword arguments for ``strands.models.bedrock.BedrockModel`` for ``role``.

    ``max_tokens`` overrides the role default for one call site (its env var,
    when set, still wins). Transport settings (region, boto config) are the
    caller's.
    """
    spec = ROLE_SPECS[role]
    model_id = model_id or resolve_model_id(role)
    profile = profile_for(model_id)
    kwargs: dict[str, Any] = {"model_id": model_id}
    tokens = _max_tokens(spec, profile, max_tokens)
    if tokens is not None:
        kwargs["max_tokens"] = tokens
    temperature = _temperature(spec, profile)
    if temperature is not None:
        kwargs["temperature"] = temperature
    fields = _model_fields(spec, profile)
    # Schema design always passed additional_request_fields (possibly empty).
    if fields or spec.thinking:
        kwargs["additional_request_fields"] = fields
    if spec.strict_tools and not profile.accepts_forced_tool_choice:
        kwargs["strict_tools"] = True
    cache_config = _cache_config(spec, profile)
    if cache_config is not None:
        kwargs["cache_config"] = cache_config
    return kwargs


def converse_request_fields(role: Role, *, model_id: str | None = None) -> dict[str, Any]:
    """``modelId``, ``inferenceConfig`` and model fields for a boto3 ``converse`` call."""
    spec = ROLE_SPECS[role]
    model_id = model_id or resolve_model_id(role)
    profile = profile_for(model_id)
    inference: dict[str, Any] = {}
    tokens = _max_tokens(spec, profile, None)
    if tokens is not None:
        inference["maxTokens"] = tokens
    temperature = _temperature(spec, profile)
    if temperature is not None:
        inference["temperature"] = temperature
    request: dict[str, Any] = {"modelId": model_id, "inferenceConfig": inference}
    fields = _model_fields(spec, profile)
    if fields:
        request["additionalModelRequestFields"] = fields
    return request


def invoke_model_body(
    role: Role, messages: list[dict], *, model_id: str | None = None
) -> dict[str, Any]:
    """The Anthropic Messages body for a boto3 ``invoke_model`` call."""
    spec = ROLE_SPECS[role]
    profile = profile_for(model_id or resolve_model_id(role))
    body: dict[str, Any] = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": _max_tokens(spec, profile, None) or LEGACY_MAX_TOKENS_FLOOR,
        "messages": messages,
    }
    temperature = _temperature(spec, profile)
    if temperature is not None:
        body["temperature"] = temperature
    body.update(_model_fields(spec, profile))
    return body


def build_bedrock_model(
    role: Role,
    *,
    model_id: str | None = None,
    max_tokens: int | None = None,
    **transport: Any,
) -> RoleBedrockModel:
    """A Strands ``BedrockModel`` for ``role`` with family-valid request settings.

    ``transport`` passes straight through (``region_name``,
    ``boto_client_config``, ...). The returned model also keeps Strands' forced
    structured-output retry valid for the family (see
    :class:`~src.shared.role_bedrock_model.RoleBedrockModel`).
    """
    from src.shared.role_bedrock_model import RoleBedrockModel

    kwargs = bedrock_model_kwargs(role, model_id=model_id, max_tokens=max_tokens)
    return RoleBedrockModel(profile=profile_for(kwargs["model_id"]), **kwargs, **transport)
