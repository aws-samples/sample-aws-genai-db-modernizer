"""
Base Schema Design Agent — shared PE review loop, retry logic, and graceful fallback.

All target-specific schema agents (DynamoDB, DocumentDB, OpenSearch, etc.)
should use this base to get consistent behavior:
  - Retry with backoff on designer failures
  - Graceful fallback to previous valid output on max_tokens errors
  - Duplicate PE feedback detection to break loops early
  - Concise revision prompts (no full output JSON)
  - Consistent logging

The runner is generic over both the output model (T) and the PE review model.
PE review models must have: verdict (str enum with "APPROVED"), change_requests,
pe_notes, strengths, summary. See dynamodb_pe_review.py for the reference impl.

Usage:
    from src.tools.schema.base_schema_agent import SchemaDesignRunner

    runner = SchemaDesignRunner(
        target_type="documentdb",
        output_model=DocumentDBModelOutputContract,
        model=bedrock_model,
        designer_agent=agent,
        pe_skill_path="src/skills/documentdb-pe-review.md",
        pe_reviewer_fn=_invoke_pe_reviewer,
        format_pe_feedback_fn=_format_pe_feedback,
    )
    output, trace = runner.run(designer_prompt, input_summary)
"""

import json
import logging
import os
import random
import time
from typing import Any, TypeVar

from pydantic import BaseModel
from strands import Agent
from strands.models.bedrock import BedrockModel

from src.contracts.schema_design_output import TradeOff

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

MAX_DESIGNER_RETRIES = 3
MAX_PE_ITERATIONS = 2
BASE_BACKOFF_SECONDS = 1.0

# Throttling gets its own, larger retry budget so a Bedrock rate-limit storm does
# not fail a group the way a genuine bug (e.g. an unparseable design) does. This
# matters most under a small account quota: the heaviest engines split into dozens
# of groups, and every engine runs in parallel, so the shared model quota is the
# real bottleneck. Transient throttle / 5xx / read-timeout errors draw from this
# budget with longer exponential backoff plus jitter (to de-synchronize the
# concurrent group workers); all other errors draw from MAX_DESIGNER_RETRIES and
# fail fast.
_DEFAULT_THROTTLE_MAX_RETRIES = 8
_THROTTLE_BASE_BACKOFF_SECONDS = 2.0
_THROTTLE_MAX_BACKOFF_SECONDS = 60.0
_THROTTLE_MARKERS = (
    "throttl",
    "serviceunavailable",
    "service unavailable",
    "toomanyrequests",
    "too many requests",
    "rate exceeded",
    "rate limit",
    "read timed out",
    "timed out",
    "modeltimeout",
    "model not ready",
    "slow down",
    "429",
    "503",
)


def _throttle_max_retries() -> int:
    """Max transient/throttle retries, overridable via SCHEMA_THROTTLE_MAX_RETRIES."""
    raw = os.environ.get("SCHEMA_THROTTLE_MAX_RETRIES")
    if raw:
        try:
            value = int(raw)
            if value > 0:
                return value
        except ValueError:
            pass
    return _DEFAULT_THROTTLE_MAX_RETRIES


def _is_throttle_error(exc: Exception) -> bool:
    """True when the exception looks like Bedrock throttling / a transient 5xx.

    Checks the botocore error code (when present) and the type name + message
    against known throttle/transient markers, so we treat a rate-limit storm as
    worth waiting out rather than a genuine failure to fail fast on.
    """
    code = ""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = str(response.get("Error", {}).get("Code", "")).lower()
    text = f"{type(exc).__name__} {exc}".lower()
    return any(marker in code or marker in text for marker in _THROTTLE_MARKERS)


def _throttle_backoff(throttle_failures: int) -> float:
    """Exponential backoff (seconds) on the throttle-failure count, capped, + jitter.

    Jitter de-synchronizes the concurrent group workers so they do not retry in
    lockstep and re-collide on the same quota.
    """
    base = _THROTTLE_BASE_BACKOFF_SECONDS * (2 ** max(0, throttle_failures - 1))
    capped = min(base, _THROTTLE_MAX_BACKOFF_SECONDS)
    jitter = random.uniform(0, capped * 0.25)  # nosec B311 - backoff jitter, not security-sensitive
    return float(capped + jitter)


class SchemaDesignTrace:
    """Collects designer iterations and PE reviews for the design_trace artifact."""

    def __init__(self) -> None:
        self.entries: list[dict] = []

    def log_designer(self, iteration: int, elapsed: float, output: BaseModel) -> None:
        self.entries.append(
            {
                "type": "designer",
                "iteration": iteration,
                "elapsed_seconds": round(elapsed, 1),
                "output_summary": self._summarize(output),
            }
        )

    def log_pe_review(self, iteration: int, elapsed: float, review: Any) -> None:
        self.entries.append(
            {
                "type": "pe_review",
                "iteration": iteration,
                "elapsed_seconds": round(elapsed, 1),
                "verdict": getattr(review.verdict, "value", str(review.verdict)),
                "change_requests": len(getattr(review, "change_requests", [])),
                "summary": (getattr(review, "summary", "") or "")[:200],
            }
        )

    def log_pe_error(self, iteration: int, error: str) -> None:
        self.entries.append(
            {
                "type": "pe_error",
                "iteration": iteration,
                "error": error[:500],
            }
        )

    def to_dict(self) -> dict:
        return {"trace": self.entries}

    @staticmethod
    def _summarize(output: BaseModel) -> dict:
        """Extract key counts from any schema output model."""
        d = output.model_dump() if hasattr(output, "model_dump") else {}
        summary: dict = {}
        for key in ("table_definitions", "collections", "cache_structures", "indexes"):
            if key in d:
                summary[key] = len(d[key])
        for key in ("access_patterns", "query_patterns"):
            if key in d:
                summary[key] = len(d[key])
        return summary


class SchemaDesignRunner:
    """Shared PE review loop with retry, fallback, and duplicate detection.

    Works with any Pydantic output model and any PE review model.
    The PE review model must have:
      - verdict: enum with value "APPROVED" for approval
      - change_requests: list with items having category, target, severity, requested_change
      - pe_notes: list[str]
      - strengths: list[str]
      - summary: str
    """

    def __init__(
        self,
        target_type: str,
        output_model: type[T],
        model: BedrockModel,
        designer_agent: Agent,
        pe_skill_path: str | None = None,
        pe_reviewer_fn=None,
        format_pe_feedback_fn=None,
    ) -> None:
        self.target_type = target_type
        self.output_model = output_model
        self.model = model
        self.designer = designer_agent
        self.pe_skill_path = pe_skill_path
        self._invoke_pe_reviewer = pe_reviewer_fn
        self._format_pe_feedback = format_pe_feedback_fn
        self.trace = SchemaDesignTrace()

    def run(self, designer_prompt: str, input_summary: dict | None = None) -> tuple[T, dict]:
        """Run the full designer + PE review loop. Returns (output, trace_dict)."""
        prefix = f"[schema-design/{self.target_type}]"

        # --- Iteration 0: initial design ---
        print(f"{prefix} === Designer iteration 1/{MAX_PE_ITERATIONS} ===")
        t0 = time.time()
        design_output: T = self._invoke_designer(designer_prompt)
        elapsed = time.time() - t0
        self.trace.log_designer(0, elapsed, design_output)
        print(f"{prefix} Designer iteration 1 complete in {elapsed:.0f}s")

        if input_summary is None:
            input_summary = {}

        if self._invoke_pe_reviewer is None:
            return design_output, self.trace.to_dict()

        # --- PE review loop ---
        previous_change_keys: set[str] = set()

        for iteration in range(MAX_PE_ITERATIONS):
            print(f"{prefix} === PE review {iteration + 1}/{MAX_PE_ITERATIONS} ===")

            try:
                t0 = time.time()
                review = self._invoke_pe_reviewer(
                    self.model, design_output, input_summary, self.pe_skill_path
                )
                elapsed = time.time() - t0
                self.trace.log_pe_review(iteration, elapsed, review)
                print(f"{prefix} PE review {iteration + 1} complete in {elapsed:.0f}s")
            except Exception as exc:
                print(f"{prefix} PE review failed: {exc} — accepting design")
                logger.warning("PE review failed: %s — accepting design", exc)
                self.trace.log_pe_error(iteration, str(exc))
                break

            # Check verdict — compare the string value for cross-model compat
            verdict_value = getattr(review.verdict, "value", str(review.verdict))
            if verdict_value.upper() == "APPROVED":
                print(f"{prefix} ✅ PE approved on iteration {iteration + 1}")
                if getattr(review, "pe_notes", None):
                    self._append_trade_offs(
                        design_output, [f"[PE note] {n}" for n in review.pe_notes]
                    )
                break

            # --- Check for repeated feedback ---
            change_requests = getattr(review, "change_requests", [])
            current_change_keys = set()
            for cr in change_requests:
                cat = getattr(
                    getattr(cr, "category", ""), "value", str(getattr(cr, "category", ""))
                )
                tgt = getattr(cr, "target", "")
                sev = getattr(
                    getattr(cr, "severity", ""), "value", str(getattr(cr, "severity", ""))
                )
                current_change_keys.add(f"{cat}:{tgt}:{sev}")

            overlap = current_change_keys & previous_change_keys
            if overlap and len(overlap) >= len(current_change_keys) * 0.7:
                print(
                    f"{prefix} ⚠️ PE requesting same changes as last iteration "
                    f"({len(overlap)}/{len(current_change_keys)} overlap) — accepting design"
                )
                logger.warning(
                    "PE feedback loop detected — %d/%d changes repeated",
                    len(overlap),
                    len(current_change_keys),
                )
                self._append_trade_offs(
                    design_output,
                    [f"[PE note] {len(overlap)} change(s) could not be resolved after revision."],
                )
                if getattr(review, "pe_notes", None):
                    self._append_trade_offs(
                        design_output, [f"[PE note] {n}" for n in review.pe_notes]
                    )
                break
            previous_change_keys = current_change_keys

            # --- Max iterations check ---
            if iteration + 1 >= MAX_PE_ITERATIONS:
                print(f"{prefix} ⚠️ Max PE iterations reached — accepting with notes")
                if getattr(review, "pe_notes", None):
                    self._append_trade_offs(
                        design_output, [f"[PE note] {n}" for n in review.pe_notes]
                    )
                break

            # --- Designer revision ---
            feedback_text = (
                self._format_pe_feedback(review)
                if self._format_pe_feedback
                else self._default_format_feedback(review)
            )
            print(f"{prefix} PE feedback ({len(change_requests)} changes):")
            for cr in change_requests:
                sev = getattr(getattr(cr, "severity", ""), "value", "?")
                cat = getattr(getattr(cr, "category", ""), "value", "?")
                tgt = getattr(cr, "target", "?")
                chg = getattr(cr, "requested_change", "?")
                print(f"  - [{sev}] {cat}: {tgt} — {chg}")

            table_names = self._get_table_names(design_output)
            revision_prompt = (
                f"The PE reviewer has requested changes to your {self.target_type} design. "
                f"Your current design has these tables/collections: {', '.join(table_names)}.\n\n"
                f"PE feedback:\n{feedback_text}\n\n"
                "Apply ONLY the requested changes. Keep all approved items intact. "
                f"Return the complete revised output."
            )

            print(
                f"{prefix} === Designer revision {iteration + 2}/{MAX_PE_ITERATIONS} "
                f"({len(change_requests)} changes requested) ==="
            )
            t0 = time.time()
            design_output = self._invoke_designer(revision_prompt, previous_output=design_output)
            elapsed = time.time() - t0
            self.trace.log_designer(iteration + 1, elapsed, design_output)
            print(f"{prefix} Designer revision {iteration + 2} complete in {elapsed:.0f}s")

        return design_output, self.trace.to_dict()

    def _invoke_designer(self, prompt: str, previous_output: T | None = None) -> T:
        """Invoke designer with retries and graceful fallback."""
        last_error: Exception | None = None
        last_partial_text: str | None = None
        prefix = f"[schema-design/{self.target_type}]"

        # Two independent budgets: genuine errors fail fast (MAX_DESIGNER_RETRIES),
        # throttling waits it out (larger, backoff + jitter). The loop stops as
        # soon as EITHER budget is exhausted.
        regular_max = MAX_DESIGNER_RETRIES
        throttle_max = _throttle_max_retries()
        regular_failures = 0
        throttle_failures = 0
        attempt = 0
        while regular_failures < regular_max and throttle_failures < throttle_max:
            attempt += 1
            try:
                print(f"{prefix} Invoking model (attempt {attempt})...")
                result = self.designer(prompt)
                output = getattr(result, "structured_output", None)
                if isinstance(output, self.output_model):
                    print(f"{prefix} Valid structured output")
                    return output  # type: ignore[return-value]

                raw_text = str(result)
                last_partial_text = raw_text
                parsed = json.loads(raw_text)
                validated = self.output_model.model_validate(parsed)
                print(f"{prefix} Parsed from text")
                return validated  # type: ignore[return-value]

            except Exception as exc:
                last_error = exc
                if _is_throttle_error(exc):
                    throttle_failures += 1
                    kind, backoff = "throttle", _throttle_backoff(throttle_failures)
                else:
                    regular_failures += 1
                    kind = "error"
                    backoff = BASE_BACKOFF_SECONDS * (2 ** max(0, regular_failures - 1))
                print(f"{prefix} Attempt {attempt} failed ({kind}): {exc}")
                if regular_failures >= regular_max or throttle_failures >= throttle_max:
                    break
                print(f"{prefix} Retry after {backoff:.1f}s")
                time.sleep(backoff)  # nosemgrep: arbitrary-sleep

        # --- Graceful fallback ---
        if previous_output is not None:
            print(f"{prefix} ⚠️ All retries failed — returning previous valid output")
            self._append_trade_offs(
                previous_output,
                [
                    (  # nosemgrep: string-concat-in-list
                        f"[WARNING] Revision failed after {attempt} attempts ({last_error}). "
                        f"Returning last valid design."
                    )
                ],
            )
            return previous_output  # type: ignore[return-value]

        if last_partial_text:
            try:
                partial = json.loads(last_partial_text)
                salvaged = self.output_model.model_validate(partial)
                print(f"{prefix} ⚠️ Salvaged partial output")
                self._append_trade_offs(
                    salvaged, ["[WARNING] Schema design produced from partial LLM output."]
                )
                return salvaged  # type: ignore[return-value]
            except (json.JSONDecodeError, ValueError, TypeError):
                logger.warning("Could not salvage partial output from last attempt")

        raise RuntimeError(f"Designer failed after {attempt} attempts: {last_error}")

    def _append_trade_offs(self, output: BaseModel, notes: list[str]) -> None:
        """Append trade-off notes to the output if it has a trade_offs field.

        Converts plain strings (PE notes, warnings) into TradeOff objects.
        """
        if hasattr(output, "trade_offs") and isinstance(output.trade_offs, list):
            output.trade_offs.extend(
                TradeOff(
                    description=note,
                    impact=note,
                    engine=self.target_type,
                )
                for note in notes
            )

    @staticmethod
    def _get_table_names(output: BaseModel) -> list[str]:
        """Extract table/collection names from any schema output model."""
        d = output.model_dump() if hasattr(output, "model_dump") else {}
        for key in ("table_definitions", "collections", "cache_structures", "indexes"):
            if key in d and isinstance(d[key], list):
                return [
                    item.get("table_name") or item.get("collection_name") or item.get("name", "?")
                    for item in d[key]
                ]
        return []

    @staticmethod
    def _default_format_feedback(review: Any) -> str:
        """Default PE feedback formatter — works with any PE review model."""
        lines = [f"Summary: {getattr(review, 'summary', '')}"]
        for cr in getattr(review, "change_requests", []):
            sev = getattr(getattr(cr, "severity", ""), "value", "?")
            cat = getattr(getattr(cr, "category", ""), "value", "?")
            tgt = getattr(cr, "target", "?")
            chg = getattr(cr, "requested_change", "?")
            rationale = getattr(cr, "rationale", "?")
            lines.append(f"- [{sev}] {cat} on {tgt}: {chg} (reason: {rationale})")
        return "\n".join(lines)
