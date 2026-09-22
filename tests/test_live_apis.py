"""Smoke tests against the real APIs. Deselected in CI with ``-m "not network"``."""

from datetime import date

import pytest

from elecprice.ingest import REGISTRY

pytestmark = pytest.mark.network

DAY = date(2024, 6, 3)


@pytest.mark.parametrize("key", sorted(REGISTRY))
def test_each_source_returns_rows_for_one_day(key):
    ds = REGISTRY[key]
    df = ds.normalise(ds.fetch(DAY, DAY))
    assert not df.empty, key
