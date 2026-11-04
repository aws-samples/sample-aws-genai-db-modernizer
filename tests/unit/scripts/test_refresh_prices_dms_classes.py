# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every DMS sizing class is priced, in both deployments (#1798)."""

from __future__ import annotations

from scripts import refresh_prices
from src.shared.catalog.dms_sizing import dms_sizing
from src.shared.catalog.prices import price_table


def test_the_selectors_cover_every_sizing_class_in_both_deployments():
    dms = refresh_prices._data_movement()["dms.replication"]
    classes = {r.instance_class for r in dms_sizing().rows}
    assert classes <= set(dms["instance_hourly"]) and classes <= set(
        dms["multi_az_instance_hourly"]
    )
    assert "multi_az_storage_price_per_gb_month" in dms


def test_the_committed_table_prices_every_sizing_class():
    dms = price_table().section("dms.replication")
    for row in dms_sizing().rows:
        assert dms["instance_hourly"][row.instance_class] > 0
        assert (
            dms["multi_az_instance_hourly"][row.instance_class]
            > dms["instance_hourly"][row.instance_class]
        )
    assert dms["multi_az_storage_price_per_gb_month"] > 0
