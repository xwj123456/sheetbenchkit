"""Public reference behavior, with literal or independent integer arithmetic gold."""

from __future__ import annotations

import importlib
import importlib.util
from dataclasses import replace
from decimal import Inexact, localcontext
from fractions import Fraction
from typing import Any

import pytest

from sheetbenchkit.models import (
    AggregateSumSpec,
    Binding,
    CaseError,
    CaseManifest,
    Cell,
    CountSpec,
    ExpectedResult,
    FilterSpec,
    InputBundle,
    RatioSpec,
    RootSumSpec,
    RuleSpec,
    SourceSpec,
    Table,
    ValidatedCase,
)


def reference_result(case: ValidatedCase) -> ExpectedResult:
    assert importlib.util.find_spec("sheetbenchkit.reference"), "Missing T3 reference evaluator"
    return importlib.import_module("sheetbenchkit.reference").evaluate_reference(case)


def reference_value(case: ValidatedCase) -> str:
    result = reference_result(case)
    assert result.status == "VALUE", result
    assert result.reason is None
    assert result.value is not None
    return result.value


def cell(value: Any, kind: str | None = None) -> Cell:
    return Cell(
        kind or ("blank" if value is None else "text"),
        str(value) if kind is None and value is not None else value,
        "A2",
    )


def build_case(
    metric: RootSumSpec | CountSpec | RatioSpec,
    tables: dict[str, Table],
) -> ValidatedCase:
    sources = {name: SourceSpec(name, "@csv", 1, (2, 1001)) for name in tables}
    labels = {name: {"@csv": {"amount": "CNY", "other": "CNY"}} for name in tables}
    manifest = CaseManifest("1", "exact", "custom", (), labels, (), {}, ())
    return ValidatedCase(
        RuleSpec("1", "metric", sources, metric),
        manifest,
        InputBundle(tables, {}, labels, {}),
        {"instruction": "confirmed fixture"},
    )


def sum_case(values: list[Any], places: int = 0) -> ValidatedCase:
    return build_case(
        RootSumSpec("sum", "s", "amount", "CNY", places=places, rounding="HALF_UP"),
        {"s": Table(("amount",), tuple((cell(v),) for v in values))},
    )


def ratio_case(numerator: list[Any], denominator: list[Any], places: int = 4) -> ValidatedCase:
    return build_case(
        RatioSpec(
            "ratio",
            AggregateSumSpec("sum", "n", "amount", "CNY"),
            AggregateSumSpec("sum", "d", "amount", "CNY"),
            places,
            "HALF_UP",
        ),
        {
            "n": Table(("amount",), tuple((cell(v),) for v in numerator)),
            "d": Table(("amount",), tuple((cell(v),) for v in denominator)),
        },
    )


def filtered_case(values: list[tuple[Any, Any]], eq: str = "yes") -> ValidatedCase:
    base = sum_case([])
    metric = replace(base.spec.metric, filter=FilterSpec("tag", eq))
    table = Table(("amount", "tag"), tuple((cell(a), cell(t)) for a, t in values))
    return replace(
        base,
        spec=replace(base.spec, metric=metric),
        inputs=replace(base.inputs, tables={"s": table}),
    )


@pytest.mark.parametrize("n,d,want", [([1], [32], "0.0313"), ([90, 1], [100, 10], "0.8273")])
def test_ratios_divide_totals_exactly(n, d, want):
    assert reference_value(ratio_case(n, d)) == want


def test_sum_large_coefficients_without_decimal_context_rounding():
    with localcontext() as context:
        context.prec = 2
        context.traps[Inexact] = True
        assert reference_value(sum_case(["9" * 30] * 1000)) == str((10**30 - 1) * 1000)


@pytest.mark.parametrize("values,want", [([0, "0.00"], "0"), ([1, -1], "0"), ([], "0")])
def test_valid_zero_and_explicitly_empty_rows(values, want):
    assert reference_value(sum_case(values)) == want


def test_empty_selected_sum():
    assert reference_value(filtered_case([(None, "no"), ("oops", None)])) == "0"


@pytest.mark.parametrize("value", [None, "", " ", "\t"])
def test_selected_missing_is_not_zero(value):
    assert reference_result(sum_case([value, 5])).reason == "MISSING_VALUE"


@pytest.mark.parametrize(
    "raw,want",
    [
        ("-1.005", "-1.01"),
        ("1.005", "1.01"),
        ("-0.005", "-0.01"),
        ("-0.0049", "0.00"),
        ("2.675", "2.68"),
        ("-.5", "-0.50"),
        ("+01.2", "1.20"),
        ("1.", "1.00"),
        ("1.25e+2", "125.00"),
    ],
)
def test_signed_half_up_and_strict_finite_decimal(raw, want):
    assert reference_value(sum_case([raw], 2)) == want


def test_only_final_sum_rounds():
    assert reference_value(sum_case(["0.004", "0.004"], 2)) == "0.01"


def test_ratio_children_do_not_round():
    assert reference_value(ratio_case(["0.004", "0.004"], ["0.008"], 2)) == "1.00"


@pytest.mark.parametrize(
    "n,d,want", [([-1], [32], "-0.0313"), ([1], [-32], "-0.0313"), ([-1], [-32], "0.0313")]
)
def test_signed_ratio_half_up(n, d, want):
    assert reference_value(ratio_case(n, d)) == want


@pytest.mark.parametrize("places", range(13))
def test_all_output_places(places):
    assert reference_value(sum_case(["2"], places)) == "2" + ("." + "0" * places if places else "")


@pytest.mark.parametrize("values", [[0], [1, -1], []])
def test_zero_denominator_is_abstain(values):
    result = reference_result(ratio_case([1], values))
    assert (result.status, result.value, result.reason) == ("ABSTAIN", None, "ZERO_DENOMINATOR")


@pytest.mark.parametrize(
    "raw",
    [
        "NaN",
        "Infinity",
        "-Infinity",
        "1,000",
        "12%",
        "1_000",
        " 1",
        "1 ",
        "1\n",
        "0x10",
        "１",
        "١",
        ".",
        "1e",
        "oops",
    ],
)
def test_invalid_numeric_text(raw):
    assert reference_result(sum_case([raw])).reason == "INVALID_VALUE"


@pytest.mark.parametrize(
    "kind,value",
    [
        ("bool", True),
        ("date", "2026-10-01"),
        ("formula", "=1+1"),
        ("formula", {"t": "dataTable", "ref": "A2"}),
        ("error", "#N/A"),
    ],
)
def test_target_cell_kind_rejects_non_numbers(kind, value):
    case = sum_case([0])
    case = replace(
        case, inputs=replace(case.inputs, tables={"s": Table(("amount",), ((cell(value, kind),),))})
    )
    assert reference_result(case).reason == "UNSUPPORTED_CELL"


def test_numeric_kind_uses_finite_loader_representation():
    case = sum_case([0], 2)
    case = replace(
        case,
        inputs=replace(
            case.inputs, tables={"s": Table(("amount",), ((cell("1.005", "number"),),))}
        ),
    )
    assert reference_value(case) == "1.01"


def test_filter_uses_raw_text_without_normalization():
    assert (
        reference_value(
            filtered_case([(1, "yes"), (100, " yes"), (100, "yes "), (100, "YES"), (100, None)])
        )
        == "1"
    )


def test_blank_filter_never_matches_even_blank_eq():
    assert reference_value(filtered_case([(100, None), (100, " "), (100, "")], "")) == "0"


@pytest.mark.parametrize(
    "kind,value",
    [
        ("number", "1"),
        ("bool", True),
        ("date", "2026-01-01"),
        ("formula", '="no"'),
        ("formula", {"t": "dataTable"}),
        ("error", "#N/A"),
    ],
)
def test_nontext_filter_rejects_instead_of_excluding(kind, value):
    case = filtered_case([(1, "no")])
    case = replace(
        case,
        inputs=replace(
            case.inputs, tables={"s": Table(("amount", "tag"), ((cell(1), cell(value, kind)),))}
        ),
    )
    assert reference_result(case).reason == "INVALID_FILTER_VALUE"


def test_count_counts_selected_rows_without_numeric_values():
    case = build_case(
        CountSpec("count", "s", FilterSpec("tag", "yes")),
        {
            "s": Table(
                ("tag", "unrelated"),
                (
                    (cell("yes"), cell(None)),
                    (cell("yes"), cell("=1", "formula")),
                    (cell("no"), cell("oops")),
                ),
            )
        },
    )
    result = reference_result(case)
    assert (result.value, result.unit) == ("2", "count")
    assert result.bindings == (Binding("s", "s", "@csv", 1, (2, 1001), ("tag",)),)


def test_count_empty_rows_has_value_zero_and_no_field_binding():
    case = build_case(CountSpec("count", "s"), {"s": Table((), ())})
    result = reference_result(case)
    assert (result.value, result.unit, result.bindings[0].fields) == ("0", "count", ())


def test_bindings_merge_fields_for_same_source_in_first_use_order():
    case = build_case(
        RatioSpec(
            "ratio",
            AggregateSumSpec("sum", "s", "amount", "CNY", FilterSpec("tag", "yes")),
            AggregateSumSpec("sum", "s", "other", "CNY", FilterSpec("tag", "yes")),
            2,
            "HALF_UP",
        ),
        {"s": Table(("tag", "other", "amount"), ((cell("yes"), cell(2), cell(1)),))},
    )
    result = reference_result(case)
    assert result.value == "0.50"
    assert result.bindings == (Binding("s", "s", "@csv", 1, (2, 1001), ("amount", "tag", "other")),)


@pytest.mark.parametrize(
    "reason",
    [
        "MISSING_SOURCE",
        "MISSING_FIELD",
        "AMBIGUOUS_FIELD",
        "UNSUPPORTED_CELL",
        "UNSUPPORTED_INPUT_LIMIT",
    ],
)
@pytest.mark.parametrize("key", ["s", "file:extra"])
def test_loader_business_limits_are_explicitly_consumed(reason, key):
    case = sum_case([])
    case = replace(
        case,
        inputs=replace(
            case.inputs, tables={}, business_limits={key: ({"reason": reason, "details": {}},)}
        ),
    )
    result = reference_result(case)
    assert (result.status, result.value, result.reason) == ("ABSTAIN", None, reason)


def test_missing_table_without_loader_limit_cannot_be_empty_value():
    case = sum_case([])
    case = replace(case, inputs=replace(case.inputs, tables={}))
    assert reference_result(case).reason == "MISSING_SOURCE"


@pytest.mark.parametrize(
    "headers,reason", [((), "MISSING_FIELD"), (("amount", "amount"), "AMBIGUOUS_FIELD")]
)
def test_field_resolution_rejects_missing_or_ambiguous(headers, reason):
    case = sum_case([])
    case = replace(case, inputs=replace(case.inputs, tables={"s": Table(headers, ())}))
    assert reference_result(case).reason == reason


@pytest.mark.parametrize("labels", [{}, {"s": {"@csv": {"amount": "USD"}}}])
def test_sum_unit_requires_exact_declared_input_label(labels):
    case = sum_case([1])
    case = replace(case, inputs=replace(case.inputs, unit_labels=labels))
    assert reference_result(case).reason == "UNSUPPORTED_UNIT_CONVERSION"


@pytest.mark.parametrize(
    "raw,want",
    [
        ("0" * 79 + "1", "1"),
        ("9" * 30, "9" * 30),
        ("0." + "0" * 29 + "1", "0"),
        ("1e12", "1000000000000"),
        ("1e-12", "0"),
        ("9" * 30 + "e12", "9" * 30 + "0" * 12),
    ],
)
def test_numeric_input_limits_accept_exact_boundaries(raw, want):
    assert reference_value(sum_case([raw])) == want


@pytest.mark.parametrize(
    "raw",
    [
        "0" * 80 + "1",
        "9" * 31,
        "0." + "0" * 30 + "1",
        "1e13",
        "1e-13",
        "0e9999999999999999999999999",
    ],
)
def test_numeric_input_limits_refuse_excess(raw):
    assert reference_result(sum_case([raw])).reason == "UNSUPPORTED_INPUT_LIMIT"


def test_large_ratio_output_not_subject_to_input_coefficient_limit():
    numerator = ["9" * 30 + "e12"] * 1000
    # 1000*(10**30-1)*10**12 / 10**-30 = (10**30-1)*10**45.
    result = reference_result(ratio_case(numerator, ["0." + "0" * 29 + "1"], 12))
    assert result.value == "9" * 30 + "0" * 45 + "." + "0" * 12
    assert result.value is not None and 30 < len(result.value) < 128


@pytest.mark.parametrize("sign,length", [("", 100), ("-", 101)])
def test_extreme_valid_ratio_stays_within_output_envelope(sign, length):
    # Minimum positive permitted value is 10**-42: 30 fractional places then e-12.
    case = ratio_case([sign + "9" * 30 + "e12"] * 1000, ["0." + "0" * 29 + "1e-12"], 12)
    result = reference_result(case)
    assert result.value == sign + "9" * 30 + "0" * 57 + "." + "0" * 12
    assert result.value is not None and len(result.value) == length


def test_ratio_unit_is_ratio_for_same_unit_counts():
    case = build_case(
        RatioSpec("ratio", CountSpec("count", "n"), CountSpec("count", "d"), 4, "HALF_UP"),
        {
            "n": Table(("irrelevant",), ((cell(None),),)),
            "d": Table(("irrelevant",), tuple((cell(None),) for _ in range(32))),
        },
    )
    result = reference_result(case)
    assert (result.value, result.unit) == ("0.0313", "ratio")


def test_ratio_rejects_different_child_units_even_if_labels_match():
    case = ratio_case([1], [2])
    assert isinstance(case.spec.metric, RatioSpec)
    denominator = replace(case.spec.metric.denominator, unit="USD")
    case = replace(
        case,
        spec=replace(case.spec, metric=replace(case.spec.metric, denominator=denominator)),
        inputs=replace(
            case.inputs,
            unit_labels={"n": {"@csv": {"amount": "CNY"}}, "d": {"@csv": {"amount": "USD"}}},
        ),
    )
    assert reference_result(case).reason == "UNSUPPORTED_UNIT_CONVERSION"


def test_ratio_sum_and_count_need_same_units():
    case = ratio_case([1], [2])
    assert isinstance(case.spec.metric, RatioSpec)
    case = replace(
        case,
        spec=replace(
            case.spec, metric=replace(case.spec.metric, denominator=CountSpec("count", "d"))
        ),
    )
    assert reference_result(case).reason == "UNSUPPORTED_UNIT_CONVERSION"


def test_filtered_count_zero_still_has_complete_binding():
    case = build_case(
        CountSpec("count", "s", FilterSpec("tag", "yes")),
        {"s": Table(("tag",), ((cell("no"),), (cell(None),)))},
    )
    result = reference_result(case)
    assert (result.value, result.unit) == ("0", "count")
    assert result.bindings == (Binding("s", "s", "@csv", 1, (2, 1001), ("tag",)),)


def test_filter_target_same_field_binding_is_deduplicated():
    case = sum_case(["1", "2"])
    case = replace(
        case,
        spec=replace(case.spec, metric=replace(case.spec.metric, filter=FilterSpec("amount", "1"))),
    )
    result = reference_result(case)
    assert result.value == "1"
    assert result.bindings[0].fields == ("amount",)


@pytest.mark.parametrize(
    "headers,reason", [((), "MISSING_FIELD"), (("tag", "tag"), "AMBIGUOUS_FIELD")]
)
def test_count_requires_unambiguous_filter_field(headers, reason):
    case = build_case(CountSpec("count", "s", FilterSpec("tag", "yes")), {"s": Table(headers, ())})
    assert reference_result(case).reason == reason


def test_loader_limit_cannot_be_ignored_when_good_target_table_exists():
    case = sum_case([5])
    case = replace(
        case,
        inputs=replace(
            case.inputs,
            business_limits={"file:extra": ({"reason": "UNSUPPORTED_INPUT_LIMIT", "details": {}},)},
        ),
    )
    result = reference_result(case)
    assert (result.status, result.value, result.reason) == (
        "ABSTAIN",
        None,
        "UNSUPPORTED_INPUT_LIMIT",
    )


def test_unknown_loader_limit_is_case_error_not_guessed_business_abstain():
    case = sum_case([5])
    case = replace(
        case,
        inputs=replace(case.inputs, business_limits={"s": ({"reason": "GUESS", "details": {}},)}),
    )
    with pytest.raises(CaseError, match="INVALID_BUSINESS_LIMIT"):
        reference_result(case)


@pytest.mark.parametrize(
    "places,rounding", [(-1, "HALF_UP"), (13, "HALF_UP"), (True, "HALF_UP"), (2, "HALF_EVEN")]
)
def test_defensive_unvalidated_precision_is_case_error(places, rounding):
    case = sum_case([1])
    case = replace(
        case,
        spec=replace(case.spec, metric=replace(case.spec.metric, places=places, rounding=rounding)),
    )
    with pytest.raises(CaseError, match="INVALID_REFERENCE_CASE"):
        reference_result(case)


@pytest.mark.parametrize("raw", ["oops" * 21, "9" * 81 + "%", " " + "9" * 80])
def test_long_invalid_syntax_stays_invalid_value(raw):
    assert reference_result(sum_case([raw])).reason == "INVALID_VALUE"


@pytest.mark.parametrize(
    "value,places,want",
    [
        (Fraction(10**127), 0, "1" + "0" * 127),
        (Fraction(-(10**126)), 0, "-1" + "0" * 126),
        (Fraction(10**114), 12, "1" + "0" * 114 + "." + "0" * 12),
    ],
)
def test_internal_formatter_accepts_128_character_boundary(value, places, want):
    # Synthetic fractions test the defensive formatter only, not a legal business case.
    formatter = importlib.import_module("sheetbenchkit.reference")._format
    assert formatter(value, places) == want
    assert len(want) == 128


@pytest.mark.parametrize(
    "value,places",
    [
        (Fraction(10**128), 0),
        (Fraction(-(10**127)), 0),
        (Fraction(10**115), 12),
    ],
)
def test_internal_formatter_excess_is_infrastructure_error(value, places):
    # The valid input envelope cannot reach 129 chars; never label this model ABSTAIN.
    formatter = importlib.import_module("sheetbenchkit.reference")._format
    with pytest.raises(CaseError, match="REFERENCE_OUTPUT_LIMIT"):
        formatter(value, places)


@pytest.mark.parametrize(
    "table",
    [
        Table(("amount",), tuple((cell("1"),) for _ in range(1001))),
        Table(("amount",) + tuple(f"extra{i}" for i in range(100)), ()),
        Table(("amount",), ((),)),
    ],
)
def test_defensive_illegal_loaded_table_is_infrastructure_error(table):
    case = sum_case([])
    case = replace(case, inputs=replace(case.inputs, tables={"s": table}))
    with pytest.raises(CaseError, match="INVALID_REFERENCE_CASE"):
        reference_result(case)


def test_defensive_unknown_contract_source_is_infrastructure_error():
    case = sum_case([1])
    case = replace(case, spec=replace(case.spec, sources={}))
    with pytest.raises(CaseError, match="INVALID_REFERENCE_CASE"):
        reference_result(case)


def test_sum_output_binding_preserves_distinct_file_and_source_identity():
    case = sum_case([5])
    case = replace(
        case,
        spec=replace(case.spec, sources={"s": SourceSpec("book", "Actual sheet", 3, (7, 7))}),
        inputs=replace(case.inputs, unit_labels={"book": {"Actual sheet": {"amount": "CNY"}}}),
    )
    result = reference_result(case)
    assert (result.value, result.unit) == ("5", "CNY")
    assert result.bindings == (Binding("s", "book", "Actual sheet", 3, (7, 7), ("amount",)),)
