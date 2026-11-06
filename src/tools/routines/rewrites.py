# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Deterministic rewrites of one routine's header (#1937), no model.

A rewrite changes only the routine's header, never its body, from the parser's positions
(never a pattern over the text). Each one comes from a rule of
``src/catalog/routine_rewrites/`` (:mod:`src.shared.catalog.routine_rewrites`) and is
recorded with the rule's id, its reason and a unified diff. What each one changes:

- **MySQL** (:func:`rewrite_mysql_routine`): the ``DEFINER`` names the target user. A
  ``DEFINER`` clause naming anyone else is replaced; a text with no clause whose
  metadata names someone else gets an explicit clause after ``CREATE`` (the target would
  make the creating user its definer anyway). A ``CURRENT_USER`` form, or a clause that
  already names the target user, is left as it is. A target user given with no host is
  ``user@%``, the account the target has, so a clause naming ``user@localhost`` is
  rewritten too. The routine's definer check (``mysql-routine-definer``) is resolved by
  it. This changes the account the routine runs as when it runs as its definer (``SQL
  SECURITY DEFINER``, the default, and every trigger and event): the record then carries
  a ``runs_as_target_user`` note naming the old account. An ``SQL SECURITY INVOKER``
  routine runs as its caller, so it gets no note.
- **PostgreSQL** (:func:`rewrite_postgresql_routine`):
  - the owner of a function or procedure becomes the target user. The statement names
    no owner (``pg_get_functiondef``): the routine is owned by the user that creates it,
    so only the record changes, and the diff is the owner's. A ``SECURITY DEFINER``
    routine runs with its owner's privileges, so it gets a ``runs_as_target_user`` note
    naming the old owner;
  - a ``SECURITY DEFINER`` function or procedure that sets no ``search_path`` gets a
    ``SET search_path TO ...`` clause naming the source's effective path (an input), put
    just before its ``AS`` clause (or before its first option, for an SQL-standard
    body). The pin keeps the name resolution the routine had on the source; it is not
    hardening (no ``pg_temp`` is added). A ``SECURITY INVOKER`` routine resolves names in
    its caller's path by design, and a body that reads, sets or resets the path would
    behave differently with a pinned one (a function-level ``SET`` restores the path on
    exit): both are left as they are. The body's tokens decide that: it names
    ``search_path``, calls ``set_config``, ``current_schema`` or ``current_schemas``, runs
    ``EXECUTE``, ``RESET`` or ``DISCARD``, or sets the role or session authorization
    (which changes what ``$user`` means).

Dependency order and the dependency check are :mod:`src.tools.routines.dependencies`'.

The output is two parts (epic #1925's two files):

- :class:`RoutineRewriteRecord`: the status, the rewrites and the checks still failing.
  It never holds a literal: each diff is of the header only, with zero context lines,
  and every literal and comment in it written as ``?``. It may go into the artifact;
  no diff goes into a log or a model input.
- :attr:`RewrittenRoutine.target_create`: the exact text to create on the target, literals
  included. It belongs with the routine sources, never in a report, log or model input;
  it is left out of the object's ``repr``.

A routine's ``status`` is ``needs_attention`` when a check other than an informational one
still fails after its rewrites, else ``rewritten`` when a rewrite applied, else ``as_is``.
A routine whose statement does not parse is never rewritten. Each rewritten text is read
again by its engine's reader; a rewrite whose result does not read is dropped. The same
input always gives the same output, byte for byte. Each routine is rewritten on its own,
in time linear in its text's length.

What the later artifact issue reads, per routine: ``record.routine_id``, ``record.kind``,
``record.status``, ``record.owner``, ``record.rewrites`` (``rule_id``, ``rewrite``,
``reason``, ``diff``), ``record.notes`` (``code``, ``reason``), ``record.failed_rules``,
and ``target_create`` for the routine
sources; and :class:`~src.tools.routines.dependencies.DependencyOrder` for the step.
"""

from __future__ import annotations

import difflib
import unicodedata
from collections.abc import Iterable, Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from src.shared.catalog.routine_rewrites import (
    RoutineRewriteKind,
    RoutineRewriteRule,
    routine_rewrite_rules,
)
from src.tools.routines import mysql_checks, postgresql_checks
from src.tools.routines.mysql_checks import MySqlRoutine
from src.tools.routines.postgresql_checks import PostgresRoutine
from src.tools.routines.verdict import RoutineVerdict
from src.tools.sql.facade import RoutineToken, RoutineTokenError, routine_tokens

__all__ = [
    "RewriteNote",
    "RewriteStatus",
    "RewrittenRoutine",
    "RoutineRewrite",
    "RoutineRewriteRecord",
    "masked",
    "rewrite_mysql_routine",
    "rewrite_postgresql_routine",
]

RewriteStatus = Literal["as_is", "rewritten", "needs_attention"]

# A record's config: frozen, strict, and no validation error repeats its input.
_RECORD = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
# Words in a PostgreSQL body that read or change the search_path (lower case).
_PATH_WORDS = frozenset(
    {
        "search_path",
        "set_config",
        "current_schema",
        "current_schemas",
        "execute",
        "reset",
        "discard",
    }
)
# What may follow SET (and SET SESSION / SET LOCAL) to change what ``$user`` resolves to.
_ROLE_WORDS = frozenset({"role", "authorization"})
# Characters no name may hold: control characters (C0, DEL, C1) and line or paragraph
# separators, which would also split a recorded diff line.
_BAD_CATEGORIES = frozenset({"Cc", "Zl", "Zp"})
_PATH_LANGUAGES = frozenset({"plpgsql", "sql"})


class RoutineRewrite(BaseModel):
    """One rewrite applied to a routine: its rule, why, and the change as a unified diff
    with zero context lines, every literal and comment in it written as ``?``."""

    model_config = _RECORD

    rule_id: str
    rewrite: RoutineRewriteKind
    reason: str
    diff: str = Field(min_length=1)


class RewriteNote(BaseModel):
    """Something a rewrite changed that the customer should see. ``runs_as_target_user``:
    the routine now runs with the target user's privileges, not the account it ran as on
    the source (named in ``reason``)."""

    model_config = _RECORD

    code: Literal["runs_as_target_user"]
    rule_id: str
    reason: str


class RoutineRewriteRecord(BaseModel):
    """What the rewrites did to one routine, with no literal in it.

    - ``status``: ``as_is``, ``rewritten`` or ``needs_attention`` (module docstring).
    - ``owner``: the user that owns the routine on the target (a PostgreSQL function or
      procedure), else None.
    - ``rewrites``: the rewrites applied, in the catalog's rule order.
    - ``notes``: what the rewrites changed that the customer should see
      (:class:`RewriteNote`); a note does not change the status.
    - ``failed_rules``: the check rules that still fail (not informational) after them.
    """

    model_config = _RECORD

    routine_id: str
    kind: str
    status: RewriteStatus
    owner: str | None = None
    rewrites: tuple[RoutineRewrite, ...] = ()
    notes: tuple[RewriteNote, ...] = ()
    failed_rules: tuple[str, ...] = ()


class RewrittenRoutine(BaseModel):
    """A routine after its rewrites: the record, and the exact text to create on the
    target (literals included; left out of ``repr``)."""

    model_config = _RECORD

    record: RoutineRewriteRecord
    target_create: str = Field(repr=False)


# --------------------------------------------------------------------------- masking


def _is_literal(tok: RoutineToken) -> bool:
    return tok.command_rest or tok.kind == "NUMBER" or tok.kind.endswith("STRING")


def masked(
    text: str,
    tokens: Iterable[RoutineToken],
    start: int = 0,
    end: int | None = None,
    *,
    keep: tuple[int, int] | None = None,
) -> str:
    """``text[start:end]`` with every literal token (strings, numbers) written as ``?``
    and every run of other characters no token covers (comments) as ``?``; whitespace
    and the other tokens are kept as written. A token inside ``keep`` (a ``DEFINER``
    clause, whose quoted parts are an account name, not data) is kept as written too.
    ``tokens`` are at their offsets in ``text``, in order. Linear in the region's length
    plus the tokens'."""
    end = len(text) if end is None else end
    out: list[str] = []
    pos = start

    def gap(upto: int) -> None:
        run = False
        for c in text[pos:upto]:
            if c.isspace():
                out.append(c)
                run = False
            elif not run:
                out.append("?")
                run = True

    for tok in tokens:
        if tok.command_rest or tok.end < start or tok.start >= end:
            continue
        gap(tok.start)
        kept = keep is not None and keep[0] <= tok.start and tok.end < keep[1]
        out.append("?" if _is_literal(tok) and not kept else text[tok.start : tok.end + 1])
        pos = tok.end + 1
    gap(end)
    return "".join(out)


def _diff(before: str, after: str, label: str = "create") -> str:
    lines = difflib.unified_diff(
        before.splitlines(),
        after.splitlines(),
        fromfile=f"source/{label}",
        tofile=f"target/{label}",
        n=0,
        lineterm="",
    )
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- shared


def _rules(
    rules: Iterable[RoutineRewriteRule] | None,
    rewrite: RoutineRewriteKind,
    engine: str,
    target: str,
    kind: str,
) -> RoutineRewriteRule | None:
    for rule in routine_rewrite_rules() if rules is None else rules:
        if (
            rule.match.rewrite == rewrite
            and engine in rule.source_engines
            and target in rule.targets
            and kind in rule.kinds
        ):
            return rule
    return None


def _check_verdict(routine_id: str, kind: str, verdict: RoutineVerdict) -> None:
    if verdict.routine_id != routine_id or verdict.kind != kind:
        raise ValueError("the verdict is not this routine's")


def _record(
    routine_id: str,
    kind: str,
    verdict: RoutineVerdict,
    rewrites: list[tuple[RoutineRewriteRule, str]],
    owner: str | None = None,
    notes: Sequence[RewriteNote] = (),
) -> RoutineRewriteRecord:
    resolved = {check for rule, _ in rewrites for check in rule.match.resolves}
    failed = tuple(
        c.rule_id
        for c in verdict.checks
        if c.outcome == "fail" and c.impact != "informational" and c.rule_id not in resolved
    )
    status: RewriteStatus = "needs_attention" if failed else ("rewritten" if rewrites else "as_is")
    return RoutineRewriteRecord(
        routine_id=routine_id,
        kind=kind,
        status=status,
        owner=owner,
        rewrites=tuple(
            RoutineRewrite(rule_id=r.id, rewrite=r.match.rewrite, reason=r.reason, diff=d)
            for r, d in sorted(rewrites, key=lambda pair: pair[0].id)
        ),
        notes=tuple(notes),
        failed_rules=failed,
    )


def _runs_as(rule: RoutineRewriteRule, old: str, new: str) -> RewriteNote:
    return RewriteNote(
        code="runs_as_target_user",
        rule_id=rule.id,
        reason=(
            f"The routine ran as {old} on the source; it now runs with the privileges of "
            f"the target user {new}, so check that user may do everything the routine does."
        ),
    )


def _bad_name(name: str) -> bool:
    return any(unicodedata.category(c) in _BAD_CATEGORIES for c in name)


# --------------------------------------------------------------------------- MySQL


def _backticked(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


def _mysql_target(target_user: str) -> tuple[str, str]:
    """The target account as ``(user, host)``; a target with no host is ``user@%``."""
    if _bad_name(target_user):
        raise ValueError("target_user holds a control or line-separator character")
    account = mysql_checks.parse_account(target_user)
    if account == "current_user":
        raise ValueError("target_user must name an account, not CURRENT_USER")
    user, host = account
    return user, host or "%"


def _account(account: tuple[str, str | None] | None) -> str:
    if account is None:
        return "an account the source names that could not be read"
    user, host = account
    return f"{user}@{host or '%'}"


def rewrite_mysql_routine(
    routine: MySqlRoutine,
    verdict: RoutineVerdict,
    *,
    target_user: str,
    rules: Iterable[RoutineRewriteRule] | None = None,
) -> RewrittenRoutine:
    """``routine`` with its ``DEFINER`` naming ``target_user`` (``admin`` or ``admin@%``)
    when a ``definer`` rule applies (module docstring). ``verdict`` is the routine's
    :func:`~src.tools.routines.mysql_checks.check_mysql_routine` verdict. A
    ``target_user`` that is not an account, or a verdict for another routine, is a
    ValueError; the routine's text never raises."""
    _check_verdict(routine.routine_id, routine.kind, verdict)
    user, host = _mysql_target(target_user)
    notes: list[RewriteNote] = []
    text = routine.create_sql
    engine = mysql_checks.SOURCE_ENGINE
    rule = _rules(rules, "definer", engine, verdict.target_engine, routine.kind)
    header = mysql_checks.mysql_header(routine) if verdict.parse_failure is None else None
    applied: list[tuple[RoutineRewriteRule, str]] = []
    new_text = text
    if rule is not None and header is not None:
        account = header.account
        if account is None and routine.definer:
            try:
                account = mysql_checks.parse_account(routine.definer)
            except ValueError:
                account = None  # unreadable: an explicit clause settles it
        needed = account is None and bool(routine.definer)
        if account is not None and not mysql_checks.is_target_user(account, (user, host)):
            needed = True
        if needed:
            clause = f"DEFINER={_backticked(user)}@{_backticked(host)}"
            if header.definer is not None:
                start, stop = header.definer
                replacement = clause
            else:
                start = stop = header.tokens[0].end + 1  # just after CREATE
                replacement = " " + clause
            candidate = text[:start] + replacement + text[stop:]
            diff = _mysql_diff(text, candidate, header, start, stop, replacement, routine)
            if diff is not None:
                new_text = candidate
                applied.append((rule, diff))
                security = header.security or routine.security_type or "DEFINER"
                if routine.kind in ("trigger", "event") or security != "INVOKER":
                    old = None if account == "current_user" else account
                    notes.append(_runs_as(rule, _account(old), f"{user}@{host}"))
    return RewrittenRoutine(
        record=_record(routine.routine_id, routine.kind, verdict, applied, notes=notes),
        target_create=new_text,
    )


def _mysql_diff(
    text: str,
    candidate: str,
    header: mysql_checks.MySqlHeader,
    start: int,
    stop: int,
    replacement: str,
    routine: MySqlRoutine,
) -> str | None:
    """The header diff of a DEFINER rewrite, or None when the rewritten text does not read
    as the same routine with the same body."""
    after_routine = routine.model_copy(update={"create_sql": candidate})
    after = mysql_checks.mysql_header(after_routine)
    shift = len(replacement) - (stop - start)
    if (
        after is None
        or after.body_start != header.body_start + shift
        or candidate[after.body_start :] != text[header.body_start :]
    ):
        return None
    begin = header.tokens[0].start
    return _diff(
        masked(text, header.tokens, begin, header.body_start, keep=header.definer),
        masked(candidate, after.tokens, begin, after.body_start, keep=after.definer),
    )


# --------------------------------------------------------------------------- PostgreSQL


def _quoted_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _valid_name(name: str) -> bool:
    return bool(name) and not _bad_name(name)


def _body_touches_path(text: str, header: postgresql_checks.PostgresHeader) -> bool:
    """Whether the body may read or change the search_path (or cannot be read to tell)."""
    if header.insert_at is None:
        return True
    body = header.body if header.body is not None else text[header.insert_at :]
    try:
        tokens = routine_tokens(body, postgresql_checks.SOURCE_ENGINE)
    except RoutineTokenError:
        return True
    words: list[str] = []
    for tok in tokens:
        word = tok.text.lower()
        if _is_literal(tok):
            # A string may name the path (set_config('search_path', ...)); it is read
            # here, never recorded.
            if "search_path" in word:
                return True
            words.append("?")
        elif word in _PATH_WORDS:
            return True
        else:
            words.extend(word.split())
    # SET [SESSION | LOCAL] ROLE ... and SET SESSION AUTHORIZATION ... change $user.
    for i, word in enumerate(words):
        if word == "set" and _ROLE_WORDS & set(words[i + 1 : i + 4]):
            return True
    return False


def _path_clause(text: str, at: int, path: Sequence[str]) -> str:
    """The ``SET search_path`` clause to put in at ``at``, laid out like its neighbours."""
    clause = "SET search_path TO " + ", ".join(_quoted_identifier(p) for p in path)
    line_start = text.rfind("\n", 0, at) + 1
    indent = text[line_start:at]
    if line_start == 0 or indent.strip():
        return clause + " "  # mid-line: the clause goes in before the next word
    if not indent:
        # ``AS`` starts its line: the clause gets a line of its own above it, indented as
        # pg_get_functiondef indents the options.
        return " " + clause + "\n"
    return clause + "\n" + indent


def _header_tokens(text: str) -> tuple[RoutineToken, ...] | None:
    try:
        return routine_tokens(text, postgresql_checks.SOURCE_ENGINE)
    except RoutineTokenError:
        return None


def rewrite_postgresql_routine(
    routine: PostgresRoutine,
    verdict: RoutineVerdict,
    *,
    target_owner: str,
    search_path: Sequence[str],
    rules: Iterable[RoutineRewriteRule] | None = None,
) -> RewrittenRoutine:
    """``routine`` owned by ``target_owner`` and, when it is ``SECURITY DEFINER``, with
    ``search_path`` (the schemas it resolved names in on the source, in order) pinned,
    when an ``owner`` or ``search_path`` rule applies (module docstring). ``verdict`` is
    the routine's :func:`~src.tools.routines.postgresql_checks.check_postgresql_routine`
    verdict. An empty ``target_owner`` or ``search_path``, a name holding a control
    character, or a verdict for another routine is a ValueError; the routine's text never
    raises."""
    _check_verdict(routine.routine_id, routine.kind, verdict)
    if not _valid_name(target_owner):
        raise ValueError("target_owner must be a role name with no control character")
    path = tuple(search_path)
    if not path or not all(path):
        raise ValueError("search_path must name at least one schema, none of them empty")
    if any(_bad_name(p) for p in path):
        raise ValueError("a search_path schema name holds a control or line-separator character")
    engine, target, kind = postgresql_checks.SOURCE_ENGINE, verdict.target_engine, routine.kind
    applied: list[tuple[RoutineRewriteRule, str]] = []
    text = routine.create_sql
    owner: str | None = None
    notes: list[RewriteNote] = []

    owner_rule = _rules(rules, "owner", engine, target, kind)
    if owner_rule is not None:
        owner = target_owner
        if routine.owner is not None and routine.owner != target_owner:
            applied.append((owner_rule, _diff(routine.owner, target_owner, "owner")))
            header_now = postgresql_checks.postgresql_header(text, routine.kind)
            definer = routine.security_definer or (
                header_now is not None and header_now.security_definer
            )
            if definer:
                notes.append(_runs_as(owner_rule, routine.owner, target_owner))

    path_rule = _rules(rules, "search_path", engine, target, kind)
    header = (
        postgresql_checks.postgresql_header(text, routine.kind)
        if path_rule is not None and verdict.parse_failure is None
        else None
    )
    if (
        path_rule is not None
        and header is not None
        and header.security_definer
        and not header.sets_search_path
        and header.insert_at is not None
        and routine.language in _PATH_LANGUAGES
        and not _body_touches_path(text, header)
    ):
        at = header.insert_at
        candidate = text[:at] + _path_clause(text, at, path) + text[at:]
        before_tokens = _header_tokens(text[:at])
        added = len(candidate) - len(text)
        after_tokens = _header_tokens(candidate[: at + added])
        after = postgresql_checks.postgresql_header(candidate, routine.kind)
        if (
            before_tokens is not None
            and after_tokens is not None
            and after is not None
            and after.sets_search_path
            and after.body == header.body
            and candidate[at + added :] == text[at:]
        ):
            text = candidate
            applied.append(
                (
                    path_rule,
                    _diff(
                        masked(routine.create_sql, before_tokens, 0, at),
                        masked(candidate, after_tokens, 0, at + added),
                    ),
                )
            )
    return RewrittenRoutine(
        record=_record(routine.routine_id, routine.kind, verdict, applied, owner, notes),
        target_create=text,
    )
