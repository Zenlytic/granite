from copy import deepcopy

from pypika import JoinType, Criterion, Table

from metrics_layer.core.sql.query_base import MetricsLayerQueryBase
from metrics_layer.core.exceptions import QueryError
from metrics_layer.core.model.filter import LiteralValueCriterion, FilterInterval
from metrics_layer.core.sql.query_design import MetricsLayerDesign
from metrics_layer.core.sql.query_dialect import query_lookup
from metrics_layer.core.sql.query_generator import MetricsLayerQuery
from metrics_layer.core.sql.query_filter import MetricsLayerFilter


class FunnelQuery(MetricsLayerQueryBase):
    """ """

    def __init__(self, definition: dict, design: MetricsLayerDesign, suppress_warnings: bool = False) -> None:
        self.design = design
        self.query_type = self.design.query_type
        self.no_group_by = self.design.no_group_by
        self.query_lookup = query_lookup
        self.suppress_warnings = suppress_warnings

        self.step_1_time = "step_1_time"
        self.result_cte_name = "result_cte"
        self.base_cte_name = design.base_cte_name

        super().__init__(definition)

    def get_query(self, semicolon: bool = True):
        event_date = self.get_event_date()
        event_date_field = self.design.get_field(event_date)
        event_date_alias = event_date_field.alias(with_view=True)
        event_condition_fields = list(set(f["field"] for step in self.funnel["steps"] for f in step))
        link_field = self.design.project.get_field_by_tag("customer")
        link_alias = link_field.alias(with_view=True)
        base_cte_query = self._base_query()

        dimensions = self.dimensions + [event_date, link_field.id()] + event_condition_fields
        query = self._subquery(self.metrics, dimensions, self.where, no_group_by=True)
        base_cte_query = base_cte_query.with_(Table(query), self.base_cte_name)

        for i, step in enumerate(self.funnel["steps"]):
            previous_step_number = i
            step_number = i + 1

            from_query = self._base_query()
            base_table = Table(self.base_cte_name)
            from_query = from_query.from_(base_table)

            # Add within clause for the step TODO make step a array
            where = self.where_for_event(step, step_number, event_date_alias)
            if previous_step_number == 0:
                from_query = from_query.select(
                    base_table.star, self.sql(event_date_alias, alias=self.step_1_time)
                )
            else:
                prev_cte = self._cte(previous_step_number)
                match_person = f"{self.base_cte_name}.{link_alias}={prev_cte}.{link_alias}"
                valid_sequence = f"{prev_cte}.{self.step_1_time}<={self.base_cte_name}.{event_date_alias}"
                criteria = LiteralValueCriterion(f"{match_person} and {valid_sequence}")

                from_query = from_query.join(Table(prev_cte), JoinType.inner).on(criteria)
                step_1_time = self.sql(f"{prev_cte}.{self.step_1_time}", alias=self.step_1_time)
                from_query = from_query.select(base_table.star, step_1_time)

            from_query = from_query.where(Criterion.all(where))
            base_cte_query = base_cte_query.with_(Table(from_query), self._cte(step_number))

            select = [
                self.sql(f"'Step {step_number}'", alias="step"),
                self.sql(f"{step_number}", alias="step_order"),
            ]
            group_by = []
            pk = self.design.functional_pk()
            for field_name in self.metrics + self.dimensions:
                field = self.design.get_field(field_name)
                field_sql = field.sql_query(query_type=self.query_type, functional_pk=pk, alias_only=True)
                if field.field_type != "measure":
                    group_by.append(self.sql(field_sql))
                select.append(self.sql(field_sql, alias=field.alias(with_view=True)))

            union_base = self._base_query()
            if step_number == 1:
                union_cte = union_base.from_(Table(self._cte(step_number))).select(*select).groupby(*group_by)
            else:
                union_cte = union_cte.union_all(
                    union_base.from_(Table(self._cte(step_number))).select(*select).groupby(*group_by)
                )

        base_cte_query = base_cte_query.with_(Table(union_cte), self.result_cte_name)

        result_table = Table(self.result_cte_name)
        base_cte_query = base_cte_query.from_(result_table).select(result_table.star)

        base_cte_query = base_cte_query.limit(self.limit)
        sql = str(base_cte_query)
        if semicolon:
            sql += ";"
        return sql

    def get_event_date(self):
        dates = []
        for metric_name in self.metrics:
            metric = self.design.get_field(metric_name)
            if "." in metric.view.default_date:
                dates.append(tuple(metric.view.default_date.split(".")))
            else:
                dates.append((metric.view.name, metric.view.default_date))

        if len(list(dates)) == 1:
            date_key = f"{list(dates)[0][0]}.{list(dates)[0][-1]}_raw"
            return date_key
        raise QueryError(f"Could not determine event date for funnel: {self._definition}")

    @staticmethod
    def _cte(step_number: int):
        return f"step_{step_number}"

    def where_for_event(self, step: list, step_number: int, event_date_alias: str):
        where = []
        for condition in step:
            where_field = self.design.get_field(condition["field"])
            where_condition = deepcopy(condition)
            where_condition["query_type"] = self.query_type
            f = MetricsLayerFilter(definition=where_condition, design=None, filter_type="where")
            where_field_sql = where_field.sql_query(query_type=self.query_type, alias_only=True)
            where.append(f.criterion(f"{self.base_cte_name}.{where_field_sql}"))

        if step_number > 1:
            within = self.funnel["within"]
            unit = FilterInterval.plural(within["unit"])
            value = int(within["value"])
            start = f"{self._cte(step_number-1)}.{self.step_1_time}"
            end = f"{self.base_cte_name}.{event_date_alias}"
            date_diff = where_field.dimension_group_duration_sql(
                start, end, query_type=self.query_type, dimension_group=unit
            )
            where.append(LiteralValueCriterion(f"{date_diff} <= {value}"))
        return where

    def _subquery(self, metrics: list, dimensions: list, where: list, no_group_by: bool):
        sub_definition = deepcopy(self._definition)
        sub_definition["metrics"] = metrics
        sub_definition["dimensions"] = dimensions
        sub_definition["where"] = where
        sub_definition["having"] = []
        sub_definition["limit"] = None

        self.design.no_group_by = no_group_by
        for dimension_name in dimensions:
            dimension = self.design.get_field(dimension_name)
            if dimension.id() not in self.design.field_lookup:
                self.design.field_lookup[dimension.id()] = dimension
        for metric_name in metrics:
            metric = self.design.get_field(metric_name)
            if metric.view.primary_key.id() not in self.design.field_lookup:
                self.design.field_lookup[metric.view.primary_key.id()] = metric.view.primary_key
                sub_definition["dimensions"].append(metric.view.primary_key.id())

        query_generator = MetricsLayerQuery(
            sub_definition, design=self.design, suppress_warnings=self.suppress_warnings
        )
        query = query_generator.get_query(semicolon=False)
        self.design.no_group_by = False

        return query