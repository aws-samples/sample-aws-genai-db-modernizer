#!/usr/bin/env python3
"""Refresh the engine price table from the public AWS Price List bulk files (#1196).

``src/catalog/prices.toml`` is the one dated table of us-east-1 on-demand list prices
every cost estimate reads (the engine catalog merges each engine's configuration into
its ``[pricing]``, ``src.shared.catalog.prices``). This script writes it from the AWS
Price List bulk files, which are public and need no credentials:

    https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/<ServiceCode>/current/us-east-1/index.csv

Each price is one ``Selector``: the offer file, the column values its row must have,
and the column the figure comes from. A selector that matches no row, or rows with
different figures, stops the refresh rather than guessing, so a renamed usage type
fails loudly instead of leaving a stale price in place.

Usage:
    uv run python scripts/refresh_prices.py                       # download and write
    uv run python scripts/refresh_prices.py --source-dir DIR      # read DIR/<ServiceCode>.csv
    uv run python scripts/refresh_prices.py --check-age           # warn when the table is old

``--check-age`` runs in lint. It only warns (exit 0): a price list ages slowly, and a
refresh is a reviewed change, never an automatic one. The scheduled workflow
``.github/workflows/refresh-prices.yml`` runs this script every month and opens (or
updates) one pull request when the table changes; it never merges.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import http.client
import ssl
import sys
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.shared.catalog.prices import PRICES_FILE, load_price_table  # noqa: E402

REGION = "us-east-1"
PRICE_LIST_HOST = "pricing.us-east-1.amazonaws.com"
OFFER_PATH = "/offers/v1.0/aws/{service}/current/{region}/index.csv"
# The age past which --check-age warns: .github/workflows/refresh-prices.yml refreshes the
# table on the 1st of every month (at most 31 days apart) and opens a pull request, and a
# refresh pull request gets two weeks to be reviewed and merged: 31 + 14 = 45.
MAX_AGE_DAYS = 45


@dataclass(frozen=True)
class Selector:
    """One price: the offer file, the row's column values and the column to read.

    A value starting with ``^`` matches a column that starts with the rest. ``scale``
    multiplies the figure (1e6 turns a per-request price into a per-million one);
    ``parse`` reads a non-price column (a node's memory, ``"6.38 GiB"``)."""

    service: str
    match: Mapping[str, str]
    # The price list's own ``Unit`` every matched row must have ("Hrs", "GB-Mo"): a SKU
    # that changes its unit stops the refresh instead of being read at the old scale.
    unit: str
    column: str = "PricePerUnit"
    scale: float = 1.0
    parse: Callable[[str], float] = float
    on_demand: bool = True

    def matches(self, row: Mapping[str, str]) -> bool:
        if self.on_demand and row.get("TermType") != "OnDemand":
            return False
        for col, want in self.match.items():
            got = row.get(col, "")
            if want.startswith("^"):
                if not got.startswith(want[1:]):
                    return False
            elif got != want:
                return False
        return True


def _gib(text: str) -> float:
    number, _, unit = text.strip().partition(" ")
    if unit != "GiB":
        raise ValueError(f"memory {text!r} is not in GiB")
    return float(number)


def _aurora(engine: str, io_optimized: bool) -> dict[str, object]:
    db = f"Aurora {engine}"
    suffix = "IOOptimized" if io_optimized else ""
    instances = {}
    for itype in ("db.t4g.medium", "db.r6g.large", "db.r6g.xlarge"):
        instances[itype] = Selector(
            "AmazonRDS",
            {
                "Instance Type": itype,
                "Database Engine": db,
                "Deployment Option": "Single-AZ",
                "usageType": f"^InstanceUsage{suffix}:",
            },
            "Hrs",
        )
    if io_optimized:
        # I/O-Optimized bills no I/O, so the section has no per-request price at all: the
        # engine is billed by capacity.
        return {
            "acu_price_per_hour": Selector(
                "AmazonRDS",
                {"usageType": "Aurora:ServerlessV2IOOptimizedUsage", "Database Engine": db},
                "ACU-Hr",
            ),
            "storage_price_per_gb_month": Selector(
                "AmazonRDS",
                {"usageType": "Aurora:IO-OptimizedStorageUsage", "Database Engine": db},
                "GB-Mo",
            ),
            "instance_hourly": instances,
        }
    # Standard bills each storage I/O, read or write, like DocumentDB: an engine priced on
    # it needs read_unit_bytes and write_unit_bytes in its engine file.
    io = Selector(
        "AmazonRDS",
        {"usageType": "Aurora:StorageIOUsage", "Database Engine": "Any"},
        "IOs",
        scale=1e6,
    )
    return {
        "acu_price_per_hour": Selector(
            "AmazonRDS", {"usageType": "Aurora:ServerlessV2Usage", "Database Engine": db}, "ACU-Hr"
        ),
        "storage_price_per_gb_month": Selector(
            "AmazonRDS", {"usageType": "Aurora:StorageUsage", "Database Engine": "Any"}, "GB-Mo"
        ),
        "read_unit_price_per_million": io,
        "write_unit_price_per_million": io,
        "instance_hourly": instances,
    }


def _documentdb(io_optimized: bool) -> dict[str, object]:
    instances = {
        itype: Selector(
            "AmazonDocDB",
            {
                "Instance Type": itype,
                "usageType": "^InstanceUsageIOOptimized:" if io_optimized else "^InstanceUsage:",
            },
            "Hrs",
        )
        for itype in ("db.r6g.large", "db.r6g.xlarge")
    }
    if io_optimized:
        # I/O-Optimized bills no I/O: a request costs nothing beyond the capacity.
        io: object = 0.0
        storage = Selector("AmazonDocDB", {"usageType": "IO-OptimizedStorageUsage"}, "GB-Mo")
    else:
        io = Selector("AmazonDocDB", {"usageType": "StorageIOUsage"}, "IOs", scale=1e6)
        storage = Selector("AmazonDocDB", {"usageType": "StorageUsage"}, "GB-Mo")
    return {
        "read_unit_price_per_million": io,
        "write_unit_price_per_million": io,
        "storage_price_per_gb_month": storage,
        "instance_hourly": instances,
    }


def _elasticache(engine: str) -> dict[str, object]:
    nodes = (
        "cache.m7g.large",
        "cache.r7g.large",
        "cache.r7g.xlarge",
        "cache.r7g.2xlarge",
        "cache.r7g.4xlarge",
    )
    row = {"Cache Engine": engine, "usageType": "^NodeUsage:"}
    return {
        "instance_hourly": {
            n: Selector("AmazonElastiCache", {**row, "Instance Type": n}, "Hrs") for n in nodes
        },
        "instance_memory_gib": {
            n: Selector(
                "AmazonElastiCache",
                {**row, "Instance Type": n},
                "Hrs",
                column="Memory",
                parse=_gib,
            )
            for n in nodes
        },
        "backup_price_per_gb_month": Selector(
            "AmazonElastiCache",
            {"Cache Engine": engine, "usageType": f"USE1-BackupUsage:{engine}"},
            "GB-months",
        ),
        # Replication between nodes in different Availability Zones is regional data
        # transfer (the data transfer offer's in/out/between-AZ rate).
        "cross_az_transfer_price_per_gb": Selector(
            "AWSDataTransfer", {"usageType": "DataTransfer-Regional-Bytes"}, "GB"
        ),
    }


# section path -> field -> Selector (or a dict of them, or a fixed figure).
SPECS: dict[str, dict[str, object]] = {
    "aurora_mysql.standard": _aurora("MySQL", io_optimized=False),
    "aurora_mysql.io_optimized": _aurora("MySQL", io_optimized=True),
    "aurora_postgresql.standard": _aurora("PostgreSQL", io_optimized=False),
    "aurora_postgresql.io_optimized": _aurora("PostgreSQL", io_optimized=True),
    "documentdb.standard": _documentdb(io_optimized=False),
    "documentdb.io_optimized": _documentdb(io_optimized=True),
    "dynamodb.on_demand": {
        "read_unit_price_per_million": Selector(
            "AmazonDynamoDB", {"usageType": "ReadRequestUnits"}, "ReadRequestUnits", scale=1e6
        ),
        "write_unit_price_per_million": Selector(
            "AmazonDynamoDB", {"usageType": "WriteRequestUnits"}, "WriteRequestUnits", scale=1e6
        ),
        # The first 25 GB-months are free (StartingRange 0); the estimate bills from the first GB.
        "storage_price_per_gb_month": Selector(
            "AmazonDynamoDB",
            {"usageType": "TimedStorage-ByteHrs", "StartingRange": "25"},
            "GB-Mo",
        ),
    },
    "opensearch.managed": {
        "instance_hourly": {
            "r6g.large.search": Selector(
                "AmazonES",
                {"Instance Type": "r6g.large.search", "usageType": "ESInstance:r6g.large"},
                "Hrs",
            )
        },
        "storage_price_per_gb_month": Selector(
            "AmazonES", {"usageType": "ES:GP3-Storage"}, "GB-Mo"
        ),
    },
    "elasticache.valkey": _elasticache("Valkey"),
    "elasticache.redis_oss": _elasticache("Redis"),
}


@dataclass
class OfferFile:
    """One service's price list file: its publication date and its price rows."""

    service: str
    published: dt.date
    rows: list[dict[str, str]] = field(default_factory=list)


def read_offer(service: str, text: Iterable[str]) -> OfferFile:
    """Parse one offer CSV: five metadata lines, then a header and the price rows."""
    lines = iter(text)
    meta: dict[str, str] = {}
    for _ in range(5):
        key, _, value = next(lines).strip().partition(",")
        meta[key.strip('"')] = value.strip().strip('"')
    if meta.get("OfferCode") not in (None, service):
        raise ValueError(f"{service}: the file is the {meta.get('OfferCode')} offer")
    published = dt.date.fromisoformat(meta["Publication Date"][:10])
    return OfferFile(service, published, list(csv.DictReader(lines)))


def _pick(sel: Selector, offers: Mapping[str, OfferFile], where: str) -> float:
    rows = [r for r in offers[sel.service].rows if sel.matches(r)]
    units = sorted({r.get("Unit", "") for r in rows} - {sel.unit})
    if units:
        raise ValueError(
            f"{where}: {sel.service} {dict(sel.match)} is priced per {units}, not {sel.unit!r}"
        )
    values = {sel.parse(r[sel.column]) for r in rows}
    if len(values) != 1:
        raise ValueError(
            f"{where}: {sel.service} {dict(sel.match)} matched "
            f"{'no row' if not values else f'{len(values)} different figures {sorted(values)}'}"
        )
    return round(values.pop() * sel.scale, 6)


def _resolve(spec: object, offers: Mapping[str, OfferFile], where: str) -> object:
    if isinstance(spec, Selector):
        return _pick(spec, offers, where)
    if isinstance(spec, dict):
        return {k: _resolve(v, offers, f"{where}.{k}") for k, v in spec.items()}
    return spec


def extract_prices(offers: Mapping[str, OfferFile]) -> dict[str, dict[str, object]]:
    """Every section of the table, each figure read from ``offers``."""
    return {
        path: {k: _resolve(v, offers, f"{path}.{k}") for k, v in spec.items()}
        for path, spec in SPECS.items()
    }


def services() -> list[str]:
    """The offer files the specs read, sorted."""

    def walk(spec: object) -> Iterator[str]:
        if isinstance(spec, Selector):
            yield spec.service
        elif isinstance(spec, dict):
            for v in spec.values():
                yield from walk(v)

    return sorted({s for spec in SPECS.values() for s in walk(spec)})


def _toml_value(value: object) -> str:
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, str):
        return f'"{value}"'
    raise TypeError(f"no TOML form for {value!r}")


def render_table(offers: Mapping[str, OfferFile], retrieved_on: dt.date) -> str:
    """The table file's text: the header, each offer's publication date, every section."""
    out = [
        "# Generated by scripts/refresh_prices.py from the public AWS Price List bulk files",
        "# (us-east-1, on-demand, USD). Do not edit by hand: run the script (#1196).",
        f'region = "{REGION}"',
        f"retrieved_on = {retrieved_on.isoformat()}",
        "",
        "# Each offer file's own publication date.",
        "[published]",
    ]
    out += [f"{s} = {offers[s].published.isoformat()}" for s in services()]
    for path, section in extract_prices(offers).items():
        out += ["", f"[{path}]"]
        nested = {k: v for k, v in section.items() if isinstance(v, dict)}
        out += [f"{k} = {_toml_value(v)}" for k, v in section.items() if k not in nested]
        for key, table in nested.items():
            out += ["", f"[{path}.{key}]"]
            out += [f'"{k}" = {_toml_value(v)}' for k, v in table.items()]
    return "\n".join(out) + "\n"


def _download(service: str) -> list[str]:
    """One offer file, over https from the public price list host (a fixed host and path)."""
    # Certificate-verified TLS to the one fixed public host (the audit rule flags any
    # HTTPSConnection; this one sets its context explicitly).
    context = ssl.create_default_context()
    conn = http.client.HTTPSConnection(PRICE_LIST_HOST, timeout=300, context=context)  # nosemgrep
    try:
        conn.request("GET", OFFER_PATH.format(service=service, region=REGION))
        resp = conn.getresponse()
        if resp.status != 200:
            raise RuntimeError(f"{service}: the price list answered HTTP {resp.status}")
        return resp.read().decode("utf-8").splitlines()
    finally:
        conn.close()


def load_offers(source_dir: Path | None) -> dict[str, OfferFile]:
    offers = {}
    for service in services():
        if source_dir is None:
            lines = _download(service)
        else:
            lines = (source_dir / f"{service}.csv").read_text(encoding="utf-8").splitlines()
        offers[service] = read_offer(service, lines)
    return offers


def age_warning(
    retrieved_on: dt.date, today: dt.date, max_age_days: int = MAX_AGE_DAYS
) -> str | None:
    """The warning --check-age prints, or None while the table is younger than the limit."""
    age = (today - retrieved_on).days
    if age <= max_age_days:
        return None
    return (
        f"warning: {PRICES_FILE.relative_to(REPO_ROOT)} was retrieved on "
        f"{retrieved_on.isoformat()}, {age} days ago (limit {max_age_days}); "
        "refresh it with: uv run python scripts/refresh_prices.py"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source-dir", type=Path, help="read <ServiceCode>.csv from here")
    parser.add_argument("--output", type=Path, default=PRICES_FILE)
    parser.add_argument("--check-age", action="store_true", help="warn when the table is old")
    parser.add_argument("--max-age-days", type=int, default=MAX_AGE_DAYS)
    args = parser.parse_args(argv)
    if args.check_age:
        retrieved_on = load_price_table(args.output).retrieved_on
        warning = age_warning(retrieved_on, dt.date.today(), args.max_age_days)
        print(
            warning
            or f"prices: retrieved {retrieved_on.isoformat()}, within {args.max_age_days} days"
        )
        return 0
    try:
        offers = load_offers(args.source_dir)
    except (OSError, http.client.HTTPException, RuntimeError, ValueError) as exc:
        # A failed download or an unreadable file: one line, and the table is untouched.
        print(f"refresh_prices: {exc}", file=sys.stderr)
        return 1
    args.output.write_text(render_table(offers, dt.date.today()), encoding="utf-8")
    load_price_table(args.output)  # the file it wrote must load
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
