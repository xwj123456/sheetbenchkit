"""Fourteen legal-output controls, each injecting one specified business mistake.

M01..11 retain the original target-contract binding declaration. M12 changes
only that declaration. Controls are test-only; the correct adapter has no flags.
"""

from __future__ import annotations

import copy
from fractions import Fraction
from typing import Any

from sheetbenchkit.examples.independent_runner import Calculator, Refusal, declared_bindings


class Contains(Calculator):
    def field(self, headers: list[Any], name: str) -> int:
        matches = [
            i for i, header in enumerate(headers) if isinstance(header, str) and name in header
        ]
        if not matches:
            raise Refusal("MISSING_FIELD")
        return matches[-1]  # A fuzzy search arbitrarily uses the last matching field.


class OldIndex(Calculator):
    def field(self, headers: list[Any], name: str) -> int:
        return 0  # Old physical column number, ignoring the exact declared header.


class BlankAsZero(Calculator):
    def number(self, value: Any) -> Fraction:
        return Fraction(0) if value is None or value == "" else super().number(value)


class InvalidAsZero(Calculator):
    def number(self, value: Any) -> Fraction:
        try:
            return super().number(value)
        except Refusal as refusal:
            if str(refusal) != "INVALID_VALUE":
                raise
            return Fraction(0)


class MeanRowRatios(Calculator):
    def ratio(self, numerator: list[Fraction], denominator: list[Fraction]) -> Fraction:
        ratios = []
        for top, bottom in zip(numerator, denominator, strict=True):
            if not bottom:
                raise Refusal("ZERO_DENOMINATOR")
            ratios.append(top / bottom)
        return sum(ratios, Fraction(0)) / len(ratios)


class PercentScaled(Calculator):
    def ratio(self, numerator: list[Fraction], denominator: list[Fraction]) -> Fraction:
        return super().ratio(numerator, denominator) * 100


class IgnoreUnits(Calculator):
    def compatible(self, numerator: dict[str, Any], denominator: dict[str, Any]) -> bool:
        return True


class HalfEven(Calculator):
    def format(self, value: Fraction, places: int) -> str:
        scaled, rest = divmod(abs(value.numerator) * 10**places, value.denominator)
        scaled += int(
            rest * 2 > value.denominator or (rest * 2 == value.denominator and scaled % 2 == 1)
        )
        sign = "-" if value < 0 and scaled else ""
        digits = str(scaled).zfill(places + 1)
        return sign + (digits[:-places] + "." + digits[-places:] if places else digits)


CONTROLS = {
    "M01": Calculator,
    "M02": Calculator,
    "M03_contains": Contains,
    "M03_index": OldIndex,
    "M04": Calculator,
    "M05": BlankAsZero,
    "M06": InvalidAsZero,
    "M07": MeanRowRatios,
    "M08_scale": PercentScaled,
    "M08_unit": IgnoreUnits,
    "M09": HalfEven,
    "M10": Calculator,
    "M11": Calculator,
    "M12": Calculator,
}


def solve(control: str, envelope: dict[str, Any]) -> dict[str, Any]:
    payload = copy.deepcopy(envelope)
    contract = payload["output_protocol"]["contract"]
    target_bindings = declared_bindings(contract)
    metric = contract["metric"]
    items = [metric["numerator"], metric["denominator"]] if metric["op"] == "ratio" else [metric]
    if control == "M01":
        # Select the other logical file while claiming the target file.
        for source in contract["sources"].values():
            if source["file_id"] == "target":
                source["file_id"] = "other"
    elif control == "M02":
        for source in contract["sources"].values():
            if source["sheet"] == "Actual":
                source["sheet"] = "Forecast"
    elif control == "M04":
        for item in items:
            if item.get("filter") and item["filter"]["field"] == "actual_status":
                item["filter"]["field"] = "forecast_status"
    if control in ("M10", "M11"):
        result = {
            "schema_version": "1",
            "case_id": payload["case_id"],
            "metric_id": payload["metric_id"],
            "bindings": target_bindings,
        }
        if control == "M10":
            result.update(status="ABSTAIN", value=None, unit=None, reason="MISSING_VALUE")
        else:
            places = metric.get("places", 0)
            unit = (
                "ratio"
                if metric["op"] == "ratio"
                else ("count" if metric["op"] == "count" else metric["unit"])
            )
            result.update(
                status="VALUE",
                value="0" + ("." + "0" * places if places else ""),
                unit=unit,
                reason=None,
            )
    else:
        result = CONTROLS[control](payload).solve()
        result["bindings"] = target_bindings
    if control == "M12":
        binding = result["bindings"][0]
        # One false declaration appropriate to the tested source-selector dimension.
        if binding["file_id"] == "target":
            binding["file_id"] = "other"
        elif "revenue" in binding["fields"]:
            binding["fields"] = ["revenue_total"]
        else:
            binding["sheet"] = "Forecast"
    return result
