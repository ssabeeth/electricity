import json
from pathlib import Path

import pandas as pd
import pytest

from elecprice.ingest import carbon, elexon, neso, openmeteo

FIX = Path(__file__).parent / "fixtures" / "api"


def load(name):
    return json.loads((FIX / name).read_text())


def test_mid():
    df = elexon.normalise_mid(load("elexon_mid.json"))
    assert list(df.columns) == [
        "start_time",
        "data_provider",
        "settlement_date",
        "settlement_period",
        "price",
        "volume",
    ]
    assert (df["data_provider"] == "APXMIDP").all()
    assert str(df["start_time"].dtype) == "datetime64[us]"  # naive UTC
    assert df["price"].dtype == "float64"


@pytest.mark.parametrize(
    ("fn", "fixture", "cols"),
    [
        (elexon.normalise_demand_outturn, "elexon_demand_outturn.json", {"indo_mw", "itsdo_mw"}),
        (elexon.normalise_fuelhh, "elexon_fuelhh.json", {"fuel_type", "generation_mw"}),
        (elexon.normalise_ndf, "elexon_ndf.json", {"publish_time", "demand_mw"}),
        (elexon.normalise_windfor, "elexon_windfor.json", {"publish_time", "generation_mw"}),
        (carbon.normalise_intensity, "carbon_intensity.json", {"intensity_actual"}),
    ],
)
def test_normalisers_produce_typed_columns(fn, fixture, cols):
    df = fn(load(fixture))
    assert not df.empty
    assert cols <= set(df.columns)
    assert str(df["start_time"].dtype) == "datetime64[us]"


def test_empty_payloads_give_empty_frames():
    assert elexon.normalise_mid({"data": []}).empty
    assert neso.normalise_embedded({"records": []}).empty
    assert carbon.normalise_intensity({"data": []}).empty


def test_neso_forecast_time_is_uk_local_and_converted_to_utc():
    df = neso.normalise_embedded(load("neso_embedded_summer.json"))
    # Issued 08:12:17 BST on 2026-06-10 == 07:12:17 UTC.
    assert (df["forecast_issued_raw"] == pd.Timestamp("2026-06-10 08:12:17")).all()
    assert (df["forecast_time_basis"] == "uk_local").all()
    assert (df["forecast_issued_at"] == pd.Timestamp("2026-06-10 07:12:17")).all()
    # SP1 of 2026-06-11 (BST) starts 23:00 UTC on 2026-06-10.
    sp1 = df[df["settlement_period"] == 1].iloc[0]
    assert sp1["start_time"] == pd.Timestamp("2026-06-10 23:00")
    assert str(sp1["settlement_date"]) == "2026-06-11"


def test_neso_ambiguous_autumn_time_resolves_to_later_instant():
    payload = {
        "records": [
            {
                "SETTLEMENT_DATE": "2025-10-27T00:00:00",
                "SETTLEMENT_PERIOD": 1,
                "DATE_GMT": "2025-10-27T00:00:00",
                "TIME_GMT": "00:30:00",
                "EMBEDDED_WIND_FORECAST": 1,
                "EMBEDDED_WIND_CAPACITY": 1,
                "EMBEDDED_SOLAR_FORECAST": 0,
                "EMBEDDED_SOLAR_CAPACITY": 1,
                "Forecast_Datetime": "2025-10-26T01:12:00",  # occurs twice locally
            }
        ]
    }
    df = neso.normalise_embedded(payload)
    assert df["forecast_issued_at"].iloc[0] == pd.Timestamp("2025-10-26 01:12")  # GMT reading


def test_neso_new_hhmm_format_uses_conservative_utc_reading():
    payload = load("neso_embedded_summer.json")
    for rec in payload["records"]:
        rec["TIME_GMT"] = rec["TIME_GMT"][:5]  # "23:30:00" -> "23:30"
    df = neso.normalise_embedded(payload)
    assert (df["forecast_time_basis"] == "utc_assumed").all()
    # Read as UTC: later than the UK-local reading (07:12:17) in summer.
    assert (df["forecast_issued_at"] == pd.Timestamp("2026-06-10 08:12:17")).all()
    sp1 = df[df["settlement_period"] == 1].iloc[0]
    assert sp1["start_time"] == pd.Timestamp("2026-06-10 23:00")


def test_openmeteo_vintages_have_conservative_availability():
    df = openmeteo.normalise_weather(load("openmeteo_weather.json"))
    assert set(df["lead_days"]) == {1, 2}
    assert set(df["location_id"]) == {"highlands_onshore", "southern_uplands"}
    expected = df["valid_time"] - pd.to_timedelta(df["lead_days"].astype(int), unit="D")
    expected += pd.Timedelta(hours=6)
    assert (df["available_at"] == expected).all()
    # A day-2 vintage is always available earlier than the day-1 vintage.
    wide = df.pivot_table(
        index=["location_id", "valid_time"], columns="lead_days", values="available_at"
    )
    assert (wide[2] < wide[1]).all()


def test_weather_locations_seed_is_valid():
    locs = openmeteo.load_locations()
    assert locs["location_id"].is_unique
    assert locs["latitude"].between(49.5, 61).all()
    assert locs["longitude"].between(-8.5, 2.5).all()
    for group in ("wind_weight", "solar_weight", "temperature_weight"):
        assert (locs[group] > 0).sum() >= 4, group
