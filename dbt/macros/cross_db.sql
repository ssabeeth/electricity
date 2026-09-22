{#-
  Cross-database helpers. Everything non-portable between DuckDB and BigQuery
  lives here behind adapter.dispatch, so models stay target-agnostic.
  Convention: timestamps are UTC. In DuckDB they are naive TIMESTAMP; in
  BigQuery they are TIMESTAMP (which is UTC by definition).
  Models write `where true` before a bare QUALIFY: BigQuery has required
  QUALIFY to be accompanied by WHERE, GROUP BY or HAVING.
-#}

{# UK local wall-clock (naive) -> UTC instant #}
{% macro uk_local_to_utc(expr) %}{{ return(adapter.dispatch('uk_local_to_utc', 'elecprice')(expr)) }}{% endmacro %}
{% macro default__uk_local_to_utc(expr) -%}
    timezone('UTC', timezone('Europe/London', {{ expr }}))
{%- endmacro %}
{% macro bigquery__uk_local_to_utc(expr) -%}
    timestamp(datetime({{ expr }}), 'Europe/London')
{%- endmacro %}

{# UTC instant -> UK local wall-clock (naive) #}
{% macro utc_to_uk_local(expr) %}{{ return(adapter.dispatch('utc_to_uk_local', 'elecprice')(expr)) }}{% endmacro %}
{% macro default__utc_to_uk_local(expr) -%}
    timezone('Europe/London', timezone('UTC', {{ expr }}))
{%- endmacro %}
{% macro bigquery__utc_to_uk_local(expr) -%}
    datetime({{ expr }}, 'Europe/London')
{%- endmacro %}

{# One row per date in [start_date, end_date], column `d` #}
{% macro date_series(start_date, end_date) %}{{ return(adapter.dispatch('date_series', 'elecprice')(start_date, end_date)) }}{% endmacro %}
{% macro default__date_series(start_date, end_date) -%}
    select cast(g.d as date) as d
    from generate_series(cast({{ start_date }} as date), cast({{ end_date }} as date), interval 1 day) as g(d)
{%- endmacro %}
{% macro bigquery__date_series(start_date, end_date) -%}
    select d from unnest(generate_date_array(cast({{ start_date }} as date), cast({{ end_date }} as date))) as d
{%- endmacro %}

{# One row per integer in [0, n), column `n` #}
{% macro int_series(n) %}{{ return(adapter.dispatch('int_series', 'elecprice')(n)) }}{% endmacro %}
{% macro default__int_series(n) -%}
    select cast(g.n as integer) as n from range({{ n }}) as g(n)
{%- endmacro %}
{% macro bigquery__int_series(n) -%}
    select n from unnest(generate_array(0, {{ n }} - 1)) as n
{%- endmacro %}

{# Add minutes to a timestamp #}
{% macro add_minutes(expr, minutes) %}{{ return(adapter.dispatch('add_minutes', 'elecprice')(expr, minutes)) }}{% endmacro %}
{% macro default__add_minutes(expr, minutes) -%}
    ({{ expr }} + ({{ minutes }}) * interval 1 minute)
{%- endmacro %}
{% macro bigquery__add_minutes(expr, minutes) -%}
    timestamp_add({{ expr }}, interval cast({{ minutes }} as int64) minute)
{%- endmacro %}

{# Add days to a date #}
{% macro add_days(expr, days) %}{{ return(adapter.dispatch('add_days', 'elecprice')(expr, days)) }}{% endmacro %}
{% macro default__add_days(expr, days) -%}
    cast({{ expr }} + ({{ days }}) * interval 1 day as date)
{%- endmacro %}
{% macro bigquery__add_days(expr, days) -%}
    date_add({{ expr }}, interval cast({{ days }} as int64) day)
{%- endmacro %}

{# Truncate a timestamp to the hour #}
{% macro trunc_hour(expr) %}{{ return(adapter.dispatch('trunc_hour', 'elecprice')(expr)) }}{% endmacro %}
{% macro default__trunc_hour(expr) -%}
    date_trunc('hour', {{ expr }})
{%- endmacro %}
{% macro bigquery__trunc_hour(expr) -%}
    timestamp_trunc({{ expr }}, hour)
{%- endmacro %}

{# Minutes between two timestamps (b - a) #}
{% macro minutes_between(a, b) %}{{ return(adapter.dispatch('minutes_between', 'elecprice')(a, b)) }}{% endmacro %}
{% macro default__minutes_between(a, b) -%}
    (epoch({{ b }}) - epoch({{ a }})) / 60.0
{%- endmacro %}
{% macro bigquery__minutes_between(a, b) -%}
    timestamp_diff({{ b }}, {{ a }}, second) / 60.0
{%- endmacro %}

{# Current instant in UTC, same type as stored timestamps #}
{% macro now_utc() %}{{ return(adapter.dispatch('now_utc', 'elecprice')()) }}{% endmacro %}
{% macro default__now_utc() -%}
    timezone('UTC', now())
{%- endmacro %}
{% macro bigquery__now_utc() -%}
    current_timestamp()
{%- endmacro %}

{# Today's date in UTC #}
{% macro today_utc() %}{{ return(adapter.dispatch('today_utc', 'elecprice')()) }}{% endmacro %}
{% macro default__today_utc() -%}
    cast(timezone('UTC', now()) as date)
{%- endmacro %}
{% macro bigquery__today_utc() -%}
    current_date('UTC')
{%- endmacro %}

{# ISO day of week, Monday=1 .. Sunday=7 #}
{% macro iso_dow(expr) %}{{ return(adapter.dispatch('iso_dow', 'elecprice')(expr)) }}{% endmacro %}
{% macro default__iso_dow(expr) -%}
    isodow({{ expr }})
{%- endmacro %}
{% macro bigquery__iso_dow(expr) -%}
    mod(extract(dayofweek from {{ expr }}) + 5, 7) + 1
{%- endmacro %}

{# Standard deviation (sample) #}
{% macro stddev(expr) -%}
    stddev_samp({{ expr }})
{%- endmacro %}

{# Naive local datetime from a date and a 'HH:MM:SS' string #}
{% macro local_datetime(date_expr, time_str) %}{{ return(adapter.dispatch('local_datetime', 'elecprice')(date_expr, time_str)) }}{% endmacro %}
{% macro default__local_datetime(date_expr, time_str) -%}
    cast(cast({{ date_expr }} as varchar) || ' {{ time_str }}' as timestamp)
{%- endmacro %}
{% macro bigquery__local_datetime(date_expr, time_str) -%}
    datetime({{ date_expr }}, time '{{ time_str }}')
{%- endmacro %}

{# NULL-safe greatest over timestamps (BigQuery's GREATEST returns NULL on any NULL) #}
{% macro greatest_ts(exprs) -%}
    nullif(greatest(
        {%- for e in exprs %}
        coalesce({{ e }}, cast('1900-01-01' as timestamp)){% if not loop.last %},{% endif %}
        {%- endfor %}
    ), cast('1900-01-01' as timestamp))
{%- endmacro %}

{# Add days to a naive local datetime #}
{% macro add_days_local(expr, days) %}{{ return(adapter.dispatch('add_days_local', 'elecprice')(expr, days)) }}{% endmacro %}
{% macro default__add_days_local(expr, days) -%}
    ({{ expr }} + ({{ days }}) * interval 1 day)
{%- endmacro %}
{% macro bigquery__add_days_local(expr, days) -%}
    datetime_add({{ expr }}, interval {{ days }} day)
{%- endmacro %}
