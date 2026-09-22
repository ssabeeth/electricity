-- One row per settlement period of every delivery day from the feature start
-- date to today + look-ahead. Handles 46/50-period clock-change days and gives
-- each delivery day its decision cutoff: cutoff_local_time on D-1, UK time.
{% set end_date = add_days(today_utc(), var('calendar_lookahead_days')) %}
{% if var('feature_end_date') %}{% set end_date = "'" ~ var('feature_end_date') ~ "'" %}{% endif %}

with days as (
    {{ date_series("'" ~ var('feature_start_date') ~ "'", end_date) }}
),

day_bounds as (
    select
        d as settlement_date,
        {{ uk_local_to_utc(local_datetime('d', '00:00:00')) }} as day_start_utc,
        {{ uk_local_to_utc(local_datetime(add_days('d', 1), '00:00:00')) }} as day_end_utc,
        {{ uk_local_to_utc(local_datetime(add_days('d', -1), var('cutoff_local_time'))) }} as cutoff_utc
    from days
),

periods as (
    select
        b.settlement_date,
        s.n + 1 as settlement_period,
        {{ add_minutes('b.day_start_utc', 's.n * 30') }} as start_time_utc,
        b.day_start_utc,
        b.day_end_utc,
        b.cutoff_utc
    from day_bounds as b
    cross join ({{ int_series(50) }}) as s
),

valid as (
    select *
    from periods
    where start_time_utc < day_end_utc
),

holidays as (
    select cast(holiday_date as date) as holiday_date from {{ ref('uk_bank_holidays') }}
)

select
    v.settlement_date,
    v.settlement_period,
    v.start_time_utc,
    {{ add_minutes('v.start_time_utc', 30) }} as end_time_utc,
    {{ trunc_hour('v.start_time_utc') }} as hour_utc,
    v.cutoff_utc,
    cast({{ minutes_between('v.day_start_utc', 'v.day_end_utc') }} / 30 as integer) as periods_in_day,
    {{ utc_to_uk_local('v.start_time_utc') }} as start_time_local,
    extract(hour from {{ utc_to_uk_local('v.start_time_utc') }})
        + extract(minute from {{ utc_to_uk_local('v.start_time_utc') }}) / 60.0 as local_hour,
    {{ iso_dow('v.settlement_date') }} as day_of_week,
    {{ iso_dow('v.settlement_date') }} >= 6 as is_weekend,
    h.holiday_date is not null as is_holiday,
    extract(month from v.settlement_date) as month,
    extract(dayofyear from v.settlement_date) as day_of_year
from valid as v
left join holidays as h on v.settlement_date = h.holiday_date
