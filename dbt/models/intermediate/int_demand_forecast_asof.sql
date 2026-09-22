-- Point-in-time national demand forecast: for each delivery period, the latest
-- NDF vintage published at or before that delivery day's decision cutoff.
with calendar as (
    select settlement_date, settlement_period, start_time_utc, cutoff_utc
    from {{ ref('int_settlement_calendar') }}
),

candidates as (
    select
        c.settlement_date,
        c.settlement_period,
        c.cutoff_utc,
        f.demand_mw,
        f.published_at
    from calendar as c
    left join {{ ref('stg_elexon__demand_forecast') }} as f
        on f.settlement_date = c.settlement_date
        and f.settlement_period = c.settlement_period
        and f.published_at <= c.cutoff_utc
    where true
    qualify row_number() over (
        partition by c.settlement_date, c.settlement_period
        order by f.published_at desc nulls last
    ) = 1
)

select
    settlement_date,
    settlement_period,
    cutoff_utc,
    demand_mw as ndf_demand_mw,
    published_at as ndf_published_at
from candidates
