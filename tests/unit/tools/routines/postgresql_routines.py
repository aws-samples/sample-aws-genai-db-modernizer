# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Synthetic PostgreSQL routines for the routine check tests (#1935).

Each one is a ``CREATE`` statement as ``pg_get_functiondef`` or ``pg_get_triggerdef``
prints it, with the metadata the routine collection gives (:class:`PostgresRoutine`).
None comes from a customer. ``LITERAL`` is a string literal several of them hold: a
verdict must never repeat it.
"""

from __future__ import annotations

from typing import Any

from src.tools.routines.postgresql_checks import PostgresRoutine

LITERAL = "zq-literal-7731"


def routine(create_sql: str, **meta: Any) -> PostgresRoutine:
    fields: dict[str, Any] = {
        "routine_id": "public.r()",
        "kind": "function",
        "language": "plpgsql",
        "owner": "app_owner",
        "owner_is_superuser": False,
        "security_definer": False,
        "extensions": (),
    }
    fields.update(meta)
    return PostgresRoutine(create_sql=create_sql, **fields)


PLPGSQL_FUNCTION = f"""CREATE OR REPLACE FUNCTION public.add_tax(amount numeric)
 RETURNS numeric
 LANGUAGE plpgsql
AS $function$
BEGIN
  IF amount IS NULL THEN
    RAISE EXCEPTION '{LITERAL}';
  END IF;
  RETURN amount * 1.2;
END;
$function$
"""

SQL_FUNCTION = """CREATE OR REPLACE FUNCTION public.order_total(order_id integer)
 RETURNS numeric
 LANGUAGE sql
 STABLE
AS $function$ SELECT sum(qty * price) FROM order_lines WHERE order_lines.order_id = $1 $function$
"""

# PostgreSQL 14's SQL-standard body: parsed with the statement itself.
SQL_ATOMIC_FUNCTION = """CREATE OR REPLACE FUNCTION public.double_it(x integer)
 RETURNS integer
 LANGUAGE sql
 IMMUTABLE
BEGIN ATOMIC
 SELECT (x * 2);
END
"""

# A trigger function: a function that RETURNS trigger.
TRIGGER_FUNCTION = """CREATE OR REPLACE FUNCTION public.touch_updated_at()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
BEGIN
  NEW.updated_at := now();
  RETURN NEW;
END;
$function$
"""

# A procedure (PostgreSQL 11 and later) that commits inside its body.
PROCEDURE = f"""CREATE OR REPLACE PROCEDURE public.archive_orders(IN cutoff date)
 LANGUAGE plpgsql
AS $procedure$
BEGIN
  INSERT INTO orders_archive SELECT * FROM orders WHERE created_at < cutoff;
  DELETE FROM orders WHERE created_at < cutoff;
  COMMIT;
  RAISE NOTICE '{LITERAL}';
END;
$procedure$
"""

# A trigger, as pg_get_triggerdef prints it, calling the moddatetime extension.
TRIGGER = (
    "CREATE TRIGGER orders_touch BEFORE UPDATE ON public.orders FOR EACH ROW "
    "EXECUTE FUNCTION moddatetime('updated_at')"
)

PLPYTHON3U_FUNCTION = """CREATE OR REPLACE FUNCTION public.slugify(title text)
 RETURNS text
 LANGUAGE plpython3u
AS $function$
import re
return re.sub(r'[^a-z0-9]+', '-', title.lower())
$function$
"""

PLV8_FUNCTION = """CREATE OR REPLACE FUNCTION public.js_len(doc jsonb)
 RETURNS integer
 LANGUAGE plv8
AS $function$ return Object.keys(doc).length; $function$
"""

C_FUNCTION = """CREATE OR REPLACE FUNCTION public.fast_hash(text)
 RETURNS integer
 LANGUAGE c
 IMMUTABLE STRICT
AS '$libdir/fast_hash', $function$fast_hash$function$
"""

POSTGIS_FUNCTION = """CREATE OR REPLACE FUNCTION public.store_distance(a integer, b integer)
 RETURNS double precision
 LANGUAGE sql
 STABLE
AS $function$ SELECT ST_Distance(s1.geom, s2.geom) FROM stores s1, stores s2 WHERE s1.id = a AND s2.id = b $function$
"""

SECURITY_DEFINER_FUNCTION = """CREATE OR REPLACE FUNCTION public.reset_counter()
 RETURNS void
 LANGUAGE plpgsql
 SECURITY DEFINER
AS $function$
BEGIN
  UPDATE counters SET value = 0;
END;
$function$
"""

# The statement itself does not parse: the dollar quote is never closed. The parser's
# message would quote the text, literal included.
UNTERMINATED = f"""CREATE OR REPLACE FUNCTION public.broken()
 RETURNS text
 LANGUAGE plpgsql
AS $function$
BEGIN
  RETURN '{LITERAL}';
END;
"""

# The statement parses (the body is a string to the SQL parser) but the PL/pgSQL body
# does not: a semicolon is missing before RETURN.
BAD_PLPGSQL_BODY = f"""CREATE OR REPLACE FUNCTION public.broken_body()
 RETURNS text
 LANGUAGE plpgsql
AS $function$
BEGIN
  PERFORM '{LITERAL}'
  RETURN 'x';
END;
$function$
"""

BAD_SQL_BODY = """CREATE OR REPLACE FUNCTION public.broken_sql()
 RETURNS integer
 LANGUAGE sql
AS $function$ SELECT FROM FROM orders $function$
"""

TWO_STATEMENTS = SQL_FUNCTION + ";\n" + SQL_FUNCTION.replace("order_total", "order_total2")


def function_in(language: str) -> str:
    """A function whose statement declares ``language`` (as the metadata gives it, so
    upper case and padding are kept), with a body that language's parser reads."""
    body = {"plpgsql": "BEGIN RETURN 1; END;", "sql": "SELECT 1"}.get(
        language.strip().lower(), "return 1"
    )
    return (
        "CREATE OR REPLACE FUNCTION public.one()\n RETURNS integer\n"
        f" LANGUAGE {language.strip()}\nAS $function$ {body} $function$\n"
    )
