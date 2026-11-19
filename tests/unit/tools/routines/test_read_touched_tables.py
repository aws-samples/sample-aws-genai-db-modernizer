# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``proof_screen.read_touched_tables``: the tables a body names, or None when it cannot be
read (#2463)."""

from __future__ import annotations

import pytest

from src.tools.routines.proof_screen import read_touched_tables

PG_FUNCTION = (
    "CREATE OR REPLACE FUNCTION public.f()\n RETURNS integer\n LANGUAGE plpgsql\n"
    "AS $function$\nBEGIN\n  UPDATE public.topics SET x = 1;\n  RETURN 1;\nEND\n$function$\n"
)
PG_NO_TABLE = (
    "CREATE OR REPLACE FUNCTION public.g()\n RETURNS integer\n LANGUAGE plpgsql\n"
    "AS $function$\nBEGIN\n  RETURN 1;\nEND\n$function$\n"
)


def test_postgresql_returns_the_tables_a_function_names():
    assert read_touched_tables(PG_FUNCTION, "postgresql", "function") == ("public.topics",)


def test_postgresql_returns_an_empty_tuple_for_a_body_with_no_table():
    assert read_touched_tables(PG_NO_TABLE, "postgresql", "function") == ()


def test_postgresql_returns_none_for_a_body_that_does_not_parse():
    broken = PG_FUNCTION.replace("UPDATE public.topics SET x = 1;", "UPDATE (((( topics;")
    assert read_touched_tables(broken, "postgresql", "function") is None


def test_mysql_returns_the_tables_of_a_body_under_a_placeholder_header():
    text = "CREATE PROCEDURE `r`() BEGIN\n  UPDATE wp_posts SET x = 1;\nEND"
    assert read_touched_tables(text, "mysql", "procedure") == ("wp_posts",)


def test_mysql_returns_none_for_a_body_that_does_not_parse():
    text = "CREATE PROCEDURE `r`() BEGIN\n  UPDATE (((( wp_users;\nEND"
    assert read_touched_tables(text, "mysql", "procedure") is None


@pytest.mark.parametrize("engine", ["oracle", "sqlserver"])
def test_an_engine_with_no_reader_returns_none(engine):
    assert read_touched_tables(PG_FUNCTION, engine, "function") is None
