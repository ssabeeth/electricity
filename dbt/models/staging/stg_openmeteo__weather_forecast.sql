-- Weather forecast vintages per location. `available_at` is a conservative
-- upper bound on when the vintage could have been known (see ingestion code).
with deduped as (
    select *
    from {{ source('lake', 'openmeteo_weather_forecast') }}
    qualify row_number() over (
        partition by location_id, valid_time, lead_days
        order by _ingested_at desc
    ) = 1
)

select
    location_id,
    cast(valid_time as timestamp) as valid_hour_utc,
    cast(lead_days as integer) as lead_days,
    cast(available_at as timestamp) as available_at,
    model as nwp_model,
    temperature_2m as temperature_c,
    wind_speed_100m as wind_speed_100m_ms,
    shortwave_radiation as shortwave_radiation_wm2,
    cloud_cover as cloud_cover_pct,
    _ingested_at as ingested_at
from deduped
