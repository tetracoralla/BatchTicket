from __future__ import annotations

from typing import Annotated, Any, Literal, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    StringConstraints,
    TypeAdapter,
    model_validator,
)

from .limits import Limits

Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_-]*$")]
NonEmptyString = Annotated[str, StringConstraints(min_length=1)]
DataFormat = Literal["json", "jsonl", "csv", "tsv", "yaml", "parquet"]
DataKind = Literal["tree", "table"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, strict=True)


class PathSource(StrictModel):
    path: NonEmptyString
    format: DataFormat | None = None
    select: NonEmptyString | None = None
    kind: DataKind | None = None


class InlineSource(StrictModel):
    inline: Any
    format: DataFormat | None = None
    select: NonEmptyString | None = None
    kind: DataKind | None = None


Source: TypeAlias = PathSource | InlineSource


class LimitsModel(StrictModel):
    max_input_bytes: Annotated[int, Field(gt=0, le=Limits.HARD_MAX_INPUT_BYTES)] | None = None
    max_sources: Annotated[int, Field(gt=0, le=Limits.HARD_MAX_SOURCES)] | None = None
    max_rows: Annotated[int, Field(gt=0, le=Limits.HARD_MAX_ROWS)] | None = None
    max_items: Annotated[int, Field(gt=0, le=Limits.HARD_MAX_ITEMS)] | None = None
    max_depth: Annotated[int, Field(gt=0, le=Limits.HARD_MAX_DEPTH)] | None = None
    max_steps: Annotated[int, Field(gt=0, le=Limits.HARD_MAX_STEPS)] | None = None
    max_memory_mb: Annotated[int, Field(gt=0, le=Limits.HARD_MAX_MEMORY_MB)] | None = None
    max_temp_bytes: Annotated[int, Field(gt=0, le=Limits.HARD_MAX_TEMP_BYTES)] | None = None
    timeout_ms: Annotated[int, Field(gt=0, le=Limits.HARD_MAX_TIMEOUT_MS)] | None = None
    max_response_bytes: (
        Annotated[
            int,
            Field(ge=Limits.MIN_RESPONSE_BYTES, le=Limits.HARD_MAX_RESPONSE_BYTES),
        ]
        | None
    ) = None


class ReturnPolicy(StrictModel):
    mode: Literal["auto", "inline", "summary", "reference"] = "auto"
    sample_rows: Annotated[int, Field(ge=0, le=Limits.HARD_MAX_SAMPLE_ROWS)] = 5
    max_inline_bytes: Annotated[int, Field(gt=0, le=Limits.HARD_MAX_INLINE_BYTES)] = 64 * 1024


class OutputSpec(StrictModel):
    path: NonEmptyString
    format: DataFormat | None = None
    overwrite: bool = False


class Condition(StrictModel):
    field: NonEmptyString | None = None
    eq: Any | None = None
    ne: Any | None = None
    gt: Any | None = None
    gte: Any | None = None
    lt: Any | None = None
    lte: Any | None = None
    in_: list[Any] | None = Field(default=None, alias="in", min_length=1, max_length=10_000)
    not_in: list[Any] | None = Field(default=None, min_length=1, max_length=10_000)
    contains: str | None = None
    starts_with: str | None = None
    ends_with: str | None = None
    is_null: Literal[True] | None = None
    not_null: Literal[True] | None = None
    all: list[Condition] | None = Field(default=None, min_length=1)
    any: list[Condition] | None = Field(default=None, min_length=1)
    not_: Condition | None = Field(default=None, alias="not")

    @model_validator(mode="after")
    def one_shape(self) -> Condition:
        groups = [self.all is not None, self.any is not None, self.not_ is not None]
        # eq/ne with JSON null cannot be distinguished from an omitted Optional field by value.
        present = self.model_fields_set
        comparison_names = {
            "eq",
            "ne",
            "gt",
            "gte",
            "lt",
            "lte",
            "in_",
            "not_in",
            "contains",
            "starts_with",
            "ends_with",
            "is_null",
            "not_null",
        }
        comparison_count = len(present & comparison_names)
        composite_count = sum(groups)
        if composite_count:
            if composite_count != 1 or self.field is not None or comparison_count:
                raise ValueError("a composite condition cannot contain sibling keys")
        elif self.field is None or comparison_count != 1:
            raise ValueError("a condition leaf requires field and exactly one comparison")
        return self


class Expression(StrictModel):
    field: NonEmptyString | None = None
    value: Any | None = None
    add: list[Expression] | None = Field(default=None, min_length=2)
    subtract: list[Expression] | None = Field(default=None, min_length=2)
    multiply: list[Expression] | None = Field(default=None, min_length=2)
    divide: list[Expression] | None = Field(default=None, min_length=2, max_length=2)
    concat: list[Expression] | None = Field(default=None, min_length=1)
    coalesce: list[Expression] | None = Field(default=None, min_length=1)
    lower: Expression | None = None
    upper: Expression | None = None
    substring: Annotated[list[Expression | int], Field(min_length=2, max_length=3)] | None = None
    round: (
        Expression | Annotated[list[Expression | int], Field(min_length=1, max_length=2)] | None
    ) = None

    @model_validator(mode="after")
    def exactly_one_operation(self) -> Expression:
        if len(self.model_fields_set) != 1:
            raise ValueError("expression requires exactly one operation")
        if self.substring is not None and (
            not isinstance(self.substring[0], Expression)
            or not all(
                isinstance(item, int) and not isinstance(item, bool) for item in self.substring[1:]
            )
        ):
            raise ValueError("substring requires [expression, start, optional length]")
        if isinstance(self.round, list) and (
            not isinstance(self.round[0], Expression)
            or (
                len(self.round) == 2
                and (not isinstance(self.round[1], int) or isinstance(self.round[1], bool))
            )
        ):
            raise ValueError("round requires [expression, optional digits]")
        return self


class FieldAlias(StrictModel):
    field: NonEmptyString
    as_: NonEmptyString | None = Field(default=None, alias="as")


FieldDescriptor: TypeAlias = NonEmptyString | FieldAlias


class SortDescriptor(StrictModel):
    field: NonEmptyString
    direction: Literal["asc", "desc"] = "asc"
    nulls: Literal["first", "last"] = "last"


class Aggregate(StrictModel):
    op: Literal["sum", "avg", "min", "max", "count", "count_distinct", "first", "last"]
    field: NonEmptyString | None = None
    as_: NonEmptyString | None = Field(default=None, alias="as")

    @model_validator(mode="after")
    def field_for_non_count(self) -> Aggregate:
        if self.op != "count" and self.field is None:
            raise ValueError("aggregate requires field unless op is count")
        return self


class StepBase(StrictModel):
    id: Identifier | None = None
    source: NonEmptyString | None = None


class FilterStep(StepBase):
    op: Literal["filter"]
    where: Condition


class SelectStep(StepBase):
    op: Literal["select"]
    fields: Annotated[list[FieldDescriptor], Field(min_length=1)]


class DropStep(StepBase):
    op: Literal["drop"]
    fields: Annotated[list[NonEmptyString], Field(min_length=1)]


class RenameStep(StepBase):
    op: Literal["rename"]
    fields: Annotated[dict[NonEmptyString, NonEmptyString], Field(min_length=1)]


class SortStep(StepBase):
    op: Literal["sort"]
    by: Annotated[list[NonEmptyString | SortDescriptor], Field(min_length=1)]


class LimitStep(StepBase):
    op: Literal["limit"]
    count: Annotated[int, Field(ge=0)]
    offset: Annotated[int, Field(ge=0)] = 0


class DedupeStep(StepBase):
    op: Literal["dedupe"]
    fields: Annotated[list[NonEmptyString], Field(min_length=1)]
    keep: Literal["first", "last"] = "first"


class CastStep(StepBase):
    op: Literal["cast"]
    fields: Annotated[dict[NonEmptyString, NonEmptyString], Field(min_length=1)] | None = None
    field: NonEmptyString | None = None
    to: NonEmptyString | None = None

    @model_validator(mode="after")
    def one_cast_shape(self) -> CastStep:
        map_form = self.fields is not None and self.field is None and self.to is None
        single_form = self.fields is None and self.field is not None and self.to is not None
        if not (map_form or single_form):
            raise ValueError("cast requires either fields or field/to")
        return self


class DeriveStep(StepBase):
    op: Literal["derive"]
    field: NonEmptyString
    expr: Expression


class ExplodeStep(StepBase):
    op: Literal["explode"]
    field: NonEmptyString


class JoinStep(StrictModel):
    id: Identifier | None = None
    op: Literal["join"]
    left: NonEmptyString
    right: NonEmptyString
    type: Literal["inner", "left", "right", "full"] = "inner"
    on: Annotated[
        list[Annotated[list[NonEmptyString], Field(min_length=2, max_length=2)]],
        Field(min_length=1),
    ]
    right_prefix: str = "right_"


class GroupStep(StepBase):
    op: Literal["group"]
    by: list[FieldDescriptor] = Field(default_factory=list)
    aggregates: Annotated[list[Aggregate], Field(min_length=1)]


class PivotStep(StepBase):
    op: Literal["pivot"]
    by: list[FieldDescriptor] = Field(default_factory=list)
    field: NonEmptyString
    value: NonEmptyString
    values: Annotated[list[Any], Field(min_length=1, max_length=100)]
    aggregate: Literal["sum", "avg", "min", "max", "count"] = "sum"
    aliases: dict[str, NonEmptyString] = Field(default_factory=dict)


class UnpivotStep(StepBase):
    op: Literal["unpivot"]
    fields: Annotated[list[NonEmptyString], Field(min_length=1)]
    keep: list[NonEmptyString] | None = None
    name_field: NonEmptyString = "field"
    value_field: NonEmptyString = "value"


class FlattenStep(StepBase):
    op: Literal["flatten"]
    separator: Annotated[str, StringConstraints(pattern=r"^[^\\]{1,4}$")] = "_"


class UnflattenStep(StepBase):
    op: Literal["unflatten"]
    separator: Annotated[str, StringConstraints(pattern=r"^[^\\]{1,4}$")] = "_"


class SetStep(StepBase):
    op: Literal["set"]
    path: NonEmptyString
    value: Any
    create: bool = False


class DeleteStep(StepBase):
    op: Literal["delete"]
    path: NonEmptyString


class MergeStep(StepBase):
    op: Literal["merge"]
    value: dict[str, Any]
    deep: bool = True


Step: TypeAlias = Annotated[
    FilterStep
    | SelectStep
    | DropStep
    | RenameStep
    | SortStep
    | LimitStep
    | DedupeStep
    | CastStep
    | DeriveStep
    | ExplodeStep
    | JoinStep
    | GroupStep
    | PivotStep
    | UnpivotStep
    | FlattenStep
    | UnflattenStep
    | SetStep
    | DeleteStep
    | MergeStep,
    Field(discriminator="op"),
]


class AssertionBase(StrictModel):
    id: NonEmptyString | None = None


class FieldExistsAssertion(AssertionBase):
    type: Literal["field_exists"]
    field: NonEmptyString


class NotNullAssertion(AssertionBase):
    type: Literal["not_null"]
    field: NonEmptyString


class UniqueAssertion(AssertionBase):
    type: Literal["unique"]
    field: NonEmptyString | None = None
    fields: Annotated[list[NonEmptyString], Field(min_length=1)] | None = None

    @model_validator(mode="after")
    def one_unique_shape(self) -> UniqueAssertion:
        if (self.field is None) == (self.fields is None):
            raise ValueError("unique requires exactly one of field or fields")
        return self


class RowCountAssertion(AssertionBase):
    type: Literal["row_count"]
    eq: Annotated[int, Field(ge=0)] | None = None
    min: Annotated[int, Field(ge=0)] | None = None
    max: Annotated[int, Field(ge=0)] | None = None

    @model_validator(mode="after")
    def has_bound(self) -> RowCountAssertion:
        if not (self.model_fields_set & {"eq", "min", "max"}):
            raise ValueError("row_count requires eq, min, or max")
        return self


class TypeAssertion(AssertionBase):
    type: Literal["type"]
    field: NonEmptyString
    is_: NonEmptyString = Field(alias="is")


Assertion: TypeAlias = Annotated[
    FieldExistsAssertion | NotNullAssertion | UniqueAssertion | RowCountAssertion | TypeAssertion,
    Field(discriminator="type"),
]


class TransformationPlan(StrictModel):
    version: Literal["1"]
    sources: dict[NonEmptyString, Source]
    steps: Annotated[list[Step], Field(min_length=1, max_length=100)]
    assertions: list[Assertion] | None = Field(default=None, alias="assert")
    schema_: dict[str, Any] | None = Field(default=None, alias="schema")
    output: OutputSpec | None = None
    return_: ReturnPolicy = Field(default_factory=ReturnPolicy, alias="return")
    limits: LimitsModel = Field(default_factory=LimitsModel)
    dry_run: bool = False

    @model_validator(mode="after")
    def has_sources(self) -> TransformationPlan:
        if not self.sources:
            raise ValueError("sources must not be empty")
        return self


class InspectToolInput(StrictModel):
    source: Source
    sample_rows: Annotated[int, Field(ge=0, le=Limits.HARD_MAX_SAMPLE_ROWS)] = 5
    limits: LimitsModel | None = None
    workspace: str | None = None


class TransformToolInput(StrictModel):
    plan: TransformationPlan
    dry_run: bool | None = None
    workspace: str | None = None


class ValidateToolInput(StrictModel):
    source: Source
    schema_: dict[str, Any] | None = Field(default=None, alias="schema")
    assertions: list[Assertion] | None = None
    sample_rows: Annotated[int, Field(ge=0, le=Limits.HARD_MAX_SAMPLE_ROWS)] = 5
    limits: LimitsModel | None = None
    workspace: str | None = None


class DiffToolInput(StrictModel):
    left: Source
    right: Source
    key_fields: list[NonEmptyString] | None = None
    sample_rows: Annotated[int, Field(ge=0, le=Limits.HARD_MAX_SAMPLE_ROWS)] = 5
    limits: LimitsModel | None = None
    workspace: str | None = None


class SourceContract(RootModel[Source]):
    pass


Condition.model_rebuild()
Expression.model_rebuild()

SOURCE_ADAPTER = TypeAdapter(Source)
ASSERTIONS_ADAPTER = TypeAdapter(list[Assertion])
