-- National demand forecast vintages. `published_at` is when the vintage became
-- public; point-in-time joins filter on it.
with deduped as (
    select *
    from {{ source('lake', 'elexon_ndf') }}
    where boundary = 'N'
    qualify row_number() over (
        partition by publish_time, settlement_date, settlement_period
        order by _ingested_at desc
    ) = 1
)

select
    cast(settlement_date as date) as settlement_date,
    cast(settlement_period as integer) as settlement_period,
    cast(start_time as timestamp) as start_time_utc,
    cast(publish_time as timestamp) as published_at,
    demand_mw,
    _ingested_at as ingested_at
from deduped
