# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The order of an "Affects" list: collected source tables first, then other names (#1914).

A risk or trade-off can name things that are not collected tables: a PostgreSQL
catalog relation (``pg_class``), a view or a materialized view. PostgreSQL table ids
are ``public.<table>``, so plain alphabetical order put those names ahead of the
tables. Synthesis orders the list once, so report.json, the engineering report and
the UI show the same order and the renderers only print it (ADR-032).
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterable


def collected_tables_first(
    names: Iterable[str],
    collected: Collection[str],
    resolve: Callable[[str], str] | None = None,
) -> list[str]:
    """``names`` with the collected tables first and every other name after, each group sorted.

    ``resolve`` maps a name to the collector's table id (a bare ``users`` to
    ``public.users``) for the membership test only; the name itself is kept as given.
    """
    resolver = resolve or (lambda name: name)
    tables: list[str] = []
    others: list[str] = []
    for name in names:
        (tables if resolver(name) in collected else others).append(name)
    return sorted(tables) + sorted(others)
