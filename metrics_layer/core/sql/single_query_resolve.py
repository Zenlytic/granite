import sqlparse
from sqlparse.tokens import Name, Punctuation

from metrics_layer.core.exceptions import QueryError
from metrics_layer.core.parse.config import ConfigError, MetricsLayerConfiguration
from metrics_layer.core.sql.query_design import MetricsLayerDesign
from metrics_layer.core.sql.query_generator import MetricsLayerQuery
from metrics_layer.core.sql.query_cumulative_metric import CumulativeMetricsQuery


class SingleSQLQueryResolver:
    def __init__(
        self,
        metrics: list,
        dimensions: list = [],
        where: str = None,  # Either a list of json or a string
        having: str = None,  # Either a list of json or a string
        order_by: str = None,  # Either a list of json or a string
        model=None,
        config: MetricsLayerConfiguration = None,
        **kwargs,
    ):
        self.field_lookup = {}
        self.no_group_by = False
        self.has_cumulative_metric = False
        self.verbose = kwargs.get("verbose", False)
        self.select_raw_sql = kwargs.get("select_raw_sql", [])
        self.explore_name = kwargs.get("explore_name")
        self.suppress_warnings = kwargs.get("suppress_warnings", False)
        self.limit = kwargs.get("limit")
        self.return_pypika_query = kwargs.get("return_pypika_query")
        self.force_group_by = kwargs.get("force_group_by", False)
        self.config = config
        self.project = self.config.project
        self.metrics = metrics
        self.dimensions = dimensions
        self.parse_field_names(where, having, order_by)
        self.model = model

        try:
            self.connection = self.config.get_connection(model.connection)
        except ConfigError:
            self.connection = None

        if "query_type" in kwargs:
            self.query_type = kwargs["query_type"]
        elif self.connection:
            self.query_type = self.connection.type
        else:
            raise ConfigError(
                "Could not determine query_type. Please have connection information for "
                "your warehouse in the configuration or explicitly pass the "
                "'query_type' argument to this function"
            )
        self.parse_input()

    def get_query(self, semicolon: bool = True):
        self.design = MetricsLayerDesign(
            no_group_by=self.no_group_by,
            query_type=self.query_type,
            field_lookup=self.field_lookup,
            model=self.model,
            project=self.project,
        )

        query_definition = {
            "metrics": self.metrics,
            "dimensions": self.dimensions,
            "where": self.where,
            "having": self.having,
            "order_by": self.order_by,
            "select_raw_sql": self.select_raw_sql,
            "limit": self.limit,
            "return_pypika_query": self.return_pypika_query,
        }
        if self.has_cumulative_metric:
            query_generator = CumulativeMetricsQuery(
                query_definition, design=self.design, suppress_warnings=self.suppress_warnings
            )
        else:
            query_generator = MetricsLayerQuery(
                query_definition, design=self.design, suppress_warnings=self.suppress_warnings
            )

        query = query_generator.get_query(semicolon=semicolon)

        return query

    def get_used_views(self):
        unique_view_names = {f.view.name for f in self.field_lookup.values()}
        return [self.project.get_view(name) for name in unique_view_names]

    def parse_input(self):
        # if self.explore.symmetric_aggregates == "no":
        #     raise NotImplementedError("MetricsLayer does not support turning off symmetric aggregates")

        all_field_names = self.metrics + self.dimensions
        if len(set(all_field_names)) != len(all_field_names):
            # TODO improve this error message
            raise QueryError("Ambiguous field names in the metrics and dimensions")

        for name in self.metrics:
            field = self.get_field_with_error_handling(name, "Metric")
            if field.type == "cumulative":
                self.has_cumulative_metric = True
            self.field_lookup[name] = field

        # Dimensions exceptions:
        #   They are coming from a different explore than the metric, not joinable (handled in get_field)
        #   They are not found in the selected explore (handled here)
        # TODO make this better
        metric_view = None if len(self.metrics) == 0 else self.field_lookup[self.metrics[0]].view.name

        for name in self.dimensions:
            field = self.get_field_with_error_handling(name, "Dimension")
            # We will not use a group by if the primary key of the main resulting table is included
            if field.primary_key == "yes" and field.view.name == metric_view and not self.force_group_by:
                self.no_group_by = True
            self.field_lookup[name] = field

        for name in self._where_field_names:
            self.field_lookup[name] = self.get_field_with_error_handling(name, "Where clause field")

        for name in self._having_field_names:
            self.field_lookup[name] = self.get_field_with_error_handling(name, "Having clause field")

        for name in self._order_by_field_names:
            self.field_lookup[name] = self.get_field_with_error_handling(name, "Order by field")

    def get_field_with_error_handling(self, field_name: str, error_prefix: str):
        field = self.project.get_field(field_name, model=self.model)
        if field is None:
            raise QueryError(f"{error_prefix} {field_name} not found")
        return field

    def parse_field_names(self, where, having, order_by):
        self.where = self._check_for_dict(where)
        if self._is_literal(self.where):
            self._where_field_names = self.parse_identifiers_from_clause(self.where)
        else:
            self._where_field_names = self.parse_identifiers_from_dicts(self.where)

        self.having = self._check_for_dict(having)
        if self._is_literal(self.having):
            self._having_field_names = self.parse_identifiers_from_clause(self.having)
        else:
            self._having_field_names = self.parse_identifiers_from_dicts(self.having)

        self.order_by = self._check_for_dict(order_by)
        if self._is_literal(self.order_by):
            self._order_by_field_names = self.parse_identifiers_from_clause(self.order_by)
        else:
            self._order_by_field_names = self.parse_identifiers_from_dicts(self.order_by)

    @staticmethod
    def _is_literal(clause):
        return isinstance(clause, str) or clause is None

    @staticmethod
    def parse_identifiers_from_clause(clause: str):
        if clause is None:
            return []
        generator = list(sqlparse.parse(clause)[0].flatten())

        field_names = []
        for i, token in enumerate(generator):
            not_already_added = i == 0 or str(generator[i - 1]) != "."
            if token.ttype == Name and not_already_added:
                field_names.append(str(token))

            if token.ttype == Punctuation and str(token) == ".":
                if generator[i - 1].ttype == Name and generator[i + 1].ttype == Name:
                    field_names[-1] += f".{str(generator[i+1])}"
        return field_names

    @staticmethod
    def parse_identifiers_from_dicts(conditions: list):
        try:
            return [cond["field"] for cond in conditions]
        except KeyError:
            for cond in conditions:
                if "field" not in cond:
                    break
            raise QueryError(f"Identifier was missing required 'field' key: {cond}")

    @staticmethod
    def _check_for_dict(conditions: list):
        if isinstance(conditions, dict):
            return [conditions]
        return conditions
