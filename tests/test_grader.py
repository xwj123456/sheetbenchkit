"""Frozen evidence grading: protocol, planned denominator, relations and preflight."""

import dataclasses as d
import importlib
import json
import socket
import subprocess
from pathlib import Path

import pytest
from test_artifacts import candidate

from sheetbenchkit import artifacts as a
from sheetbenchkit import models as m
from sheetbenchkit.contracts import to_document


def capability(module, name):
    try:
        loaded = importlib.import_module("sheetbenchkit." + module)
    except ModuleNotFoundError:
        pytest.fail(f"Missing T5 capability: {module}.{name}")
    result = getattr(loaded, name, None)
    assert callable(result), f"Missing T5 capability: {module}.{name}"
    return result


def frozen(tmp_path, items=None, families=()):
    root = tmp_path / "suite"
    suite = a.freeze_suite(items or (candidate(),), families, root)
    return suite, capability("artifacts", "preflight_suite")(suite, root), root


def output(case, **changes):
    result = {
        "schema_version": "1",
        "case_id": case.gold.case_id,
        "metric_id": case.validated.spec.metric_id,
        **to_document(case.gold.expected),
    }
    result.update(changes)
    return json.dumps(result, ensure_ascii=False)


def obs(case, raw=None, config="one", attempt=1, execution="SUCCESS"):
    return m.Observation(
        case.gold.case_id,
        attempt,
        config,
        execution,
        output(case) if raw is None else raw,
        "",
        None,
        {"elapsed_s": 0.1},
    )


def grade(case, observation):
    return capability("grader", "grade_case")(case, observation)


def suite_grade(suite, cases, observations=(), runs=None):
    return capability("grader", "grade_suite")(
        suite, cases, m.ObservationBatch("1", runs or (m.RunSpec("one", 1),), observations)
    )


def test_grader_reads_only_frozen_gold(tmp_path, monkeypatch):
    suite, cases, _ = frozen(tmp_path)
    from sheetbenchkit import reference

    def forbidden(*args, **kwargs):
        pytest.fail("grade attempted forbidden side effect")

    monkeypatch.setattr(reference, "evaluate_reference", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    original_open = Path.open

    def private_open(path, *args, **kwargs):
        if "credential" in str(path) or ".env" in str(path):
            forbidden()
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", private_open)
    report = suite_grade(suite, cases, (obs(cases[0]),))
    assert report.valid and report.case_grades[0].verdict == "PASS"
    assert a.canonical_json(report) == a.canonical_json(suite_grade(suite, cases, (obs(cases[0]),)))
    assert report.case_grades[0].evidence["source_claim_scope"] == "declared_bindings_only"


@pytest.mark.parametrize(
    "raw", ["garbage", "```json\n{}\n```", "{}{}", '{"a":NaN}', '{"a":1,"a":2}', '{"a":1e999}', ""]
)
def test_illegal_json_is_no_result(tmp_path, raw):
    _, cases, _ = frozen(tmp_path)
    result = grade(cases[0], obs(cases[0], raw))
    assert (result.verdict, result.reason) == ("NO_RESULT", "INVALID_JSON")


@pytest.mark.parametrize(
    "change",
    [
        {"bindings": []},
        {"case_id": "other"},
        {"metric_id": "other"},
        {"value": "03.00"},
        {"extra": "unrequested"},
        {"value": 3},
        {"value": "-0.00"},
    ],
)
def test_legal_wrong_protocol_fails(tmp_path, change):
    _, cases, _ = frozen(tmp_path)
    result = grade(cases[0], obs(cases[0], output(cases[0], **change)))
    assert (result.verdict, result.reason) == ("FAIL", "PROTOCOL_VIOLATION")


def test_missing_bindings_and_numeric_binding_separation(tmp_path):
    _, cases, _ = frozen(tmp_path)
    case = cases[0]
    raw = json.loads(output(case))
    del raw["bindings"]
    assert grade(case, obs(case, json.dumps(raw))).reason == "PROTOCOL_VIOLATION"
    wrong_value = grade(case, obs(case, output(case, value="13.00")))
    assert wrong_value.verdict == "FAIL"
    assert wrong_value.value_check["verdict"] == "FAIL"
    assert wrong_value.binding_check["verdict"] == "PASS"
    wrong_binding = to_document(case.gold.expected.bindings)
    wrong_binding[0]["file_id"] = "wrong"
    result = grade(case, obs(case, output(case, bindings=wrong_binding)))
    assert result.verdict == "FAIL"
    assert result.value_check["verdict"] == "PASS"
    assert result.binding_check["verdict"] == "FAIL"
    for changes in (
        {"value": "3.0"},
        {"unit": "USD"},
        {"status": "ABSTAIN", "value": None, "reason": "MISSING_FIELD", "bindings": []},
    ):
        assert grade(case, obs(case, output(case, **changes))).verdict == "FAIL"


def test_B22_accepts_only_missing_field(tmp_path):
    item = candidate("B22")
    manifest = d.replace(item.validated.manifest, intentional_boundaries=("MISSING_FIELD",))
    entry = manifest.inputs[0]
    snapshot = b"other\n1\n2\n"
    manifest = d.replace(manifest, inputs=(d.replace(entry, sha256=a.sha256_bytes(snapshot)),))
    case = d.replace(
        item.validated,
        manifest=manifest,
        inputs=d.replace(item.validated.inputs, snapshots={"work": snapshot}),
    )
    expected = m.ExpectedResult("ABSTAIN", None, None, "MISSING_FIELD", ())
    _, cases, _ = frozen(tmp_path, (d.replace(item, validated=case, expected=expected),))
    case = cases[0]
    assert grade(case, obs(case)).verdict == "PASS"
    assert grade(case, obs(case, output(case, reason="MISSING_VALUE"))).verdict == "FAIL"
    assert (
        grade(
            case,
            obs(
                case,
                output(
                    case,
                    status="VALUE",
                    value="0.00",
                    unit="CNY",
                    reason=None,
                    bindings=to_document(item.expected.bindings),
                ),
            ),
        ).verdict
        == "FAIL"
    )


@pytest.mark.parametrize("execution", ["TIMEOUT", "NONZERO_EXIT", "OUTPUT_LIMIT", "START_ERROR"])
def test_execution_failures_keep_denominator(tmp_path, execution):
    suite, cases, _ = frozen(tmp_path)
    report = suite_grade(suite, cases, (obs(cases[0], execution=execution),))
    verdict = "ERROR" if execution == "START_ERROR" else "NO_RESULT"
    assert report.case_grades[0].verdict == verdict
    assert report.valid == (verdict != "ERROR")
    assert report.attempted == report.planned == 1


def test_counts_missing_round_and_configs(tmp_path):
    suite, cases, _ = frozen(tmp_path, tuple(candidate(f"B{i}") for i in range(30)))
    observations = tuple(obs(case, attempt=attempt) for attempt in (1, 2) for case in cases)
    report = suite_grade(suite, cases, observations, (m.RunSpec("one", 3),))
    assert report.attempted == report.planned == 90
    assert report.observed_count == 60
    assert report.missing_count == report.status_counts["NO_RESULT"] == 30
    assert all(
        g.evidence["execution_status"] == "UNKNOWN"
        for g in report.case_grades
        if g.reason == "MISSING_OUTPUT"
    )
    report = suite_grade(
        suite,
        cases,
        (obs(cases[0]), obs(cases[0], output(cases[0], value="99.00"), "two")),
        (m.RunSpec("one", 1), m.RunSpec("two", 1)),
    )
    assert report.per_config["one"]["status_counts"]["PASS"] == 1
    assert report.per_config["two"]["status_counts"]["FAIL"] == 1
    assert report.per_config["one"]["attempted"] == report.per_config["two"]["attempted"] == 30
    assert report.strata["expected_status"]["VALUE"]["attempted"] == 60


@pytest.mark.parametrize(
    "mutation",
    [
        "duplicate",
        "unknown_config",
        "unknown_case",
        "unknown_attempt",
        "duplicate_run",
        "empty_runs",
        "empty_suite",
        "missing_case",
        "stale_case",
    ],
)
def test_duplicate_identity_and_keys(tmp_path, mutation):
    suite, cases, _ = frozen(tmp_path)
    batch = m.ObservationBatch("1", (m.RunSpec("one", 1),), (obs(cases[0]),))
    if mutation == "duplicate":
        batch = d.replace(batch, observations=batch.observations * 2)
    elif mutation in ("unknown_config", "unknown_case", "unknown_attempt"):
        field, value = {
            "unknown_config": ("config_id", "other"),
            "unknown_case": ("case_id", "other"),
            "unknown_attempt": ("attempt", 2),
        }[mutation]
        batch = d.replace(batch, observations=(d.replace(batch.observations[0], **{field: value}),))
    elif mutation == "duplicate_run":
        batch = d.replace(batch, runs=batch.runs * 2)
    elif mutation == "empty_runs":
        batch = d.replace(batch, runs=())
    elif mutation == "empty_suite":
        suite = d.replace(suite, cases=())
    elif mutation == "missing_case":
        cases = ()
    else:
        cases = (d.replace(cases[0], content_sha256="0" * 64),)
    report = capability("grader", "grade_suite")(suite, cases, batch)
    assert not report.valid
    assert report.status_counts["ERROR"] >= 1


def family_items():
    base = candidate("base")
    target = candidate("target")
    snapshot = b"amount\n1\n3\n"
    manifest = target.validated.manifest
    manifest = d.replace(
        manifest, inputs=(d.replace(manifest.inputs[0], sha256=a.sha256_bytes(snapshot)),)
    )
    target = d.replace(
        target,
        validated=d.replace(
            target.validated,
            manifest=manifest,
            inputs=d.replace(target.validated.inputs, snapshots={"work": snapshot}),
        ),
        expected=d.replace(target.expected, value="4.00"),
    )
    return (base, target, candidate("distractor"))


def test_family_requires_all_gold_and_relations_per_config_attempt(tmp_path):
    family = m.FamilySpec("F", "base", "target", "distractor", {"target": "A3"})
    suite, cases, _ = frozen(tmp_path, family_items(), (family,))
    observations = tuple(
        obs(case, config=config, attempt=attempt)
        for config in ("one", "two")
        for attempt in (1, 2)
        for case in cases
    )
    report = suite_grade(suite, cases, observations, (m.RunSpec("one", 2), m.RunSpec("two", 2)))
    assert len(report.family_grades) == 4
    assert all(g.verdict == "PASS" for g in report.family_grades)
    shifted = tuple(
        obs(c, output(c, value=("14.00" if c.gold.case_id == "target" else "13.00"))) for c in cases
    )
    assert suite_grade(suite, cases, shifted).family_grades[0].verdict == "FAIL"
    missing = suite_grade(suite, cases, tuple(obs(c) for c in cases if c.gold.case_id != "target"))
    assert missing.family_grades[0].verdict == "NOT_CHECKED"
    assert missing.status_counts["NO_RESULT"] == 1


@pytest.mark.parametrize("kind", ["suite", "contract", "manifest", "task", "gold", "input"])
def test_preflight_rejects_persisted_tampering(tmp_path, kind):
    suite, _, root = frozen(tmp_path)
    ref = suite.cases[0]
    path = root / (
        "suite.json"
        if kind == "suite"
        else "cases/base-001/inputs/work.csv"
        if kind == "input"
        else getattr(ref, kind + "_path")
    )
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(m.CaseError):
        capability("artifacts", "preflight_suite")(suite, root)


def test_preflight_duplicate_json_even_with_updated_hash(tmp_path):
    suite, _, root = frozen(tmp_path)
    ref = suite.cases[0]
    path = root / ref.task_path
    raw = b'{"description":"a","description":"b"}'
    path.write_bytes(raw)
    ref = d.replace(ref, task_sha256=a.sha256_bytes(raw))
    suite = d.replace(suite, cases=(ref,))
    suite = d.replace(suite, content_sha256=a.content_hash(suite))
    (root / "suite.json").write_bytes(a.canonical_json(suite))
    with pytest.raises(m.CaseError, match="DUPLICATE_JSON_KEY"):
        capability("artifacts", "preflight_suite")(suite, root)


def test_preflight_path_escape_and_oversized_snapshot(tmp_path):
    suite, _, root = frozen(tmp_path)
    ref = suite.cases[0]
    path = root / ref.gold_path
    outside = tmp_path / "outside.json"
    outside.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(outside)
    with pytest.raises(m.CaseError, match="PATH_ESCAPE"):
        capability("artifacts", "preflight_suite")(suite, root)
    path.unlink()
    path.write_bytes(outside.read_bytes())
    (root / "cases/base-001/inputs/work.csv").write_bytes(b"x" * (10 * 1024 * 1024 + 1))
    with pytest.raises(m.CaseError, match="UNVERIFIED_SNAPSHOT"):
        capability("artifacts", "preflight_suite")(suite, root)


def test_preflight_suite_aggregate_budgets_and_case_count(tmp_path, monkeypatch):
    suite, _, root = frozen(tmp_path)
    preflight = capability("artifacts", "preflight_suite")
    with pytest.raises(m.CaseError, match="SUITE_LIMIT"):
        preflight(d.replace(suite, cases=suite.cases * 101), root)
    monkeypatch.setattr(a, "MAX_SUITE_BYTES", 2)
    with pytest.raises(m.CaseError, match="SUITE_LIMIT"):
        preflight(suite, root)
    monkeypatch.setattr(a, "MAX_SUITE_BYTES", 128 * 1024 * 1024)
    monkeypatch.setattr(a, "MAX_SUITE_CELLS", 1)
    with pytest.raises(m.CaseError, match="SUITE_LIMIT"):
        preflight(suite, root)


def test_two_sources_binding_and_field_order_is_immaterial(tmp_path):
    item = candidate("ratio")
    spec = d.replace(
        item.validated.spec,
        sources={
            "n": m.SourceSpec("work", "@csv", 1, (2, 3)),
            "d": m.SourceSpec("work", "@csv", 1, (2, 3)),
        },
        metric=m.RatioSpec(
            "ratio",
            m.AggregateSumSpec("sum", "n", "amount", "CNY", m.FilterSpec("kind", "keep")),
            m.CountSpec("count", "d", m.FilterSpec("kind", "keep")),
            2,
            "HALF_UP",
        ),
    )
    snapshot = b"amount,kind\n1,keep\n2,keep\n"
    confirmation = {
        **item.confirmation,
        "reviewed_contract_sha256": a.sha256_bytes(a.canonical_json(spec)),
    }
    manifest = d.replace(
        item.validated.manifest,
        business_confirmation=confirmation,
        inputs=(d.replace(item.validated.manifest.inputs[0], sha256=a.sha256_bytes(snapshot)),),
    )
    expected = m.ExpectedResult(
        "VALUE",
        "1.50",
        "ratio",
        None,
        (
            m.Binding("n", "work", "@csv", 1, (2, 3), ("amount", "kind")),
            m.Binding("d", "work", "@csv", 1, (2, 3), ("kind",)),
        ),
    )
    validated = d.replace(
        item.validated,
        spec=spec,
        manifest=manifest,
        inputs=d.replace(item.validated.inputs, snapshots={"work": snapshot}),
    )
    _, cases, _ = frozen(
        tmp_path,
        (d.replace(item, validated=validated, expected=expected, confirmation=confirmation),),
    )
    bindings = to_document(expected.bindings)
    bindings.reverse()
    bindings[1]["fields"].reverse()
    result = grade(cases[0], obs(cases[0], output(cases[0], bindings=bindings)))
    assert result.verdict == "PASS"
    assert result.evidence["agent_result"]["bindings"][0]["source"] == "d"
    bindings[1]["fields"] = ["amount"]
    assert grade(cases[0], obs(cases[0], output(cases[0], bindings=bindings))).verdict == "FAIL"


def large_cell_items(count):
    items = []
    headers = ("amount",) + tuple(f"col{i}" for i in range(99))
    snapshot = (
        ",".join(headers) + "\n" + ("1," + ",".join("x" for _ in range(99)) + "\n") * 1000
    ).encode()
    for index in range(count):
        item = candidate(f"C{index}")
        spec = d.replace(
            item.validated.spec, sources={"target": m.SourceSpec("work", "@csv", 1, (2, 1001))}
        )
        confirmation = {
            **item.confirmation,
            "reviewed_contract_sha256": a.sha256_bytes(a.canonical_json(spec)),
        }
        manifest = d.replace(
            item.validated.manifest,
            business_confirmation=confirmation,
            inputs=(d.replace(item.validated.manifest.inputs[0], sha256=a.sha256_bytes(snapshot)),),
        )
        expected = d.replace(
            item.expected,
            value="1000.00",
            bindings=(d.replace(item.expected.bindings[0], rows=(2, 1001)),),
        )
        validated = d.replace(
            item.validated,
            spec=spec,
            manifest=manifest,
            inputs=d.replace(item.validated.inputs, snapshots={"work": snapshot}),
        )
        items.append(
            d.replace(item, validated=validated, expected=expected, confirmation=confirmation)
        )
    return tuple(items)


@pytest.mark.parametrize("count", [9, 10, 11])
def test_preflight_independently_counts_real_cells(tmp_path, count):
    root = tmp_path / "large"
    suite = a.freeze_suite(large_cell_items(count), (), root)  # supplied tables are empty
    preflight = capability("artifacts", "preflight_suite")
    if count == 9:
        cases = preflight(suite, root)
        assert len(cases) == 9
        report = suite_grade(suite, cases, tuple(obs(case) for case in cases))
        assert report.valid and report.status_counts["PASS"] == 9
        return
    with pytest.raises(m.CaseError, match="SUITE_LIMIT") as caught:
        preflight(suite, root)
    assert caught.value.details["read_cells"] == 1_001_000


def test_freeze_includes_headers_in_read_budget(tmp_path):
    items = large_cell_items(10)
    source_root = tmp_path / "source"
    suite = a.freeze_suite(items[:1], (), source_root)
    table = capability("artifacts", "preflight_suite")(suite, source_root)[
        0
    ].validated.inputs.tables
    loaded = tuple(
        d.replace(
            item,
            validated=d.replace(
                item.validated, inputs=d.replace(item.validated.inputs, tables=table)
            ),
        )
        for item in items
    )
    under_limit = a.freeze_suite(loaded[:9], (), tmp_path / "nine")
    assert len(under_limit.cases) == 9  # 900,900 actual selected cells
    with pytest.raises(m.CaseError, match="SUITE_LIMIT"):
        a.freeze_suite(loaded, (), tmp_path / "ten")  # 1,001,000 includes 1,000 headers
    assert not (tmp_path / "ten").exists()


def test_grade_includes_headers_in_read_budget(tmp_path, monkeypatch):
    root = tmp_path / "large"
    suite = a.freeze_suite(large_cell_items(10), (), root)
    # Obtain all frozen inputs with a test-local preflight cap; grader retains its real cap.
    with monkeypatch.context() as scoped:
        scoped.setattr(a, "MAX_SUITE_CELLS", 1_001_000)
        cases = capability("artifacts", "preflight_suite")(suite, root)
    report = suite_grade(suite, cases, tuple(obs(case) for case in cases))
    assert not report.valid
    assert report.status_counts["ERROR"] == 1
    assert report.case_grades[0].reason == "SUITE_LIMIT"
    assert report.case_grades[0].evidence["scope"] == "suite"
    assert report.attempted == report.planned == 10


def test_protocol_valid_abstain_against_value_has_exact_reason(tmp_path):
    _, cases, _ = frozen(tmp_path)
    case = cases[0]
    result = grade(
        case,
        obs(case, output(case, status="ABSTAIN", value=None, reason="MISSING_FIELD", bindings=[])),
    )
    assert result.verdict == "FAIL"
    assert result.reason == "UNEXPECTED_ABSTENTION"
    assert result.evidence["protocol_valid"] is True
    assert result.value_check["verdict"] == "FAIL"


@pytest.mark.parametrize(
    "tamper",
    ["gold_identity", "gold_contract", "confirmation", "invalid_version", "invalid_binding"],
)
def test_preflight_checks_rehashed_business_evidence(tmp_path, tamper):
    suite, _, root = frozen(tmp_path)
    ref = suite.cases[0]
    path = root / ref.gold_path
    document = json.loads(path.read_bytes())
    if tamper == "gold_identity":
        document["case_id"] = "other"
    elif tamper == "gold_contract":
        document["contract_sha256"] = "0" * 64
    elif tamper == "confirmation":
        document["confirmation"]["task_sha256"] = "0" * 64
    elif tamper == "invalid_version":
        document["schema_version"] = "2"
    else:
        document["expected"]["bindings"][0]["file_id"] = "wrong"
    raw = a.canonical_json(document)
    path.write_bytes(raw)
    ref = d.replace(ref, gold_sha256=a.sha256_bytes(raw))
    suite = d.replace(suite, cases=(ref,))
    suite = d.replace(suite, content_sha256=a.content_hash(suite))
    (root / "suite.json").write_bytes(a.canonical_json(suite))
    with pytest.raises(m.CaseError):
        capability("artifacts", "preflight_suite")(suite, root)


def test_grade_case_invalid_metadata_is_error(tmp_path):
    _, cases, _ = frozen(tmp_path)
    observation = d.replace(obs(cases[0]), runtime_metadata={"elapsed": float("nan")})
    result = grade(cases[0], observation)
    assert result.verdict == "ERROR"
    assert result.reason == "INVALID_JSON"
    assert result.evidence["scope"] == "case"
    assert result.evidence["raw_stdout"] == observation.raw_stdout


def test_regrading_has_zero_external_accesses(tmp_path, monkeypatch):
    import builtins
    import os

    suite, cases, _ = frozen(tmp_path)
    original_open = builtins.open
    original_path_open = Path.open
    original_getenv = os.getenv
    credential = tmp_path / "synthetic-credential.txt"
    credential.write_text("synthetic-secret-not-a-real-key")
    accesses = {"socket": 0, "process": 0, "credential": 0}

    def forbidden(kind):
        def probe(*args, **kwargs):
            accesses[kind] += 1
            raise AssertionError("unexpected " + kind)

        return probe

    def checked_open(path, *args, **kwargs):
        if str(path) == str(credential):
            forbidden("credential")()
        return original_open(path, *args, **kwargs)

    def checked_path_open(path, *args, **kwargs):
        if path == credential:
            forbidden("credential")()
        return original_path_open(path, *args, **kwargs)

    def checked_getenv(key, *args):
        if key == "SHEETBENCHKIT_SYNTHETIC_CREDENTIAL":
            forbidden("credential")()
        return original_getenv(key, *args)

    monkeypatch.setenv("SHEETBENCHKIT_SYNTHETIC_CREDENTIAL", str(credential))
    monkeypatch.setattr(builtins, "open", checked_open)
    monkeypatch.setattr(Path, "open", checked_path_open)
    monkeypatch.setattr(os, "getenv", checked_getenv)
    monkeypatch.setattr(socket.socket, "connect", forbidden("socket"))
    monkeypatch.setattr(socket, "create_connection", forbidden("socket"))
    monkeypatch.setattr(subprocess, "Popen", forbidden("process"))
    monkeypatch.setattr(os, "system", forbidden("process"))
    batch = (obs(cases[0]),)
    first = suite_grade(suite, cases, batch)
    second = suite_grade(suite, cases, batch)
    assert first.case_grades[0].verdict == "PASS"
    assert a.content_hash(first) == a.content_hash(second)
    assert accesses == {"socket": 0, "process": 0, "credential": 0}


def test_unverified_in_memory_snapshot_cannot_score(tmp_path):
    suite, cases, _ = frozen(tmp_path)
    case = cases[0]
    validated = d.replace(case.validated, inputs=d.replace(case.validated.inputs, snapshots={}))
    unverified = d.replace(case, validated=validated)
    assert grade(unverified, obs(case)).verdict == "ERROR"
    report = suite_grade(suite, (unverified,), (obs(case),))
    assert not report.valid
    assert report.status_counts["ERROR"] == 1
    assert report.attempted == 1
    assert report.observed_count == 0
    assert report.case_grades[0].evidence["scope"] == "suite"


def test_error_diagnostic_preserves_plan_without_config_or_execution_invention(tmp_path):
    suite, cases, _ = frozen(tmp_path)
    observation = obs(cases[0])
    report = suite_grade(suite, cases, (observation, observation), (m.RunSpec("one", 3),))
    assert report.planned == report.attempted == 3
    assert report.observed_count == report.missing_count == 0
    assert set(report.per_config) == {"one"}
    assert report.per_config["one"]["planned"] == 3
    assert report.status_counts == {"PASS": 0, "FAIL": 0, "NO_RESULT": 0, "ERROR": 1}
    assert report.case_grades[0].expected_status == "UNKNOWN"
    assert report.case_grades[0].attempt == 0
    assert report.case_grades[0].evidence["scope"] == "suite"
