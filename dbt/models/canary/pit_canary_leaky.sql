-- DELIBERATELY LEAKY. Enabled only with --vars '{pit_canary: true}'.
-- Reproduces a realistic bug (an off-by-hours cutoff in an as-of join) so that
-- tests/test_dbt_point_in_time.py can prove the point_in_time test catches it.
{{ config(enabled=var('pit_canary', false)) }}

with calendar as (
    select settlement_date, settlement_period, cutoff_utc
    from {{ ref('int_settlement_calendar') }}
    where cutoff_utc <= {{ now_utc() }}
)

select
    c.settlement_date,
    c.settlement_period,
    c.cutoff_utc,
    f.demand_mw as ndf_demand_mw,
    f.published_at as ndf_published_at
from calendar as c
inner join {{ ref('stg_elexon__demand_forecast') }} as f
    on f.settlement_date = c.settlement_date
    and f.settlement_period = c.settlement_period
    and f.published_at <= {{ add_minutes('c.cutoff_utc', 180) }}  -- the bug
where true
qualify row_number() over (
    partition by c.settlement_date, c.settlement_period order by f.published_at desc
) = 1
