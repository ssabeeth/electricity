-- Point-in-time weather features. Per location and valid hour, take the latest
-- forecast vintage available at the cutoff, then aggregate across locations
-- with the weights in the weather_locations seed:
--   wind   : 100 m wind speed and a normalised turbine power-curve index
--   solar  : shortwave radiation and cloud cover
--   demand : population-weighted 2 m temperature
with hours as (
    -- Every UTC hour belongs to exactly one delivery day, hence one cutoff.
    select distinct hour_utc, cutoff_utc
    from {{ ref('int_settlement_calendar') }}
),

per_location as (
    select
        h.hour_utc,
        h.cutoff_utc,
        w.location_id,
        w.lead_days,
        w.available_at,
        w.temperature_c,
        w.wind_speed_100m_ms,
        w.shortwave_radiation_wm2,
        w.cloud_cover_pct,
        -- Generic power curve: cut-in 3 m/s, rated 12 m/s, cut-out 25 m/s.
        case
            when w.wind_speed_100m_ms is null then null
            when w.wind_speed_100m_ms < 3 or w.wind_speed_100m_ms >= 25 then 0.0
            when w.wind_speed_100m_ms >= 12 then 1.0
            else power((w.wind_speed_100m_ms - 3) / 9.0, 3)
        end as wind_power_index
    from hours as h
    inner join {{ ref('stg_openmeteo__weather_forecast') }} as w
        on w.valid_hour_utc = h.hour_utc
        and w.available_at <= h.cutoff_utc
    qualify row_number() over (
        partition by h.hour_utc, w.location_id
        order by w.available_at desc
    ) = 1
),

weighted as (
    select
        p.hour_utc,
        p.cutoff_utc,
        sum(p.wind_speed_100m_ms * l.wind_weight)
            / nullif(sum(case when p.wind_speed_100m_ms is not null then l.wind_weight end), 0)
            as wx_wind_speed_100m_ms,
        sum(p.wind_power_index * l.wind_weight)
            / nullif(sum(case when p.wind_power_index is not null then l.wind_weight end), 0)
            as wx_wind_power_index,
        sum(p.shortwave_radiation_wm2 * l.solar_weight)
            / nullif(sum(case when p.shortwave_radiation_wm2 is not null then l.solar_weight end), 0)
            as wx_solar_radiation_wm2,
        sum(p.cloud_cover_pct * l.solar_weight)
            / nullif(sum(case when p.cloud_cover_pct is not null then l.solar_weight end), 0)
            as wx_cloud_cover_pct,
        sum(p.temperature_c * l.temperature_weight)
            / nullif(sum(case when p.temperature_c is not null then l.temperature_weight end), 0)
            as wx_temperature_c,
        max(p.available_at) as wx_available_at,
        max(p.lead_days) as wx_max_lead_days,
        count(*) as wx_locations
    from per_location as p
    inner join {{ ref('weather_locations') }} as l on p.location_id = l.location_id
    group by p.hour_utc, p.cutoff_utc
)

select
    c.settlement_date,
    c.settlement_period,
    c.cutoff_utc,
    w.wx_wind_speed_100m_ms,
    w.wx_wind_power_index,
    w.wx_solar_radiation_wm2,
    w.wx_cloud_cover_pct,
    w.wx_temperature_c,
    w.wx_available_at,
    w.wx_max_lead_days,
    coalesce(w.wx_locations, 0) as wx_locations
from {{ ref('int_settlement_calendar') }} as c
left join weighted as w on c.hour_utc = w.hour_utc
