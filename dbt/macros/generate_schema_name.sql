{#-
  DuckDB: use the custom schema name as-is (staging, intermediate, marts...).
  Other targets (BigQuery): prefix with the target dataset to avoid collisions,
  e.g. elecprice_marts. This is dbt's default behaviour.
-#}
{% macro generate_schema_name(custom_schema_name, node) -%}
    {%- if custom_schema_name is none -%}
        {{ target.schema }}
    {%- elif target.type == 'duckdb' -%}
        {{ custom_schema_name | trim }}
    {%- else -%}
        {{ target.schema }}_{{ custom_schema_name | trim }}
    {%- endif -%}
{%- endmacro %}
