# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The canonical key of a table name (#1780): one rule for the schema design and the
facts layer, which may not import ``src.agents`` (ADR-032 Decision 3)."""

from __future__ import annotations


def normalize_table_name(name: str) -> str:
    """Canonical table key: unquoted, lower-case, without any ``<db>.``/schema prefix.

    ``wordpress.wp_posts``, ```WordPress`.`wp_posts```` and ``wp_posts`` all
    normalise to ``wp_posts``, so designs and assignments match whether or not
    either side spells the prefix.
    """
    cleaned = "".join(c for c in name if c not in '`"[]').strip().lower()
    return cleaned.rsplit(".", 1)[-1].strip()
