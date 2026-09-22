"""Ingestion: plain Python pulling each source into a cached raw store + Parquet lake."""

from __future__ import annotations

from datetime import date

from elecprice.ingest import carbon, elexon, neso, openmeteo
from elecprice.ingest.store import ChunkStore, Dataset, IngestReport, ingest_dataset

REGISTRY: dict[str, Dataset] = {
    ds.key: ds for ds in [*elexon.DATASETS, *neso.DATASETS, *openmeteo.DATASETS, *carbon.DATASETS]
}

# Parallel requests per source. Kept low to be polite to free public APIs.
WORKERS = {"elexon": 4, "neso": 2, "openmeteo": 1, "carbon": 2}


def select(patterns: list[str] | None) -> list[Dataset]:
    """Datasets matching ``source`` or ``source/name`` patterns (all if empty)."""
    if not patterns:
        return list(REGISTRY.values())
    out = []
    for key, ds in REGISTRY.items():
        if any(p == key or p == ds.source for p in patterns):
            out.append(ds)
    unknown = [p for p in patterns if not any(p in (k, d.source) for k, d in REGISTRY.items())]
    if unknown:
        raise KeyError(f"Unknown dataset(s) {unknown}; choose from {sorted(REGISTRY)}")
    return out


def ingest(
    patterns: list[str] | None,
    start: date,
    end: date | None = None,
    *,
    force: bool = False,
    refresh_days: int = 3,
    store: ChunkStore | None = None,
) -> list[IngestReport]:
    return [
        ingest_dataset(
            ds,
            start,
            end,
            force=force,
            refresh_days=refresh_days,
            workers=WORKERS.get(ds.source, 1),
            store=store,
        )
        for ds in select(patterns)
    ]


__all__ = ["REGISTRY", "ChunkStore", "Dataset", "IngestReport", "ingest", "select"]
