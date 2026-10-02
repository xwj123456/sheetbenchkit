"""Pure grading of frozen gold and recorded outputs; no execution or source inference."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from . import models as m
from .artifacts import (
    MAX_SUITE_BYTES,
    MAX_SUITE_CELLS,
    _read_cells,
    _verify_frozen,
    canonical_json,
    content_hash,
    sha256_bytes,
)
from .contracts import decode_document, strict_json_loads, to_document, validate_schema

_VERDICTS = ("PASS", "FAIL", "NO_RESULT", "ERROR")


def _check(verdict: str, expected: Any = None, actual: Any = None) -> dict[str, Any]:
    return {"verdict": verdict, "expected": expected, "actual": actual}


def _bindings(bindings: tuple[m.Binding, ...]) -> list[dict[str, Any]]:
    # Declaration ordering is immaterial; names, fields and ranges remain exact.
    return [
        {**to_document(binding), "fields": sorted(binding.fields)}
        for binding in sorted(bindings, key=lambda item: item.source)
    ]


def _evidence(case: m.FrozenCase, observation: m.Observation | None) -> dict[str, Any]:
    validated = case.validated
    return {
        "scope": "case",
        "input_paths": [entry.relative_path for entry in validated.manifest.inputs],
        "input_hashes": {entry.file_id: entry.sha256 for entry in validated.manifest.inputs},
        "contract_sha256": case.gold.contract_sha256,
        "manifest_sha256": case.gold.manifest_sha256,
        "task_sha256": sha256_bytes(canonical_json(validated.task)),
        "frozen_sha256": case.content_sha256,
        "gold_origin": case.gold.origin,
        "gold_evidence": to_document(case.gold.evidence),
        "raw_stdout": observation.raw_stdout if observation else "",
        "raw_stderr": observation.raw_stderr if observation else "",
        "execution_status": observation.execution_status if observation else "UNKNOWN",
        "usage": to_document(observation.usage) if observation else None,
        "runtime_metadata": to_document(observation.runtime_metadata) if observation else {},
        "protocol_valid": False,
        "source_claim_scope": "declared_bindings_only",
    }


def _case_grade(
    case: m.FrozenCase,
    observation: m.Observation,
    verdict: str,
    reason: str | None,
    evidence: Mapping[str, Any],
    value_check: Any = None,
    binding_check: Any = None,
) -> m.CaseGrade:
    return m.CaseGrade(
        case.gold.case_id,
        case.validated.spec.metric_id,
        observation.config_id,
        observation.attempt,
        case.validated.manifest.category,
        case.gold.expected.status,
        verdict,
        reason,
        value_check or _check("NOT_CHECKED"),
        binding_check or _check("NOT_CHECKED"),
        evidence,
    )


def grade_case(case: m.FrozenCase, observation: m.Observation) -> m.CaseGrade:
    """Compare exact normalized values and self-reported declarations separately."""
    evidence = {
        "scope": "case",
        "raw_stdout": observation.raw_stdout,
        "raw_stderr": observation.raw_stderr,
        "execution_status": observation.execution_status,
        "protocol_valid": False,
        "source_claim_scope": "declared_bindings_only",
    }
    try:
        evidence = _evidence(case, observation)
        _verify_frozen(case)
        validate_schema(
            "observations",
            m.ObservationBatch(
                "1", (m.RunSpec(observation.config_id, observation.attempt),), (observation,)
            ),
        )
        if observation.case_id != case.gold.case_id:
            raise m.CaseError("OBSERVATION_IDENTITY_MISMATCH")
    except m.CaseError as exc:
        evidence["error_details"] = to_document(exc.details)
        return _case_grade(case, observation, "ERROR", exc.reason, evidence)
    if observation.execution_status != "SUCCESS":
        verdict = "ERROR" if observation.execution_status == "START_ERROR" else "NO_RESULT"
        return _case_grade(case, observation, verdict, observation.execution_status, evidence)
    try:
        strict_json_loads(observation.raw_stdout)
    except m.CaseError as exc:
        evidence["protocol_error"] = {"reason": exc.reason, "details": to_document(exc.details)}
        return _case_grade(case, observation, "NO_RESULT", "INVALID_JSON", evidence)
    try:
        result = decode_document("result", observation.raw_stdout)
        if (result.case_id, result.metric_id) != (case.gold.case_id, case.validated.spec.metric_id):
            raise m.CaseError("RESULT_IDENTITY_MISMATCH")
    except m.CaseError as exc:
        evidence["protocol_error"] = {"reason": exc.reason, "details": to_document(exc.details)}
        return _case_grade(case, observation, "FAIL", "PROTOCOL_VIOLATION", evidence)
    evidence["protocol_valid"] = True
    evidence["agent_result"] = to_document(result)
    expected = case.gold.expected
    expected_value = {
        "status": expected.status,
        "value": expected.value,
        "unit": expected.unit,
        "reason": expected.reason,
    }
    actual_value = {
        "status": result.status,
        "value": result.value,
        "unit": result.unit,
        "reason": result.reason,
    }
    # Rejection permits any legal attempted declarations; the required reason remains exact.
    value_ok = (
        (
            (result.status, result.reason, result.value)
            == (expected.status, expected.reason, expected.value)
        )
        if expected.status == "ABSTAIN"
        else actual_value == expected_value
    )
    binding_ok = _bindings(result.bindings) == _bindings(expected.bindings)
    binding = (
        _check(
            "PASS" if binding_ok else "FAIL",
            _bindings(expected.bindings),
            _bindings(result.bindings),
        )
        if expected.status == "VALUE"
        else _check("NOT_CHECKED", _bindings(expected.bindings), _bindings(result.bindings))
    )
    passed = value_ok and (binding_ok or expected.status == "ABSTAIN")
    if passed:
        reason = None
    elif expected.status == "VALUE" and result.status == "ABSTAIN":
        reason = "UNEXPECTED_ABSTENTION"
    else:
        reason = "VALUE_MISMATCH" if not value_ok else "BINDING_MISMATCH"
    return _case_grade(
        case,
        observation,
        "PASS" if passed else "FAIL",
        reason,
        evidence,
        _check("PASS" if value_ok else "FAIL", expected_value, actual_value),
        binding,
    )


def _counts(grades: tuple[m.CaseGrade, ...], planned: int) -> dict[str, Any]:
    actual = tuple(g for g in grades if g.evidence.get("scope") == "case")
    missing = sum(g.reason == "MISSING_OUTPUT" for g in actual)
    return {
        "planned": planned,
        "attempted": planned,
        "observed_count": len(actual) - missing,
        "missing_count": missing,
        "status_counts": {status: sum(g.verdict == status for g in grades) for status in _VERDICTS},
    }


def _error_report(suite: m.Suite, batch: m.ObservationBatch, error: m.CaseError) -> m.SuiteReport:
    # One explicit suite diagnostic, never counted as a planned or observed execution.
    configs = {run.config_id: run for run in batch.runs}
    planned = len(suite.cases) * sum(
        run.repeats
        for run in configs.values()
        if type(run.repeats) is int and 1 <= run.repeats <= 10
    )
    diagnostic = m.CaseGrade(
        "__suite__",
        "__suite__",
        "__suite__",
        0,
        "infrastructure",
        "UNKNOWN",
        "ERROR",
        error.reason,
        _check("NOT_CHECKED"),
        _check("NOT_CHECKED"),
        {
            "scope": "suite",
            "error_details": to_document(error.details),
            "protocol_valid": False,
            "execution_status": "UNKNOWN",
        },
    )
    per_config = {
        config: _counts((), len(suite.cases) * run.repeats)
        for config, run in configs.items()
        if type(run.repeats) is int and 1 <= run.repeats <= 10
    }
    counts = _counts((diagnostic,), planned)
    return m.SuiteReport(
        suite.suite_id,
        batch.runs,
        (diagnostic,),
        (),
        **counts,
        per_config=per_config,
        strata={},
        valid=False,
    )


def _validate_suite(
    suite: m.Suite, cases: tuple[m.FrozenCase, ...], batch: m.ObservationBatch
) -> None:
    if not 1 <= len(suite.cases) <= 100:
        raise m.CaseError("SUITE_LIMIT")
    validate_schema("suite", suite)
    validate_schema("observations", batch)
    if suite.content_sha256 != content_hash(suite):
        raise m.CaseError("SUITE_HASH_MISMATCH")
    ids = [case.gold.case_id for case in cases]
    if len(set(ids)) != len(ids) or set(ids) != {ref.case_id for ref in suite.cases}:
        raise m.CaseError("FROZEN_CASE_SET_MISMATCH")
    if any(observation.case_id not in ids for observation in batch.observations):
        raise m.CaseError("UNPLANNED_OBSERVATION")
    byte_count = cell_count = 0
    case_map = {case.gold.case_id: case for case in cases}
    for ref in suite.cases:
        case = case_map[ref.case_id]
        _verify_frozen(case)
        for name, document in (
            ("contract", case.validated.spec),
            ("manifest", case.validated.manifest),
            ("task", case.validated.task),
            ("gold", case.gold),
        ):
            if getattr(ref, name + "_sha256") != sha256_bytes(canonical_json(document)):
                raise m.CaseError("ARTIFACT_HASH_MISMATCH", ref.case_id)
        byte_count += sum(len(snapshot) for snapshot in case.validated.inputs.snapshots.values())
        cell_count += _read_cells(case.validated.inputs)
    if byte_count > MAX_SUITE_BYTES or cell_count > MAX_SUITE_CELLS:
        raise m.CaseError("SUITE_LIMIT")


def _signature(grade: m.CaseGrade) -> Any:
    return grade.value_check["actual"]


def _family_grades(
    suite: m.Suite, runs: tuple[m.RunSpec, ...], grades: tuple[m.CaseGrade, ...]
) -> tuple[m.FamilyGrade, ...]:
    slots = {(g.config_id, g.case_id, g.attempt): g for g in grades}
    output = []
    for run in runs:
        for attempt in range(1, run.repeats + 1):
            for family in suite.families:
                base, target, distractor = [
                    slots[(run.config_id, case_id, attempt)]
                    for case_id in (family.base, family.target, family.distractor)
                ]
                members = (base, target, distractor)
                if any(
                    g.verdict in ("NO_RESULT", "ERROR") or g.value_check["verdict"] == "NOT_CHECKED"
                    for g in members
                ):
                    verdict, reason = "NOT_CHECKED", "INCOMPLETE_FAMILY"
                elif not all(g.verdict == "PASS" for g in members):
                    verdict, reason = "FAIL", "FAMILY_GOLD_MISMATCH"
                elif _signature(base) != _signature(distractor) or _signature(base) == _signature(
                    target
                ):
                    verdict, reason = "FAIL", "FAMILY_RELATION_MISMATCH"
                else:
                    verdict, reason = "PASS", None
                output.append(
                    m.FamilyGrade(family.family_id, attempt, run.config_id, verdict, reason)
                )
    return tuple(output)


def grade_suite(
    suite: m.Suite, cases: tuple[m.FrozenCase, ...], observations: m.ObservationBatch
) -> m.SuiteReport:
    """Every declared config/case/attempt slot contributes to the fixed denominator."""
    try:
        _validate_suite(suite, cases, observations)
    except m.CaseError as exc:
        return _error_report(suite, observations, exc)
    slots = {(o.config_id, o.case_id, o.attempt): o for o in observations.observations}
    case_map = {case.gold.case_id: case for case in cases}
    grades = []
    for run in observations.runs:
        for attempt in range(1, run.repeats + 1):
            for ref in suite.cases:
                case = case_map[ref.case_id]
                observation = slots.get((run.config_id, ref.case_id, attempt))
                if observation is None:
                    placeholder = m.Observation(
                        ref.case_id, attempt, run.config_id, "SUCCESS", "", "", None, {}
                    )
                    grades.append(
                        _case_grade(
                            case, placeholder, "NO_RESULT", "MISSING_OUTPUT", _evidence(case, None)
                        )
                    )
                else:
                    grades.append(grade_case(case, observation))
    case_grades = tuple(grades)
    planned = len(suite.cases) * sum(run.repeats for run in observations.runs)
    counts = _counts(case_grades, planned)
    per_config = {
        run.config_id: _counts(
            tuple(g for g in case_grades if g.config_id == run.config_id),
            len(suite.cases) * run.repeats,
        )
        for run in observations.runs
    }
    strata = {}
    for field in ("expected_status", "category"):
        labels = sorted({getattr(g, field) for g in case_grades})
        strata[field] = {
            label: _counts(
                tuple(g for g in case_grades if getattr(g, field) == label),
                sum(getattr(g, field) == label for g in case_grades),
            )
            for label in labels
        }
    return m.SuiteReport(
        suite.suite_id,
        observations.runs,
        case_grades,
        _family_grades(suite, observations.runs, case_grades),
        **counts,
        per_config=per_config,
        strata=strata,
        valid=counts["status_counts"]["ERROR"] == 0,
    )
