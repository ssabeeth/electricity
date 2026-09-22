-- Every delivery day has exactly `periods_in_day` periods, numbered 1..n, and
-- only clock-change Sundays deviate from 48 (46 in March, 50 in October).
with per_day as (
    select
        settlement_date,
        max(periods_in_day) as periods_in_day,
        count(*) as n_rows,
        min(settlement_period) as first_period,
        max(settlement_period) as last_period
    from {{ ref('int_settlement_calendar') }}
    group by settlement_date
)

select *
from per_day
where n_rows != periods_in_day
   or first_period != 1
   or last_period != periods_in_day
   or (periods_in_day = 46 and not (extract(month from settlement_date) = 3 and {{ iso_dow('settlement_date') }} = 7))
   or (periods_in_day = 50 and not (extract(month from settlement_date) = 10 and {{ iso_dow('settlement_date') }} = 7))
