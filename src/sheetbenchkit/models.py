"""Shared immutable types. Persisted shapes are validated by packaged schemas."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields
from dataclasses import field as dataclass_field
from pathlib import Path
from types import MappingProxyType
from typing import Any

from jsonschema import Draft202012Validator


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


# These are exactly the bundled schema integer slots, not metadata or cell values.
_INTEGER_FIELDS = {
    "SourceSpec": ("header_row", "rows"),
    "MutationRecipe": ("header_row", "rows", "row"),
    "RootSumSpec": ("places",),
    "RatioSpec": ("places",),
    "Binding": ("header_row", "rows"),
    "RunSpec": ("repeats",),
    "Observation": ("attempt",),
}
_INTEGER_TYPE = Draft202012Validator({"type": "integer"})


def _schema_integer(value: Any) -> Any:
    # Validate the slot's mathematical-integer type before normalization. Full
    # document/range validation remains at the public consumer trust boundaries.
    # Invalid values stay intact so those boundaries return their controlled error.
    if type(value) is float and _INTEGER_TYPE.is_valid(value):
        return int(value)
    return value


class _Model:
    def __post_init__(self) -> None:
        for field in fields(self):  # type: ignore[arg-type]  # Only dataclass subclasses call this.
            value = getattr(self, field.name)
            if field.name in _INTEGER_FIELDS.get(type(self).__name__, ()):
                if field.name == "rows" and isinstance(value, (list, tuple)):
                    value = tuple(_schema_integer(item) for item in value)
                else:
                    value = _schema_integer(value)
            object.__setattr__(self, field.name, _freeze(value))


class CaseError(Exception):
    """Invalid case/infrastructure; distinct from a legitimate business ABSTAIN."""

    def __init__(self, reason: str, details: Any = None):
        self.reason = reason
        self.details = _freeze(details)
        super().__init__(reason if details is None else f"{reason}: {details}")


@dataclass(frozen=True)
class SourceSpec(_Model):
    file_id: str
    sheet: str
    header_row: int
    rows: tuple[int, int]


@dataclass(frozen=True)
class FilterSpec(_Model):
    field: str
    eq: str


@dataclass(frozen=True)
class CountSpec(_Model):
    op: str
    source: str
    filter: FilterSpec | None = None


@dataclass(frozen=True)
class AggregateSumSpec(_Model):
    op: str
    source: str
    field: str
    unit: str
    filter: FilterSpec | None = None


@dataclass(frozen=True, kw_only=True)
class RootSumSpec(AggregateSumSpec):
    places: int
    rounding: str


@dataclass(frozen=True)
class RatioSpec(_Model):
    op: str
    numerator: AggregateSumSpec | CountSpec
    denominator: AggregateSumSpec | CountSpec
    places: int
    rounding: str


@dataclass(frozen=True)
class RuleSpec(_Model):
    schema_version: str
    metric_id: str
    sources: Mapping[str, SourceSpec]
    metric: RootSumSpec | CountSpec | RatioSpec


@dataclass(frozen=True)
class InputEntry(_Model):
    file_id: str
    relative_path: str
    format: str
    sha256: str | None
    expected_missing: bool


@dataclass(frozen=True)
class MutationRecipe(_Model):
    kind: str
    file_id: str
    sheet: str
    header_row: int
    rows: tuple[int, int]
    field: str | None
    row: int | None
    numeric_delta: str | None
    text_replacement: str | None


@dataclass(frozen=True)
class CaseManifest(_Model):
    schema_version: str
    case_id: str
    category: str
    inputs: tuple[InputEntry, ...]
    unit_labels: Mapping[str, Mapping[str, Mapping[str, str]]]
    intentional_boundaries: tuple[str, ...]
    business_confirmation: Mapping[str, Any]
    variant_recipes: tuple[MutationRecipe, ...]


@dataclass(frozen=True)
class Cell(_Model):
    kind: str
    value: Any
    coordinate: str


@dataclass(frozen=True)
class Table(_Model):
    headers: tuple[str, ...]
    rows: tuple[tuple[Cell, ...], ...]


@dataclass(frozen=True)
class InputBundle(_Model):
    tables: Mapping[str, Table]
    snapshots: Mapping[str, bytes]
    unit_labels: Mapping[str, Mapping[str, Mapping[str, str]]]
    business_limits: Mapping[str, Any]
    # Loader accounting survives semantic refusals that omit usable Tables.
    selected_read_cells: Mapping[str, int] = dataclass_field(default_factory=dict)


@dataclass(frozen=True)
class ValidatedCase(_Model):
    spec: RuleSpec
    manifest: CaseManifest
    inputs: InputBundle
    task: Mapping[str, Any]


@dataclass(frozen=True)
class Binding(_Model):
    source: str
    file_id: str
    sheet: str
    header_row: int
    rows: tuple[int, int]
    fields: tuple[str, ...]


@dataclass(frozen=True)
class ExpectedResult(_Model):
    status: str
    value: str | None
    unit: str | None
    reason: str | None
    bindings: tuple[Binding, ...]


@dataclass(frozen=True)
class AgentResult(_Model):
    schema_version: str
    case_id: str
    metric_id: str
    status: str
    value: str | None
    unit: str | None
    reason: str | None
    bindings: tuple[Binding, ...]


@dataclass(frozen=True)
class CandidateCase(_Model):
    validated: ValidatedCase
    expected: ExpectedResult
    origin: str
    confirmation: Mapping[str, Any]
    evidence: Mapping[str, Any]


@dataclass(frozen=True)
class GoldRecord(_Model):
    case_id: str
    contract_sha256: str
    manifest_sha256: str
    expected: ExpectedResult
    origin: str
    confirmation: Mapping[str, Any]
    evidence: Mapping[str, Any]


@dataclass(frozen=True)
class FrozenCase(_Model):
    validated: ValidatedCase
    gold: GoldRecord
    content_sha256: str


@dataclass(frozen=True)
class CaseRef(_Model):
    case_id: str
    contract_path: str
    contract_sha256: str
    manifest_path: str
    manifest_sha256: str
    task_path: str
    task_sha256: str
    gold_path: str
    gold_sha256: str


@dataclass(frozen=True)
class FamilySpec(_Model):
    family_id: str
    base: str
    target: str
    distractor: str
    mutation_locations: Mapping[str, Any]


@dataclass(frozen=True)
class Suite(_Model):
    schema_version: str
    suite_id: str
    cases: tuple[CaseRef, ...]
    families: tuple[FamilySpec, ...]
    content_sha256: str


@dataclass(frozen=True)
class RunSpec(_Model):
    config_id: str
    repeats: int


@dataclass(frozen=True)
class Observation(_Model):
    case_id: str
    attempt: int
    config_id: str
    execution_status: str
    raw_stdout: str
    raw_stderr: str
    usage: Mapping[str, Any] | None
    runtime_metadata: Mapping[str, Any]


@dataclass(frozen=True)
class ObservationBatch(_Model):
    schema_version: str
    runs: tuple[RunSpec, ...]
    observations: tuple[Observation, ...]


@dataclass(frozen=True)
class CaseGrade(_Model):
    case_id: str
    metric_id: str
    config_id: str
    attempt: int
    category: str
    expected_status: str
    verdict: str
    reason: str | None
    value_check: Any
    binding_check: Any
    evidence: Mapping[str, Any]


@dataclass(frozen=True)
class FamilyGrade(_Model):
    family_id: str
    attempt: int
    config_id: str
    verdict: str
    reason: str | None


@dataclass(frozen=True)
class SuiteReport(_Model):
    suite_id: str
    runs: tuple[RunSpec, ...]
    case_grades: tuple[CaseGrade, ...]
    family_grades: tuple[FamilyGrade, ...]
    planned: int
    attempted: int
    observed_count: int
    missing_count: int
    status_counts: Mapping[str, int]
    per_config: Mapping[str, Any]
    strata: Mapping[str, Any]
    valid: bool


@dataclass(frozen=True)
class TaskEnvelope(_Model):
    schema_version: str
    case_id: str
    metric_id: str
    task: Mapping[str, Any]
    inputs: tuple[InputEntry, ...]
    output_protocol: Mapping[str, Any]
    source_ids: tuple[str, ...]


@dataclass(frozen=True)
class ReportPaths(_Model):
    json_path: Path
    html_path: Path
