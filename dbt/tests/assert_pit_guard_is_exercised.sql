{{ config(severity='warn') }}
-- The point-in-time tests are only meaningful if post-cutoff vintages exist in
-- the raw data (ingestion deliberately keeps some). Warn if there are none.
with post_cutoff as (
    select count(*) as n
    from {{ ref('int_settlement_calendar') }} as c
    inner join {{ ref('stg_elexon__demand_forecast') }} as f
        on f.settlement_date = c.settlement_date
        and f.settlement_period = c.settlement_period
        and f.published_at > c.cutoff_utc
)

select n from post_cutoff where n = 0
