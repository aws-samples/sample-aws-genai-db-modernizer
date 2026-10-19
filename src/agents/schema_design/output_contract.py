"""The output contract every written ``schema_output.json`` must pass (#1186).

Every writer of ``schema-<engine>/v<N>/schema_output.json`` calls
:func:`write_schema_output` (``tests/unit/agents/schema_design/
test_only_the_gate_writes_schema_outputs.py`` fails on any direct write). It
checks a design against its engine's output contract, and a skipped placeholder
(``status: "skipped"``) against ``SkippedSchemaOutput``, before writing. A
design that fails raises :class:`SchemaContractError` and is never written: the
caller sends the errors back for a correction turn (external mode: the
``--merge`` / ``--finalize`` attempts) or fails the phase with them.

Error messages never carry the design's own text (#143): they give the field
path and the contract's message. The names of the tables and patterns they
refer to (model-written) are kept apart in :attr:`SchemaContractError.named`,
for :func:`frame_contract_names` to wrap as untrusted data.
"""

from __future__ import annotations

import json
import re
from collections.abc import Collection
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from src.shared.contract_versioning import stamp_artifact_version
from src.storage.artifact_store import ArtifactStore

#: Prefix of every contract error message, so a reader can tell them from
#: scope violations and merge failures.
CONTRACT_PREFIX = "Contract: "

#: Fields that name a list item, so a reader can find the table or pattern an
#: error is about (a merged design's list index matches no one group draft's).
_NAME_FIELDS = ("table_name", "pattern_id", "collection_name", "index_name", "key_pattern")

#: Longest item name passed on; longer model-written names are cut.
_NAME_CAP = 80


@dataclass(frozen=True)
class ContractIssue:
    """One contract error: its message, and the name of the item it is about."""

    message: str
    name_field: str | None = None
    name: str | None = None

    @property
    def key(self) -> tuple[str, str | None]:
        """Identity across revisions: the message without list indices, plus the name."""
        return re.sub(r"\.\d+(?=[.:])", ".*", self.message), self.name


class SchemaContractError(ValueError):
    """A schema design output fails its output contract."""

    def __init__(self, engine: str, issues: list[ContractIssue]) -> None:
        self.engine = engine
        self.issues = issues
        self.errors = [i.message for i in issues]
        super().__init__(
            f"{engine} schema design fails its output contract: " + "; ".join(self.errors)
        )

    @property
    def named(self) -> list[dict[str, str]]:
        """The model-written name of each item an error is about (untrusted text)."""
        return _named(self.issues)


def engine_contract(engine: str) -> type[BaseModel] | None:
    """The Pydantic output contract of ``engine``'s schema design, or None."""
    from src.contracts.aurora_mysql_model_output import AuroraMySQLModelOutputContract
    from src.contracts.aurora_postgresql_model_output import AuroraPostgresqlModelOutputContract
    from src.contracts.documentdb_model_output import DocumentDBModelOutputContract
    from src.contracts.dynamodb_model_output import DynamoDBModelOutputContract
    from src.contracts.elasticache_model_output import ElastiCacheModelOutputContract
    from src.contracts.opensearch_model_output import OpenSearchModelOutputContract

    contracts: dict[str, type[BaseModel]] = {
        "dynamodb": DynamoDBModelOutputContract,
        "documentdb": DocumentDBModelOutputContract,
        "opensearch": OpenSearchModelOutputContract,
        "elasticache": ElastiCacheModelOutputContract,
        "aurora_postgresql": AuroraPostgresqlModelOutputContract,
        "aurora_mysql": AuroraMySQLModelOutputContract,
    }
    return contracts.get(engine)


def _contract_for(output: Any, engine: str) -> type[BaseModel] | None:
    if isinstance(output, dict) and output.get("status") == "skipped":
        from src.contracts.skipped_placeholders import SkippedSchemaOutput

        return SkippedSchemaOutput
    return engine_contract(engine)


def contract_issues(output: Any, engine: str) -> list[ContractIssue]:
    """Every contract error of ``output`` (a design, or a skipped placeholder)."""
    contract = _contract_for(output, engine)
    if contract is None:
        return [ContractIssue(f"{CONTRACT_PREFIX}no output contract registered for {engine!r}")]
    try:
        contract.model_validate(output)
    except ValidationError as exc:
        issues = []
        for error in exc.errors(include_url=False):
            parts = tuple(error.get("loc", ()))
            loc = ".".join(str(part) for part in parts) or "(root)"
            field, name = _item_name(output, parts)
            issues.append(
                ContractIssue(f"{CONTRACT_PREFIX}{loc}: {error.get('msg', 'invalid')}", field, name)
            )
        return issues
    return []


def contract_errors(output: Any, engine: str) -> list[str]:
    """One ``Contract: <field path>: <message>`` per error; empty when it passes.

    For example ``Contract: table_definitions.13: Value error, 'entities' is
    only valid when aggregate_pattern='item_collection'.`` No text of the
    design itself is included.
    """
    return [i.message for i in contract_issues(output, engine)]


def _item_name(output: Any, loc: tuple) -> tuple[str | None, str | None]:
    """The naming field and (capped) value of the list item at ``loc[:2]``."""
    if len(loc) < 2 or not isinstance(output, dict) or not isinstance(loc[1], int):
        return None, None
    items = output.get(loc[0])
    if not isinstance(items, list) or not 0 <= loc[1] < len(items):
        return None, None
    item = items[loc[1]]
    if not isinstance(item, dict):
        return None, None
    for field in _NAME_FIELDS:
        value = item.get(field)
        if isinstance(value, str) and value:
            return field, value[:_NAME_CAP]
    return None, None


def _named(issues: list[ContractIssue]) -> list[dict[str, str]]:
    return [
        {"error": i.message, i.name_field: i.name}
        for i in issues
        if i.name_field is not None and i.name is not None
    ]


def frame_contract_names(named: list[dict[str, str]]) -> str | None:
    """The table and pattern names behind contract errors, framed as untrusted data."""
    if not named:
        return None
    from src.agents.prompt_framing import frame_untrusted

    return frame_untrusted(
        json.dumps(named, indent=1),
        label="the names, as the group drafts spell them, of the items the Contract errors are about",
    )


def _gate_issues(output: dict, engine: str) -> list[ContractIssue]:
    """The write gate's check (tests that only exercise something else turn it off)."""
    return contract_issues(output, engine)


def write_schema_output(
    store: ArtifactStore,
    key: str,
    output: dict,
    engine: str,
    *,
    tolerated: Collection[tuple[str, str | None]] = (),
) -> dict:
    """Check ``output`` against its contract, then stamp and write it.

    Raises :class:`SchemaContractError`, writing nothing, when it fails.
    ``tolerated``: :attr:`ContractIssue.key` values written anyway (a revision
    of a saved design keeps the errors it did not introduce). A ``not_implemented``
    placeholder of an engine with no designer contract is written as is.
    Returns the stamped output that was written.
    """
    stamped = stamp_artifact_version(key, output)
    if _contract_for(stamped, engine) is not None:
        refused = [i for i in _gate_issues(stamped, engine) if i.key not in set(tolerated)]
        if refused:
            raise SchemaContractError(engine, refused)
    store.write_json(key, stamped)
    return stamped
