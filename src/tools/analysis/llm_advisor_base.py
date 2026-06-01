"""Generic LLM Advisor base class with group-splitting for large workloads.

All engine-specific LLM advisors inherit from this base class. It provides:
- Retry with exponential backoff (3 attempts: immediate, 1s, 2s)
- Automatic group splitting when queries exceed MAX_LLM_QUERIES
- Schema filtering per group (only tables referenced by the group's queries)
- Result merging across groups

Subclasses implement:
- _build_prompt(): format the LLM prompt for a single group
- _parse_result(): extract structured output from the LLM response
- _merge_results(): combine outputs from multiple groups into one
"""

from __future__ import annotations

import logging
import os
import time
from abc import ABC, abstractmethod
from typing import TypeVar

from pydantic import BaseModel

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# Default thresholds
MAX_LLM_QUERIES = 30
MAX_RETRIES = 3
BASE_BACKOFF_SECONDS = 1.0


class LlmAdvisorBase(ABC):
    """Base class for engine-specific LLM advisors with group splitting."""

    MAX_LLM_QUERIES: int = MAX_LLM_QUERIES
    MAX_RETRIES: int = MAX_RETRIES
    BASE_BACKOFF_SECONDS: float = BASE_BACKOFF_SECONDS

    def __init__(
        self,
        system_prompt: str,
        enabled: bool | None = None,
    ):
        self.system_prompt = system_prompt
        self.enabled = (
            enabled
            if enabled is not None
            else os.environ.get("ENABLE_LLM_ADVISOR", "true").lower() == "true"
        )
        self._agent = None
        self.attempts_made = 0

    def _get_agent(self):
        """Lazily create the Strands Agent on first use."""
        if self._agent is None:
            from strands import Agent
            from strands.models.bedrock import BedrockModel

            model_id = os.environ.get("ANALYSIS_MODEL_ID", "us.anthropic.claude-sonnet-4-6-v1")
            model = BedrockModel(model_id=model_id)
            self._agent = Agent(
                model=model,
                system_prompt=self.system_prompt,
                tools=[],
                structured_output_model=self._output_model(),
                callback_handler=None,
            )
        return self._agent

    @abstractmethod
    def _output_model(self) -> type[BaseModel]:
        """Return the Pydantic model class for structured output."""

    @abstractmethod
    def _build_prompt(self, schema: dict, queries: list[dict], **kwargs) -> str:
        """Build the LLM prompt for a single group of queries."""

    @abstractmethod
    def _parse_result(self, result) -> BaseModel | None:
        """Parse structured output from the agent result."""

    @abstractmethod
    def _merge_results(self, results: list) -> BaseModel | None:
        """Merge outputs from multiple groups into a single result."""

    @abstractmethod
    def _filter_kwargs_for_group(
        self, group_queries: list[dict], referenced_tables: set[str], **kwargs
    ) -> dict:
        """Filter kwargs (aggregates, candidates, etc.) relevant to a query group."""

    def advise(
        self,
        schema: dict,
        queries: list[dict],
        **kwargs,
    ) -> BaseModel | None:
        """Run LLM advisor. Splits into groups if queries exceed MAX_LLM_QUERIES.

        Args:
            schema: database_schema dict with "tables" key
            queries: list of query pattern dicts
            **kwargs: engine-specific arguments (aggregates, candidates, etc.)

        Returns:
            Structured output model or None if all retries exhausted.
        """
        if not self.enabled:
            return None

        self.attempts_made = 0

        if len(queries) > self.MAX_LLM_QUERIES:
            return self._advise_grouped(schema, queries, **kwargs)

        return self._advise_single(schema, queries, **kwargs)

    def _advise_single(self, schema: dict, queries: list[dict], **kwargs) -> BaseModel | None:
        """Call LLM once for a small workload, with retry."""
        for attempt in range(self.MAX_RETRIES):
            self.attempts_made = attempt + 1
            if attempt > 0:
                time.sleep(self.BASE_BACKOFF_SECONDS * (2 ** (attempt - 1)))
            try:
                return self._call_llm(schema, queries, **kwargs)
            except Exception as exc:  # noqa: B112
                logger.warning(
                    "%s attempt %d/%d failed: %s",
                    self.__class__.__name__,
                    attempt + 1,
                    self.MAX_RETRIES,
                    exc,
                )
        return None

    def _advise_grouped(self, schema: dict, queries: list[dict], **kwargs) -> BaseModel | None:
        """Split large workloads into groups, call LLM per group, merge results."""
        all_tables = schema.get("tables", [])
        table_by_id = {t.get("table_id", t.get("table_name", "")): t for t in all_tables}

        # Split queries into chunks
        groups: list[list[dict]] = []
        for i in range(0, len(queries), self.MAX_LLM_QUERIES):
            groups.append(queries[i : i + self.MAX_LLM_QUERIES])

        logger.info(
            "%s splitting %d queries into %d groups of <= %d",
            self.__class__.__name__,
            len(queries),
            len(groups),
            self.MAX_LLM_QUERIES,
        )

        group_results: list = []

        for group_idx, group_queries in enumerate(groups):
            # Build schema subset: only tables referenced by this group
            referenced_tables: set[str] = set()
            for q in group_queries:
                referenced_tables.update(q.get("tables_accessed", []))

            group_tables = [table_by_id[tid] for tid in referenced_tables if tid in table_by_id]
            group_schema = {**schema, "tables": group_tables}

            # Filter kwargs for this group
            group_kwargs = self._filter_kwargs_for_group(group_queries, referenced_tables, **kwargs)

            group_result = None
            for attempt in range(self.MAX_RETRIES):
                self.attempts_made = attempt + 1
                if attempt > 0:
                    time.sleep(self.BASE_BACKOFF_SECONDS * (2 ** (attempt - 1)))
                try:
                    group_result = self._call_llm(group_schema, group_queries, **group_kwargs)
                    break
                except Exception as exc:  # noqa: B112
                    logger.warning(
                        "%s group %d/%d attempt %d failed: %s",
                        self.__class__.__name__,
                        group_idx + 1,
                        len(groups),
                        attempt + 1,
                        exc,
                    )

            if group_result is None:
                logger.warning(
                    "%s group %d/%d failed after all retries, skipping",
                    self.__class__.__name__,
                    group_idx + 1,
                    len(groups),
                )
                continue

            group_results.append(group_result)
            logger.info(
                "%s group %d/%d done",
                self.__class__.__name__,
                group_idx + 1,
                len(groups),
            )

        if not group_results:
            return None

        return self._merge_results(group_results)

    def _call_llm(self, schema: dict, queries: list[dict], **kwargs) -> BaseModel:
        """Format prompt and call the Strands Agent for structured output."""
        import json as _json

        prompt = self._build_prompt(schema, queries, **kwargs)
        agent = self._get_agent()
        result = agent(prompt)

        # Try structured output first
        output = self._parse_result(result)
        if output is not None:
            return output

        # Fallback: parse from text
        text = str(result)
        parsed = _json.loads(text)
        model_cls = self._output_model()
        return model_cls(**parsed)
