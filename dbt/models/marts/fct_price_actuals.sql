-- Realised Market Index Price per settlement period: the forecasting target and
-- the price the battery simulation settles against. Never joined into features.
select
    settlement_date,
    settlement_period,
    start_time_utc,
    {{ utc_to_uk_local('start_time_utc') }} as start_time_local,
    price_gbp_mwh,
    volume_mwh,
    price_gbp_mwh is not null as is_traded
from {{ ref('stg_elexon__market_index') }}
