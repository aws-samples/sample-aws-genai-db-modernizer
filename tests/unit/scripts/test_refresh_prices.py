"""The price table and the script that refreshes it from the AWS Price List (#1196).

``tests/fixtures/price_list/`` holds the us-east-1 offer files the committed table was
read from, trimmed to the rows the script's selectors match plus rows that look alike
(the other storage configuration, Redis OSS next to Valkey, the free storage tier, the
unused-reservation rows), so a selector that grows ambiguous fails here.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from scripts import refresh_prices as rp
from src.shared import catalog
from src.shared.catalog.prices import PRICES_FILE, load_price_table, price_basis

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "price_list"


@pytest.fixture(scope="module")
def offers() -> dict[str, rp.OfferFile]:
    return rp.load_offers(FIXTURES)


@pytest.fixture(scope="module")
def prices(offers) -> dict[str, dict[str, object]]:
    return rp.extract_prices(offers)


class TestTheErrorsTheResearchFound:
    """Each stale or wrong price #1196 lists, read from the price list files."""

    def test_aurora_io_optimized_is_priced_at_its_own_rates(self, prices) -> None:
        for engine in ("aurora_mysql", "aurora_postgresql"):
            io = prices[f"{engine}.io_optimized"]
            std = prices[f"{engine}.standard"]
            assert (io["acu_price_per_hour"], io["storage_price_per_gb_month"]) == (0.16, 0.225)
            assert (std["acu_price_per_hour"], std["storage_price_per_gb_month"]) == (0.12, 0.1)
            # I/O-Optimized has no per-request price; Standard bills $0.20 per million I/Os.
            assert "read_unit_price_per_million" not in io
            assert std["read_unit_price_per_million"] == std["write_unit_price_per_million"] == 0.2

    def test_aurora_instance_prices(self, prices) -> None:
        for engine in ("aurora_mysql", "aurora_postgresql"):
            assert prices[f"{engine}.standard"]["instance_hourly"] == {
                "db.t4g.medium": 0.073,
                "db.r6g.large": 0.26,
                "db.r6g.xlarge": 0.519,
            }
            assert prices[f"{engine}.io_optimized"]["instance_hourly"] == {
                "db.t4g.medium": 0.095,
                "db.r6g.large": 0.338,
                "db.r6g.xlarge": 0.675,
            }

    def test_opensearch_storage_is_gp3_not_serverless_managed_storage(self, prices) -> None:
        assert prices["opensearch.managed"]["storage_price_per_gb_month"] == 0.122
        assert prices["opensearch.managed"]["instance_hourly"] == {"r6g.large.search": 0.167}

    def test_elasticache_nodes_exist_and_valkey_is_below_redis_oss(self, prices) -> None:
        valkey, redis = prices["elasticache.valkey"], prices["elasticache.redis_oss"]
        assert "cache.r7g.medium" not in valkey["instance_hourly"]
        assert valkey["instance_hourly"]["cache.m7g.large"] == 0.1264
        assert valkey["instance_memory_gib"]["cache.m7g.large"] == 6.38
        for node, price in valkey["instance_hourly"].items():
            assert price < redis["instance_hourly"][node], node
        # Cross-AZ replication is regional data transfer, from the data transfer offer.
        assert valkey["cross_az_transfer_price_per_gb"] == 0.01

    def test_documentdb_instance_price(self, prices) -> None:
        assert prices["documentdb.standard"]["instance_hourly"]["db.r6g.large"] == 0.26315
        assert prices["documentdb.io_optimized"]["instance_hourly"]["db.r6g.large"] == 0.2895

    def test_documentdb_instance_memory_comes_from_the_memory_column(self, prices) -> None:
        for section in ("documentdb.standard", "documentdb.io_optimized"):
            assert prices[section]["instance_memory_gib"] == {
                "db.r6g.large": 16.0,
                "db.r6g.xlarge": 32.0,
            }

    def test_dynamodb_storage_skips_the_free_tier_row(self, prices) -> None:
        assert prices["dynamodb.on_demand"] == {
            "read_unit_price_per_million": 0.125,
            "write_unit_price_per_million": 0.625,
            "storage_price_per_gb_month": 0.25,
            "streams_read_unit_price_per_million": 0.2,
        }

    def test_dynamodb_streams_price_skips_the_free_tier_row(self, prices) -> None:
        """$0.0000002 a DynamoDB Streams read request unit beyond the free 2.5 million (#1527)."""
        assert prices["dynamodb.on_demand"]["streams_read_unit_price_per_million"] == 0.2
        both = rp.Selector("AmazonDynamoDB", {"usageType": "USE1-Streams-Requests"}, "Requests")
        with pytest.raises(ValueError, match="2 different figures"):
            rp._pick(both, rp.load_offers(FIXTURES), "dynamodb.on_demand.streams")


class TestTheCommittedTable:
    def test_is_what_the_script_writes_from_the_offer_files(self, offers) -> None:
        """The committed table is the script's output for the fixture files, byte for byte
        (a refresh updates both: copy the downloaded files' matched rows over)."""
        committed = load_price_table()
        assert rp.render_table(offers, committed.retrieved_on) == PRICES_FILE.read_text()

    @pytest.mark.passes_on_main
    def test_states_each_offer_files_publication_date(self, offers) -> None:
        table = load_price_table()
        assert set(table.published) == set(offers)
        assert table.published == {s: o.published for s, o in offers.items()}

    def test_every_estimator_reads_it_through_the_engine_catalog(self) -> None:
        table = load_price_table()
        for engine in ("aurora_mysql", "aurora_postgresql", "documentdb", "dynamodb"):
            pricing = catalog.pricing(engine)
            section = table.section(pricing.list_prices or "")
            for field, value in section.items():
                assert getattr(pricing, field) == value, (engine, field)
        for engine in ("opensearch", "elasticache"):
            pricing = catalog.pricing(engine)
            assert (
                pricing.instance_hourly
                == table.section(pricing.list_prices or "")["instance_hourly"]
            )

    def test_price_basis_is_the_tables_date(self) -> None:
        assert price_basis() == {
            "source": "AWS Price List",
            "region": "us-east-1",
            "pricing_model": "on-demand",
            "retrieved_on": load_price_table().retrieved_on.isoformat(),
        }


class TestSelectorsFailLoudly:
    def test_no_matching_row_stops_the_refresh(self, offers) -> None:
        sel = rp.Selector("AmazonES", {"usageType": "ES:GP9-Storage"}, "GB-Mo")
        with pytest.raises(ValueError, match="matched no row"):
            rp._pick(sel, offers, "opensearch.managed.storage")

    def test_rows_with_different_figures_stop_the_refresh(self, offers) -> None:
        """Without the storage tier, the free and the paid DynamoDB storage rows tie."""
        sel = rp.Selector("AmazonDynamoDB", {"usageType": "TimedStorage-ByteHrs"}, "GB-Mo")
        with pytest.raises(ValueError, match="2 different figures"):
            rp._pick(sel, offers, "dynamodb.on_demand.storage")

    def test_a_file_of_another_offer_is_rejected(self) -> None:
        text = (FIXTURES / "AmazonES.csv").read_text().splitlines()
        with pytest.raises(ValueError, match="AmazonES offer"):
            rp.read_offer("AmazonRDS", text)


class TestAgeCheck:
    def test_a_table_within_the_limit_is_quiet(self) -> None:
        day = dt.date(2026, 10, 19)
        assert rp.age_warning(day, day) is None
        assert rp.age_warning(day, day + dt.timedelta(days=rp.MAX_AGE_DAYS)) is None

    def test_an_old_table_warns_with_the_refresh_command(self) -> None:
        day = dt.date(2026, 10, 19)
        warning = rp.age_warning(day, day + dt.timedelta(days=rp.MAX_AGE_DAYS + 1))
        assert warning is not None
        assert f"{rp.MAX_AGE_DAYS + 1} days ago" in warning
        assert "scripts/refresh_prices.py" in warning

    def test_check_age_only_warns(self, tmp_path: Path, capsys) -> None:
        old = tmp_path / "prices.toml"
        old.write_text(
            PRICES_FILE.read_text().replace(
                f"retrieved_on = {load_price_table().retrieved_on.isoformat()}",
                "retrieved_on = 2020-01-01",
            )
        )
        assert rp.main(["--check-age", "--output", str(old)]) == 0
        assert capsys.readouterr().out.startswith("warning:")


class _Response:
    def __init__(self, status: int, body: bytes = b"") -> None:
        self.status, self._body = status, body

    def read(self) -> bytes:
        return self._body


class _Connection:
    """A stand-in HTTPSConnection: ``answer`` is the response, or the error to raise."""

    answer: object = None
    hosts: list[str] = []

    def __init__(self, host: str, timeout: float, context: object) -> None:
        type(self).hosts.append(host)

    def request(self, method: str, path: str) -> None:
        if isinstance(self.answer, BaseException):
            raise self.answer

    def getresponse(self) -> _Response:
        assert isinstance(self.answer, _Response)
        return self.answer

    def close(self) -> None:
        pass


class TestDownloadFailures:
    """A failed download stops the refresh with one line and never writes the table."""

    @pytest.fixture
    def connection(self, monkeypatch: pytest.MonkeyPatch) -> type[_Connection]:
        _Connection.hosts = []
        monkeypatch.setattr(rp.http.client, "HTTPSConnection", _Connection)
        return _Connection

    @pytest.mark.parametrize(
        "answer, message",
        [
            (_Response(503), "answered HTTP 503"),
            (OSError("network is unreachable"), "network is unreachable"),
            (TimeoutError("timed out"), "timed out"),
        ],
    )
    def test_the_refresh_stops_and_writes_nothing(
        self, connection, answer, message, tmp_path: Path, capsys
    ) -> None:
        connection.answer = answer
        out = tmp_path / "prices.toml"
        assert rp.main(["--output", str(out)]) == 1
        err = capsys.readouterr().err
        assert message in err and err.count("\n") == 1
        assert not out.exists()
        assert connection.hosts == [rp.PRICE_LIST_HOST]

    def test_a_successful_download_is_parsed(self, connection) -> None:
        connection.answer = _Response(200, (FIXTURES / "AmazonES.csv").read_bytes())
        offer = rp.read_offer("AmazonES", rp._download("AmazonES"))
        assert offer.published == load_price_table().published["AmazonES"]


def test_a_sku_that_changes_its_unit_stops_the_refresh(offers) -> None:
    sel = rp.Selector("AmazonES", {"usageType": "ES:GP3-Storage"}, "GB-Hr")
    with pytest.raises(ValueError, match=r"priced per \['GB-Mo'\], not 'GB-Hr'"):
        rp._pick(sel, offers, "opensearch.managed.storage")


class TestDataMovementPrices:
    """DMS, MSK and MSK Connect (#1424): defined in the script, read only when their offer
    files are there. The files below are synthetic, shaped like the price list's; their
    figures are test inputs, not AWS prices."""

    HEADER = "TermType,Unit,PricePerUnit,Instance Type,Deployment Option,usageType"

    def _offer(self, code: str, rows: list[str]) -> list[str]:
        meta = [
            '"FormatVersion","v1.0"',
            '"Disclaimer","synthetic"',
            '"Publication Date","2026-11-01T00:00:00Z"',
            '"Version","1"',
            f'"OfferCode","{code}"',
        ]
        return [*meta, self.HEADER, *rows]

    def _write(self, tmp_path: Path) -> Path:
        dms = [
            "OnDemand,Hrs,0.5,dms.r6i.large,Single-AZ,USE1-InstanceUsg:dms.r6i.large",
            "OnDemand,GB-Mo,0.1,,Single-AZ,USE1-Storage:GP2",
        ]
        msk = [
            "OnDemand,Hrs,0.2,kafka.m5.large,,USE1-Kafka.m5.large",
            "OnDemand,GB-Mo,0.1,,,USE1-Kafka.Storage.GP2",
            "OnDemand,Hrs,0.1,,,USE1-Connect-MCU-Hours",
        ]
        (tmp_path / "AWSDatabaseMigrationSvc.csv").write_text(
            "\n".join(self._offer("AWSDatabaseMigrationSvc", dms))
        )
        (tmp_path / "AmazonMSK.csv").write_text("\n".join(self._offer("AmazonMSK", msk)))
        for f in FIXTURES.glob("*.csv"):
            (tmp_path / f.name).write_text(f.read_text())
        return tmp_path

    def test_the_script_defines_the_three_sections_the_plan_reads(self) -> None:
        assert {"dms.replication", "msk.provisioned", "msk_connect.workers"} <= set(rp.SPECS)
        assert {"AWSDatabaseMigrationSvc", "AmazonMSK"} <= set(rp.services())

    @pytest.mark.passes_on_main
    def test_a_refresh_without_the_offer_files_leaves_the_sections_out(self, offers) -> None:
        assert "dms.replication" not in rp.extract_prices(offers)
        assert "[dms." not in PRICES_FILE.read_text()

    def test_a_refresh_with_the_offer_files_writes_them_with_their_dates(
        self, tmp_path: Path
    ) -> None:
        offers = rp.load_offers(self._write(tmp_path))
        prices = rp.extract_prices(offers)
        assert prices["dms.replication"] == {
            "instance_hourly": {"dms.r6i.large": 0.5},
            "storage_price_per_gb_month": 0.1,
        }
        assert prices["msk.provisioned"]["instance_hourly"] == {"kafka.m5.large": 0.2}
        assert prices["msk_connect.workers"] == {"mcu_price_per_hour": 0.1}
        table = rp.render_table(offers, dt.date(2026, 11, 2))
        assert "AmazonMSK = 2026-11-01" in table and "AWSDatabaseMigrationSvc = 2026-11-01" in table

    def test_a_renamed_usage_type_stops_the_refresh(self, tmp_path: Path) -> None:
        src = self._write(tmp_path)
        text = (src / "AmazonMSK.csv").read_text().replace("Kafka.Storage.GP2", "Kafka.Storage.X")
        (src / "AmazonMSK.csv").write_text(text)
        with pytest.raises(ValueError, match="msk.provisioned.storage_price_per_gb_month"):
            rp.extract_prices(rp.load_offers(src))
