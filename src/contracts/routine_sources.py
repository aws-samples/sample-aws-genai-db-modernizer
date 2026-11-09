# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
RoutineSourcesContract Pydantic Models
Version: 1.0
Last Modified: 2026-11-06
Description: The CREATE statement of each collected routine (#1652)

The routine sources hold, per PostgreSQL or MySQL function, procedure, trigger and
event, and per PostgreSQL event trigger (#2023), the statement that creates it on the
target, with its string literals: they are code the target runs as written.
PostgreSQL's and a live MySQL collector's are the server's own text
(``pg_get_functiondef``, ``SHOW CREATE``); the MySQL script's are
rebuilt from ``information_schema``, exact only for a body with no string literal
(``RoutineSource.exact``). They are the one part of a job kept unredacted (the
exception to #1625 recorded on #1652, ADR-034 Decision 4), and no report, model input
or API response reads them; routine migration does.

They are stored apart from the collection, in gzip-compressed chunks of at most
``MAX_CHUNK_SOURCES`` routines (``collector/routine_sources/part-<n>.json.gz``, each a
:class:`RoutineSourcesChunk`), listed by an index, ``collector/routine_sources.json``
(:class:`RoutineSourcesContract`). A writer holds one chunk at a time and a reader
loads one chunk at a time, so a library of any size is carried in bounded memory.

Version History:
- 1.0 (2026-11-06, #1652): First version.
- 1.1 (2026-11-09, #2023): RoutineSource.kind event_trigger and origin pg_event_trigger:
  a PostgreSQL event trigger's CREATE, rebuilt from pg_event_trigger with the server's
  own quoting. Backward compatible: new values only
"""

from __future__ import annotations

import hashlib
import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

#: The most routines one chunk holds (#1652).
MAX_CHUNK_SOURCES = 5000


class RoutineSession(BaseModel):
    """The session a CREATE is replayed in. MySQL parses a body under the sql_mode and
    character set it was created with, and reads an event's times in its time zone; the
    statement is sent encoded in character_set_client. PostgreSQL needs none."""

    sql_mode: str | None = Field(
        None, max_length=4096, description="MySQL: the sql_mode the routine was created with"
    )
    character_set_client: str | None = Field(
        None, max_length=64, description="MySQL: the client character set it was created in"
    )
    collation_connection: str | None = Field(
        None, max_length=64, description="MySQL: the connection collation it was created in"
    )
    database_collation: str | None = Field(
        None, max_length=64, description="MySQL: its database's collation when it was created"
    )
    time_zone: str | None = Field(
        None, max_length=64, description="MySQL: an event's time zone, which its times are in"
    )


class RoutineSource(BaseModel):
    kind: Literal["function", "procedure", "trigger", "event", "event_trigger"] = Field(
        ...,
        description="What the routine is; event_trigger: a PostgreSQL event trigger (#2023)",
    )
    routine_id: str = Field(
        ...,
        min_length=1,
        description=(
            "The collector output's procedure_id, trigger_id or event_id (an event "
            "trigger's is its non_dml_triggers trigger_id)"
        ),
    )
    schema_name: str | None = Field(None, description="The routine's schema or database")
    specific_name: str = Field(
        ...,
        min_length=1,
        description=(
            "Unique per kind and schema: PostgreSQL '<name>_<oid>', a trigger "
            "'<table>.<trigger>', an event trigger its name, MySQL the routine or event name"
        ),
    )
    create_statement: str = Field(
        ..., min_length=1, description="The exact statement that creates it, literals included"
    )
    origin: Literal[
        "pg_get_functiondef",
        "pg_get_triggerdef",
        "pg_event_trigger",
        "show_create",
        "information_schema",
    ] = Field(
        ...,
        description=(
            "Where the statement comes from: PostgreSQL's own text, a PostgreSQL event "
            "trigger rebuilt from pg_event_trigger with the server's own quoting (as "
            "pg_dump does: exact), MySQL's SHOW CREATE (a live collector), or rebuilt from "
            "MySQL's information_schema (the script)"
        ),
    )
    exact: bool = Field(
        True,
        description=(
            "True when create_statement is the server's own text, or a rebuild from a body "
            "with no string or hex literal. False for a rebuild around a literal: "
            "information_schema drops a literal's character-set introducer (_binary'a' "
            "reads 'a'), which can change what the routine does, so it is not carried "
            "until it is collected again with SHOW CREATE"
        ),
    )
    inexact_reason: str | None = Field(
        None, max_length=500, description="Why exact is False, in one sentence"
    )
    session: RoutineSession = Field(
        default_factory=RoutineSession, description="The session to replay it in"
    )
    sha256: str = Field(
        "", pattern=r"^([0-9a-f]{64})?$", description="SHA-256 of create_statement (UTF-8)"
    )

    @model_validator(mode="after")
    def _hash(self) -> RoutineSource:
        digest = hashlib.sha256(self.create_statement.encode("utf-8")).hexdigest()
        if self.sha256 and self.sha256 != digest:
            raise ValueError(
                f"sha256 does not match the create_statement of {self.kind} {self.specific_name}"
            )
        self.sha256 = digest
        return self

    def key(self) -> tuple[str, str, str]:
        """The routine's identity among the sources: kind, schema and specific name."""
        return (self.kind, self.schema_name or "", self.specific_name)


def _one_entry_per_routine(sources: list[RoutineSource]) -> None:
    seen: set[tuple[str, str, str]] = set()
    for s in sources:
        if s.key() in seen:
            raise ValueError(f"two entries for {s.kind} {s.schema_name}.{s.specific_name}")
        seen.add(s.key())


class RoutineSourcesChunk(BaseModel):
    """One chunk file: up to ``MAX_CHUNK_SOURCES`` routines."""

    contract_version: str = Field(
        default="1.1", pattern=r"^\d+\.\d+$", description="Contract version (MAJOR.MINOR)"
    )
    part: int = Field(..., ge=1, description="The chunk's number, from 1")
    sources: list[RoutineSource] = Field(
        ..., min_length=1, max_length=MAX_CHUNK_SOURCES, description="The chunk's routines"
    )

    @model_validator(mode="after")
    def _unique(self) -> RoutineSourcesChunk:
        _one_entry_per_routine(self.sources)
        return self


class RoutineSourcesPart(BaseModel):
    path: str = Field(
        ...,
        pattern=r"^routine_sources/part-[0-9]{4,}\.json\.gz$",
        description="The chunk's path, relative to the job's collector folder",
    )
    count: int = Field(..., ge=1, le=MAX_CHUNK_SOURCES, description="Routines in the chunk")
    sha256: str = Field(
        ..., pattern=r"^[0-9a-f]{64}$", description="SHA-256 of the chunk file's bytes"
    )


#: A finding kind as the scan names it; any other is recorded as ``other``.
FINDING_KIND = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _()-]{0,63}")


def _kind_names(v: list[str]) -> list[str]:
    for kind in v:
        if not FINDING_KIND.fullmatch(kind):
            raise ValueError(f"a finding kind is a kind name, got {kind!r}")
    return v


class RoutineSecretFlag(BaseModel):
    """One routine whose CREATE the credential scan flagged (#1933): which routine, and
    the kinds found, never a value. A hard-coded public address is not a credential: it
    is listed apart, in ``address_kinds``."""

    kind: Literal["function", "procedure", "trigger", "event", "event_trigger"] = Field(
        ..., description="What the routine is"
    )
    schema_name: str | None = Field(None, description="The routine's schema or database")
    specific_name: str = Field(..., min_length=1, description="As in its RoutineSource")
    credential_kinds: list[str] = Field(
        default_factory=list,
        description="What looks like a credential, e.g. 'AWS Access Key', 'password value'",
    )
    address_kinds: list[str] = Field(
        default_factory=list,
        description="A hard-coded public address: 'Public IP (ipv4)', 'Public IP (ipv6)'",
    )

    _credential_names = field_validator("credential_kinds")(_kind_names)
    _address_names = field_validator("address_kinds")(_kind_names)

    @model_validator(mode="after")
    def _something_found(self) -> RoutineSecretFlag:
        if not self.credential_kinds and not self.address_kinds:
            raise ValueError("a flagged routine names at least one finding kind")
        return self


class RoutineSecretScan(BaseModel):
    """How the routine bodies were scanned for credentials (#1933), by the repository's
    own rules (``src/shared/secret_scan.py``), which always run."""

    rule_set: Literal["regex", "regex+detect-secrets"] = Field(
        ...,
        description=(
            "regex: the repository's own rules (credential key-value pairs, AWS keys, "
            "private key headers, provider tokens, public addresses); "
            "regex+detect-secrets: detect-secrets' plugins on top, where it is installed"
        ),
    )
    flagged: list[RoutineSecretFlag] = Field(
        default_factory=list, description="The routines whose CREATE the scan flagged"
    )


class RoutineSourcesContract(BaseModel):
    """The index of a job's routine sources: which chunks hold them, in order. Its
    version moves on its own from the chunk's (the module's history above).

    Version History:
    - 1.0 (2026-11-06, #1652): First version.
    - 1.1 (2026-11-09, #1933): Optional secret_scan (RoutineSecretScan: rule_set, regex
      or regex+detect-secrets, and flagged RoutineSecretFlag entries with
      credential_kinds and address_kinds): what the repository's credential scan found in
      the bodies, each flagged routine with the kinds found, never a value. Backward
      compatible: defaults to None
    """

    contract_version: str = Field(
        default="1.1", pattern=r"^\d+\.\d+$", description="Contract version (MAJOR.MINOR)"
    )
    job_id: str = Field(..., description="The job the collection belongs to")
    source_engine: Literal["postgresql", "mysql", "mariadb"] | None = Field(
        ...,
        description=(
            "The source engine the routines come from (MariaDB's are read as MySQL's: it "
            "moves to Aurora MySQL); None for an empty index"
        ),
    )
    source_version: str | None = Field(None, description="The source engine's version")
    total: int = Field(
        ...,
        ge=0,
        description=(
            "Routines across every chunk; 0 for an emptied index (a store that cannot "
            "delete keeps one when a re-run writes no routine sources)"
        ),
    )
    parts: list[RoutineSourcesPart] = Field(
        ..., description="The chunks, in the order the routines were written"
    )
    secret_scan: RoutineSecretScan | None = Field(
        None,
        description=(
            "The credential scan of the bodies (#1933); None for an index written before it"
        ),
    )

    @model_validator(mode="after")
    def _parts_add_up(self) -> RoutineSourcesContract:
        if sum(p.count for p in self.parts) != self.total:
            raise ValueError("the parts' counts do not add up to total")
        if len({p.path for p in self.parts}) != len(self.parts):
            raise ValueError("two parts name the same chunk")
        return self
