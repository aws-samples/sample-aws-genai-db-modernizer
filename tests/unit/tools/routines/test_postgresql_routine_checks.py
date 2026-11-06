# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Parse and check PostgreSQL routines against Aurora PostgreSQL (#1935): every rule,
pass, fail and edge, on synthetic routines (``postgresql_routines.py``)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.shared.catalog.gaps import AuroraExtensionLines, aurora_extension_lines
from src.shared.catalog.routine_checks import routine_check_rules
from src.tools.analysis.relational_gaps import detect_relational_gaps, extension_landing_line
from src.tools.routines.postgresql_checks import (
    ParseFailure,
    RoutineVerdict,
    check_postgresql_routine,
    parse_routine,
    routine_target_line,
)
from tests.unit.tools.routines import postgresql_routines as fx
from tests.unit.tools.routines.postgresql_routines import routine

TARGET = "aurora_postgresql"
PARSE = "postgresql-routine-unparseable"
LANGUAGE = "postgresql-routine-language"
EXTENSIONS = "postgresql-routine-extension-not-offered"
OWNERSHIP = "postgresql-routine-superuser-security-definer"
FUNCTION_RULES = (PARSE, LANGUAGE, EXTENSIONS, OWNERSHIP)
# The Aurora capability body check every function and procedure gets (#2016 review).
BACKUP = "postgresql-routine-backup-control"
CHECKED_RULES = (*FUNCTION_RULES, BACKUP)


def _verdict(r, line: str = "16", **kwargs) -> RoutineVerdict:
    # A same-major source by default, so no version-change rule (#1951) is in the verdict.
    kwargs.setdefault("source_version", f"{line}.1")
    return check_postgresql_routine(r, target_engine=TARGET, target_line=line, **kwargs)


def _outcome(verdict: RoutineVerdict, rule_id: str) -> str:
    (result,) = [c for c in verdict.checks if c.rule_id == rule_id]
    return result.outcome


# ------------------------------------------------------------------ the verdict shape


def test_a_clean_function_passes_every_rule_with_its_reason_and_fix_hint() -> None:
    verdict = _verdict(routine(fx.PLPGSQL_FUNCTION))
    assert verdict.parsed and not verdict.needs_attention and verdict.failed_rules == ()
    assert [c.rule_id for c in verdict.checks] == list(CHECKED_RULES)
    by_id = {r.id: r for r in routine_check_rules()}
    for result in verdict.checks:
        rule = by_id[result.rule_id]
        assert result.outcome == "pass" and result.findings == ()
        assert (result.reason, result.fix_hint, result.impact, result.check) == (
            rule.reason,
            rule.fix_hint,
            rule.impact,
            rule.match.check,
        )
    assert (verdict.target_engine, verdict.target_line) == (TARGET, "16")


def test_a_routine_that_fails_several_rules_lists_each_one() -> None:
    """Unparseable, in an untrusted language, using an extension Aurora lacks, and a
    superuser's SECURITY DEFINER: four failures, each with its own reason."""
    verdict = _verdict(
        routine(
            fx.UNTERMINATED,
            language="plpython3u",
            security_definer=True,
            owner_is_superuser=True,
            extensions=("acme_ext", "postgis"),
        )
    )
    assert not verdict.parsed and verdict.needs_attention
    assert verdict.failed_rules == FUNCTION_RULES
    findings = {c.rule_id: c.findings for c in verdict.checks}
    assert findings[LANGUAGE] == ("plpython3u",)
    assert findings[EXTENSIONS] == ("acme_ext",)
    assert len({c.reason for c in verdict.checks if c.outcome == "fail"}) == 4


def test_the_verdict_never_holds_the_routine_text() -> None:
    """The parser's message quotes the statement, literals included: the verdict keeps
    only the stage and position, and the catalog's own sentences."""
    for text in (fx.UNTERMINATED, fx.BAD_PLPGSQL_BODY, fx.PLPGSQL_FUNCTION, fx.PROCEDURE):
        kind = "procedure" if text is fx.PROCEDURE else "function"
        dumped = _verdict(routine(text, kind=kind)).model_dump_json()
        assert fx.LITERAL not in dumped
        assert "RETURN" not in dumped and "$function$" not in dumped


# ----------------------------------------------------------------------------- parse


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        (fx.PLPGSQL_FUNCTION, "function"),
        (fx.SQL_FUNCTION, "function"),
        (fx.SQL_ATOMIC_FUNCTION, "function"),
        (fx.TRIGGER_FUNCTION, "function"),
        (fx.PROCEDURE, "procedure"),
        (fx.TRIGGER, "trigger"),
        # A C function's AS holds a library path and a symbol, never parsed as SQL.
        (fx.C_FUNCTION, "function"),
        (fx.PLPYTHON3U_FUNCTION, "function"),
    ],
)
def test_parse_passes_each_routine_shape(text: str, kind: str) -> None:
    assert parse_routine(text, kind) is None  # type: ignore[arg-type]


def test_a_statement_the_parser_rejects_is_unparseable_at_its_position() -> None:
    failure = parse_routine(fx.UNTERMINATED, "function")
    assert failure is not None and failure.stage == "statement"
    # 1-based: the unterminated dollar quote opens the body.
    assert failure.position == fx.UNTERMINATED.index("$function$") + 1
    verdict = _verdict(routine(fx.UNTERMINATED))
    assert verdict.parse_failure == failure and _outcome(verdict, PARSE) == "fail"


@pytest.mark.parametrize(
    ("text", "language"), [(fx.BAD_PLPGSQL_BODY, "plpgsql"), (fx.BAD_SQL_BODY, "sql")]
)
def test_a_body_that_does_not_parse_is_unparseable(text: str, language: str) -> None:
    """The SQL parser reads a quoted body as a string; the body is parsed on its own,
    by PL/pgSQL's parser or as SQL."""
    failure = parse_routine(text, "function")
    assert failure is not None and (failure.stage, failure.position) == ("body", None)
    assert _outcome(_verdict(routine(text, language=language)), PARSE) == "fail"


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        (fx.PROCEDURE, "function"),
        (fx.PLPGSQL_FUNCTION, "procedure"),
        (fx.TRIGGER_FUNCTION, "trigger"),
        (fx.TRIGGER, "function"),
        (fx.TWO_STATEMENTS, "function"),
        ("", "function"),
        ("SELECT 1", "function"),
    ],
)
def test_a_statement_that_is_not_one_routine_of_its_kind_is_unparseable(
    text: str, kind: str
) -> None:
    failure = parse_routine(text, kind)  # type: ignore[arg-type]
    assert failure is not None and failure.stage == "kind"


def test_the_metadata_checks_still_run_when_the_statement_does_not_parse() -> None:
    verdict = _verdict(routine(fx.UNTERMINATED, language="sql"))
    assert verdict.failed_rules == (PARSE,)
    assert [c.rule_id for c in verdict.checks] == list(CHECKED_RULES)


# -------------------------------------------------------------------------- language


@pytest.mark.parametrize("language", ["plpgsql", "sql", "PLPGSQL", " Sql "])
def test_plpgsql_and_sql_pass(language: str) -> None:
    r = routine(fx.function_in(language), language=language)
    assert _outcome(_verdict(r), LANGUAGE) == "pass"


@pytest.mark.parametrize(
    "language",
    ["plpythonu", "plpython3u", "plpython2u", "plperlu", "pltclu", "c", "internal", "plr"],
)
def test_untrusted_c_and_unknown_languages_fail_and_are_named(language: str) -> None:
    verdict = _verdict(routine(fx.function_in(language), language=language))
    (result,) = [c for c in verdict.checks if c.rule_id == LANGUAGE]
    assert (result.outcome, result.findings, result.impact) == ("fail", (language,), "blocks_move")


def test_the_c_function_fixture_fails_the_language_rule() -> None:
    verdict = _verdict(routine(fx.C_FUNCTION, language="c"))
    assert verdict.parsed and verdict.failed_rules == (LANGUAGE,)


def test_a_trusted_language_passes_only_where_the_target_line_offers_it() -> None:
    plv8 = routine(fx.PLV8_FUNCTION, language="plv8")
    assert _outcome(_verdict(plv8), LANGUAGE) == "pass"
    lists = AuroraExtensionLines.model_validate(
        {
            **aurora_extension_lines()[TARGET].model_dump(),
            "lines": {
                "16": {"newest_release": "16.1", "extensions": {"plpgsql": "1.0", "plv8": "3.1"}},
                "17": {"newest_release": "17.1", "extensions": {"plpgsql": "1.0"}},
            },
        }
    )
    assert _outcome(_verdict(plv8, "16", extension_lines={TARGET: lists}), LANGUAGE) == "pass"
    verdict = _verdict(plv8, "17", extension_lines={TARGET: lists})
    assert _outcome(verdict, LANGUAGE) == "fail"
    # plpgsql and sql pass on a line whose list does not name them.
    assert (
        _outcome(
            _verdict(
                routine(fx.SQL_FUNCTION, language="sql"), "17", extension_lines={TARGET: lists}
            ),
            LANGUAGE,
        )
        == "pass"
    )


# ------------------------------------------------------------------------ extensions


def test_an_extension_the_target_line_offers_passes() -> None:
    r = routine(fx.POSTGIS_FUNCTION, language="sql", extensions=("postgis",))
    assert _outcome(_verdict(r), EXTENSIONS) == "pass"
    assert _outcome(_verdict(routine(fx.SQL_FUNCTION, language="sql")), EXTENSIONS) == "pass"


def test_every_extension_the_target_line_lacks_fails_and_is_named() -> None:
    r = routine(fx.SQL_FUNCTION, language="sql", extensions=("postgis", "acme_ext", "Zeta_Ext"))
    (result,) = [c for c in _verdict(r).checks if c.rule_id == EXTENSIONS]
    assert (result.outcome, result.findings) == ("fail", ("acme_ext", "zeta_ext"))


def test_a_server_module_aurora_does_not_offer_fails() -> None:
    """xml2 ships with PostgreSQL but no Aurora PostgreSQL line offers it."""
    r = routine(fx.SQL_FUNCTION, language="sql", extensions=("xml2",))
    assert _outcome(_verdict(r), EXTENSIONS) == "fail"


def test_an_extension_only_a_later_line_offers_fails_on_the_earlier_line() -> None:
    r = routine(fx.SQL_FUNCTION, language="sql", extensions=("roaringbitmap",))
    assert _outcome(_verdict(r, "16"), EXTENSIONS) == "fail"
    assert _outcome(_verdict(r, "18"), EXTENSIONS) == "pass"


def test_a_trigger_is_checked_for_its_extensions_only() -> None:
    trigger = routine(
        fx.TRIGGER,
        kind="trigger",
        language=None,
        owner_is_superuser=None,
        extensions=("moddatetime",),
    )
    verdict = _verdict(trigger)
    assert [c.rule_id for c in verdict.checks] == [PARSE, EXTENSIONS]
    assert not verdict.needs_attention
    missing = trigger.model_copy(update={"extensions": ("acme_ext",)})
    assert _verdict(missing).failed_rules == (EXTENSIONS,)


# ------------------------------------------------------------------ the target line


def test_the_routines_line_is_the_one_the_gap_checks_move_lands_on() -> None:
    """#1078's line selection: a PostgreSQL 16 source with roaringbitmap installed moves
    to 18, the gap check says so, and its routines are checked against 18."""
    installed = {"postgis": "3.4.0", "roaringbitmap": "0.5.4"}
    assert routine_target_line("16.4", installed, TARGET) == "18"
    collector = {
        "metadata": {
            "source_database": {"engine": "postgresql", "version": "16.4"},
            "extensions": [{"name": n, "version": v} for n, v in installed.items()],
        }
    }
    gaps = detect_relational_gaps(collector, TARGET)
    assert gaps is not None
    later = [g for g in gaps["gaps"] if g["rule_id"] == "postgresql-extension-on-a-later-version"]
    assert later[0]["objects"][0]["note"] == "offered from Aurora PostgreSQL 18"


@pytest.mark.parametrize(
    ("version", "installed", "line"),
    [
        ("16.4", {}, "16"),
        ("16.4", None, "16"),
        ("16.4", {"postgis": "3.4.0"}, "16"),
        # No line offers acme_ext: the source's own line.
        ("15.2", {"acme_ext": "1.0"}, "15"),
        # Newer than every line: the newest.
        ("19.1", {}, "18"),
        # Unknown version: the lowest line that offers them all.
        (None, {"roaringbitmap": "0.5.4"}, "18"),
        (None, {}, "14"),
        # Older than every line, with an extension no line offers: the newest.
        ("12.9", {"acme_ext": "1.0"}, "18"),
        # TimescaleDB has a gap rule of its own: it does not move the line (with it
        # counted, no line would fit and the newest would be chosen).
        (None, {"timescaledb": "2.14.0"}, "14"),
    ],
)
def test_the_target_line_follows_the_gap_checks_choice(
    version: str | None, installed: dict[str, str] | None, line: str
) -> None:
    assert routine_target_line(version, installed, TARGET) == line


def test_no_extension_list_means_no_target_line() -> None:
    assert routine_target_line("8.0.36", {}, "aurora_mysql") is None


def test_a_target_line_the_list_does_not_name_is_refused() -> None:
    with pytest.raises(ValueError, match="is not listed"):
        _verdict(routine(fx.SQL_FUNCTION), "11")
    with pytest.raises(ValueError, match="no extension list"):
        check_postgresql_routine(
            routine(fx.SQL_FUNCTION), target_engine="aurora_mysql", target_line="8.0"
        )


# ------------------------------------------------------------------------- ownership


def test_a_superusers_security_definer_routine_is_flagged() -> None:
    r = routine(fx.SECURITY_DEFINER_FUNCTION, security_definer=True, owner_is_superuser=True)
    verdict = _verdict(r)
    assert verdict.failed_rules == (OWNERSHIP,)
    (result,) = [c for c in verdict.checks if c.rule_id == OWNERSHIP]
    assert result.impact == "needs_rewrite" and result.findings == ()


@pytest.mark.parametrize(("definer", "superuser"), [(True, False), (False, True), (False, False)])
def test_a_definer_owned_by_a_role_or_an_invoker_owned_by_a_superuser_passes(
    definer: bool, superuser: bool
) -> None:
    r = routine(
        fx.SECURITY_DEFINER_FUNCTION, security_definer=definer, owner_is_superuser=superuser
    )
    assert _outcome(_verdict(r), OWNERSHIP) == "pass"


def test_a_superusers_definer_procedure_is_flagged() -> None:
    r = routine(fx.PROCEDURE, kind="procedure", security_definer=True, owner_is_superuser=True)
    assert _verdict(r).failed_rules == (OWNERSHIP,)


# -------------------------------------------------------------- procedures, triggers


def test_a_procedure_passes_every_rule() -> None:
    verdict = _verdict(routine(fx.PROCEDURE, kind="procedure"))
    assert verdict.kind == "procedure" and not verdict.needs_attention
    assert [c.rule_id for c in verdict.checks] == list(CHECKED_RULES)


def test_a_trigger_function_passes_every_rule() -> None:
    verdict = _verdict(routine(fx.TRIGGER_FUNCTION))
    assert verdict.parsed and not verdict.needs_attention


# ------------------------------------------------------------------------ the input


@pytest.mark.parametrize(
    ("meta", "message"),
    [
        ({"language": None}, "needs its language"),
        ({"language": "  "}, "needs its language"),
        ({"owner_is_superuser": None}, "needs owner_is_superuser"),
        ({"kind": "trigger", "owner_is_superuser": None}, "no language"),
        ({"kind": "trigger", "language": None, "security_definer": True}, "no language"),
        ({"kind": "event"}, "kind"),
        ({"routine_id": ""}, "routine_id"),
        ({"unknown": 1}, "unknown"),
    ],
)
def test_the_input_refuses_metadata_that_does_not_fit_its_kind(meta: dict, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        routine(fx.SQL_FUNCTION, **meta)


def test_the_input_normalizes_language_and_extension_names() -> None:
    r = routine(fx.SQL_FUNCTION, language=" PLpgSQL ", extensions=("PostGIS", "postgis", " ", "h3"))
    assert (r.language, r.extensions) == ("plpgsql", ("h3", "postgis"))


# ------------------------------------------------------- review round 1 (#1961)


def test_a_source_engine_with_no_extension_list_has_no_landing_line() -> None:
    """The source engine is the caller's (#1177: no guess outside the detector)."""
    assert extension_landing_line({}, "postgresql", "16.4", TARGET) == "16"
    assert extension_landing_line({}, "mysql", "8.0.36", TARGET) is None


@pytest.mark.parametrize("where", ["start", "body", "end"])
def test_text_the_parser_cannot_encode_is_unparseable_and_raises_nothing(where: str) -> None:
    """A lone surrogate (json.loads makes one from "\\ud800") made the parser raise a
    UnicodeEncodeError whose .object held the whole statement."""
    text = {
        "start": "\ud800" + fx.PLPGSQL_FUNCTION,
        "body": fx.PLPGSQL_FUNCTION.replace("RETURN amount", "RETURN '\ud800' || amount"),
        "end": fx.SQL_FUNCTION + "\ud800",
    }[where]
    assert parse_routine(text, "function") == ParseFailure(stage="statement")
    verdict = _verdict(routine(text, language="sql" if where == "end" else "plpgsql"))
    assert verdict.parsed is False and verdict.failed_rules == (PARSE,)
    assert fx.LITERAL not in verdict.model_dump_json()


@pytest.mark.parametrize(
    "text",
    [
        "CREATE FUNCTION public.f() RETURNS integer LANGUAGE sql AS $$ SELECT 1 $$;"
        "\x00 DROP TABLE x; garbage",
        fx.PLPGSQL_FUNCTION + "\x00 trailing garbage the parser never reads",
        fx.PLPGSQL_FUNCTION.replace("BEGIN", "BEGIN\x00", 1),
    ],
)
def test_a_nul_anywhere_is_unparseable_at_its_position(text: str) -> None:
    """PostgreSQL text holds no NUL, and the C parser stopped reading at one, so the
    text after it passed unread."""
    failure = parse_routine(text, "function")
    assert failure == ParseFailure(stage="statement", position=text.index("\x00") + 1)
    assert _verdict(routine(text, language="sql")).parsed is False


@pytest.mark.parametrize(
    "meta",
    [
        {"kind": "trigger"},
        {"language": None},
        {"kind": "event"},
        {"surprise": 1},
        {"owner_is_superuser": "maybe"},
        {"extensions": "postgis"},
    ],
)
def test_a_validation_error_never_holds_the_routine_text(meta: dict) -> None:
    with pytest.raises(ValidationError) as caught:
        routine(fx.PLPGSQL_FUNCTION, **meta)
    error = caught.value
    assert fx.LITERAL not in str(error)
    assert fx.LITERAL not in repr(error.errors())
    assert all(e.get("input") is None for e in error.errors())
    assert error.__context__ is None and error.__cause__ is None


def test_a_bad_create_sql_type_does_not_echo_it() -> None:
    with pytest.raises(ValidationError) as caught:
        routine([fx.LITERAL])  # type: ignore[arg-type]
    assert fx.LITERAL not in str(caught.value)
    assert fx.LITERAL not in repr(caught.value.errors())


@pytest.mark.parametrize(
    ("metadata", "declared", "findings"),
    [
        ("sql", "plpython3u", ("metadata: sql", "statement: plpython3u")),
        ("plpython3u", "plpgsql", ("metadata: plpython3u", "statement: plpgsql")),
        ("plpgsql", "sql", ("metadata: plpgsql", "statement: sql")),
    ],
)
def test_the_metadata_and_the_statements_language_must_agree(
    metadata: str, declared: str, findings: tuple[str, ...]
) -> None:
    """The body parse follows the statement's LANGUAGE; the rule now reads both."""
    verdict = _verdict(routine(fx.function_in(declared), language=metadata))
    (result,) = [c for c in verdict.checks if c.rule_id == LANGUAGE]
    assert (result.outcome, result.findings) == ("fail", findings)


def test_a_declared_language_that_is_not_a_name_is_not_repeated() -> None:
    """LANGUAGE also takes a string literal; the verdict names it by a placeholder."""
    text = fx.function_in("sql").replace("LANGUAGE sql", f"LANGUAGE '{fx.LITERAL}'")
    verdict = _verdict(routine(text, language="sql"))
    (result,) = [c for c in verdict.checks if c.rule_id == LANGUAGE]
    assert result.findings == ("metadata: sql", "statement: (not a language name)")
    assert fx.LITERAL not in verdict.model_dump_json()


def test_an_unparsed_statement_leaves_the_language_to_the_metadata() -> None:
    verdict = _verdict(routine(fx.UNTERMINATED, language="plpgsql"))
    assert verdict.failed_rules == (PARSE,)


def test_parsed_is_none_when_no_parse_rule_ran() -> None:
    (language_rule,) = [r for r in routine_check_rules() if r.id == LANGUAGE]
    for text in (fx.PLPGSQL_FUNCTION, fx.UNTERMINATED):
        verdict = _verdict(routine(text), rules=[language_rule])
        assert verdict.parsed is None and verdict.parse_failure is None
    assert _verdict(routine(fx.PLPGSQL_FUNCTION)).parsed is True
    assert _verdict(routine(fx.UNTERMINATED)).parsed is False


def test_the_shared_verdict_models_do_not_echo_their_input() -> None:
    from src.tools.routines.verdict import RoutineCheckResult

    with pytest.raises(ValidationError) as caught:
        RoutineCheckResult.model_validate({"rule_id": fx.LITERAL, "check": "parse"})
    assert fx.LITERAL not in str(caught.value)
