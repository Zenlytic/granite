from collections import defaultdict
from copy import deepcopy

from metrics_layer.core.parse.config import MetricsLayerConfiguration
from metrics_layer.core.sql.query_merged_results import MetricsLayerMergedResultsQuery
from metrics_layer.core.sql.single_query_resolve import SingleSQLQueryResolver


class SQLQueryResolver(SingleSQLQueryResolver):
    """
    Method of resolving the explore name:
        if there is not explore passed (using the format explore_name.field_name), we'll search for
        just the field name and iff that field is used in only one explore, set that as the active explore.
            - Any fields specified that are not in that explore will raise an error

        if it's passed explicitly, use the first metric's explore, and raise an error if anything conflicts
        with that explore
    """

    def __init__(
        self,
        metrics: list,
        dimensions: list = [],
        where: str = None,  # Either a list of json or a string
        having: str = None,  # Either a list of json or a string
        order_by: str = None,  # Either a list of json or a string
        config: MetricsLayerConfiguration = None,
        **kwargs,
    ):
        self.field_lookup = {}
        self.no_group_by = False
        self.verbose = kwargs.get("verbose", False)
        self.merged_result = kwargs.get("merged_result", False)
        self.select_raw_sql = kwargs.get("select_raw_sql", [])
        self.explore_name = kwargs.get("explore_name")
        self.suppress_warnings = kwargs.get("suppress_warnings", False)
        self.limit = kwargs.get("limit")
        self.config = config
        self.project = self.config.project
        self.metrics = metrics
        self.dimensions = dimensions
        self.where = where
        self.having = having
        self.order_by = order_by
        self.kwargs = kwargs
        self.connection = None
        self.model = self.project.get_model("test_model")

    def get_query(self, semicolon: bool = True):
        if self.merged_result:
            return self._get_merged_result_query(semicolon=semicolon)
        return self._get_single_query(semicolon=semicolon)

    def _get_single_query(self, semicolon: bool):
        resolver = SingleSQLQueryResolver(
            metrics=self.metrics,
            dimensions=self.dimensions,
            where=self.where,
            having=self.having,
            order_by=self.order_by,
            model=self.model,
            config=self.config,
            **self.kwargs,
        )
        query = resolver.get_query(semicolon)
        self.connection = resolver.connection
        return query

    def _get_merged_result_query(self, semicolon: bool):
        self.parse_field_names(self.where, self.having, self.order_by)
        self.derive_sub_queries3()
        # self.derive_sub_queries2()

        queries_to_join = {}
        for join_hash in self.query_metrics.keys():
            metrics = [f.id() for f in self.query_metrics[join_hash]]
            dimensions = [f.id() for f in self.query_dimensions.get(join_hash, [])]

            # Overwrite the limit arg because these are subqueries
            kws = {**self.kwargs, "limit": None, "return_pypika_query": True}
            resolver = SingleSQLQueryResolver(
                metrics=metrics,
                dimensions=dimensions,
                where=self.query_where[join_hash],
                having=[],
                order_by=[],
                model=self.model,
                config=self.config,
                **kws,
            )
            query = resolver.get_query(semicolon=False)
            queries_to_join[join_hash] = query

        query_config = {
            "merged_metrics": self.merged_metrics,
            "query_metrics": self.query_metrics,
            "query_dimensions": self.query_dimensions,
            "having": self.having,
            "queries_to_join": queries_to_join,
            "join_hashes": list(sorted(self.query_metrics.keys())),
            "query_type": resolver.query_type,
            "limit": self.limit,
            "project": self.project,
        }
        merged_result_query = MetricsLayerMergedResultsQuery(query_config)
        query = merged_result_query.get_query(semicolon=semicolon)

        self.connection = resolver.connection
        return query

    def derive_sub_queries3(self):
        self.query_metrics = defaultdict(list)
        self.merged_metrics = []
        self.secondary_metrics = []

        for metric in self.metrics:
            field = self.project.get_field(metric)
            if field.is_merged_result:
                self.merged_metrics.append(field)
            else:
                self.secondary_metrics.append(field)

        for merged_metric in self.merged_metrics:
            for ref_field in merged_metric.referenced_fields(merged_metric.sql):
                if isinstance(ref_field, str):
                    raise ValueError(f"Unable to find the field {ref_field} in the project")

                join_group_hash = self.project.join_graph.join_graph_hash(ref_field.view.name)
                canon_date = ref_field.canon_date.replace(".", "_")
                key = f"{canon_date}__{join_group_hash}"
                self.query_metrics[key].append(ref_field)

        print(self.query_metrics)

        for field in self.secondary_metrics:
            join_group_hash = self.project.join_graph.join_graph_hash(field.view.name)
            canon_date = field.canon_date.replace(".", "_")
            key = f"{canon_date}__{join_group_hash}"
            if key in self.query_metrics:
                already_in_query = any(field.id() in f.id() for f in self.query_metrics[key])
                if not already_in_query:
                    self.query_metrics[key].append(field)
            else:
                self.query_metrics[key].append(field)

        print(self.query_metrics)

        canon_dates = []
        dimension_mapping = defaultdict(list)
        for join_hash, field_set in self.query_metrics.items():
            print(field_set)
            if len({f.canon_date for f in field_set}) > 1:
                raise NotImplementedError(
                    "Zenlytic does not currently support different canon_date "
                    "values for metrics in the same subquery"
                )
            canon_date = field_set[0].canon_date
            canon_dates.append(canon_date)
            print(canon_date)
            print()
            for other_explore_name, other_field_set in self.query_metrics.items():
                if other_explore_name != join_hash:
                    other_canon_date = other_field_set[0].canon_date
                    canon_date_data = {"field": other_canon_date, "join_hash": other_explore_name}
                    dimension_mapping[canon_date].append(canon_date_data)

        print(self.model.mappings)
        print(self.model)
        for _, mapped_values in self.model.mappings.items():

            for mapped_from_field in mapped_values:
                from_field = self.project.get_field(mapped_from_field)
                if from_field.field_type in {"dimension_group", "measure"}:
                    raise ValueError(
                        "This mapping is invalid because it contains a dimension group or "
                        "a measure. Mappings can only contain dimensions."
                    )
                # from_join_group_hash = self.project.join_graph.join_graph_hash(from_field.view.name)

                for mapped_to_field in mapped_values:
                    if mapped_to_field != mapped_from_field:
                        to_field = self.project.get_field(mapped_to_field)
                        to_join_group_hash = self.project.join_graph.join_graph_hash(to_field.view.name)

                        for other_join_hash, other_field_set in self.query_metrics.items():
                            if to_join_group_hash in other_join_hash:
                                map_data = {"field": mapped_to_field, "join_hash": other_join_hash}
                                dimension_mapping[mapped_from_field].append(map_data)

        print(dimension_mapping)
        print()

        self.query_dimensions = defaultdict(list)
        for dimension in self.dimensions:
            field = self.project.get_field(dimension)
            join_group_hash = self.project.join_graph.join_graph_hash(field.view.name)
            field_key = f"{field.view.name}.{field.name}"
            if field_key in canon_dates:
                join_hash = f'{field_key.replace(".", "_")}__{join_group_hash}'
                self.query_dimensions[join_hash].append(field)

                dimension_group = field.dimension_group
                for mapping_info in dimension_mapping[field_key]:
                    key = f"{mapping_info['field']}_{dimension_group}"
                    ref_field = self.project.get_field(key)
                    self.query_dimensions[mapping_info["join_hash"]].append(ref_field)
            else:

                # dimension_group = field.dimension_group
                # print(join_group_hash)
                for join_hash in self.query_metrics.keys():
                    #     print(join_hash)
                    if join_group_hash in join_hash:
                        self.query_dimensions[join_hash].append(field)
                    else:
                        if field_key not in dimension_mapping:
                            raise ValueError(
                                f"Could not find mapping from field {field_key} to other views. "
                                "Please add a mapping to your view definition to allow this."
                            )
                        for mapping_info in dimension_mapping[field_key]:
                            ref_field = self.project.get_field(mapping_info["field"])
                            self.query_dimensions[mapping_info["join_hash"]].append(ref_field)
            #     else:
        print(self.query_dimensions)

        # Get rid of duplicates while keeping order to make joining work properly
        self.query_dimensions = {
            k: sorted(list(set(v)), key=lambda x: v.index(x)) for k, v in self.query_dimensions.items()
        }

        print(self.query_dimensions)

        self.query_where = defaultdict(list)
        for where in self.where:
            field = self.project.get_field(where["field"])
            dimension_group = field.dimension_group
            join_group_hash = self.project.join_graph.join_graph_hash(field.view.name)
            print(join_group_hash)
            for join_hash in self.query_metrics.keys():
                print(join_hash)
                if join_group_hash in join_hash:
                    self.query_where[join_hash].append(where)
                else:
                    key = f"{field.view.name}.{field.name}"
                    for mapping_info in dimension_mapping[key]:
                        key = f"{mapping_info['field']}_{dimension_group}"
                        ref_field = self.project.get_field(key)
                        mapped_where = deepcopy(where)
                        mapped_where["field"] = ref_field.id()
                        join_group_hash = self.project.join_graph.join_graph_hash(ref_field.view.name)
                        self.query_where[join_hash].append(mapped_where)

        # Key cleanup
        # self.query_metrics = {k.split("__")[-1]: v for k, v in self.query_metrics.items()}
        # self.query_dimensions = {k.split("__")[-1]: v for k, v in self.query_dimensions.items()}

    def derive_sub_queries2(self):
        self.query_metrics = defaultdict(list)
        self.merged_metrics = []

        for metric in self.metrics:
            field = self.project.get_field(metric)
            if field.is_merged_result:
                self.merged_metrics.append(field)
            else:
                if field.canon_date is None:
                    raise ValueError(
                        "You must specify the canon_date property if you want to use a merged result query"
                    )
                join_group_hash = self.project.join_graph.join_graph_hash(field.view.name)
                self.query_metrics[join_group_hash].append(field)

        print(self.query_metrics)
        print(self.merged_metrics)

        for merged_metric in self.merged_metrics:
            for ref_field in merged_metric.referenced_fields(merged_metric.sql):
                print(ref_field)
                if isinstance(ref_field, str):
                    raise ValueError(f"Unable to find the field {ref_field} in the project")

                join_group_hash = self.project.join_graph.join_graph_hash(ref_field.view.name)
                self.query_metrics[join_group_hash].append(ref_field)

        print(self.query_metrics)

        for join_hash in self.query_metrics.keys():
            self.query_metrics[join_hash] = list(set(self.query_metrics[join_hash]))

        dimension_mapping = defaultdict(list)
        for explore_name, field_set in self.query_metrics.items():
            print(field_set)
            if len({f.canon_date for f in field_set}) > 1:
                raise NotImplementedError(
                    "Zenlytic does not currently support different canon_date "
                    "values for metrics in the same query in the same explore"
                )
            canon_date = field_set[0].canon_date
            print(canon_date)
            print()
            for other_explore_name, other_field_set in self.query_metrics.items():
                if other_explore_name != explore_name:
                    other_canon_date = other_field_set[0].canon_date
                    canon_date_data = {"field": other_canon_date}
                    dimension_mapping[canon_date].append(canon_date_data)

        self.query_dimensions = defaultdict(list)
        for dimension in self.dimensions:
            field = self.project.get_field(dimension)
            dimension_group = field.dimension_group
            join_group_hash = self.project.join_graph.join_graph_hash(field.view.name)
            self.query_dimensions[join_group_hash].append(field)
            key = f"{field.view.name}.{field.name}"
            for mapping_info in dimension_mapping[key]:
                key = f"{mapping_info['field']}_{dimension_group}"
                field = self.project.get_field(key)
                join_group_hash = self.project.join_graph.join_graph_hash(field.view.name)
                self.query_dimensions[join_group_hash].append(field)

        print(dimension_mapping)
        print(self.query_dimensions)

        self.query_where = defaultdict(list)
        for where in self.where:
            field = self.project.get_field(where["field"])
            dimension_group = field.dimension_group
            join_group_hash = self.project.join_graph.join_graph_hash(field.view.name)
            self.query_where[join_group_hash].append(where)
            for mapping_info in dimension_mapping[field.name]:
                key = f"{mapping_info['field']}_{dimension_group}"
                ref_field = self.project.get_field(key)
                mapped_where = deepcopy(where)
                mapped_where["field"] = ref_field.id()
                join_group_hash = self.project.join_graph.join_graph_hash(ref_field.view.name)
                self.query_where[join_group_hash].append(mapped_where)

    def derive_sub_queries(self):
        # The different explores used are determined by the metrics referenced
        self.explore_metrics = defaultdict(list)
        self.merged_metrics = []
        for metric in self.metrics:
            explore_name = self.project.get_explore_from_field(metric)
            field = self.project.get_field(metric, explore_name=explore_name)
            if field.is_merged_result:
                self.merged_metrics.append(field)
            else:
                if field.canon_date is None:
                    raise ValueError(
                        "You must specify the canon_date property if you want to use a merged result query"
                    )
                self.explore_metrics[explore_name].append(field)

        for merged_metric in self.merged_metrics:
            for ref_field in merged_metric.referenced_fields(merged_metric.sql):
                if isinstance(ref_field, str):
                    raise ValueError(f"Unable to find the field {ref_field} in the project")
                if ref_field.view.explore is None:
                    explore_name = merged_metric.view.explore.name
                else:
                    explore_name = ref_field.view.explore.name
                self.explore_metrics[explore_name].append(ref_field)

        for explore_name in self.explore_metrics.keys():
            self.explore_metrics[explore_name] = list(set(self.explore_metrics[explore_name]))

        dimension_mapping = defaultdict(list)
        for explore_name, field_set in self.explore_metrics.items():
            if len({f.canon_date for f in field_set}) > 1:
                raise NotImplementedError(
                    "Zenlytic does not currently support different canon_date "
                    "values for metrics in the same query in the same explore"
                )
            canon_date = field_set[0].canon_date
            for other_explore_name, other_field_set in self.explore_metrics.items():
                if other_explore_name != explore_name:
                    other_canon_date = other_field_set[0].canon_date
                    other_view_name = other_field_set[0].view.name
                    canon_date_data = {
                        "field": f"{other_view_name}.{other_canon_date}",
                        "explore_name": other_explore_name,
                    }
                    dimension_mapping[canon_date].append(canon_date_data)

        self.explore_dimensions = defaultdict(list)
        for dimension in self.dimensions:
            explore_name = self.project.get_explore_from_field(dimension)
            if explore_name not in self.explore_metrics:
                raise ValueError(
                    f"Could not find a metric in {self.metrics} that references the explore {explore_name}"
                )
            field = self.project.get_field(dimension, explore_name=explore_name)
            dimension_group = field.dimension_group
            self.explore_dimensions[explore_name].append(field)
            for mapping_info in dimension_mapping[field.name]:
                key = f"{mapping_info['field']}_{dimension_group}"
                field = self.project.get_field(key, explore_name=mapping_info["explore_name"])
                self.explore_dimensions[mapping_info["explore_name"]].append(field)

        self.explore_where = defaultdict(list)
        for where in self.where:
            explore_name = self.project.get_explore_from_field(where["field"])
            if explore_name not in self.explore_metrics:
                raise ValueError(
                    f"Could not find a metric in {self.metrics} that references the explore {explore_name}"
                )
            field = self.project.get_field(where["field"], explore_name=explore_name)
            dimension_group = field.dimension_group
            self.explore_where[explore_name].append(where)
            for mapping_info in dimension_mapping[field.name]:
                key = f"{mapping_info['field']}_{dimension_group}"
                field = self.project.get_field(key, explore_name=mapping_info["explore_name"])
                mapped_where = deepcopy(where)
                mapped_where["field"] = field.id()
                self.explore_where[mapping_info["explore_name"]].append(mapped_where)
