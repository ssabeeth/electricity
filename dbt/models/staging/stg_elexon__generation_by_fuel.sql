with deduped as (
    select *
    from {{ source('lake', 'elexon_fuelhh') }}
    where true
    qualify row_number() over (
        partition by settlement_date, settlement_period, fuel_type
        order by _ingested_at desc, publish_time desc
    ) = 1
)

select
    cast(d.settlement_date as date) as settlement_date,
    cast(d.settlement_period as integer) as settlement_period,
    cast(d.start_time as timestamp) as start_time_utc,
    cast(d.publish_time as timestamp) as published_at,
    d.fuel_type,
    coalesce(f.category, 'other') as fuel_category,
    coalesce(f.is_interconnector, false) as is_interconnector,
    d.generation_mw,
    d._ingested_at as ingested_at
from deduped as d
left join {{ ref('fuel_types') }} as f on d.fuel_type = f.fuel_type
