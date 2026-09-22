-- Only `intensity_actual` is used downstream: the API's forecast has no issue time.
with deduped as (
    select *
    from {{ source('lake', 'carbon_intensity') }}
    where true
    qualify row_number() over (partition by start_time order by _ingested_at desc) = 1
)

select
    cast(start_time as timestamp) as start_time_utc,
    cast(end_time as timestamp) as end_time_utc,
    intensity_actual as intensity_actual_gco2_kwh,
    intensity_forecast as intensity_forecast_gco2_kwh,
    intensity_index,
    _ingested_at as ingested_at
from deduped
