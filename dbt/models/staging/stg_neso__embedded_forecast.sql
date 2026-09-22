-- Embedded (distribution-connected) wind and solar forecast vintages.
with deduped as (
    select *
    from {{ source('lake', 'neso_embedded_forecast') }}
    qualify row_number() over (
        partition by forecast_issued_at, settlement_date, settlement_period
        order by _ingested_at desc
    ) = 1
)

select
    cast(settlement_date as date) as settlement_date,
    cast(settlement_period as integer) as settlement_period,
    cast(start_time as timestamp) as start_time_utc,
    cast(forecast_issued_at as timestamp) as issued_at,
    forecast_time_basis,
    embedded_wind_mw,
    embedded_wind_capacity_mw,
    embedded_solar_mw,
    embedded_solar_capacity_mw,
    _ingested_at as ingested_at
from deduped
