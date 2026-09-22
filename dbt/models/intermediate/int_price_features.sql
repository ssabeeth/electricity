-- Lagged price features known at the decision cutoff.
--
-- MID has no publish timestamp, so a period's price is treated as known
-- `mid_availability_lag_minutes` after the period ends. Lags are matched on UK
-- local clock time, so "same half-hour last week" is right across clock changes.
{% set lag = var('mid_availability_lag_minutes') %}

with calendar as (
    select settlement_date, settlement_period, start_time_local, cutoff_utc
    from {{ ref('int_settlement_calendar') }}
),

mid as (
    select
        settlement_date,
        settlement_period,
        start_time_utc,
        {{ utc_to_uk_local('start_time_utc') }} as start_time_local,
        price_gbp_mwh,
        {{ add_minutes('start_time_utc', 30 + lag) }} as available_at
    from {{ ref('stg_elexon__market_index') }}
),

{% for days_back in [1, 2, 7] %}
lag_d{{ days_back }} as (
    select
        c.settlement_date,
        c.settlement_period,
        m.price_gbp_mwh,
        m.available_at
    from calendar as c
    inner join mid as m
        on m.start_time_local = {{ add_days_local('c.start_time_local', -days_back) }}
        and m.available_at <= c.cutoff_utc
    -- the repeated hour on the autumn clock-change day matches twice; keep the later
    qualify row_number() over (
        partition by c.settlement_date, c.settlement_period order by m.start_time_utc desc
    ) = 1
),
{% endfor %}

days as (
    select distinct settlement_date, cutoff_utc from calendar
),

window_stats as (
    select
        d.settlement_date,
        avg(case when m.available_at > {{ add_minutes('d.cutoff_utc', -24 * 60) }} then m.price_gbp_mwh end)
            as price_24h_mean,
        avg(m.price_gbp_mwh) as price_7d_mean,
        {{ stddev('m.price_gbp_mwh') }} as price_7d_std,
        min(m.price_gbp_mwh) as price_7d_min,
        max(m.price_gbp_mwh) as price_7d_max,
        max(m.available_at) as window_available_at
    from days as d
    inner join mid as m
        on m.available_at <= d.cutoff_utc
        and m.available_at > {{ add_minutes('d.cutoff_utc', -7 * 24 * 60) }}
    group by d.settlement_date
),

last_known as (
    select
        d.settlement_date,
        m.price_gbp_mwh as price_last_known,
        m.available_at as last_known_available_at
    from days as d
    inner join mid as m
        on m.available_at <= d.cutoff_utc
        and m.available_at > {{ add_minutes('d.cutoff_utc', -24 * 60) }}
        and m.price_gbp_mwh is not null
    qualify row_number() over (partition by d.settlement_date order by m.available_at desc) = 1
)

select
    c.settlement_date,
    c.settlement_period,
    c.cutoff_utc,
    l7.price_gbp_mwh as price_d7_same_period,
    l2.price_gbp_mwh as price_d2_same_period,
    -- Only early-morning periods of D-1 are known at a 09:00 cutoff; null otherwise.
    l1.price_gbp_mwh as price_d1_same_period,
    lk.price_last_known,
    w.price_24h_mean,
    w.price_7d_mean,
    w.price_7d_std,
    w.price_7d_min,
    w.price_7d_max,
    {{ greatest_ts(['l1.available_at', 'l2.available_at', 'l7.available_at',
                    'lk.last_known_available_at', 'w.window_available_at']) }} as price_available_at
from calendar as c
left join lag_d1 as l1 using (settlement_date, settlement_period)
left join lag_d2 as l2 using (settlement_date, settlement_period)
left join lag_d7 as l7 using (settlement_date, settlement_period)
left join window_stats as w on c.settlement_date = w.settlement_date
left join last_known as lk on c.settlement_date = lk.settlement_date
