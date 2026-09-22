-- The feature mart: one row per delivery half-hour, containing only what was
-- knowable at that delivery day's decision cutoff. Every feature group carries
-- the availability timestamp of its newest input (`*_published_at`,
-- `*_issued_at`, `*_available_at`); the point_in_time test fails the build if
-- any of them is later than `cutoff_utc`.
--
-- The target lives in fct_price_actuals, deliberately not here.
-- Rows appear only once their cutoff has passed.
with calendar as (
    select * from {{ ref('int_settlement_calendar') }}
    where settlement_date >= cast('{{ var("feature_start_date") }}' as date)
      and cutoff_utc <= {{ now_utc() }}
)

select
    -- keys and timing
    c.settlement_date,
    c.settlement_period,
    c.start_time_utc,
    c.cutoff_utc,

    -- calendar
    c.local_hour,
    c.day_of_week,
    c.is_weekend,
    c.is_holiday,
    c.month,
    c.day_of_year,
    c.periods_in_day,

    -- demand forecast (NESO NDF)
    ndf.ndf_demand_mw,
    ndf.ndf_published_at,

    -- transmission wind forecast (NESO WINDFOR)
    wf.windfor_mw,
    wf.windfor_published_at,
    ndf.ndf_demand_mw - wf.windfor_mw as residual_demand_mw,

    -- embedded wind and solar forecast (NESO)
    emb.emb_wind_mw,
    emb.emb_solar_mw,
    emb.emb_solar_mw / nullif(emb.emb_solar_capacity_mw, 0) as emb_solar_load_factor,
    emb.emb_issued_at,

    -- weather forecast (Open-Meteo ICON, as issued)
    wx.wx_wind_speed_100m_ms,
    wx.wx_wind_power_index,
    wx.wx_solar_radiation_wm2,
    wx.wx_cloud_cover_pct,
    wx.wx_temperature_c,
    wx.wx_max_lead_days,
    wx.wx_available_at,

    -- lagged prices
    p.price_d7_same_period,
    p.price_d2_same_period,
    p.price_d1_same_period,
    p.price_last_known,
    p.price_24h_mean,
    p.price_7d_mean,
    p.price_7d_std,
    p.price_7d_min,
    p.price_7d_max,
    p.price_available_at,

    -- recent system state
    s.ci_actual_24h_mean,
    s.gas_share_7d,
    s.wind_share_7d,
    s.nuclear_share_7d,
    s.net_imports_7d_mean_mw,
    s.demand_outturn_7d_mean_mw,
    s.system_available_at

from calendar as c
left join {{ ref('int_demand_forecast_asof') }} as ndf using (settlement_date, settlement_period)
left join {{ ref('int_wind_forecast_asof') }} as wf using (settlement_date, settlement_period)
left join {{ ref('int_embedded_forecast_asof') }} as emb using (settlement_date, settlement_period)
left join {{ ref('int_weather_asof') }} as wx using (settlement_date, settlement_period)
left join {{ ref('int_price_features') }} as p using (settlement_date, settlement_period)
left join {{ ref('int_system_features') }} as s on c.settlement_date = s.settlement_date
