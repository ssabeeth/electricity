-- One row per settlement period. Zero-volume periods carry a placeholder price
-- of 0 from the source; they are not real trades, so price is set to null.
with source as (
    select * from {{ source('lake', 'elexon_mid') }}
    where data_provider = 'APXMIDP'
),

deduped as (
    select *
    from source
    qualify row_number() over (
        partition by settlement_date, settlement_period
        order by _ingested_at desc
    ) = 1
)

select
    cast(settlement_date as date) as settlement_date,
    cast(settlement_period as integer) as settlement_period,
    cast(start_time as timestamp) as start_time_utc,
    case when volume > 0 then price end as price_gbp_mwh,
    volume as volume_mwh,
    price as reported_price_gbp_mwh,
    _ingested_at as ingested_at
from deduped
