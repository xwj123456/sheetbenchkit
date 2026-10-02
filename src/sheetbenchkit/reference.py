"""Bounded exact gold computation; never import this module from the grader."""

from __future__ import annotations

import re
from collections.abc import Mapping
from decimal import Decimal
from fractions import Fraction

from .models import (
    AggregateSumSpec,
    Binding,
    CaseError,
    Cell,
    CountSpec,
    ExpectedResult,
    RatioSpec,
    RootSumSpec,
    Table,
    ValidatedCase,
)

_NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE]([+-]?[0-9]+))?")
_LOADER_REASONS = frozenset(
    {
        "MISSING_SOURCE",
        "MISSING_FIELD",
        "AMBIGUOUS_FIELD",
        "UNSUPPORTED_CELL",
        "UNSUPPORTED_INPUT_LIMIT",
    }
)


class _Abstain(Exception):
    """Internal, explicit business refusal; infrastructure errors are not caught."""


def _number(cell: Cell) -> Fraction:
    if cell.kind == "blank":
        raise _Abstain("MISSING_VALUE")
    if cell.kind not in {"number", "text"}:
        raise _Abstain("UNSUPPORTED_CELL")
    raw = cell.value
    if not isinstance(raw, str):
        raise _Abstain("INVALID_VALUE")
    if not raw.strip():
        raise _Abstain("MISSING_VALUE")
    match = _NUMBER.fullmatch(raw)
    if match is None:
        raise _Abstain("INVALID_VALUE")
    if len(raw) > 80:
        raise _Abstain("UNSUPPORTED_INPUT_LIMIT")
    mantissa = re.split("[eE]", raw)[0].lstrip("+-")
    digits = mantissa.replace(".", "").lstrip("0") or "0"
    fraction_digits = len(mantissa.split(".", 1)[1]) if "." in mantissa else 0
    explicit_exponent = int(match.group(1) or "0")
    if len(digits) > 30 or fraction_digits > 30 or not -12 <= explicit_exponent <= 12:
        raise _Abstain("UNSUPPORTED_INPUT_LIMIT")
    # Decimal construction/as_tuple are exact and independent of context precision.
    parts = Decimal(raw).as_tuple()
    coefficient = 0
    for digit in parts.digits:
        coefficient = coefficient * 10 + digit
    if parts.sign:
        coefficient = -coefficient
    exponent = parts.exponent
    assert isinstance(exponent, int)  # The grammar excludes every nonfinite representation.
    return (
        Fraction(coefficient * 10**exponent)
        if exponent >= 0
        else Fraction(coefficient, 10 ** (-exponent))
    )


def _format(value: Fraction, places: int) -> str:
    """One signed HALF_UP step, using integer quotient and remainder only."""
    scale = 10**places
    quotient, remainder = divmod(abs(value.numerator) * scale, value.denominator)
    if 2 * remainder >= value.denominator:
        quotient += 1
    sign = "-" if value < 0 and quotient else ""
    whole, fractional = divmod(quotient, scale)
    result = sign + str(whole)
    if places:
        result += f".{fractional:0{places}d}"
    if len(result) > 128:
        raise CaseError("REFERENCE_OUTPUT_LIMIT", "formatted result exceeds 128 characters")
    return result


def _aggregates(case: ValidatedCase) -> tuple[AggregateSumSpec | CountSpec, ...]:
    metric = case.spec.metric
    return (metric.numerator, metric.denominator) if isinstance(metric, RatioSpec) else (metric,)


def _bindings(case: ValidatedCase) -> tuple[Binding, ...]:
    fields: dict[str, list[str]] = {}
    for aggregate in _aggregates(case):
        required = fields.setdefault(aggregate.source, [])
        if isinstance(aggregate, AggregateSumSpec) and aggregate.field not in required:
            required.append(aggregate.field)
        if aggregate.filter and aggregate.filter.field not in required:
            required.append(aggregate.filter.field)
    bindings = []
    for source_id, required in fields.items():
        source = case.spec.sources.get(source_id)
        if source is not None:
            bindings.append(
                Binding(
                    source_id,
                    source.file_id,
                    source.sheet,
                    source.header_row,
                    source.rows,
                    tuple(required),
                )
            )
    return tuple(bindings)


def _loader_limits(case: ValidatedCase) -> None:
    # Also consume unbound file:<id> limits: unsupported extra input is not silently ignored.
    for key in sorted(case.inputs.business_limits):
        limits = case.inputs.business_limits[key]
        if not isinstance(limits, tuple):
            raise CaseError("INVALID_BUSINESS_LIMIT", key)
        for limit in limits:
            if not isinstance(limit, Mapping) or set(limit) != {"reason", "details"}:
                raise CaseError("INVALID_BUSINESS_LIMIT", key)
            reason = limit["reason"]
            if not isinstance(reason, str) or reason not in _LOADER_REASONS:
                raise CaseError("INVALID_BUSINESS_LIMIT", key)
            raise _Abstain(reason)


def _field(table: Table, field: str) -> int:
    count = table.headers.count(field)
    if count == 0:
        raise _Abstain("MISSING_FIELD")
    if count > 1:
        raise _Abstain("AMBIGUOUS_FIELD")
    return table.headers.index(field)


def _aggregate(
    case: ValidatedCase,
    aggregate: AggregateSumSpec | CountSpec,
) -> tuple[Fraction, str]:
    source = case.spec.sources.get(aggregate.source)
    table = case.inputs.tables.get(aggregate.source)
    if source is None:
        raise CaseError("INVALID_REFERENCE_CASE", "unknown contract source")
    if table is None:
        raise _Abstain("MISSING_SOURCE")
    if len(table.headers) > 100 or len(table.rows) > 1000:
        raise CaseError("INVALID_REFERENCE_CASE", "loaded table exceeds loader envelope")
    if any(len(row) != len(table.headers) for row in table.rows):
        raise CaseError("INVALID_REFERENCE_CASE", "nonrectangular loaded table")
    numeric_index = None
    unit = "count"
    if isinstance(aggregate, AggregateSumSpec):
        numeric_index = _field(table, aggregate.field)
        unit = aggregate.unit
        label = (
            case.inputs.unit_labels.get(source.file_id, {})
            .get(source.sheet, {})
            .get(aggregate.field)
        )
        if label != unit:
            raise _Abstain("UNSUPPORTED_UNIT_CONVERSION")
    filter_index = _field(table, aggregate.filter.field) if aggregate.filter else None
    selected = []
    for row in table.rows:
        if filter_index is not None:
            cell = row[filter_index]
            if cell.kind == "blank":
                continue
            if cell.kind != "text" or not isinstance(cell.value, str):
                raise _Abstain("INVALID_FILTER_VALUE")
            if not cell.value.strip():
                continue
            assert aggregate.filter is not None
            if cell.value != aggregate.filter.eq:
                continue
        selected.append(row)
    if numeric_index is None:
        return Fraction(len(selected)), unit
    total = Fraction(0)
    for row in selected:
        total += _number(row[numeric_index])
    return total, unit


def evaluate_reference(case: ValidatedCase) -> ExpectedResult:
    """Evaluate a validated contract with immutable T2 inputs for gold generation."""
    metric = case.spec.metric
    bindings = _bindings(case)
    if isinstance(metric, (RootSumSpec, RatioSpec)):
        if (
            type(metric.places) is not int
            or not 0 <= metric.places <= 12
            or metric.rounding != "HALF_UP"
        ):
            raise CaseError("INVALID_REFERENCE_CASE", "places/rounding")
    try:
        _loader_limits(case)
        if isinstance(metric, RatioSpec):
            numerator, numerator_unit = _aggregate(case, metric.numerator)
            denominator, denominator_unit = _aggregate(case, metric.denominator)
            if numerator_unit != denominator_unit:
                raise _Abstain("UNSUPPORTED_UNIT_CONVERSION")
            if denominator == 0:
                raise _Abstain("ZERO_DENOMINATOR")
            value, unit, places = numerator / denominator, "ratio", metric.places
        else:
            value, unit = _aggregate(case, metric)
            places = metric.places if isinstance(metric, RootSumSpec) else 0
        return ExpectedResult("VALUE", _format(value, places), unit, None, bindings)
    except _Abstain as error:
        return ExpectedResult("ABSTAIN", None, None, str(error), bindings)
