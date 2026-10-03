"""Custom examples bind reviewed literals to exact user input bytes, without certifying gold."""

from __future__ import annotations

import hashlib
import importlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
from openpyxl import load_workbook

from sheetbenchkit import artifacts as a
from sheetbenchkit import models as m
from sheetbenchkit.cli import main as cli_main
from sheetbenchkit.contracts import to_document
from sheetbenchkit.runner import make_task_envelope


def example():
    try:
        return importlib.import_module("sheetbenchkit.examples.custom_suite")
    except ModuleNotFoundError:
        pytest.fail("Missing custom_suite: exact-input reviewed CSV/XLSX example")


def prepared(tmp_path):
    inputs = tmp_path / "自定义 输入"
    example().create_demo_inputs(inputs)
    return inputs, inputs / "review.json"


def files(root):
    return {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def save_review(path, document):
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")


def capture_independent(suite, root):
    observations = []
    for ref, case in zip(suite.cases, a.preflight_suite(suite, root), strict=True):
        envelope = to_document(make_task_envelope(case))
        for entry in envelope["inputs"]:
            entry["relative_path"] = str(
                (root / ref.contract_path).parent / entry["relative_path"]
            )
        result = subprocess.run(
            [sys.executable, "-m", "sheetbenchkit.examples.independent_runner"],
            input=json.dumps(envelope).encode(),
            capture_output=True,
            timeout=10,
            check=True,
        )
        observations.append(
            m.Observation(
                ref.case_id, 1, "independent", "SUCCESS", result.stdout.decode("utf-8"),
                result.stderr.decode("utf-8"), None, {"capture": "test-only subprocess"},
            )
        )
    return m.ObservationBatch("1", (m.RunSpec("independent", 1),), tuple(observations))


def test_demo_inputs_have_inspectable_literals_and_reviewed_exact_hashes(tmp_path):
    # Catches fabricated fixture data, a wrong literal, or hashes of re-saved inputs.
    inputs, path = prepared(tmp_path)
    assert (inputs / "sales.csv").read_bytes() == (
        b"status,amount\nConfirmed,10.25\nPending,999.00\nConfirmed,5.50\n"
    )
    book = load_workbook(inputs / "budget.xlsx", data_only=False)
    try:
        assert list(book["Budget"].values) == [
            ("project", "actual", "planned"), ("Alpha", 40, 120), ("Beta", 10, 80),
        ]
    finally:
        book.close()
    review = json.loads(path.read_bytes())
    assert review["expected_values"] == {
        "custom_csv_sum": "15.75", "custom_xlsx_ratio": "0.2500",
    }
    assert review["input_sha256"] == {
        name: hashlib.sha256((inputs / name).read_bytes()).hexdigest()
        for name in ("sales.csv", "budget.xlsx")
    }


def test_freeze_copies_original_bytes_binds_gold_and_never_mutates_inputs(tmp_path):
    # Catches reading v1 resources, input reserialization, or unbound review evidence.
    inputs, review = prepared(tmp_path)
    before = files(inputs)
    root = tmp_path / "新 suite"
    suite = example().freeze_custom_suite(inputs, review, root)
    cases = a.preflight_suite(suite, root)
    assert {c.validated.manifest.case_id: c.gold.expected.value for c in cases} == {
        "custom_csv_sum": "15.75", "custom_xlsx_ratio": "0.2500",
    }
    assert not suite.families
    for case in cases:
        entry, = case.validated.manifest.inputs
        original = before[Path("sales.csv" if entry.format == "csv" else "budget.xlsx")]
        assert case.validated.inputs.snapshots[entry.file_id] == original
        assert entry.sha256 == hashlib.sha256(original).hexdigest()
        assert case.gold.evidence["input_sha256"] == entry.sha256
        assert case.gold.evidence["review_authentication"] == "not_authenticated"
        assert case.gold.origin == "preset_independent"
    assert files(inputs) == before
    assert cli_main(["validate", "--suite", str(root)]) == 0


@pytest.mark.parametrize("command", ["demo", "freeze"])
def test_existing_destinations_are_never_overwritten(tmp_path, command):
    # Catches an overwrite of an existing output or a change to unrelated user files.
    inputs, review = prepared(tmp_path)
    output = tmp_path / "already-exists"
    output.mkdir()
    (output / "sentinel.txt").write_bytes(b"keep user data")
    before = files(output)
    with pytest.raises(m.CaseError, match="DESTINATION_EXISTS"):
        if command == "demo":
            example().create_demo_inputs(output)
        else:
            example().freeze_custom_suite(inputs, review, output)
    assert files(output) == before


@pytest.mark.parametrize("filename", ["sales.csv", "budget.xlsx"])
def test_changed_input_refuses_the_previous_review_without_publication(tmp_path, filename):
    # Catches silently reusing fixed gold after either physical input changes.
    inputs, review = prepared(tmp_path)
    path = inputs / filename
    path.write_bytes(path.read_bytes() + b"\n")
    before = files(inputs)
    destination = tmp_path / "not-published"
    with pytest.raises(m.CaseError, match="REVIEW_INPUT_HASH_MISMATCH"):
        example().freeze_custom_suite(inputs, review, destination)
    assert not destination.exists()
    assert files(inputs) == before


def test_user_supplied_new_review_is_preserved_without_computing_gold(tmp_path):
    # Catches automatic replacement of an explicitly re-reviewed expected result.
    inputs, review = prepared(tmp_path)
    path = inputs / "sales.csv"
    path.write_bytes(path.read_bytes().replace(b"10.25", b"20.25"))
    document = json.loads(review.read_bytes())
    document["input_sha256"]["sales.csv"] = hashlib.sha256(path.read_bytes()).hexdigest()
    document["expected_values"]["custom_csv_sum"] = "25.75"
    document["derivations"]["custom_csv_sum"] = "Re-reviewed: 20.25 + 5.50 = 25.75."
    save_review(review, document)
    suite = example().freeze_custom_suite(inputs, review, tmp_path / "reviewed-suite")
    cases = a.preflight_suite(suite, tmp_path / "reviewed-suite")
    assert cases[0].gold.expected.value == "25.75"
    assert cases[0].gold.evidence["derivation"] == document["derivations"]["custom_csv_sum"]


@pytest.mark.parametrize("wrong_gold", [False, True])
def test_actual_independent_adapter_and_offline_grade_distinguish_wrong_gold(tmp_path, wrong_gold):
    # Catches wrong source selection/row bounds; also shows freeze is not a math certification.
    inputs, review = prepared(tmp_path)
    if wrong_gold:
        document = json.loads(review.read_bytes())
        document["expected_values"]["custom_csv_sum"] = "0.00"
        save_review(review, document)
    root = tmp_path / "suite"
    suite = example().freeze_custom_suite(inputs, review, root)
    batch = capture_independent(suite, root)
    actual = {o.case_id: json.loads(o.raw_stdout)["value"] for o in batch.observations}
    assert actual == {"custom_csv_sum": "15.75", "custom_xlsx_ratio": "0.2500"}
    saved = tmp_path / "captured.json"
    saved.write_bytes(a.canonical_json(batch))
    output = tmp_path / "graded"
    code = cli_main([
        "grade", "--suite", str(root), "--observations", str(saved), "--output", str(output),
    ])
    assert code == (1 if wrong_gold else 0)
    report = json.loads((output / "report.json").read_bytes())
    assert report["status_counts"] == {
        "PASS": 1 if wrong_gold else 2, "FAIL": 1 if wrong_gold else 0,
        "NO_RESULT": 0, "ERROR": 0,
    }


@pytest.mark.parametrize("damage", ["invalid-json", "empty-note", "missing-hash", "bad-value"])
def test_invalid_review_never_publishes_a_partial_suite(tmp_path, damage):
    # Catches skipping strict review loading or publication before gold validation.
    inputs, review = prepared(tmp_path)
    document = json.loads(review.read_bytes())
    if damage == "invalid-json":
        review.write_bytes(b'{"schema_version":"1","schema_version":"1"}')
    else:
        if damage == "empty-note":
            document["review_note"] = ""
        elif damage == "missing-hash":
            del document["input_sha256"]["budget.xlsx"]
        else:
            document["expected_values"]["custom_csv_sum"] = "15.755"
        save_review(review, document)
    with pytest.raises(m.CaseError):
        example().freeze_custom_suite(inputs, review, tmp_path / "invalid")
    assert not (tmp_path / "invalid").exists()


def test_example_cli_runs_explicit_inputs_and_reports_errors_without_traceback(tmp_path, capsys):
    # Catches unusable module arguments or success exits after rejected input/review.
    inputs = tmp_path / "inputs"
    assert example().main(["demo-inputs", "--output", str(inputs)]) == 0
    root = tmp_path / "suite"
    args = [
        "freeze", "--inputs", str(inputs), "--review", str(inputs / "review.json"),
        "--output", str(root),
    ]
    assert example().main(args) == 0
    assert example().main(args) == 2
    captured = capsys.readouterr()
    assert "DESTINATION_EXISTS" in captured.err
    assert "Traceback" not in captured.err
