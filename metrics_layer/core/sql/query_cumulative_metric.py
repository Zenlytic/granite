from copy import deepcopy

from pypika import JoinType, Criterion, Table

from metrics_layer.core.sql.query_base import MetricsLayerQueryBase
from metrics_layer.core.model.definitions import Definitions
from metrics_layer.core.exceptions import AccessDeniedOrDoesNotExistException
from metrics_layer.core.model.filter import LiteralValueCriterion
from metrics_layer.core.sql.query_design import MetricsLayerDesign
from metrics_layer.core.sql.query_dialect import query_lookup
from metrics_layer.core.sql.query_generator import MetricsLayerQuery
from metrics_layer.core.sql.query_filter import MetricsLayerFilter

SNOWFLAKE_DATE_SPINE = (
    "select dateadd(day, seq4(), '2000-01-01') as date from table(generator(rowcount => 365*40))"
)
BIGQUERY_DATE_SPINE = "select date from unnest(generate_date_array('2000-01-01', '2040-01-01')) as date"


class CumulativeMetricsQuery(MetricsLayerQueryBase):
    """ """

    def __init__(self, definition: dict, design: MetricsLayerDesign, suppress_warnings: bool = False) -> None:
        self.design = design
        self.query_type = self.design.query_type
        self.no_group_by = self.design.no_group_by
        self.query_lookup = query_lookup
        self.suppress_warnings = suppress_warnings

        self.date_spine_cte_name = design.date_spine_cte_name
        self.base_cte_name = design.base_cte_name

        self._default_date_memo = {}
        super().__init__(definition)

    def get_query(self, semicolon: bool = True):
        self.cumulative_metrics, self.non_cumulative_metrics = self.separate_metrics()
        has_non_cumulative_metrics = len(self.non_cumulative_metrics) > 0

        base_cte_query = self._base_query()

        date_spine_query = self.date_spine(self.query_type)

        base_cte_query = base_cte_query.with_(Table(date_spine_query), self.date_spine_cte_name)

        for cumulative_metric in self.cumulative_metrics:
            subquery, subquery_alias = self.cumulative_subquery(cumulative_metric)
            base_cte_query = base_cte_query.with_(Table(subquery), subquery_alias)
            aggregate_subquery, aggregate_alias = self.aggregate_cumulative_subquery(cumulative_metric)
            base_cte_query = base_cte_query.with_(Table(aggregate_subquery), aggregate_alias)

        if has_non_cumulative_metrics:
            query, base_cte_name = self.non_cumulative_subquery()
            base_cte_query = base_cte_query.with_(Table(query), base_cte_name)

        if has_non_cumulative_metrics:
            base = Table(self.base_cte_name)
            cumulative_idx = 0
        else:
            base = Table(self.cumulative_metrics[0].cte_prefix())
            cumulative_idx = 1

        base_cte_query = base_cte_query.from_(base)

        for cumulative_metric in self.cumulative_metrics[cumulative_idx:]:
            criteria, cte_alias = self._derive_join(cumulative_metric, base)
            base_cte_query = base_cte_query.join(Table(cte_alias), JoinType.left).on(criteria)

        select = self._get_select_columns(base)
        base_cte_query = base_cte_query.select(*select)

        if self.having:
            where = self.get_where_from_having(project=self.design)
            base_cte_query = base_cte_query.where(Criterion.all(where))

        base_cte_query = base_cte_query.limit(self.limit)
        sql = str(base_cte_query)
        if semicolon:
            sql += ";"
        return sql

    def separate_metrics(self):
        cumulative_metrics, non_cumulative_metrics = [], []
        for field_name in self.metrics + self.having:
            if isinstance(field_name, str):
                field = self.design.get_field(field_name)
            else:
                field = self.design.get_field(field_name["field"])

            if field.is_cumulative():
                if field.type == "number":
                    for reference in field.referenced_fields(field.sql):
                        if reference.is_cumulative():
                            cumulative_metrics.append(reference)
                        else:
                            non_cumulative_metrics.append(reference)
                else:
                    cumulative_metrics.append(field)
            else:
                non_cumulative_metrics.append(field)

        non_cumulative_metrics = self.design.deduplicate_fields(non_cumulative_metrics)
        cumulative_metrics = self.design.deduplicate_fields(cumulative_metrics)
        return cumulative_metrics, non_cumulative_metrics

    @staticmethod
    def date_spine(query_type: str):
        if query_type in {Definitions.snowflake, Definitions.redshift}:
            return SNOWFLAKE_DATE_SPINE
        elif query_type == Definitions.bigquery:
            return BIGQUERY_DATE_SPINE
        raise NotImplementedError(f"Database {query_type} not implemented yet")

    def cumulative_subquery(self, cumulative_metric):
        cumulative_metric_cte_alias = cumulative_metric.cte_prefix(aggregated=False)
        reference_metric = cumulative_metric.measure
        dimensions = [self.design.get_field(d) for d in self.dimensions]
        date_field = self._get_default_date(reference_metric, cumulative_metric)
        if not any(date_field.name == d.name and date_field.view.name == d.view.name for d in dimensions):
            dimensions.append(date_field)
        dimension_ids = [d.id() for d in dimensions]

        where = []
        for w in self.where:
            where_field = self.design.get_field(w["field"])
            if not self._is_default_date(where_field):
                where.append(w)

        query = self._subquery(
            metrics=[reference_metric], dimensions=dimension_ids, where=where, no_group_by=True
        )
        return query, cumulative_metric_cte_alias

    def non_cumulative_subquery(self):
        print(self.dimensions)
        print(self.where)
        query = self._subquery(
            metrics=self.non_cumulative_metrics,
            dimensions=self.dimensions,
            where=self.where,
            no_group_by=False,
        )
        return query, self.base_cte_name

    def _subquery(self, metrics: list, dimensions: list, where: list, no_group_by: bool):
        sub_definition = deepcopy(self._definition)
        sub_definition["metrics"] = [metric.id() for metric in metrics]
        sub_definition["dimensions"] = dimensions
        sub_definition["where"] = where
        sub_definition["having"] = []
        sub_definition["limit"] = None

        field_lookup = {k: v for k, v in self.design.field_lookup.items()}
        self.design.field_lookup = {k: v for k, v in field_lookup.items() if v.field_type != "measure"}
        self.design.no_group_by = no_group_by
        for metric in metrics:
            self.design.field_lookup[metric.id()] = metric
        print(sub_definition)
        query_generator = MetricsLayerQuery(
            sub_definition, design=self.design, suppress_warnings=self.suppress_warnings
        )
        query = query_generator.get_query(semicolon=False)
        self.design.no_group_by = False
        self.design.field_lookup = field_lookup

        return query

    def aggregate_cumulative_subquery(self, cumulative_metric):
        cte_alias = cumulative_metric.cte_prefix(aggregated=False)

        referenced_metric = cumulative_metric.measure
        date_field = self._get_default_date(referenced_metric, cumulative_metric)

        from_query = self._base_query()
        from_query = from_query.from_(Table(self.date_spine_cte_name))

        date_spine_reference = f"{self.date_spine_cte_name}.date"
        less_than_now = f"{cte_alias}.{date_field.alias(with_view=True)}<={date_spine_reference}"

        # TODO For a 7 day window
        # window = f"{cte_alias}.{date_field_name}>DATEADD(day, -7, {self.date_spine_cte_name}.date)"
        criteria = LiteralValueCriterion(less_than_now)
        from_query = from_query.join(Table(cte_alias), JoinType.inner).on(criteria)

        select = []
        for field_name in [referenced_metric.id()] + self.dimensions:
            field = self.design.get_field(field_name)
            # Date field is the default date but not the default date on the referenced metric
            # I need to check if the date field is the default date on any other metric in the query,
            # and if so use the date spine instead of the date field itself
            if self._is_default_date(field):
                field_sql = date_spine_reference
            else:
                field_sql = field.sql_query(query_type=self.query_type, alias_only=True)
            select.append(self.sql(field_sql, alias=field.alias(with_view=True)))

        from_query = from_query.select(*select)

        from_query = from_query.where(LiteralValueCriterion(f"{date_spine_reference}<={self.current_date}"))

        having = []
        for w in self.where:
            where_field = self.design.get_field(w["field"])
            if self._is_default_date(where_field):
                date_having = deepcopy(w)
                dimension_group = deepcopy(date_field.dimension_group)
                date_field.dimension_group = where_field.dimension_group
                date_having["field"] = date_field.id()
                date_having["query_type"] = self.query_type
                f = MetricsLayerFilter(definition=date_having, design=None, filter_type="having")
                date_spine_sql = date_field.apply_dimension_group_time_sql(
                    date_spine_reference, self.query_type
                )
                date_field.dimension_group = dimension_group
                having.append(f.criterion(date_spine_sql))

        from_query = from_query.groupby(self.sql(date_spine_reference))

        if having:
            from_query = from_query.having(LiteralValueCriterion(Criterion.all(having)))

        return from_query, cumulative_metric.cte_prefix()

    def _is_default_date(self, field):
        date_aliases = []
        for cumulative_metric in self.cumulative_metrics:
            date_field = self._get_default_date(cumulative_metric.measure, cumulative_metric)
            date_aliases.append(f"{date_field.view.name}.{date_field.name}")
        return f"{field.view.name}.{field.name}" in date_aliases

    def _get_default_date(self, field, cumulative_metric):
        date_name = field.view.default_date
        date_field_name = f"{date_name}_date"
        date_key = f"{field.view.name}.{date_field_name}"
        if date_key in self._default_date_memo:
            return self._default_date_memo[date_key]
        try:
            self._default_date_memo[date_key] = self.design.get_field(date_key)
        except Exception:
            raise AccessDeniedOrDoesNotExistException(
                f"Could not find date needed to aggregate cumulative metric {cumulative_metric.name}, "
                f"looking for date field named {date_field_name} in view {field.view.name}",
                object_type="field",
                object_name=date_field_name,
            )
        return self._default_date_memo[date_key]

    def _derive_join(self, cumulative_metric, base_table: Table):
        cte_alias = cumulative_metric.cte_prefix()

        conditions = []
        for field_name in self.dimensions:
            field = self.design.get_field(field_name)
            field_alias = field.alias(with_view=True)
            conditions.append(f"{base_table.get_table_name()}.{field_alias}={cte_alias}.{field_alias}")

        if len(conditions) > 0:
            raw_criteria = " and ".join(conditions)
        else:
            raw_criteria = "1=1"

        criteria = LiteralValueCriterion(raw_criteria)
        return criteria, cte_alias

    def _get_select_columns(self, base_table: Table):
        select = []
        for field_name in self.dimensions + self.metrics:
            field = self.design.get_field(field_name)
            if field.type == "number" and field.is_cumulative():

                field_sql = field.sql_query(query_type=self.query_type, alias_only=True)
            elif field.is_cumulative():
                field_sql = field.measure.alias(with_view=True)
            else:
                field_sql = field.alias(with_view=True)

            if not field.is_cumulative():
                field_sql = f"{base_table.get_table_name()}.{field_sql}"
            elif field.type != "number":
                field_sql = f"{field.cte_prefix()}.{field_sql}"

            select.append(MetricsLayerQuery.sql(field_sql, alias=field.alias(with_view=True)))
        return select

    @property
    def current_date(self):
        if self.query_type == Definitions.snowflake:
            return "current_date()"
        elif self.query_type == Definitions.redshift:
            return "current_date()"
        elif self.query_type == Definitions.bigquery:
            return "current_date()"
        else:
            raise NotImplementedError(f"Query type {self.query_type} not supported")
