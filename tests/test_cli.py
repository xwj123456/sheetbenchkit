"""Exercise the public CLI with frozen files, saved observations and trusted processes."""

from __future__ import annotations

import builtins
import importlib
import json
import os
import socket
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from test_artifacts import candidate
from test_runner import argv, starts
from test_variants import make_base

from sheetbenchkit import artifacts as a
from sheetbenchkit import models as m
from sheetbenchkit.contracts import decode_document, to_document


def main(args):
    try:
        cli = importlib.import_module("sheetbenchkit.cli")
    except ModuleNotFoundError:
        pytest.fail("Missing T7b capability: cli.main")
    return cli.main(args)


def frozen(tmp_path, count=1):
    root = tmp_path / "suite"
    suite = a.freeze_suite(tuple(candidate(f"case-{i}") for i in range(count)), (), root)
    return suite, root


def observation(item, *, config="saved", attempt=1, raw=None, status="SUCCESS"):
    result = m.AgentResult(
        "1",
        item.validated.manifest.case_id,
        item.validated.spec.metric_id,
        item.gold.expected.status,
        item.gold.expected.value,
        item.gold.expected.unit,
        item.gold.expected.reason,
        item.gold.expected.bindings,
    )
    return m.Observation(
        item.validated.manifest.case_id,
        attempt,
        config,
        status,
        a.canonical_json(result).decode() if raw is None else raw,
        "saved stderr <script>",
        {"tokens": 0, "price": None},
        {"saved": True},
    )


def saved(tmp_path, suite, root, observations=None, runs=None):
    cases = a.preflight_suite(suite, root)
    batch = m.ObservationBatch(
        "1",
        runs or (m.RunSpec("saved", 1),),
        tuple(observation(c) for c in cases) if observations is None else observations,
    )
    path = tmp_path / "saved.json"
    path.write_bytes(a.canonical_json(batch))
    return path, batch


def files(root):
    return {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def grade_args(root, observations, out):
    return [
        "grade",
        "--suite",
        str(root),
        "--observations",
        str(observations),
        "--output",
        str(out),
    ]


def offline_traps(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("offline grade/report attempted network, process or credential access")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    original_getitem = type(os.environ).__getitem__
    original_open = Path.open
    original_import = builtins.__import__

    def env_getitem(self, key):
        if any(word in str(key).upper() for word in ("TOKEN", "API_KEY", "SECRET", "CREDENTIAL")):
            forbidden()
        return original_getitem(self, key)

    def file_open(self, *args, **kwargs):
        if self.name in {".env", "credentials", "credentials.json", "auth.json"}:
            forbidden()
        return original_open(self, *args, **kwargs)

    def import_module(name, *args, **kwargs):
        if name.endswith("reference") or name.endswith("variants"):
            pytest.fail("offline grade imported gold evaluator/generator")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(type(os.environ), "__getitem__", env_getitem)
    monkeypatch.setattr(Path, "open", file_open)
    monkeypatch.setattr(builtins, "__import__", import_module)


def test_grade_offline_actual_main_and_report_preserve_evidence(tmp_path, monkeypatch):
    suite, root = frozen(tmp_path, 2)
    path, batch = saved(tmp_path, suite, root)
    before = files(tmp_path)
    out = tmp_path / "reports"
    out.mkdir()
    (out / "unrelated.txt").write_text("keep")
    offline_traps(monkeypatch)
    assert main(grade_args(root, path, out)) == 0
    report = json.loads((out / "report.json").read_bytes())
    assert report["planned"] == report["attempted"] == 2
    assert report["observed_count"] == 2 and report["missing_count"] == 0
    assert report["status_counts"]["PASS"] == 2
    for grade, obs in zip(report["case_grades"], batch.observations, strict=True):
        assert grade["evidence"]["raw_stdout"] == obs.raw_stdout
        assert grade["evidence"]["usage"] == to_document(obs.usage)
    assert "<script>" not in (out / "report.html").read_text()
    assert (out / "unrelated.txt").read_text() == "keep"
    assert all((tmp_path / name).read_bytes() == raw for name, raw in before.items())


@pytest.mark.parametrize(
    "mode,code,reason",
    [
        ("wrong", 1, "VALUE_MISMATCH"),
        ("empty", 1, "INVALID_JSON"),
        ("logs", 1, "INVALID_JSON"),
        ("multiple", 1, "INVALID_JSON"),
        ("nan", 1, "INVALID_JSON"),
        ("duplicate", 1, "INVALID_JSON"),
        ("protocol", 1, "PROTOCOL_VIOLATION"),
        ("missing", 1, "MISSING_OUTPUT"),
        ("unexpected", 2, "UNPLANNED_OBSERVATION"),
    ],
)
def test_grade_exit_and_strict_stdout(tmp_path, mode, code, reason):
    suite, root = frozen(tmp_path)
    item = a.preflight_suite(suite, root)[0]
    obs = observation(item)
    if mode == "wrong":
        obs = replace(obs, raw_stdout=obs.raw_stdout.replace('"3.00"', '"9.00"'))
    elif mode == "empty":
        obs = replace(obs, raw_stdout="")
    elif mode == "logs":
        obs = replace(obs, raw_stdout="log\n" + obs.raw_stdout)
    elif mode == "multiple":
        obs = replace(obs, raw_stdout=obs.raw_stdout + "{}")
    elif mode == "nan":
        obs = replace(obs, raw_stdout='{"x":NaN}')
    elif mode == "duplicate":
        obs = replace(obs, raw_stdout='{"x":1,"x":2}')
    elif mode == "protocol":
        obs = replace(obs, raw_stdout=obs.raw_stdout.replace('"3.00"', '"NaN"'))
    elif mode == "unexpected":
        obs = replace(obs, case_id="unexpected")
    path, _ = saved(tmp_path, suite, root, () if mode == "missing" else (obs,))
    out = tmp_path / "out"
    assert main(grade_args(root, path, out)) == code
    report = json.loads((out / "report.json").read_bytes())
    assert report["planned"] == report["attempted"] == 1
    assert report["case_grades"][0]["reason"] == reason


def test_grade_complete_multi_config_plan_missing_entire_config(tmp_path):
    suite, root = frozen(tmp_path, 2)
    cases = a.preflight_suite(suite, root)
    path, _ = saved(
        tmp_path,
        suite,
        root,
        tuple(observation(c) for c in cases),
        (m.RunSpec("saved", 1), m.RunSpec("missing-config", 2)),
    )
    out = tmp_path / "out"
    assert main(grade_args(root, path, out)) == 1
    report = json.loads((out / "report.json").read_bytes())
    assert report["planned"] == report["attempted"] == 6
    assert report["missing_count"] == 4 and len(report["case_grades"]) == 6
    assert report["per_config"]["missing-config"]["planned"] == 4


@pytest.mark.parametrize(
    "raw",
    [
        b'{"schema_version":"1","schema_version":"1"}',
        b'{"x":NaN}',
        b'{"x":Infinity}',
        b'{"x":1e999}',
        b"{}{}",
        b"\xff",
        b"{}",
    ],
)
def test_invalid_observation_document_stderr_no_fabricated_report(tmp_path, capsys, raw):
    suite, root = frozen(tmp_path)
    path = tmp_path / "saved.json"
    path.write_bytes(raw)
    out = tmp_path / "out"
    assert main(grade_args(root, path, out)) == 2
    assert "ERROR" in capsys.readouterr().err
    assert not out.exists()


def test_validate_actual_files_and_corruption(tmp_path, capsys):
    suite, root = frozen(tmp_path, 2)
    before = files(root)
    assert main(["validate", "--suite", str(root)]) == 0
    assert files(root) == before
    (root / suite.cases[-1].gold_path).write_bytes(b"{}")
    assert main(["validate", "--suite", str(root)]) == 2
    assert "ERROR" in capsys.readouterr().err


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["bad"],
        ["validate"],
        ["grade"],
        ["generate"],
        ["run"],
        ["generate", "--demo", "--base", "x", "--seed", "1", "--output", "y"],
    ],
)
def test_parse_errors_are_return_codes(args, capsys):
    assert main(args) == 2
    assert capsys.readouterr().err


def test_help_returns_zero(capsys):
    assert main(["--help"]) == 0
    output = capsys.readouterr().out
    assert all(name in output for name in ("validate", "generate", "grade", "run"))


@pytest.mark.parametrize(
    "options",
    [
        [],
        ["--repeats", "0"],
        ["--repeats", "11"],
        ["--repeats", "1.5"],
        ["--timeout", "0"],
        ["--timeout", "-1"],
        ["--timeout", "0.5"],
        ["--config-id", "../x"],
        ["--config-id", ""],
    ],
)
def test_run_invalid_options_or_no_adapter_zero_starts(tmp_path, options, capsys):
    _, root = frozen(tmp_path)
    args = ["run", "--suite", str(root), "--results", str(tmp_path / "out"), *options]
    if options:
        args += ["--", *argv(tmp_path)]
    assert main(args) == 2
    assert starts(tmp_path) == 0
    assert capsys.readouterr().err


def test_run_requires_explicit_separator(tmp_path):
    _, root = frozen(tmp_path)
    assert (
        main(["run", "--suite", str(root), "--results", str(tmp_path / "out"), *argv(tmp_path)])
        == 2
    )
    assert starts(tmp_path) == 0


@pytest.mark.skipif(os.name != "posix", reason="run requires POSIX")
@pytest.mark.parametrize("mode,code", [("valid", 0), ("nonzero", 1), ("logs", 1), ("empty", 1)])
def test_run_actual_argv_preserves_full_batch(tmp_path, mode, code):
    _, root = frozen(tmp_path, 2)
    before = files(root)
    out = tmp_path / "out"
    adapter = argv(tmp_path, mode)
    assert (
        main(
            [
                "run",
                "--suite",
                str(root),
                "--results",
                str(out),
                "--repeats",
                "2",
                "--config-id",
                "local",
                "--timeout",
                "2",
                "--",
                *adapter,
            ]
        )
        == code
    )
    batch = decode_document("observations", (out / "observations.json").read_bytes())
    assert batch.runs == (m.RunSpec("local", 2),)
    assert len(batch.observations) == starts(tmp_path) == 4
    assert all(o.usage is None for o in batch.observations)
    assert all(o.runtime_metadata["adapter_argv"] == adapter for o in batch.observations)
    report = json.loads((out / "report.json").read_bytes())
    assert report["planned"] == report["attempted"] == report["observed_count"] == 4
    assert files(root) == before


@pytest.mark.skipif(os.name != "posix", reason="run requires POSIX")
def test_run_preflight_entire_suite_prevents_any_start(tmp_path):
    suite, root = frozen(tmp_path, 2)
    (root / suite.cases[-1].gold_path).write_bytes(b"{}")
    out = tmp_path / "out"
    assert main(["run", "--suite", str(root), "--results", str(out), "--", *argv(tmp_path)]) == 2
    assert starts(tmp_path) == 0 and not (out / "observations.json").exists()


@pytest.mark.skipif(os.name != "posix", reason="run requires POSIX")
def test_run_repreflight_after_adapter_mutates_input_preserves_observations(tmp_path, capsys):
    _, root = frozen(tmp_path)
    adapter = tmp_path / "mutator.py"
    adapter.write_text(
        "import json,sys\nfrom pathlib import Path\n"
        "e=json.load(sys.stdin)\n"
        "Path(e['inputs'][0]['relative_path']).write_text('amount\\n9\\n9\\n')\n"
        "print('{}')\n"
    )
    out = tmp_path / "out"
    assert (
        main(
            ["run", "--suite", str(root), "--results", str(out), "--", sys.executable, str(adapter)]
        )
        == 2
    )
    batch = decode_document("observations", (out / "observations.json").read_bytes())
    assert len(batch.observations) == 1 and batch.observations[0].raw_stdout == "{}\n"
    assert "INPUT_HASH_MISMATCH" in capsys.readouterr().err
    assert not (out / "report.json").exists()


def test_run_windows_early_refusal(tmp_path, monkeypatch, capsys):
    _, root = frozen(tmp_path)
    monkeypatch.setattr(sys, "platform", "win32")
    assert (
        main(
            ["run", "--suite", str(root), "--results", str(tmp_path / "out"), "--", *argv(tmp_path)]
        )
        == 2
    )
    assert starts(tmp_path) == 0
    assert "UNSUPPORTED_PLATFORM" in capsys.readouterr().err


@pytest.mark.parametrize("kind", ["observations", "symlink"])
def test_output_preserves_source_and_unrelated_targets(tmp_path, kind, capsys):
    suite, root = frozen(tmp_path)
    path, _ = saved(tmp_path, suite, root)
    out = tmp_path / "out"
    if kind == "observations":
        out.mkdir()
        path.rename(out / "report.json")
        path = out / "report.json"
    else:
        out.mkdir()
        (out / "report.json").symlink_to(path)
    before = files(tmp_path)
    assert main(grade_args(root, path, out)) == 2
    assert files(tmp_path) == before
    assert "ERROR" in capsys.readouterr().err


def test_output_io_failure_exit_two(tmp_path, capsys):
    suite, root = frozen(tmp_path)
    path, _ = saved(tmp_path, suite, root)
    out = tmp_path / "file"
    out.write_text("unrelated")
    assert main(grade_args(root, path, out)) == 2
    assert out.read_text() == "unrelated" and "ERROR" in capsys.readouterr().err


def test_generate_base_actual_files_no_process_and_confirmation_unchanged(tmp_path, monkeypatch):
    # A frozen custom suite is the explicitly supplied collection of confirmed bases.
    base = make_base(tmp_path / "custom")
    from sheetbenchkit.variants import generate_suite

    base_suite = generate_suite((base,), 7, tmp_path / "base")
    before = files(tmp_path / "base")

    # An already generated triplet must be filtered by its family plan, not expanded again.
    def forbidden(*args, **kwargs):
        pytest.fail("generate attempted adapter/network")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    out = tmp_path / "generated"
    assert (
        main(["generate", "--base", str(tmp_path / "base"), "--seed", "23", "--output", str(out)])
        == 0
    )
    result = decode_document("suite", (out / "suite.json").read_bytes())
    assert len(result.cases) == 3 and len(result.families) == 1
    originals = a.preflight_suite(base_suite, tmp_path / "base")
    for item in a.preflight_suite(result, out):
        assert item.validated.task == originals[0].validated.task
        assert item.gold.confirmation == originals[0].gold.confirmation
        assert item.gold.origin == "generated_contract"
    assert before == files(tmp_path / "base")


def test_demo_resource_selection_22_bases_30_cases_four_families(tmp_path, monkeypatch):
    from sheetbenchkit import cli
    from sheetbenchkit.variants import generate_suite

    bases = []
    for i in range(4):
        case = make_base(tmp_path / f"custom-{i}")
        bases.append(replace(case, manifest=replace(case.manifest, case_id=f"base-{i}")))
    for i in range(18):
        case = make_base(tmp_path / f"plain-{i}")
        bases.append(
            replace(case, manifest=replace(case.manifest, case_id=f"plain-{i}", variant_recipes=()))
        )
    package = tmp_path / "package"
    preset = package / "data/v1"
    suite = generate_suite(tuple(bases), 7, preset)
    assert len(suite.cases) == 30 and len(suite.families) == 4
    before = files(package)
    real_files = cli.resources.files
    monkeypatch.setattr(
        cli.resources,
        "files",
        lambda name: package if name == "sheetbenchkit" else real_files(name),
    )
    out = tmp_path / "demo-generated"
    assert main(["generate", "--demo", "--seed", "71", "--output", str(out)]) == 0
    result = decode_document("suite", (out / "suite.json").read_bytes())
    assert len(result.cases) == 30 and len(result.families) == 4
    assert {c.gold.evidence["seed"] for c in a.preflight_suite(result, out)} == {71}
    assert before == files(package)


def test_generate_existing_destination_preserved_and_nested_new_allowed(tmp_path):
    _, root = frozen(tmp_path)
    before = files(root)
    assert main(["generate", "--base", str(root), "--seed", "1", "--output", str(root)]) == 2
    assert files(root) == before
    assert (
        main(["generate", "--base", str(root), "--seed", "1", "--output", str(root / "nested")])
        == 0
    )
    assert all((root / path).read_bytes() == raw for path, raw in before.items())


def test_public_module_and_script_registration(tmp_path):
    import tomllib

    project = Path(__file__).resolve().parents[1]
    with (project / "pyproject.toml").open("rb") as handle:
        assert (
            tomllib.load(handle)["project"]["scripts"]["sheetbenchkit"] == "sheetbenchkit.cli:main"
        )
    result = subprocess.run(
        [sys.executable, "-m", "sheetbenchkit", "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0 and "grade" in result.stdout


def test_error_suite_report_has_stderr_diagnostic(tmp_path, capsys):
    suite, root = frozen(tmp_path)
    item = a.preflight_suite(suite, root)[0]
    path, _ = saved(tmp_path, suite, root, (replace(observation(item), case_id="outside"),))
    out = tmp_path / "out"
    assert main(grade_args(root, path, out)) == 2
    assert "UNPLANNED_OBSERVATION" in capsys.readouterr().err
    assert (out / "report.json").exists()


def test_generate_explicit_base_role_wins_over_variant_role(tmp_path):
    root = tmp_path / "base"
    families = (m.FamilySpec("first", "a", "b", "c", {}), m.FamilySpec("second", "b", "a", "c", {}))
    a.freeze_suite(tuple(candidate(i) for i in ("a", "b", "c")), families, root)
    out = tmp_path / "generated"
    assert main(["generate", "--base", str(root), "--seed", "1", "--output", str(out)]) == 0
    generated = decode_document("suite", (out / "suite.json").read_bytes())
    assert {case.gold.evidence["base_case_id"] for case in a.preflight_suite(generated, out)} == {
        "a",
        "b",
    }


@pytest.mark.skipif(os.name != "posix", reason="run requires POSIX")
def test_run_defaults_and_results_subdirectory_inside_suite(tmp_path):
    _, root = frozen(tmp_path)
    before = files(root)
    out = root / "results"
    assert main(["run", "--suite", str(root), "--results", str(out), "--", *argv(tmp_path)]) == 0
    batch = decode_document("observations", (out / "observations.json").read_bytes())
    assert batch.runs == (m.RunSpec("adapter", 1),)
    assert len(batch.observations) == starts(tmp_path) == 1
    assert batch.observations[0].runtime_metadata["timeout_s"] == 60
    assert all((root / path).read_bytes() == raw for path, raw in before.items())
    assert main(["validate", "--suite", str(root)]) == 0


@pytest.mark.skipif(os.name != "posix", reason="run requires POSIX")
def test_run_short_timeout_records_failed_slot_and_no_retry(tmp_path):
    _, root = frozen(tmp_path)
    out = tmp_path / "out"
    assert (
        main(
            [
                "run",
                "--suite",
                str(root),
                "--results",
                str(out),
                "--timeout",
                "1",
                "--",
                *argv(tmp_path, "no_read"),
            ]
        )
        == 1
    )
    batch = decode_document("observations", (out / "observations.json").read_bytes())
    assert len(batch.observations) == starts(tmp_path) == 1
    assert batch.observations[0].execution_status == "TIMEOUT"
    report = json.loads((out / "report.json").read_bytes())
    assert report["planned"] == report["attempted"] == report["observed_count"] == 1
    assert report["case_grades"][0]["verdict"] == "NO_RESULT"


@pytest.mark.skipif(os.name != "posix", reason="run requires POSIX")
def test_run_start_error_saved_report_exit_priority(tmp_path, capsys):
    _, root = frozen(tmp_path, 2)
    out = tmp_path / "out"
    assert (
        main(
            [
                "run",
                "--suite",
                str(root),
                "--results",
                str(out),
                "--",
                str(tmp_path / "nonexistent-executable"),
            ]
        )
        == 2
    )
    batch = decode_document("observations", (out / "observations.json").read_bytes())
    assert len(batch.observations) == 2
    assert all(o.execution_status == "START_ERROR" for o in batch.observations)
    report = json.loads((out / "report.json").read_bytes())
    assert report["status_counts"]["ERROR"] == report["planned"] == 2
    assert "START_ERROR" in capsys.readouterr().err


def test_grade_legal_abstain_not_checked_binding_still_zero(tmp_path, monkeypatch):
    item = candidate()
    data = b"other\n1\n2\n"
    manifest = replace(
        item.validated.manifest,
        intentional_boundaries=("MISSING_FIELD",),
        inputs=(replace(item.validated.manifest.inputs[0], sha256=a.sha256_bytes(data)),),
    )
    item = replace(
        item,
        validated=replace(
            item.validated,
            manifest=manifest,
            inputs=replace(item.validated.inputs, snapshots={"work": data}),
        ),
        expected=m.ExpectedResult("ABSTAIN", None, None, "MISSING_FIELD", ()),
    )
    root = tmp_path / "suite"
    suite = a.freeze_suite((item,), (), root)
    path, _ = saved(tmp_path, suite, root)
    out = tmp_path / "out"
    offline_traps(monkeypatch)
    assert main(grade_args(root, path, out)) == 0
    report = json.loads((out / "report.json").read_bytes())
    assert report["case_grades"][0]["binding_check"]["verdict"] == "NOT_CHECKED"
    assert report["status_counts"]["PASS"] == 1


@pytest.mark.parametrize("missing", [False, True])
def test_grade_complete_and_incomplete_family_exit(tmp_path, missing):
    from sheetbenchkit.variants import generate_suite

    base = make_base(tmp_path / "base")
    root = tmp_path / "suite"
    suite = generate_suite((base,), 7, root)
    cases = a.preflight_suite(suite, root)
    observations = tuple(observation(c) for c in cases)
    if missing:
        observations = observations[:-1]
    path, _ = saved(tmp_path, suite, root, observations)
    out = tmp_path / "out"
    assert main(grade_args(root, path, out)) == (1 if missing else 0)
    report = json.loads((out / "report.json").read_bytes())
    assert report["planned"] == report["attempted"] == 3
    assert report["family_grades"][0]["verdict"] == ("NOT_CHECKED" if missing else "PASS")


def test_demo_absent_resource_reports_error_without_output(tmp_path, monkeypatch, capsys):
    from sheetbenchkit import cli

    monkeypatch.setattr(cli.resources, "files", lambda name: tmp_path / "missing-package")
    out = tmp_path / "out"
    assert main(["generate", "--demo", "--seed", "1", "--output", str(out)]) == 2
    assert "ERROR" in capsys.readouterr().err and not out.exists()


def test_grade_io_error_preserves_observations(tmp_path, capsys):
    _, root = frozen(tmp_path)
    out = tmp_path / "out"
    assert main(grade_args(root, tmp_path / "missing.json", out)) == 2
    assert "ERROR CLI_IO_ERROR" in capsys.readouterr().err
    assert not out.exists()


def test_grade_will_not_overwrite_existing_source_file(tmp_path, monkeypatch, capsys):
    from sheetbenchkit import cli

    suite, root = frozen(tmp_path)
    path, _ = saved(tmp_path, suite, root)
    source = tmp_path / "controlled-package"
    source.mkdir()
    (source / "cli.py").write_text("controlled source location")
    (source / "report.json").write_text("preserve source data")
    monkeypatch.setattr(cli, "__file__", str(source / "cli.py"))
    before = files(source)
    assert main(grade_args(root, path, source)) == 2
    assert "OUTPUT_PATH_CONFLICT" in capsys.readouterr().err
    assert files(source) == before


@pytest.mark.skipif(os.name != "posix", reason="run output checks require POSIX execution")
def test_run_output_conflict_starts_zero(tmp_path, capsys):
    _, root = frozen(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    protected = tmp_path / "unrelated.txt"
    protected.write_text("keep")
    (out / "observations.json").symlink_to(protected)
    assert main(["run", "--suite", str(root), "--results", str(out), "--", *argv(tmp_path)]) == 2
    assert starts(tmp_path) == 0 and protected.read_text() == "keep"
    assert "OUTPUT_PATH_CONFLICT" in capsys.readouterr().err


def test_new_result_directory_in_checkout_source_is_allowed(tmp_path, monkeypatch):
    from sheetbenchkit import cli

    suite, root = frozen(tmp_path)
    path, _ = saved(tmp_path, suite, root)
    source = tmp_path / "controlled-package"
    source.mkdir()
    (source / "cli.py").write_text("controlled source location")
    monkeypatch.setattr(cli, "__file__", str(source / "cli.py"))
    assert main(grade_args(root, path, source / "new-results")) == 0
    assert (source / "cli.py").read_text() == "controlled source location"


def test_hardlink_output_cannot_overwrite_observations(tmp_path, capsys):
    suite, root = frozen(tmp_path)
    path, _ = saved(tmp_path, suite, root)
    out = tmp_path / "out"
    out.mkdir()
    (out / "report.json").hardlink_to(path)
    before = files(tmp_path)
    assert main(grade_args(root, path, out)) == 2
    assert files(tmp_path) == before
    assert "OUTPUT_PATH_CONFLICT" in capsys.readouterr().err


@pytest.mark.skipif(os.name != "posix", reason="run suite preflight requires POSIX execution")
def test_run_aggregate_suite_budget_prevents_any_adapter_start(tmp_path, capsys):
    from test_grader import large_cell_items

    root = tmp_path / "suite"
    a.freeze_suite(large_cell_items(10), (), root)
    out = tmp_path / "out"
    assert main(["run", "--suite", str(root), "--results", str(out), "--", *argv(tmp_path)]) == 2
    assert "SUITE_LIMIT" in capsys.readouterr().err
    assert starts(tmp_path) == 0 and not (out / "observations.json").exists()
