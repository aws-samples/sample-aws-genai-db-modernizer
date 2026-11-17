# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Quoting a collected name into SQL (#2381): one rule for every writer.

A collected name is untrusted text. Doubling the quote mark keeps it inside its quotes for
the server, but not for the tools a customer runs the file with: ``psql -f`` ends a line at
a NUL, so a quoted name holding one stays open and the next statement's collector text runs
as SQL. Editors show a line or paragraph separator, or a bidirectional override, as a break
or a reordering the server never sees. So no quoted name may carry a control (Cc), format
(Cf), surrogate (Cs), line separator (Zl) or paragraph separator (Zp) character: quoting
one raises ``UnsafeIdentifierError``, and each DDL writer turns that into an object it
leaves out with a fixed reason, never into text it writes. The same characters are refused
in the SQL fragments ``sql_safety`` parses.
"""

from __future__ import annotations

import unicodedata

#: Unicode categories no quoted name, SQL fragment or SQL comment may hold.
UNPRINTABLE_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})

#: The fixed reason a writer gives for an object it leaves out because of its name.
UNSAFE_NAME_REASON = (
    "its name holds a control, format, surrogate or line separator character, which no SQL "
    "file this release writes may carry"
)


class SqlFragmentError(ValueError):
    """A SQL fragment or a name does not match its grammar (``sql_safety`` re-exports it)."""


class UnsafeIdentifierError(SqlFragmentError):
    """A collected name cannot be quoted into SQL (see the module docstring)."""


def is_unprintable(character: str) -> bool:
    """Whether ``character`` is one no quoted name, fragment or comment may carry."""
    return unicodedata.category(character) in UNPRINTABLE_CATEGORIES


def has_unprintable(text: str) -> bool:
    return any(is_unprintable(ch) for ch in text)


def quote_identifier(name: str, mark: str) -> str:
    """``name`` between ``mark`` quotes (``"`` or a backtick), each ``mark`` in it doubled.
    Raises ``UnsafeIdentifierError`` for a name ``has_unprintable`` refuses; the message
    quotes none of it."""
    if has_unprintable(name):
        raise UnsafeIdentifierError(f"not a name SQL may quote: {UNSAFE_NAME_REASON}")
    return mark + name.replace(mark, mark + mark) + mark
