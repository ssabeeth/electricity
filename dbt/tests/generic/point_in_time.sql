{#-
  Point-in-time guard. Fails if any row uses an input that became available
  after the row's decision cutoff.

    available_at_columns: timestamps of the newest input behind each feature
                          group (publish / issue / availability time)
    cutoff_column:        the decision cutoff for the row's delivery day
    guarded_columns:      optional {available_at_column: [feature columns]} map;
                          a non-null feature with a null availability timestamp
                          also fails, so the guard cannot be bypassed by
                          forgetting to carry the timestamp through.
-#}
{% test point_in_time(model, cutoff_column, available_at_columns, guarded_columns=none) %}

select
    *
from {{ model }}
where
    {%- for col in available_at_columns %}
    {{ col }} > {{ cutoff_column }}
    {%- if not loop.last %} or{% endif %}
    {%- endfor %}
    {%- if guarded_columns %}
    {%- for ts_col, features in guarded_columns.items() %}
    {%- for f in features %}
    or ({{ f }} is not null and {{ ts_col }} is null)
    {%- endfor %}
    {%- endfor %}
    {%- endif %}

{% endtest %}
