from typing import List, Union

from metrics_layer.core.exceptions import (
    AccessDeniedOrDoesNotExistException,
    JoinError,
    QueryError,
)
from metrics_layer.core.model.definitions import Definitions
from metrics_layer.core.model.project import Project
from metrics_layer.core.sql.query_arbitrary_merged_queries import (
    MetricsLayerMergedQueries,
)
from metrics_layer.core.sql.resolve import SQLQueryResolver
from metrics_layer.core.sql.single_query_resolve import SingleSQLQueryResolver


class ArbitraryMergedQueryResolver(SingleSQLQueryResolver):
    def __init__(
        self,
        merged_queries: List[dict],
        where: List[dict] = [],
        having: List[dict] = [],
        order_by: List[dict] = [],
        project: Union[Project, None] = None,
        connections: List = [],
        **kwargs,
    ):
        self.validate_arbitrary_merged_queries(merged_queries)
        self.merged_queries = merged_queries
        self.verbose = kwargs.get("verbose", False)
        self.where = where
        self.having = having
        self.order_by = order_by
        self.limit = kwargs.pop("limit", None)
        self.project = project
        self.connections = connections
        self.connection = None
        self.kwargs = kwargs

    def get_query(self, semicolon: bool = True):
        sub_queries = []
        primary_resolver = self._init_resolver(self.merged_queries[0])
        for i, merged_query in enumerate(self.merged_queries):
            resolver = self._init_resolver(merged_query)
            self.connection = resolver.connection
            join_fields = merged_query.get("join_fields", [])
            if i > 0:
                join_fields = self._resolve_join_fields_mappings(primary_resolver, resolver, join_fields, i)

            sub_query = resolver.get_query(semicolon=False)
            sub_queries.append(
                {
                    "metrics": resolver.metrics,
                    "dimensions": resolver.dimensions,
                    "cte_alias": f"merged_query_{i}",
                    "query": sub_query,
                    "join_fields": join_fields,
                }
            )

        merged_queries_resolver = MetricsLayerMergedQueries(
            {
                "merged_queries": sub_queries,
                "query_type": resolver.query_type,
                "where": self.where,
                "having": self.having,
                "order_by": self.order_by,
                "limit": self.limit,
                "project": self.project,
            }
        )
        # Druid does not allow semicolons
        if resolver.query_type == Definitions.druid:
            semicolon = False

        query = merged_queries_resolver.get_query(semicolon=semicolon)

        return query

    def _resolve_join_fields_mappings(
        self,
        primary_resolver: SQLQueryResolver,
        secondary_resolver: SQLQueryResolver,
        join_fields: List[dict],
        query_number: int,
    ):
        join_fields_resolved = []
        for join_field in join_fields:
            self._resolve_join_logic(secondary_resolver, "field", join_field, query_number + 1)
            self._resolve_join_logic(primary_resolver, "source_field", join_field, 1)
            join_fields_resolved.append(join_field)
        return join_fields_resolved

    # Note: this mutates join_field
    def _resolve_join_logic(self, resolver, key, join_field, query_number: int):
        if mapping_object := resolver.mapping_lookup.get(join_field[key]):
            try:
                join_field[key] = next((f for f in mapping_object["fields"] if f in resolver.dimensions))
            except StopIteration:
                self._raise_join_error(join_field[key], query_number)
        else:
            try:
                field = self.project.get_field(join_field[key])
                dimension_ids = [
                    resolver.field_object_lookup[d].id() if d in resolver.field_object_lookup else d
                    for d in resolver.dimensions
                ]
                if field.id() not in dimension_ids:
                    self._raise_join_error(join_field[key], query_number)
            except AccessDeniedOrDoesNotExistException:
                self._raise_join_error(join_field[key], query_number)

    def _raise_join_error(self, field_name: str, query_number):
        raise JoinError(
            f"Join field {field_name} not found in the query number {query_number}. To be used as a join the"
            f" field must be included in query {query_number}."
        )

    def _init_resolver(self, merged_query: dict):
        kws = {**self.kwargs, "limit": merged_query.get("limit"), "return_pypika_query": True}
        return SQLQueryResolver(
            metrics=merged_query.get("metrics", []),
            dimensions=merged_query.get("dimensions", []),
            where=merged_query.get("where", []),
            having=merged_query.get("having", []),
            order_by=merged_query.get("order_by", []),
            project=self.project,
            connections=self.connections,
            **kws,
        )

    @staticmethod
    def validate_arbitrary_merged_queries(merged_queries: list):
        for i, merged_query in enumerate(merged_queries):
            if not isinstance(merged_query, dict):
                raise QueryError(
                    f"merged_queries must be a list of dictionaries. Item {i} is not a dictionary."
                )
            if not merged_query.get("metrics") or not merged_query.get("dimensions"):
                raise QueryError(f"Each item in merged_queries must have 'metrics' and 'dimensions' keys.")
            if merged_query.get("funnel"):
                raise QueryError(
                    f"Each item in merged_queries must not have 'funnel' key. Funnels are not supported in"
                    f" merged queries."
                )
            if not isinstance(merged_query.get("join_fields", []), list):
                raise QueryError(f"Each item in merged_queries must have 'join_fields' key as a list.")
            if i > 0 and not merged_query.get("join_fields"):
                raise QueryError(f"Each item in merged_queries after the first must have 'join_fields' key.")
