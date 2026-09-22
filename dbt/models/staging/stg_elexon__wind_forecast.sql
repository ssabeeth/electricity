-- Transmission wind forecast vintages at hourly resolution.
with deduped as (
    select *
    from {{ source('lake', 'elexon_windfor') }}
    where true
    qualify row_number() over (
        partition by publish_time, start_time
        order by _ingested_at desc
    ) = 1
)

select
    cast(start_time as timestamp) as valid_hour_utc,
    cast(publish_time as timestamp) as published_at,
    generation_mw as wind_mw,
    _ingested_at as ingested_at
from deduped
