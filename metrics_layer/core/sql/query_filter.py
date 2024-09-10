import datetime
from typing import Dict

import pandas as pd
from pypika import Criterion, Field, Table
from pypika.terms import LiteralValue

from metrics_layer.core.exceptions import QueryError
from metrics_layer.core.model.base import MetricsLayerBase
from metrics_layer.core.model.definitions import Definitions
from metrics_layer.core.model.field import Field as MetricsLayerField
from metrics_layer.core.model.filter import (
    Filter,
    LiteralValueCriterion,
    MetricsLayerFilterExpressionType,
    MetricsLayerFilterGroupLogicalOperatorType,
)
from metrics_layer.core.sql.query_design import MetricsLayerDesign
from metrics_layer.core.sql.query_errors import ParseError


def datatype_cast(field, value):
    if field.datatype.upper() == "DATE":
        return LiteralValue(f"CAST(CAST('{value}' AS TIMESTAMP) AS DATE)")
    return LiteralValue(f"CAST('{value}' AS {field.datatype.upper()})")


class FunnelFilterTypes:
    converted = "converted"
    dropped_off = "dropped_off"


class MetricsLayerFilter(MetricsLayerBase):
    """
    An internal representation of a Filter (WHERE or HAVING clause)
    defined in a MetricsLayerQuery.

    definition: {"field", "expression", "value"}
    """

    def __init__(
        self, definition: Dict = {}, design: MetricsLayerDesign = None, filter_type: str = None
    ) -> None:
        # The design is used for filters in queries against specific designs
        #  to validate that all the tables and attributes (columns/aggregates)
        #  are properly defined in the design
        self.design = design
        self.is_literal_filter = "literal" in definition
        # This is a filter with parenthesis like (XYZ or ABC)
        self.is_filter_group = "conditions" in definition or "conditional_filter_logic" in definition

        if self.design:
            self.query_type = self.design.query_type
        else:
            self.query_type = definition["query_type"]
        self.filter_type = filter_type

        self.validate(definition)

        if not self.is_literal_filter and not self.is_filter_group:
            self.expression_type = MetricsLayerFilterExpressionType.parse(definition["expression"])

        super().__init__(definition)

    @property
    def conditions(self):
        if "conditional_filter_logic" in self._definition:
            return self._definition["conditional_filter_logic"]
        return self._definition.get("conditions", [])

    @property
    def is_group_by(self):
        return self.group_by is not None

    @property
    def is_funnel(self):
        return self.expression in {FunnelFilterTypes.converted, FunnelFilterTypes.dropped_off}

    def validate(self, definition: Dict) -> None:
        """
        Validate the Filter definition
        """
        key = definition.get("field", None)
        filter_literal = definition.get("literal", None)
        filter_group_conditions = definition.get("conditions", None)
        filter_group_conditional_filter_logic = definition.get("conditional_filter_logic", None)
        if filter_group_conditions is None:
            filter_group_conditions = filter_group_conditional_filter_logic

        if filter_group_conditions:
            for f in filter_group_conditions:
                MetricsLayerFilter(f, self.design, self.filter_type)

            if (
                "logical_operator" in definition
                and definition["logical_operator"] not in MetricsLayerFilterGroupLogicalOperatorType.options
            ):
                raise ParseError(
                    f"Filter group '{definition}' needs a valid logical operator. Options are:"
                    f" {MetricsLayerFilterGroupLogicalOperatorType.options}"
                )
            return

        is_boolean_value = str(definition.get("value")).lower() == "true" and key is None
        if is_boolean_value:
            definition["value"] = True
        if key is None and filter_literal is None and not is_boolean_value:
            raise ParseError(f"An attribute key or literal was not provided for filter '{definition}'.")

        if key is None and filter_literal:
            return

        if definition["expression"] == "UNKNOWN":
            raise NotImplementedError(f"Unknown filter expression: {definition['expression']}.")

        no_expr = {"is_null", "is_not_null", "boolean_true", "boolean_false", "converted", "dropped_off"}
        if definition.get("value", None) is None and definition["expression"] not in no_expr:
            raise ParseError(f"Filter expression: {definition['expression']} needs a non-empty value.")

        if self.design:
            self.week_start_day = self.design.week_start_day
            self.timezone = self.design.project.timezone
        else:
            self.week_start_day = None
            self.timezone = None

        if self.design and not is_boolean_value:
            # Will raise ParseError if not found
            try:
                self.field = self.design.get_field(key)
            except ParseError:
                raise ParseError(f"We could not find field {self.field_name}")

            # If the value is a string, it might be a field reference.
            # If it is a field reference, we need to replace it with the actual
            # field's sql as a LiteralValue
            if "value" in definition and isinstance(definition["value"], str):
                try:
                    value_field = self.design.get_field(definition["value"])
                    functional_pk = self.design.functional_pk()
                    definition["value"] = LiteralValue(value_field.sql_query(self.query_type, functional_pk))
                except Exception:
                    pass

            if self.design.query_type in Definitions.needs_datetime_cast and isinstance(
                definition["value"], datetime.datetime
            ):
                definition["value"] = datatype_cast(self.field, definition["value"])

            if self.field.type == "yesno" and "False" in str(definition["value"]):
                definition["expression"] = "boolean_false"

            if self.field.type == "yesno" and "True" in str(definition["value"]):
                definition["expression"] = "boolean_true"

    def group_sql_query(self, functional_pk: str):
        pypika_conditions = []
        for condition in self.conditions:
            condition_object = MetricsLayerFilter(condition, self.design, self.filter_type)
            if condition_object.is_filter_group:
                pypika_conditions.append(condition_object.group_sql_query(functional_pk))
            else:
                pypika_conditions.append(
                    condition_object.criterion(
                        condition_object.field.sql_query(self.query_type, functional_pk)
                    )
                )
        if self.logical_operator == MetricsLayerFilterGroupLogicalOperatorType.or_:
            return Criterion.any(pypika_conditions)
        if (
            self.logical_operator is None
            or self.logical_operator == MetricsLayerFilterGroupLogicalOperatorType.and_
        ):
            return Criterion.all(pypika_conditions)
        raise ParseError(f"Invalid logical operator: {self.logical_operator}")

    def sql_query(self):
        if self.is_literal_filter:
            return LiteralValueCriterion(self.replace_fields_literal_filter())
        functional_pk = self.design.functional_pk()
        if self.is_filter_group:
            return self.group_sql_query(functional_pk)
        return self.criterion(self.field.sql_query(self.query_type, functional_pk))

    def isin_sql_query(self, cte_alias, field_name, query_generator):
        group_by_field = self.design.get_field(field_name)
        base = query_generator._base_query()
        subquery = base.from_(Table(cte_alias)).select(group_by_field.alias(with_view=True)).distinct()
        definition = {
            "query_type": self.query_type,
            "field": field_name,
            "expression": MetricsLayerFilterExpressionType.IsIn.value,
            "value": subquery,
        }
        f = MetricsLayerFilter(definition=definition, design=None, filter_type="where")
        return f.criterion(group_by_field.sql_query(self.query_type))

    def replace_fields_literal_filter(self):
        if self.filter_type == "where":
            extra_args = {"field_type": None}
        else:
            extra_args = {"field_type": "measure", "type": "number"}
        view = self.design.get_view(self.design.base_view_name)
        field = MetricsLayerField({"sql": self.literal, "name": None, **extra_args}, view=view)
        return field.sql_query(self.query_type, functional_pk=self.design.functional_pk())

    def criterion(self, field_sql: str) -> Criterion:
        """
        Generate the Pypika Criterion for this filter

        We have to use the following cases as PyPika does not allow an str
         representation of the clause on its where() and having() functions
        """
        if self.expression_type == MetricsLayerFilterExpressionType.Matches:
            criteria = []
            filter_dict = {
                "field": self.field.alias(),
                "value": self.value,
                "week_start_day": self.week_start_day,
                "timezone": self.timezone,
            }
            for f in Filter(filter_dict).filter_dict():
                if self.query_type in Definitions.needs_datetime_cast:
                    value = datatype_cast(self.field, f["value"])
                else:
                    value = f["value"]
                criteria.append(Filter.sql_query(field_sql, f["expression"], value, self.field.type))
            return Criterion.all(criteria)
        if isinstance(self.field, MetricsLayerField):
            field_datatype = self.field.type
        else:
            field_datatype = "unknown"
        return Filter.sql_query(field_sql, self.expression_type, self.value, field_datatype)

    def cte(self, query_class, design_class):
        if not self.is_group_by:
            raise QueryError("A CTE is invalid for a filter with no group_by property")

        having_filter = {k: v for k, v in self._definition.items() if k != "group_by"}
        field_names = [self.group_by, having_filter["field"]]
        field_lookup = {}
        for n in field_names:
            field = self.design.get_field(n)
            field_lookup[field.id()] = field

        design = design_class(
            no_group_by=False,
            query_type=self.design.query_type,
            field_lookup=field_lookup,
            model=self.design.model,
            project=self.design.project,
        )

        config = {
            "metrics": [],
            "dimensions": [self.group_by],
            "having": [having_filter],
            "return_pypika_query": True,
        }
        generator = query_class(config, design=design)
        return generator.get_query()

    def funnel_cte(self):
        if not self.is_funnel:
            raise QueryError("A funnel CTE is invalid for a filter with no funnel property")

        _from, _to = self._definition["from"], self._definition["to"]
        from_cte, to_cte = self.query_class._cte(_from), self.query_class._cte(_to)

        base_query = self.query_class._base_query()
        base_table = Table(self.query_class.base_cte_name)
        base_query = base_query.from_(base_table).select(self.query_class.link_alias)

        from_cond = self.__funnel_in_step(from_cte, isin=True)
        converted = self.expression == FunnelFilterTypes.converted
        to_cond = self.__funnel_in_step(to_cte, isin=converted)

        base_query = base_query.where(Criterion.all([from_cond, to_cond])).distinct()
        return base_query

    def __funnel_in_step(self, step_cte: str, isin: bool):
        base_query = self.query_class._base_query()
        field = Field(self.query_class.link_alias)
        subquery = base_query.from_(Table(step_cte)).select(self.query_class.link_alias).distinct()
        if isin:
            return field.isin(subquery)
        return field.isin(subquery).negate()
