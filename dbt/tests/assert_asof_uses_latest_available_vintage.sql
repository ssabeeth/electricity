-- Correctness (not leakage): the as-of join must pick the *newest* vintage
-- that was available at the cutoff, not merely some earlier one.
select a.settlement_date, a.settlement_period, a.ndf_published_at, f.published_at as newer_vintage
from {{ ref('int_demand_forecast_asof') }} as a
inner join {{ ref('stg_elexon__demand_forecast') }} as f
    on f.settlement_date = a.settlement_date
    and f.settlement_period = a.settlement_period
    and f.published_at <= a.cutoff_utc
    and (a.ndf_published_at is null or f.published_at > a.ndf_published_at)
