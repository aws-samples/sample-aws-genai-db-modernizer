#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Warn when the Aurora PostgreSQL extension lists are old (#1078); never fail.

``src/catalog/aurora_postgresql_extensions.toml`` is copied by hand from the public
"Extension versions for Amazon Aurora PostgreSQL" page, which changes with every Aurora
release. Like the price table's age check (``scripts/refresh_prices.py --check-age``),
this prints one line, a warning once ``retrieved_on`` is more than ``MAX_AGE_DAYS`` old,
and exits 0 either way: ``ci/lint.sh`` runs it.

    uv run python scripts/check_extension_list_age.py [--max-age-days N]
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.shared.catalog.gaps import AURORA_EXTENSIONS_FILE, load_aurora_extensions  # noqa: E402

# Aurora PostgreSQL adds minor versions every few months.
MAX_AGE_DAYS = 90


def age_warning(
    retrieved_on: dt.date, today: dt.date, max_age_days: int = MAX_AGE_DAYS
) -> str | None:
    """The warning to print, or None while the lists are younger than the limit."""
    age = (today - retrieved_on).days
    if age <= max_age_days:
        return None
    return (
        f"warning: {AURORA_EXTENSIONS_FILE.relative_to(REPO_ROOT)} was retrieved on "
        f"{retrieved_on.isoformat()}, {age} days ago (limit {max_age_days}); update it "
        'from the public "Extension versions for Amazon Aurora PostgreSQL" page'
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--max-age-days", type=int, default=MAX_AGE_DAYS)
    parser.add_argument("--today", type=dt.date.fromisoformat, default=dt.date.today())
    args = parser.parse_args(argv)
    listed = load_aurora_extensions()["aurora_postgresql"]
    retrieved_on = dt.date.fromisoformat(listed.retrieved_on)
    print(
        age_warning(retrieved_on, args.today, args.max_age_days)
        or f"extension lists: retrieved {listed.retrieved_on}, within {args.max_age_days} days"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
