"""Freeze two starter contracts against supplied CSV/XLSX bytes and reviewed gold.

This example consumes review declarations; it never calculates or authenticates gold.
Copy and edit the contract/task definitions for a different business question.
"""

from __future__ import annotations

import argparse
import io
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, cast

from openpyxl import Workbook  # type: ignore[import-untyped]

from sheetbenchkit import models as m
from sheetbenchkit.artifacts import canonical_json, freeze_suite, sha256_bytes
from sheetbenchkit.contracts import decode_document, strict_json_loads, validate_case

_MAX_BYTES = 10 * 1024 * 1024
_FILENAMES = ("sales.csv", "budget.xlsx")
_CASE_IDS = ("custom_csv_sum", "custom_xlsx_ratio")


def _definitions() -> tuple[dict[str, Any], ...]:
    """Explicit business scope, independent of input hashes and expected answers."""
    return (
        {
            "case_id": "custom_csv_sum", "filename": "sales.csv", "format": "csv",
            "sheet": "@csv", "rows": [2, 4], "fields": ["amount", "status"], "unit": "CNY",
            "task": {"description": (
                "Sum amount for status exactly Confirmed in CSV records 2 through 4, "
                "with header record 1. Unit CNY; round once to 2 places using HALF_UP."
            )},
            "metric": {
                "op": "sum", "source": "target", "field": "amount", "unit": "CNY",
                "filter": {"field": "status", "eq": "Confirmed"},
                "places": 2, "rounding": "HALF_UP",
            },
            "unit_labels": {"work": {"@csv": {"amount": "CNY"}}},
        },
        {
            "case_id": "custom_xlsx_ratio", "filename": "budget.xlsx", "format": "xlsx",
            "sheet": "Budget", "rows": [2, 3], "fields": ["actual", "planned"],
            "unit": "ratio",
            "task": {"description": (
                "On XLSX sheet Budget, header row 1 and data rows 2 through 3, divide "
                "the sum of actual by the sum of planned. Both columns are CNY. "
                "Return a ratio rounded once to 4 places using HALF_UP."
            )},
            "metric": {
                "op": "ratio",
                "numerator": {"op": "sum", "source": "target", "field": "actual", "unit": "CNY"},
                "denominator": {
                    "op": "sum", "source": "target", "field": "planned", "unit": "CNY",
                },
                "places": 4, "rounding": "HALF_UP",
            },
            "unit_labels": {"work": {"Budget": {"actual": "CNY", "planned": "CNY"}}},
        },
    )


def _write_new(path: Path, data: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(data)


def create_demo_inputs(destination: Path) -> None:
    """Create synthetic physical files and an inspectable, fixed-literal review record."""
    destination = Path(destination)
    if destination.exists() or destination.is_symlink():
        raise m.CaseError("DESTINATION_EXISTS", str(destination))
    csv = b"status,amount\nConfirmed,10.25\nPending,999.00\nConfirmed,5.50\n"
    book = Workbook()
    sheet = book.active
    sheet.title = "Budget"
    for row in (("project", "actual", "planned"), ("Alpha", 40, 120), ("Beta", 10, 80)):
        sheet.append(row)
    stream = io.BytesIO()
    try:
        book.save(stream)
    finally:
        book.close()
    snapshots = {"sales.csv": csv, "budget.xlsx": stream.getvalue()}
    review = {
        "schema_version": "1",
        "review_note": (
            "Synthetic fixed literals derived from the displayed operands, independent "
            "of the adapter/reference. No human signature or customer validation is claimed."
        ),
        "input_sha256": {name: sha256_bytes(data) for name, data in snapshots.items()},
        "expected_values": {"custom_csv_sum": "15.75", "custom_xlsx_ratio": "0.2500"},
        "derivations": {
            "custom_csv_sum": "10.25 + 5.50 = 15.75 CNY; Pending 999.00 is excluded.",
            "custom_xlsx_ratio": "(40 + 10) / (120 + 80) = 50 / 200 = 0.2500 ratio.",
        },
    }
    destination.mkdir(parents=True, exist_ok=False)
    for name, data in snapshots.items():
        _write_new(destination / name, data)
    _write_new(destination / "review.json", canonical_json(review) + b"\n")


def _read_bounded(path: Path) -> bytes:
    if not path.is_file():
        raise m.CaseError("INPUT_IO_ERROR", f"regular file required: {path}")
    with path.open("rb") as handle:
        data = handle.read(_MAX_BYTES + 1)
    if len(data) > _MAX_BYTES:
        raise m.CaseError("UNSUPPORTED_INPUT_LIMIT", str(path))
    return data


def _review(path: Path) -> dict[str, Any]:
    document = strict_json_loads(_read_bounded(path))
    required = {"schema_version", "review_note", "input_sha256", "expected_values", "derivations"}
    if not isinstance(document, dict) or set(document) != required:
        raise m.CaseError("INVALID_REVIEW", "use the documented review.json shape")
    if document["schema_version"] != "1":
        raise m.CaseError("INVALID_REVIEW", "schema_version must be 1")
    note = document["review_note"]
    if not isinstance(note, str) or not note.strip():
        raise m.CaseError("INVALID_REVIEW", "supply a nonempty review_note")
    for key, names in (
        ("input_sha256", _FILENAMES), ("expected_values", _CASE_IDS), ("derivations", _CASE_IDS),
    ):
        values = document[key]
        if not isinstance(values, dict) or set(values) != set(names):
            raise m.CaseError("INVALID_REVIEW", key)
        for value in values.values():
            if not isinstance(value, str) or not value.strip():
                raise m.CaseError("INVALID_REVIEW", key)
            if key == "input_sha256" and re.fullmatch("[a-f0-9]{64}", value) is None:
                raise m.CaseError("INVALID_REVIEW", key)
    return document


def _candidate(
    definition: dict[str, Any], snapshot: bytes, review: dict[str, Any], root: Path,
) -> m.CandidateCase:
    case_id = definition["case_id"]
    source = {"file_id": "work", "sheet": definition["sheet"], "header_row": 1,
              "rows": definition["rows"]}
    contract = {
        "schema_version": "1", "metric_id": case_id,
        "sources": {"target": source}, "metric": definition["metric"],
    }
    task = definition["task"]
    confirmation = {
        "reviewed_contract_sha256": sha256_bytes(canonical_json(contract)),
        "task_sha256": sha256_bytes(canonical_json(task)),
        "review_note": review["review_note"], "review_authentication": "not_authenticated",
    }
    manifest = {
        "schema_version": "1", "case_id": case_id, "category": "custom-starter",
        "inputs": [{
            "file_id": "work", "relative_path": definition["filename"],
            "format": definition["format"], "sha256": sha256_bytes(snapshot),
            "expected_missing": False,
        }],
        "unit_labels": definition["unit_labels"], "intentional_boundaries": [],
        "business_confirmation": confirmation, "variant_recipes": [],
    }
    root.mkdir()
    _write_new(root / definition["filename"], snapshot)
    _write_new(root / "task.json", canonical_json(task))
    spec = cast(m.RuleSpec, decode_document("contract", canonical_json(contract)))
    man = cast(m.CaseManifest, decode_document("manifest", canonical_json(manifest)))
    validated = validate_case(spec, man, root)
    binding = m.Binding("target", "work", source["sheet"], 1, tuple(source["rows"]),
                        tuple(definition["fields"]))
    expected = m.ExpectedResult("VALUE", review["expected_values"][case_id], definition["unit"],
                                None, (binding,))
    return m.CandidateCase(
        validated, expected, "preset_independent", confirmation,
        {"method": "supplied fixed literal; no gold evaluator called",
         "derivation": review["derivations"][case_id],
         "review_note": review["review_note"], "review_authentication": "not_authenticated",
         "input_sha256": sha256_bytes(snapshot)},
    )


def freeze_custom_suite(inputs: Path, review_path: Path, destination: Path) -> m.Suite:
    """Bind a supplied review to original bytes, validate, then atomically freeze.

    Matching a hash verifies identity, not the truth of the supplied expected value.
    A changed input requires fresh independent review and a new review record.
    """
    inputs, destination = Path(inputs).resolve(strict=True), Path(destination)
    if destination.exists() or destination.is_symlink():
        raise m.CaseError("DESTINATION_EXISTS", str(destination))
    review = _review(Path(review_path))
    snapshots = {}
    for filename in _FILENAMES:
        path = inputs / filename
        if not path.resolve().is_relative_to(inputs):
            raise m.CaseError("PATH_ESCAPE", filename)
        snapshot = _read_bounded(path)
        if sha256_bytes(snapshot) != review["input_sha256"][filename]:
            raise m.CaseError(
                "REVIEW_INPUT_HASH_MISMATCH", filename + "; independently re-review gold",
            )
        snapshots[filename] = snapshot
    with tempfile.TemporaryDirectory(prefix="sheetbenchkit-custom-") as directory:
        root = Path(directory)
        candidates = tuple(
            _candidate(definition, snapshots[definition["filename"]], review,
                       root / definition["case_id"])
            for definition in _definitions()
        )
        return freeze_suite(candidates, (), destination)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    demo = commands.add_parser("demo-inputs", help="create synthetic CSV/XLSX and fixed review")
    demo.add_argument("--output", type=Path, required=True, help="new directory; never overwrite")
    freeze = commands.add_parser("freeze", help="freeze supplied files and reviewed fixed values")
    freeze.add_argument(
        "--inputs", type=Path, required=True, help="sales.csv and budget.xlsx directory",
    )
    freeze.add_argument("--review", type=Path, required=True, help="explicit review.json path")
    freeze.add_argument("--output", type=Path, required=True, help="new directory; never overwrite")
    try:
        options = parser.parse_args(argv)
        if options.command == "demo-inputs":
            create_demo_inputs(options.output)
            print(f"Created synthetic inputs and inspectable review at {options.output}")
        else:
            suite = freeze_custom_suite(options.inputs, options.review, options.output)
            print(f"Frozen {len(suite.cases)} supplied cases at {options.output}; gold is declared")
        return 0
    except SystemExit as exc:
        return int(exc.code or 0)
    except (m.CaseError, OSError, RuntimeError) as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
