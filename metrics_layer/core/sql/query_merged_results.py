from pypika import AliasedQuery
from pypika.terms import LiteralValue

from metrics_layer.core.model.base import MetricsLayerBase
from metrics_layer.core.sql.pypika_types import LiteralValueCriterion
from metrics_layer.core.sql.query_dialect import query_lookup


class MetricsLayerMergedResultsQuery(MetricsLayerBase):
    """ """

    def __init__(self, definition: dict) -> None:
        self.query_lookup = query_lookup
        super().__init__(definition)

    def get_query(self, semicolon: bool = True):
        # Build the base_cte table from the referenced queries + join them with all dimensions
        base_cte_query = self.build_cte_from()

        # Add all columns in the SELECT clause
        select = self.get_select_columns()
        complete_query = base_cte_query.select(*select)

        sql = str(complete_query.limit(self.limit))
        if semicolon:
            sql += ";"
        return sql

    def build_cte_from(self):
        base_cte_query = self._get_base_query()
        for explore_name, query in self.explore_queries.items():
            base_cte_query = base_cte_query.with_(query, explore_name)

        for i, explore_name in enumerate(self.explore_names):
            if i == 0:
                base_cte_query = base_cte_query.from_(AliasedQuery(explore_name))
            else:
                criteria = self._build_join_criteria(self.explore_names[0], explore_name)
                base_cte_query = base_cte_query.inner_join(AliasedQuery(explore_name)).on(criteria)

        return base_cte_query

    def _build_join_criteria(self, first_query_alias, second_query_alias):
        no_dimensions = all(len(v) == 0 for v in self.explore_dimensions.values())
        # No dimensions to join on, the query results must be just one number each
        if no_dimensions:
            return LiteralValueCriterion("1=1")

        join_criteria = []
        for i in range(len(self.explore_dimensions[first_query_alias])):
            first_field = self.explore_dimensions[first_query_alias][i]
            second_field = self.explore_dimensions[second_query_alias][i]
            first_alias_and_id = f"{first_query_alias}.{first_field.alias(with_view=True)}"
            second_alias_and_id = f"{second_query_alias}.{second_field.alias(with_view=True)}"
            join_criteria.append(f"{first_alias_and_id}={second_alias_and_id}")

        return LiteralValueCriterion(" and ".join(join_criteria))

    # Code to handle SELECT portion of query
    def get_select_columns(self):
        select = []
        for explore_name, field_set in self.explore_metrics.items():
            for field in field_set:
                alias = field.alias(with_view=True)
                select.append(self.sql(f"{explore_name}.{alias}", alias=alias))

        for explore_name, field_set in self.explore_dimensions.items():
            for field in field_set:
                alias = field.alias(with_view=True)
                select.append(self.sql(f"{explore_name}.{alias}", alias=alias))

        for field in self.merged_metrics:
            alias = field.alias(with_view=True)
            select.append(self.sql(field.strict_replaced_query(), alias=alias))

        return select

    def _get_group_by_select_columns(self):
        select = []
        for field_name in self.dimensions + self.metrics:
            field = self.design.get_field(field_name)
            select.append(self.get_sql(field, alias=field.alias(with_view=True), use_symmetric=True))
        return select

    def _get_base_query(self):
        return self.query_lookup[self.query_type]

    @staticmethod
    def sql(sql: str, alias: str = None):
        if alias:
            return LiteralValue(sql + f" as {alias}")
        return LiteralValue(sql)
