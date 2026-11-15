# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The database part of a MySQL ``GRANT`` (#1970).

MySQL reads ``_`` and ``%`` in the database name of a ``GRANT ... ON db.*`` as wildcards, so
``GRANT ALL ON `my_ql`.*`` also covers ``mysql``, and ``sales_db`` its look-alikes. Every
grant on a database writes the name through :func:`grant_database`, which escapes both (and
the backslash that escapes them) and doubles a backtick. Only a ``GRANT`` or ``REVOKE`` reads
the name this way; ``CREATE DATABASE`` and a ``USE`` take the plain quoted name.
"""

from __future__ import annotations

__all__ = ["grant_database"]


def grant_database(name: str) -> str:
    """``name`` as the database part of a ``GRANT``/``REVOKE``: quoted, with ``\\``, ``_``
    and ``%`` escaped so each matches itself only."""
    escaped = name.replace("\\", "\\\\").replace("_", "\\_").replace("%", "\\%")
    return "`" + escaped.replace("`", "``") + "`"
