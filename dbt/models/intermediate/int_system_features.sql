-- Daily system-state features known at the cutoff: recent carbon intensity,
-- generation mix, interconnector flows and demand. One row per delivery day.
{% set ci_lag = var('carbon_availability_lag_minutes') %}

with days as (
    select distinct settlement_date, cutoff_utc from {{ ref('int_settlement_calendar') }}
),

carbon as (
    select
        intensity_actual_gco2_kwh,
        {{ add_minutes('end_time_utc', ci_lag) }} as available_at
    from {{ ref('stg_carbon__intensity') }}
    where intensity_actual_gco2_kwh is not null
),

ci as (
    select
        d.settlement_date,
        avg(c.intensity_actual_gco2_kwh) as ci_actual_24h_mean,
        max(c.available_at) as ci_available_at
    from days as d
    inner join carbon as c
        on c.available_at <= d.cutoff_utc
        and c.available_at > {{ add_minutes('d.cutoff_utc', -24 * 60) }}
    group by d.settlement_date
),

gen_by_period as (
    select
        start_time_utc,
        max(published_at) as published_at,
        sum(case when fuel_category = 'gas' then generation_mw else 0 end) as gas_mw,
        sum(case when fuel_category = 'wind' then generation_mw else 0 end) as wind_mw,
        sum(case when fuel_category = 'nuclear' then generation_mw else 0 end) as nuclear_mw,
        sum(case when not is_interconnector and generation_mw > 0 then generation_mw else 0 end)
            as domestic_gen_mw,
        sum(case when is_interconnector then generation_mw else 0 end) as net_imports_mw
    from {{ ref('stg_elexon__generation_by_fuel') }}
    group by start_time_utc
),

gen as (
    select
        d.settlement_date,
        sum(g.gas_mw) / nullif(sum(g.domestic_gen_mw), 0) as gas_share_7d,
        sum(g.wind_mw) / nullif(sum(g.domestic_gen_mw), 0) as wind_share_7d,
        sum(g.nuclear_mw) / nullif(sum(g.domestic_gen_mw), 0) as nuclear_share_7d,
        avg(g.net_imports_mw) as net_imports_7d_mean_mw,
        max(g.published_at) as gen_available_at
    from days as d
    inner join gen_by_period as g
        on g.published_at <= d.cutoff_utc
        and g.start_time_utc >= {{ add_minutes('d.cutoff_utc', -7 * 24 * 60) }}
    group by d.settlement_date
),

demand as (
    select
        d.settlement_date,
        avg(o.indo_mw) as demand_outturn_7d_mean_mw,
        max(o.published_at) as demand_available_at
    from days as d
    inner join {{ ref('stg_elexon__demand_outturn') }} as o
        on o.published_at <= d.cutoff_utc
        and o.start_time_utc >= {{ add_minutes('d.cutoff_utc', -7 * 24 * 60) }}
    group by d.settlement_date
)

select
    d.settlement_date,
    d.cutoff_utc,
    ci.ci_actual_24h_mean,
    gen.gas_share_7d,
    gen.wind_share_7d,
    gen.nuclear_share_7d,
    gen.net_imports_7d_mean_mw,
    demand.demand_outturn_7d_mean_mw,
    {{ greatest_ts(['ci.ci_available_at', 'gen.gen_available_at', 'demand.demand_available_at']) }}
        as system_available_at
from days as d
left join ci on d.settlement_date = ci.settlement_date
left join gen on d.settlement_date = gen.settlement_date
left join demand on d.settlement_date = demand.settlement_date
