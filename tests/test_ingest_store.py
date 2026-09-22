from datetime import date, timedelta

import pandas as pd
import pytest

from elecprice.config import Settings
from elecprice.ingest.store import ChunkStore, Dataset, aligned_chunks, ingest_dataset


@pytest.fixture
def store(tmp_path):
    return ChunkStore(Settings(data_dir=tmp_path))


class FakeSource:
    """A dataset whose fetch counts calls and returns one row per day."""

    def __init__(self, fail_on: date | None = None):
        self.calls: list[tuple[date, date]] = []
        self.fail_on = fail_on

    def fetch(self, start, end):
        self.calls.append((start, end))
        if self.fail_on and start <= self.fail_on <= end:
            raise RuntimeError("boom")
        days = pd.date_range(start, end, freq="D")
        return {"data": [{"day": d.date().isoformat(), "v": 1.0} for d in days]}

    @staticmethod
    def normalise(payload):
        return pd.DataFrame(payload["data"])

    def dataset(self, **kw) -> Dataset:
        return Dataset("fake", "src", 7, self.fetch, self.normalise, **kw)


def test_aligned_chunks_are_stable_regardless_of_start():
    a = aligned_chunks(date(2024, 1, 3), date(2024, 1, 31), 7)
    b = aligned_chunks(date(2024, 1, 10), date(2024, 1, 31), 7)
    assert set(b) <= set(a)
    for s, e in a:
        assert (e - s).days == 6
    assert a[0][0] <= date(2024, 1, 3) <= a[0][1]
    assert a[-1][0] <= date(2024, 1, 31) <= a[-1][1]


def test_settled_chunks_are_not_refetched(store):
    src = FakeSource()
    ds = src.dataset()
    today = date(2024, 3, 1)
    r1 = ingest_dataset(ds, date(2024, 1, 1), date(2024, 1, 31), today=today, store=store)
    assert r1.ok and r1.fetched == r1.chunks
    n_calls = len(src.calls)

    r2 = ingest_dataset(ds, date(2024, 1, 1), date(2024, 1, 31), today=today, store=store)
    assert r2.skipped == r2.chunks
    assert len(src.calls) == n_calls  # no HTTP on the second run


def test_recent_chunks_are_refetched(store):
    src = FakeSource()
    ds = src.dataset()
    today = date(2024, 1, 20)
    ingest_dataset(ds, date(2024, 1, 1), today, today=today, store=store, refresh_days=3)
    before = len(src.calls)
    r = ingest_dataset(ds, date(2024, 1, 1), today, today=today, store=store, refresh_days=3)
    # Only chunks ending within 3 days of today (or later) are fetched again.
    assert r.fetched >= 1
    assert r.skipped == r.chunks - r.fetched
    assert len(src.calls) == before + r.fetched


def test_missing_lake_is_rebuilt_from_raw_cache_without_network(store):
    src = FakeSource()
    ds = src.dataset()
    today = date(2024, 3, 1)
    ingest_dataset(ds, date(2024, 1, 1), date(2024, 1, 14), today=today, store=store)
    for p in (store.settings.lake_dir / "fake" / "src").glob("*.parquet"):
        p.unlink()
    calls = len(src.calls)
    r = ingest_dataset(ds, date(2024, 1, 1), date(2024, 1, 14), today=today, store=store)
    assert r.from_cache == r.chunks
    assert len(src.calls) == calls
    assert list((store.settings.lake_dir / "fake" / "src").glob("*.parquet"))


def test_force_refetches_everything(store):
    src = FakeSource()
    ds = src.dataset()
    today = date(2024, 3, 1)
    ingest_dataset(ds, date(2024, 1, 1), date(2024, 1, 14), today=today, store=store)
    r = ingest_dataset(
        ds, date(2024, 1, 1), date(2024, 1, 14), today=today, store=store, force=True
    )
    assert r.fetched == r.chunks


def test_failed_chunk_is_reported_and_others_complete(store):
    src = FakeSource(fail_on=date(2024, 1, 10))
    ds = src.dataset()
    r = ingest_dataset(ds, date(2024, 1, 1), date(2024, 1, 31), today=date(2024, 3, 1), store=store)
    assert len(r.failed) == 1
    assert r.fetched == r.chunks - 1
    assert not r.ok


def test_never_requests_past_horizon_and_respects_min_start(store):
    src = FakeSource()
    ds = src.dataset(lookahead_days=1, min_start=date(2024, 1, 8))
    today = date(2024, 1, 17)
    ingest_dataset(ds, date(2024, 1, 1), date(2024, 12, 31), today=today, store=store)
    assert max(end for _, end in src.calls) == today + timedelta(days=1)
    assert min(start for start, _ in src.calls) >= date(2024, 1, 6)  # aligned chunk start
    written = pd.concat(
        pd.read_parquet(p) for p in (store.settings.lake_dir / "fake" / "src").glob("*.parquet")
    )
    assert {"_chunk", "_ingested_at"} <= set(written.columns)
    assert written["day"].max() == (today + timedelta(days=1)).isoformat()
