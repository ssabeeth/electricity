-- Point-in-time embedded wind and solar forecast (NESO).
with calendar as (
    select settlement_date, settlement_period, cutoff_utc
    from {{ ref('int_settlement_calendar') }}
),

candidates as (
    select
        c.settlement_date,
        c.settlement_period,
        c.cutoff_utc,
        f.embedded_wind_mw,
        f.embedded_solar_mw,
        f.embedded_wind_capacity_mw,
        f.embedded_solar_capacity_mw,
        f.issued_at
    from calendar as c
    left join {{ ref('stg_neso__embedded_forecast') }} as f
        on f.settlement_date = c.settlement_date
        and f.settlement_period = c.settlement_period
        and f.issued_at <= c.cutoff_utc
    where true
    qualify row_number() over (
        partition by c.settlement_date, c.settlement_period
        order by f.issued_at desc nulls last
    ) = 1
)

select
    settlement_date,
    settlement_period,
    cutoff_utc,
    embedded_wind_mw as emb_wind_mw,
    embedded_solar_mw as emb_solar_mw,
    embedded_wind_capacity_mw as emb_wind_capacity_mw,
    embedded_solar_capacity_mw as emb_solar_capacity_mw,
    issued_at as emb_issued_at
from candidates
