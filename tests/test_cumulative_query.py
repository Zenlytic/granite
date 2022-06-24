import pytest

from metrics_layer.core.model.definitions import Definitions


@pytest.mark.query
def test_cumulative_query_metric_only_one(connection):
    query = connection.get_sql_query(metrics=["total_lifetime_revenue"])

    correct = (
        "WITH date_spine AS ("
        "select dateadd(day, seq4(), '2000-01-01') as date from table(generator(rowcount => 365*40))) ,"
        "subquery_orders_total_lifetime_revenue AS ("
        "SELECT orders.revenue as orders_total_revenue FROM analytics.orders orders) ,"
        "aggregated_orders_total_lifetime_revenue AS ("
        "SELECT SUM(orders_total_revenue) as orders_total_revenue FROM date_spine "
        "JOIN subquery_orders_total_lifetime_revenue ON subquery_orders_total_lifetime_revenue"
        ".orders_order_date<=date_spine.date WHERE date_spine.date<=current_date() "
        "GROUP BY date_spine.date) "
        "SELECT aggregated_orders_total_lifetime_revenue.orders_total_revenue "
        "as orders_total_lifetime_revenue FROM aggregated_orders_total_lifetime_revenue;"
    )
    assert query == correct


@pytest.mark.query
def test_cumulative_query_metric_with_number(connection):
    query = connection.get_sql_query(metrics=["average_order_value_custom", "cumulative_aov"])

    date_spine = "select dateadd(day, seq4(), '2000-01-01') as date from table(generator(rowcount => 365*40))"
    correct = (
        f"WITH date_spine AS ({date_spine}) ,subquery_orders_cumulative_aov "
        "AS (SELECT orders.id as orders_number_of_orders,orders.revenue as "
        "orders_total_revenue FROM analytics.orders orders) "
        ",aggregated_orders_cumulative_aov AS (SELECT (SUM(orders_total_revenue)) "
        "/ (COUNT(orders_number_of_orders)) as orders_average_order_value_custom FROM "
        "date_spine JOIN subquery_orders_cumulative_aov ON subquery_orders_cumulative_aov."
        "orders_order_date<=date_spine.date WHERE date_spine.date<=current_date() GROUP BY date_spine.date)"
        " ,base AS (SELECT (SUM(orders.revenue)) "
        "/ (COUNT(orders.id)) as orders_average_order_value_custom FROM analytics.orders "
        "orders ORDER BY orders_average_order_value_custom DESC) SELECT "
        "base.orders_average_order_value_custom as orders_average_order_value_custom,"
        "aggregated_orders_cumulative_aov.orders_average_order_value_custom as "
        "orders_cumulative_aov FROM base LEFT JOIN aggregated_orders_cumulative_aov ON 1=1;"
    )
    assert query == correct


@pytest.mark.query
@pytest.mark.parametrize("query_type", [Definitions.snowflake, Definitions.bigquery])
def test_cumulative_query_metric_only_two(connection, query_type):
    query = connection.get_sql_query(metrics=["ltv", "total_lifetime_revenue"], query_type=query_type)

    if query_type == Definitions.bigquery:
        date_spine = "select date from unnest(generate_date_array('2000-01-01', '2040-01-01')) as date"
    else:
        date_spine = (
            "select dateadd(day, seq4(), '2000-01-01') as date from table(generator(rowcount => 365*40))"
        )
    correct = (
        f"WITH date_spine AS ({date_spine}) ,"
        "subquery_orders_total_lifetime_revenue AS ("
        "SELECT orders.revenue as orders_total_revenue FROM analytics.orders orders) ,"
        "aggregated_orders_total_lifetime_revenue AS ("
        "SELECT SUM(orders_total_revenue) as orders_total_revenue FROM date_spine "
        "JOIN subquery_orders_total_lifetime_revenue ON subquery_orders_total_lifetime_revenue"
        ".orders_order_date<=date_spine.date WHERE date_spine.date<=current_date() GROUP BY date_spine.date) ,"  # noqa
        "subquery_orders_cumulative_customers AS ("
        "SELECT customers.customer_id as customers_number_of_customers FROM analytics.customers customers) ,"
        "aggregated_orders_cumulative_customers AS ("
        "SELECT COUNT(customers_number_of_customers) as customers_number_of_customers FROM date_spine "
        "JOIN subquery_orders_cumulative_customers ON subquery_orders_cumulative_customers"
        ".customers_first_order_date<=date_spine.date WHERE date_spine.date<=current_date() "
        "GROUP BY date_spine.date) "
        "SELECT (aggregated_orders_total_lifetime_revenue.orders_total_revenue) "
        "/ nullif((aggregated_orders_cumulative_customers.customers_number_of_customers), 0) "
        "as orders_ltv,aggregated_orders_total_lifetime_revenue.orders_total_revenue "
        "as orders_total_lifetime_revenue FROM aggregated_orders_total_lifetime_revenue "
        "LEFT JOIN aggregated_orders_cumulative_customers ON 1=1;"
    )
    assert query == correct


@pytest.mark.query
def test_cumulative_query_metric_dimension_no_time(connection):
    query = connection.get_sql_query(
        metrics=["total_lifetime_revenue"],
        dimensions=["new_vs_repeat"],
        where={"field": "region", "expression": "equal_to", "value": "West"},
        having={"field": "total_item_revenue", "expression": "greater_than", "value": 2000},
    )

    correct = (
        "WITH date_spine AS ("
        "select dateadd(day, seq4(), '2000-01-01') as date from table(generator(rowcount => 365*40))) ,"
        "subquery_orders_total_lifetime_revenue AS ("
        "SELECT orders.new_vs_repeat as orders_new_vs_repeat,orders.revenue as orders_total_revenue "
        "FROM analytics.orders orders LEFT JOIN analytics.customers customers "
        "ON orders.customer_id=customers.customer_id WHERE customers.region='West') ,"
        "aggregated_orders_total_lifetime_revenue AS ("
        "SELECT SUM(orders_total_revenue) as orders_total_revenue,orders_new_vs_repeat "
        "as orders_new_vs_repeat FROM date_spine "
        "JOIN subquery_orders_total_lifetime_revenue ON subquery_orders_total_lifetime_revenue"
        ".orders_order_date<=date_spine.date WHERE date_spine.date<=current_date() GROUP BY date_spine.date) "  # noqa
        ",base AS (SELECT orders.new_vs_repeat as orders_new_vs_repeat,SUM(order_lines.revenue) "
        "as order_lines_total_item_revenue FROM analytics.order_line_items order_lines "
        "LEFT JOIN analytics.orders orders ON order_lines.order_unique_id=orders.id "
        "LEFT JOIN analytics.customers customers ON order_lines.customer_id=customers.customer_id "
        "WHERE customers.region='West' GROUP BY orders.new_vs_repeat "
        "ORDER BY order_lines_total_item_revenue DESC) "
        "SELECT base.orders_new_vs_repeat as orders_new_vs_repeat,"
        "aggregated_orders_total_lifetime_revenue.orders_total_revenue "
        "as orders_total_lifetime_revenue FROM base LEFT JOIN "
        "aggregated_orders_total_lifetime_revenue ON base.orders_new_vs_repeat"
        "=aggregated_orders_total_lifetime_revenue.orders_new_vs_repeat "
        "WHERE order_lines_total_item_revenue>2000;"
    )
    assert query == correct


@pytest.mark.query
def test_cumulative_query_metrics_and_time(connection):
    query = connection.get_sql_query(
        metrics=["total_lifetime_revenue", "total_item_revenue"],
        dimensions=["orders.order_date"],
    )

    correct = (
        "WITH date_spine AS ("
        "select dateadd(day, seq4(), '2000-01-01') as date from table(generator(rowcount => 365*40))) ,"
        "subquery_orders_total_lifetime_revenue AS ("
        "SELECT DATE_TRUNC('DAY', orders.order_date) as orders_order_date,orders.revenue "
        "as orders_total_revenue FROM analytics.orders orders) ,"
        "aggregated_orders_total_lifetime_revenue AS ("
        "SELECT SUM(orders_total_revenue) as orders_total_revenue,date_spine.date as orders_order_date "
        "FROM date_spine JOIN subquery_orders_total_lifetime_revenue "
        "ON subquery_orders_total_lifetime_revenue.orders_order_date<=date_spine.date "
        "WHERE date_spine.date<=current_date() GROUP BY date_spine.date) ,"
        "base AS (SELECT DATE_TRUNC('DAY', orders.order_date) as orders_order_date,"
        "SUM(order_lines.revenue) as order_lines_total_item_revenue FROM analytics.order_line_items "
        "order_lines LEFT JOIN analytics.orders orders ON order_lines.order_unique_id=orders.id "
        "GROUP BY DATE_TRUNC('DAY', orders.order_date) ORDER BY order_lines_total_item_revenue DESC) "
        "SELECT base.orders_order_date as orders_order_date,"
        "aggregated_orders_total_lifetime_revenue.orders_total_revenue "
        "as orders_total_lifetime_revenue,base.order_lines_total_item_revenue "
        "as order_lines_total_item_revenue FROM base "
        "LEFT JOIN aggregated_orders_total_lifetime_revenue "
        "ON base.orders_order_date=aggregated_orders_total_lifetime_revenue.orders_order_date;"
    )
    assert query == correct


@pytest.mark.query
def test_cumulative_query_metric_dimension_and_time(connection):
    query = connection.get_sql_query(
        metrics=["total_lifetime_revenue"], dimensions=["new_vs_repeat", "orders.order_date"]
    )

    correct = (
        "WITH date_spine AS ("
        "select dateadd(day, seq4(), '2000-01-01') as date from table(generator(rowcount => 365*40))) ,"
        "subquery_orders_total_lifetime_revenue AS ("
        "SELECT orders.new_vs_repeat as orders_new_vs_repeat,DATE_TRUNC('DAY', orders.order_date) "
        "as orders_order_date,orders.revenue as orders_total_revenue FROM analytics.orders orders) ,"
        "aggregated_orders_total_lifetime_revenue AS ("
        "SELECT SUM(orders_total_revenue) as orders_total_revenue,orders_new_vs_repeat "
        "as orders_new_vs_repeat,date_spine.date as orders_order_date FROM date_spine "
        "JOIN subquery_orders_total_lifetime_revenue ON subquery_orders_total_lifetime_revenue"
        ".orders_order_date<=date_spine.date WHERE date_spine.date<=current_date() GROUP BY date_spine.date) "  # noqa
        "SELECT aggregated_orders_total_lifetime_revenue.orders_new_vs_repeat "
        "as orders_new_vs_repeat,aggregated_orders_total_lifetime_revenue.orders_order_date "
        "as orders_order_date,aggregated_orders_total_lifetime_revenue.orders_total_revenue "
        "as orders_total_lifetime_revenue FROM aggregated_orders_total_lifetime_revenue;"
    )
    assert query == correct


@pytest.mark.query
def test_cumulative_query_metrics_dimensions_and_time(connection):
    query = connection.get_sql_query(
        metrics=["total_lifetime_revenue", "cumulative_customers", "total_item_revenue", "ltv"],
        dimensions=["new_vs_repeat", "orders.order_date"],
    )

    correct = (
        "WITH date_spine AS (select dateadd(day, seq4(), '2000-01-01') as date "
        "from table(generator(rowcount => 365*40))) ,subquery_orders_total_lifetime_revenue "
        "AS (SELECT orders.new_vs_repeat as orders_new_vs_repeat,DATE_TRUNC('DAY', orders.order_date) "
        "as orders_order_date,orders.revenue as orders_total_revenue FROM analytics.orders orders) "
        ",aggregated_orders_total_lifetime_revenue AS (SELECT SUM(orders_total_revenue) as "
        "orders_total_revenue,orders_new_vs_repeat as orders_new_vs_repeat,date_spine.date as "
        "orders_order_date FROM date_spine JOIN subquery_orders_total_lifetime_revenue "
        "ON subquery_orders_total_lifetime_revenue.orders_order_date<=date_spine.date"
        " WHERE date_spine.date<=current_date() GROUP BY date_spine.date) "
        ",subquery_orders_cumulative_customers AS (SELECT orders.new_vs_repeat "
        "as orders_new_vs_repeat,DATE_TRUNC('DAY', orders.order_date) as orders_order_date,"
        "customers.customer_id as customers_number_of_customers FROM analytics.orders orders "
        "LEFT JOIN analytics.customers customers ON orders.customer_id=customers.customer_id) ,"
        "aggregated_orders_cumulative_customers AS (SELECT COUNT(customers_number_of_customers) "
        "as customers_number_of_customers,orders_new_vs_repeat as orders_new_vs_repeat,date_spine.date "
        "as orders_order_date FROM date_spine JOIN subquery_orders_cumulative_customers "
        "ON subquery_orders_cumulative_customers.customers_first_order_date<=date_spine.date"
        " WHERE date_spine.date<=current_date() GROUP BY date_spine.date) "
        ",base AS (SELECT orders.new_vs_repeat as orders_new_vs_repeat,DATE_TRUNC('DAY', orders.order_date) "
        "as orders_order_date,SUM(order_lines.revenue) as order_lines_total_item_revenue "
        "FROM analytics.order_line_items order_lines LEFT JOIN analytics.orders orders "
        "ON order_lines.order_unique_id=orders.id GROUP BY orders.new_vs_repeat,"
        "DATE_TRUNC('DAY', orders.order_date) ORDER BY order_lines_total_item_revenue DESC) "
        "SELECT base.orders_new_vs_repeat as orders_new_vs_repeat,base.orders_order_date as "
        "orders_order_date,aggregated_orders_total_lifetime_revenue.orders_total_revenue "
        "as orders_total_lifetime_revenue,aggregated_orders_cumulative_customers"
        ".customers_number_of_customers as orders_cumulative_customers,"
        "base.order_lines_total_item_revenue as order_lines_total_item_revenue,"
        "(aggregated_orders_total_lifetime_revenue.orders_total_revenue) "
        "/ nullif((aggregated_orders_cumulative_customers.customers_number_of_customers), 0) "
        "as orders_ltv FROM base LEFT JOIN "
        "aggregated_orders_total_lifetime_revenue ON base.orders_new_vs_repeat"
        "=aggregated_orders_total_lifetime_revenue.orders_new_vs_repeat and "
        "base.orders_order_date=aggregated_orders_total_lifetime_revenue.orders_order_date "
        "LEFT JOIN aggregated_orders_cumulative_customers ON base.orders_new_vs_repeat"
        "=aggregated_orders_cumulative_customers.orders_new_vs_repeat and base.orders_order_date"
        "=aggregated_orders_cumulative_customers.orders_order_date;"
    )
    assert query == correct
