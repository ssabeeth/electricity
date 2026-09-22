with deduped as (
    select *
    from {{ source('lake', 'elexon_demand_outturn') }}
    qualify row_number() over (
        partition by settlement_date, settlement_period
        order by _ingested_at desc, publish_time desc
    ) = 1
)

select
    cast(settlement_date as date) as settlement_date,
    cast(settlement_period as integer) as settlement_period,
    cast(start_time as timestamp) as start_time_utc,
    cast(publish_time as timestamp) as published_at,
    indo_mw,
    itsdo_mw,
    _ingested_at as ingested_at
from deduped
