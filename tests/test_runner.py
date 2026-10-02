"""Runner tests catch gold leakage, partial preflight, unbounded IO and unsafe cleanup."""

import base64
import dataclasses as d
import importlib
import inspect
import json
import os
import selectors
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from test_artifacts import candidate
from test_grader import large_cell_items

from sheetbenchkit import artifacts as a
from sheetbenchkit import models as m
from sheetbenchkit.contracts import strict_json_loads, to_document, validate_schema
from sheetbenchkit.grader import grade_suite

CONTROL = Path(__file__).parent / "controls" / "adapter_process.py"
POSIX = pytest.mark.skipif(os.name != "posix", reason="runner requires POSIX process groups")


def capability(name):
    try:
        module = importlib.import_module("sheetbenchkit.runner")
    except ModuleNotFoundError:
        pytest.fail("Missing T6 capability: runner." + name)
    result = getattr(module, name, None)
    assert callable(result), "Missing T6 capability: runner." + name
    return result


def frozen(tmp_path, count=1, items=None):
    root = tmp_path / "suite"
    suite = a.freeze_suite(
        items or tuple(candidate(f"case-{number}") for number in range(count)), (), root
    )
    return suite, root


def argv(tmp_path, mode="valid"):
    control_root = tmp_path / "control"
    control_root.mkdir(exist_ok=True)
    return (sys.executable, str(CONTROL), mode, str(control_root))


def starts(tmp_path):
    path = tmp_path / "control" / "starts"
    return len(path.read_text().splitlines()) if path.exists() else 0


def keys(value):
    if isinstance(value, dict):
        return set(value).union(*(keys(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(keys(item) for item in value))
    return set()


def test_envelope_has_no_gold(tmp_path):
    suite, root = frozen(tmp_path)
    case = a.preflight_suite(suite, root)[0]
    before = a.canonical_json(
        (
            case.validated.spec,
            case.validated.manifest,
            case.validated.task,
            case.gold,
            case.content_sha256,
        )
    )
    envelope = capability("make_task_envelope")(case)
    document = to_document(envelope)
    assert keys(document).isdisjoint(
        {
            "gold",
            "expected",
            "expected_value",
            "expected_reason",
            "mutation_kind",
            "intentional_boundaries",
            "variant_recipes",
            "business_confirmation",
            "origin",
        }
    )
    assert envelope.task == case.validated.task
    assert envelope.source_ids == ("target",)
    assert document["inputs"][0]["relative_path"] == "inputs/work.csv"
    assert document["output_protocol"]["contract"]["sources"] == {
        "target": {"file_id": "work", "sheet": "@csv", "header_row": 1, "rows": [2, 3]}
    }
    assert document["output_protocol"]["contract"]["metric"]["places"] == 2
    assert document["output_protocol"]["unit_labels"] == {"work": {"@csv": {"amount": "CNY"}}}
    assert document["output_protocol"]["result_schema"]["additionalProperties"] is False
    assert (
        a.canonical_json(
            (
                case.validated.spec,
                case.validated.manifest,
                case.validated.task,
                case.gold,
                case.content_sha256,
            )
        )
        == before
    )


@POSIX
@pytest.mark.parametrize("damage", ["gold", "input", "escape"])
def test_bad_suite_starts_no_adapter(tmp_path, damage):
    suite, root = frozen(tmp_path, 2)
    if damage == "gold":
        last = root / suite.cases[-1].gold_path
        last.write_bytes(last.read_bytes() + b" ")
        reason = "ARTIFACT_HASH_MISMATCH"
    else:
        last = root / "cases/case-1/inputs/work.csv"
        if damage == "input":
            last.write_bytes(b"amount\n8\n9\n")
            reason = "INPUT_HASH_MISMATCH"
        else:
            external = tmp_path / "outside.csv"
            external.write_bytes(last.read_bytes())
            last.unlink()
            last.symlink_to(external)
            reason = "PATH_ESCAPE"
    with pytest.raises(m.CaseError, match=reason):
        capability("run_suite")(suite, root, argv(tmp_path))
    assert starts(tmp_path) == 0


@POSIX
def test_full_preflight_header_budget_starts_zero(tmp_path):
    suite, root = frozen(tmp_path, items=large_cell_items(10))
    with pytest.raises(m.CaseError, match="SUITE_LIMIT") as caught:
        capability("run_suite")(suite, root, argv(tmp_path))
    assert caught.value.details["read_cells"] == 1_001_000
    assert starts(tmp_path) == 0


@POSIX
def test_execution_paths_do_not_depend_on_cwd_or_change_frozen_hash(tmp_path, monkeypatch):
    suite, root = frozen(tmp_path)
    initial = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    monkeypatch.chdir(unrelated)
    batch = capability("run_suite")(suite, root, argv(tmp_path))
    report = grade_suite(suite, a.preflight_suite(suite, root), batch)
    assert report.status_counts["PASS"] == 1
    received = json.loads((tmp_path / "control/envelope-case-0.json").read_text())
    path = Path(received["inputs"][0]["relative_path"])
    assert path.is_absolute() and path.is_relative_to(root.resolve())
    assert path.read_bytes() == b"amount\n1\n2\n"
    assert received["task"] == {
        "description": ("Sum exact amount in target rows 2..3; CNY; HALF_UP places=2.")
    }
    final = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert initial == final
    metadata = batch.observations[0].runtime_metadata
    assert metadata["adapter_argv"] == argv(tmp_path)
    assert metadata["input_paths"]["work"] == str(path)
    assert metadata["pid"] == metadata["pgid"]


@POSIX
def test_preserves_every_planned_attempt_without_retry_or_best_selection(tmp_path):
    suite, root = frozen(tmp_path, 2)
    batch = capability("run_suite")(
        suite, root, argv(tmp_path, "nonzero"), repeats=3, config_id="local-control"
    )
    assert batch.runs == (m.RunSpec("local-control", 3),)
    assert [(o.case_id, o.attempt, o.config_id) for o in batch.observations] == [
        (f"case-{case}", attempt, "local-control") for attempt in (1, 2, 3) for case in (0, 1)
    ]
    assert starts(tmp_path) == 6
    assert all(o.execution_status == "NONZERO_EXIT" for o in batch.observations)
    assert all(o.raw_stderr == "adapter diagnostic\n" for o in batch.observations)
    assert all(o.usage is None for o in batch.observations)
    assert all(o.runtime_metadata["returncode"] == 7 for o in batch.observations)
    validate_schema("observations", batch)
    assert grade_suite(suite, a.preflight_suite(suite, root), batch).status_counts["NO_RESULT"] == 6


@POSIX
@pytest.mark.parametrize(
    "mode,reason",
    [("empty", "INVALID_JSON"), ("logs", "INVALID_JSON"), ("usage_extra", "PROTOCOL_VIOLATION")],
)
def test_raw_output_is_never_repaired_or_invented_usage(tmp_path, mode, reason):
    suite, root = frozen(tmp_path)
    batch = capability("run_suite")(suite, root, argv(tmp_path, mode))
    grade = grade_suite(suite, a.preflight_suite(suite, root), batch).case_grades[0]
    assert grade.reason == reason
    assert batch.observations[0].usage is None
    assert starts(tmp_path) == 1


@POSIX
@pytest.mark.parametrize("mode", ["flood_stdout", "flood_stderr"])
def test_output_limits_cannot_grade_valid_prefix(tmp_path, mode):
    suite, root = frozen(tmp_path)
    batch = capability("run_suite")(suite, root, argv(tmp_path, mode), timeout_s=3)
    observation = batch.observations[0]
    assert observation.execution_status == "OUTPUT_LIMIT"
    assert len(observation.raw_stdout.encode()) <= 1024 * 1024
    assert len(observation.raw_stderr.encode()) <= 64 * 1024
    grade = grade_suite(suite, a.preflight_suite(suite, root), batch).case_grades[0]
    assert (grade.verdict, grade.reason) == ("NO_RESULT", "OUTPUT_LIMIT")
    assert observation.runtime_metadata["elapsed_s"] < 3
    assert observation.runtime_metadata["capture_complete"] is False
    stream = "stdout" if mode == "flood_stdout" else "stderr"
    assert observation.runtime_metadata[stream + "_truncated"] is True
    assert starts(tmp_path) == 1


@POSIX
@pytest.mark.parametrize("mode", ["exact_stdout", "exact_stderr"])
def test_exact_byte_budget_is_accepted(tmp_path, mode):
    suite, root = frozen(tmp_path)
    batch = capability("run_suite")(suite, root, argv(tmp_path, mode))
    assert batch.observations[0].execution_status == "SUCCESS"
    assert grade_suite(suite, a.preflight_suite(suite, root), batch).status_counts["PASS"] == 1


def wait_heartbeat(path):
    deadline = time.monotonic() + 2
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert path.exists()
    return path.read_text()


@POSIX
def test_timeout_kills_owned_group_only(tmp_path):
    suite, root = frozen(tmp_path)
    unrelated_path = tmp_path / "unrelated-heartbeat"
    unrelated = subprocess.Popen(
        [sys.executable, str(CONTROL), "heartbeat", str(unrelated_path)], start_new_session=True
    )
    try:
        wait_heartbeat(unrelated_path)
        batch = capability("run_suite")(suite, root, argv(tmp_path, "tree"), timeout_s=1)
        observation = batch.observations[0]
        owned = json.loads((tmp_path / "control/owned.json").read_text())
        assert owned["pid"] == owned["pgid"] == observation.runtime_metadata["pgid"]
        assert observation.execution_status == "TIMEOUT"
        assert 0.9 <= observation.runtime_metadata["elapsed_s"] < 3
        before = (tmp_path / "control/child").read_text()
        grandchild_before = (tmp_path / "control/grandchild").read_text()
        other_before = unrelated_path.read_text()
        time.sleep(0.12)
        assert (tmp_path / "control/child").read_text() == before
        assert (tmp_path / "control/grandchild").read_text() == grandchild_before
        assert unrelated.poll() is None and unrelated_path.read_text() != other_before
        assert observation.runtime_metadata["returncode"] == -signal.SIGKILL
        assert starts(tmp_path) == 1
        grade = grade_suite(suite, a.preflight_suite(suite, root), batch).case_grades[0]
        assert (grade.verdict, grade.reason) == ("NO_RESULT", "TIMEOUT")
    finally:
        unrelated.kill()
        unrelated.wait(timeout=3)
    assert inspect.signature(capability("run_suite")).parameters["timeout_s"].default == 60


@POSIX
def test_parent_exit_does_not_leave_descendant_holding_pipes(tmp_path):
    suite, root = frozen(tmp_path)
    batch = capability("run_suite")(suite, root, argv(tmp_path, "exit_with_tree"), timeout_s=1)
    assert batch.observations[0].execution_status == "TIMEOUT"
    before = (tmp_path / "control/child").read_text()
    time.sleep(0.12)
    assert (tmp_path / "control/child").read_text() == before


@POSIX
def test_successful_parent_also_cleans_owned_descendants_with_closed_pipes(tmp_path):
    suite, root = frozen(tmp_path)
    batch = capability("run_suite")(suite, root, argv(tmp_path, "closed_tree"))
    assert batch.observations[0].execution_status == "SUCCESS"
    assert grade_suite(suite, a.preflight_suite(suite, root), batch).status_counts["PASS"] == 1
    child_before = (tmp_path / "control/child").read_text()
    grandchild_before = (tmp_path / "control/grandchild").read_text()
    time.sleep(0.12)
    assert (tmp_path / "control/child").read_text() == child_before
    assert (tmp_path / "control/grandchild").read_text() == grandchild_before


@pytest.mark.parametrize(
    "changes",
    [
        {"repeats": 0},
        {"repeats": 11},
        {"repeats": True},
        {"config_id": "bad config"},
        {"timeout_s": 0},
        {"timeout_s": True},
        {"timeout_s": float("nan")},
        {"adapter_argv": ()},
        {"adapter_argv": ("",)},
        {"adapter_argv": ("ok", "x\0y")},
    ],
)
@POSIX
def test_invalid_run_configuration_rejects_before_start(tmp_path, changes):
    suite, root = frozen(tmp_path)
    arguments = {"adapter_argv": argv(tmp_path), **changes}
    with pytest.raises(m.CaseError):
        capability("run_suite")(suite, root, **arguments)
    assert starts(tmp_path) == 0


def test_windows_rejects_before_process_start(tmp_path, monkeypatch):
    suite, root = frozen(tmp_path)
    module = importlib.import_module("sheetbenchkit.runner") if capability("run_suite") else None
    monkeypatch.setattr(module, "sys", type("Platform", (), {"platform": "win32"}))

    def forbidden(*args, **kwargs):
        pytest.fail("Windows must reject before process start")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    with pytest.raises(m.CaseError, match="UNSUPPORTED_PLATFORM"):
        module.run_suite(suite, root, argv(tmp_path))
    assert starts(tmp_path) == 0


@POSIX
def test_start_error_recorded_for_every_planned_slot(tmp_path):
    suite, root = frozen(tmp_path)
    batch = capability("run_suite")(suite, root, (str(tmp_path / "missing-executable"),), repeats=2)
    assert [o.execution_status for o in batch.observations] == ["START_ERROR", "START_ERROR"]
    assert all(o.runtime_metadata["started"] is False for o in batch.observations)
    assert all(o.usage is None for o in batch.observations)
    assert grade_suite(suite, a.preflight_suite(suite, root), batch).status_counts["ERROR"] == 2


@POSIX
def test_trusted_argv_is_not_a_shell(tmp_path):
    suite, root = frozen(tmp_path)
    sentinel = tmp_path / "should-not-exist"
    command = ("echo ; touch " + str(sentinel),)
    batch = capability("run_suite")(suite, root, command)
    assert batch.observations[0].execution_status == "START_ERROR"
    assert not sentinel.exists()


@POSIX
def test_missing_input_descriptor_is_absolute_and_contained(tmp_path):
    item = candidate()
    entry = d.replace(item.validated.manifest.inputs[0], sha256=None, expected_missing=True)
    manifest = d.replace(
        item.validated.manifest, inputs=(entry,), intentional_boundaries=("MISSING_SOURCE",)
    )
    item = d.replace(
        item,
        validated=d.replace(
            item.validated, manifest=manifest, inputs=d.replace(item.validated.inputs, snapshots={})
        ),
        expected=m.ExpectedResult("ABSTAIN", None, None, "MISSING_SOURCE", ()),
    )
    suite, root = frozen(tmp_path, items=(item,))
    batch = capability("run_suite")(suite, root, argv(tmp_path, "echo"))
    received = strict_json_loads(batch.observations[0].raw_stdout)
    entry = received["inputs"][0]
    assert entry["sha256"] is None and entry["expected_missing"] is True
    path = Path(entry["relative_path"])
    assert path.is_absolute() and path.is_relative_to(root.resolve()) and not path.exists()


@POSIX
@pytest.mark.parametrize("mode", ["invalid_utf8", "invalid_stderr"])
def test_invalid_utf8_preserves_bytes_without_repairing_json(tmp_path, mode):
    suite, root = frozen(tmp_path)
    batch = capability("run_suite")(suite, root, argv(tmp_path, mode))
    observation = batch.observations[0]
    grade = grade_suite(suite, a.preflight_suite(suite, root), batch).case_grades[0]
    assert observation.execution_status == "SUCCESS"
    stream = "stdout" if mode == "invalid_utf8" else "stderr"
    assert observation.runtime_metadata[stream + "_encoding_error"] is True
    assert base64.b64decode(observation.runtime_metadata["raw_" + stream + "_base64"]) == b'"\xff"'
    assert getattr(observation, "raw_" + stream).startswith("b'")
    if stream == "stdout":
        assert (grade.verdict, grade.reason) == ("NO_RESULT", "INVALID_JSON")
    else:
        assert grade.verdict == "PASS"


@POSIX
def test_stdin_backpressure_cannot_hide_timeout(tmp_path):
    item = candidate()
    task = {"description": "x" * 200_000}
    confirmation = {**item.confirmation, "task_sha256": a.sha256_bytes(a.canonical_json(task))}
    item = d.replace(
        item,
        confirmation=confirmation,
        validated=d.replace(
            item.validated,
            task=task,
            manifest=d.replace(item.validated.manifest, business_confirmation=confirmation),
        ),
    )
    suite, root = frozen(tmp_path, items=(item,))
    batch = capability("run_suite")(suite, root, argv(tmp_path, "no_read"), timeout_s=1)
    assert batch.observations[0].execution_status == "TIMEOUT"
    assert batch.observations[0].runtime_metadata["elapsed_s"] < 3
    assert starts(tmp_path) == 1


@POSIX
def test_selector_resource_error_cannot_leave_started_adapter(tmp_path, monkeypatch):
    suite, root = frozen(tmp_path)
    real_popen = subprocess.Popen
    processes = []

    def recording_popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process

    def resource_error():
        raise OSError("controlled selector resource exhaustion")

    monkeypatch.setattr(selectors, "DefaultSelector", resource_error)
    monkeypatch.setattr(subprocess, "Popen", recording_popen)
    try:
        batch = capability("run_suite")(suite, root, argv(tmp_path))
        assert batch.observations[0].execution_status == "START_ERROR"
        assert batch.observations[0].runtime_metadata["started"] is False
        assert processes == []
        assert starts(tmp_path) == 0
    finally:
        for process in processes:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=3)
            for pipe in (process.stdin, process.stdout, process.stderr):
                pipe.close()
