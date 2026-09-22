-- Point-in-time transmission wind forecast. WINDFOR is hourly: both half-hours
-- of an hour take that hour's value.
with calendar as (
    select settlement_date, settlement_period, hour_utc, cutoff_utc
    from {{ ref('int_settlement_calendar') }}
),

candidates as (
    select
        c.settlement_date,
        c.settlement_period,
        c.cutoff_utc,
        f.wind_mw,
        f.published_at
    from calendar as c
    left join {{ ref('stg_elexon__wind_forecast') }} as f
        on f.valid_hour_utc = c.hour_utc
        and f.published_at <= c.cutoff_utc
    qualify row_number() over (
        partition by c.settlement_date, c.settlement_period
        order by f.published_at desc nulls last
    ) = 1
)

select
    settlement_date,
    settlement_period,
    cutoff_utc,
    wind_mw as windfor_mw,
    published_at as windfor_published_at
from candidates
