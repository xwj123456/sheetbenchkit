"""Saved-state report safety, completeness, and exit semantics."""

import importlib
import json
from dataclasses import replace
from html.parser import HTMLParser

import pytest

from sheetbenchkit import models as m
from sheetbenchkit.artifacts import canonical_json
from sheetbenchkit.contracts import to_document


def capability(name):
    try:
        module = importlib.import_module("sheetbenchkit.report")
    except ModuleNotFoundError:
        pytest.fail(f"Missing T7a capability: report.{name}")
    result = getattr(module, name, None)
    assert callable(result), f"Missing T7a capability: report.{name}"
    return result


def counts(planned=1, observed=1, missing=0, **statuses):
    return {
        "planned": planned,
        "attempted": planned,
        "observed_count": observed,
        "missing_count": missing,
        "status_counts": {s: statuses.get(s, 0) for s in ("PASS", "FAIL", "NO_RESULT", "ERROR")},
    }


def case(case_id="C1", verdict="PASS", reason=None, config="one", **changes):
    result = {"status": "VALUE", "value": "0.8273", "unit": "ratio", "reason": None}
    check = {"verdict": "PASS", "expected": result, "actual": result}
    grade = m.CaseGrade(
        case_id,
        "metric",
        config,
        1,
        "比例",
        "VALUE",
        verdict,
        reason,
        check,
        {"verdict": "PASS", "expected": [], "actual": []},
        {
            "scope": "case",
            "protocol_valid": True,
            "input_paths": ["inputs/work.csv"],
            "execution_status": "SUCCESS",
            "raw_stdout": json.dumps(result),
            "raw_stderr": "",
            "usage": None,
            "runtime_metadata": {},
        },
    )
    return replace(grade, **changes)


def report(grades=None, families=(), **changes):
    grades = (case(),) if grades is None else grades
    stats = counts(len(grades), PASS=sum(g.verdict == "PASS" for g in grades))
    saved = m.SuiteReport(
        "suite",
        (m.RunSpec("one", 1),),
        grades,
        families,
        stats["planned"],
        stats["attempted"],
        stats["observed_count"],
        stats["missing_count"],
        stats["status_counts"],
        {"one": stats},
        {"expected_status": {"VALUE": stats}, "category": {"比例": stats}},
        True,
    )
    return replace(saved, **changes)


class ParsedHTML(HTMLParser):
    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.tags = []
        self.attributes = []
        self.text = []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.attributes.extend(attrs)

    def handle_data(self, data):
        self.text.append(data)

    @property
    def content(self):
        return "".join(self.text)


def render(saved, tmp_path):
    paths = capability("write_report")(saved, tmp_path / "nested" / "report")
    html = paths.html_path.read_text(encoding="utf-8")
    return paths, html, ParsedHTML(html)


def test_escaped_report_preserves_machine_data_and_does_not_execute(tmp_path):
    malicious = '<script>alert("x")</script> & https://evil.invalid/a'
    grade = case(
        malicious,
        "FAIL",
        malicious,
        category=malicious,
        evidence={
            "scope": "case",
            "protocol_valid": True,
            "input_paths": [malicious],
            "execution_status": "SUCCESS",
            "raw_stdout": malicious,
            "raw_stderr": malicious,
            "runtime_metadata": {malicious: malicious},
            "usage": {"tokens": 0},
        },
    )
    saved = report((grade,), suite_id=malicious)
    before = canonical_json(saved)
    paths, html, parsed = render(saved, tmp_path)
    assert paths.json_path.name == "report.json" and paths.html_path.name == "report.html"
    assert paths.json_path.read_bytes() == before
    assert json.loads(paths.json_path.read_text()) == to_document(saved)
    assert canonical_json(saved) == before
    assert "<script>" not in html and malicious in parsed.content
    assert not set(parsed.tags) & {"script", "a", "iframe", "img", "link", "form"}
    assert not any(k.startswith("on") or k in {"src", "href"} for k, _ in parsed.attributes)
    assert 'value="0.8273"' in parsed.content and "unit=ratio" in parsed.content
    assert "82.73%" in parsed.content


def test_complete_config_strata_records_and_family_are_retained(tmp_path):
    missing = case(
        "C_missing",
        "NO_RESULT",
        "MISSING_OUTPUT",
        config="two",
        evidence={
            "scope": "case",
            "protocol_valid": False,
            "execution_status": "UNKNOWN",
            "raw_stdout": "",
            "raw_stderr": "",
            "usage": None,
            "runtime_metadata": {},
        },
        value_check={"verdict": "NOT_CHECKED", "expected": None, "actual": None},
    )
    abstain = case(
        "C_abstain",
        expected_status="ABSTAIN",
        binding_check={"verdict": "NOT_CHECKED", "expected": [], "actual": []},
    )
    grades = (case(), abstain, missing, case("C_error", "ERROR", "START_ERROR"))
    families = (
        m.FamilyGrade("F_pass", 1, "one", "PASS", None),
        m.FamilyGrade("F_missing", 1, "two", "NOT_CHECKED", "INCOMPLETE_FAMILY"),
    )
    saved = report(
        grades,
        families,
        runs=(m.RunSpec("one", 1), m.RunSpec("two", 2)),
        planned=6,
        attempted=6,
        observed_count=3,
        missing_count=3,
        status_counts={"PASS": 2, "FAIL": 0, "NO_RESULT": 3, "ERROR": 1},
        per_config={"one": counts(2, 2, 0, PASS=2), "two": counts(4, 1, 3, NO_RESULT=3, ERROR=1)},
        strata={
            "expected_status": {"VALUE": counts(4), "ABSTAIN": counts(2)},
            "category": {"比例": counts(6)},
        },
    )
    _, html, parsed = render(saved, tmp_path)
    for token in (
        "C1",
        "C_abstain",
        "C_missing",
        "C_error",
        "F_pass",
        "F_missing",
        "VALUE",
        "ABSTAIN",
        "NOT_CHECKED",
        "MISSING_OUTPUT",
        "INCOMPLETE_FAMILY",
        "unknown",
    ):
        assert token in parsed.content
    assert 'data-config="one"' in html and 'data-config="two"' in html
    assert 'data-case="C_missing"' in html
    assert "计划评测槽位" in parsed.content and "结构合法" in parsed.content
    assert "实际调用次数" not in parsed.content
    assert parsed.tags.count("caption") == 4
    assert "异常视图不新增案例" in parsed.content


def test_suite_diagnostic_is_quarantined_not_a_real_case(tmp_path):
    diagnostic = case(
        "__suite__",
        "ERROR",
        "DUPLICATE_SLOT",
        config="__suite__",
        evidence={"scope": "suite", "error_details": {"count": 2}},
    )
    saved = report(
        (diagnostic,),
        valid=False,
        planned=3,
        attempted=3,
        observed_count=0,
        missing_count=0,
        per_config={"one": counts(3, 0, 0)},
        strata={},
        status_counts={"PASS": 0, "FAIL": 0, "NO_RESULT": 0, "ERROR": 1},
    )
    _, html, parsed = render(saved, tmp_path)
    assert 'data-scope="suite"' in html
    assert 'data-case="__suite__"' not in html
    assert 'data-config="__suite__"' not in html
    assert "valid: false" in parsed.content and "隔离" in parsed.content
    assert "DUPLICATE_SLOT" in parsed.content and "unknown" in parsed.content
    assert "0.00%" not in parsed.content


def test_incomplete_non_utf8_streams_and_usage_are_explicit(tmp_path):
    grade = case(
        "C_capture",
        "NO_RESULT",
        "OUTPUT_LIMIT",
        evidence={
            "scope": "case",
            "protocol_valid": False,
            "raw_stdout": "b'\\xff'",
            "raw_stderr": "partial\n" * 120,
            "execution_status": "OUTPUT_LIMIT",
            "usage": {"input_tokens": 0, "output_tokens": None},
            "runtime_metadata": {
                "capture_complete": False,
                "stdout_truncated": True,
                "stdout_encoding_error": True,
                "raw_stdout_base64": "/w==",
                "stdout_representation": "ASCII bytes repr",
            },
        },
    )
    _, html, parsed = render(report((grade,)), tmp_path)
    assert "捕获不完整" in parsed.content and "截断" in parsed.content
    assert "非 UTF-8" in parsed.content and "/w==" in parsed.content
    assert "unknown（缺少价格依据）" in parsed.content
    assert '"input_tokens": 0' in parsed.content and '"output_tokens": null' in parsed.content
    assert "未提供" in parsed.content and "partial\n" * 120 in parsed.content
    assert ("open", None) in parsed.attributes and "summary" in parsed.tags


@pytest.mark.parametrize(
    "saved,want",
    [
        (report(), 0),
        (
            report(
                (case("A", expected_status="ABSTAIN", binding_check={"verdict": "NOT_CHECKED"}),)
            ),
            0,
        ),
        (report((case(verdict="FAIL"),)), 1),
        (report((case(verdict="NO_RESULT", reason="MISSING_OUTPUT"),)), 1),
        (report((case(verdict="ERROR"),)), 2),
        (report(valid=False), 2),
        (report(()), 1),
        (report(planned=2, attempted=2, missing_count=1), 1),
        (report(families=(m.FamilyGrade("F", 1, "one", "NOT_CHECKED", "INCOMPLETE_FAMILY"),)), 1),
        (report(families=(m.FamilyGrade("F", 1, "one", "FAIL", "FAMILY_GOLD_MISMATCH"),)), 1),
        (report(families=(m.FamilyGrade("F", 1, "one", "PASS", None),)), 0),
        (report(families=(m.FamilyGrade("F", 1, "one", "ERROR", "ERROR"),)), 2),
    ],
)
def test_exit_codes_require_saved_complete_case_and_family_pass(saved, want):
    assert capability("exit_code")(saved) == want


def test_input_paths_keep_original_characters_and_missing_result_fields(tmp_path):
    path = 'inputs/中文 空格 "引号" & 表.csv'
    grade = case(
        evidence={"scope": "case", "input_paths": [path], "protocol_valid": True},
        value_check={"verdict": "NOT_CHECKED", "actual": {"unit": "ratio"}},
    )
    _, _, parsed = render(report((grade,)), tmp_path)
    # A JSON-escaped quoted filename is not the user's original selectable path.
    assert path in parsed.text
    assert "value=未提供" in parsed.content
    assert "unit=ratio" in parsed.content


def test_real_grader_saved_report_is_offline_and_complete(tmp_path, monkeypatch):
    import socket
    import subprocess

    from test_artifacts import candidate
    from test_grader import obs

    from sheetbenchkit import artifacts, grader, reference

    suite_root = tmp_path / "suite"
    suite = artifacts.freeze_suite((candidate("real_case"),), (), suite_root)
    frozen = artifacts.preflight_suite(suite, suite_root)
    saved = grader.grade_suite(
        suite,
        frozen,
        m.ObservationBatch(
            "1",
            (m.RunSpec("present", 1), m.RunSpec("missing", 2)),
            (obs(frozen[0], config="present"),),
        ),
    )

    def forbidden(*args, **kwargs):
        pytest.fail("report attempted grading, execution or network")

    monkeypatch.setattr(grader, "grade_suite", forbidden)
    monkeypatch.setattr(reference, "evaluate_reference", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    paths, _, parsed = render(saved, tmp_path)
    machine = json.loads(paths.json_path.read_bytes())
    assert (
        machine["planned"],
        machine["attempted"],
        machine["observed_count"],
        machine["missing_count"],
    ) == (3, 3, 1, 2)
    assert machine["per_config"]["missing"] == counts(2, 0, 2, NO_RESULT=2)
    assert len(machine["case_grades"]) == 3
    assert parsed.content.count("case_id=real_case") >= 3
    assert "MISSING_OUTPUT" in parsed.content and "unknown" in parsed.content
    assert capability("exit_code")(saved) == 1


def test_report_retains_all_cases_and_families_above_preview_size(tmp_path):
    grades = tuple(case(f"C{n:03}") for n in range(100))
    families = tuple(m.FamilyGrade(f"F{n:03}", 1, "one", "PASS", None) for n in range(34))
    paths, html, parsed = render(report(grades, families), tmp_path)
    machine = json.loads(paths.json_path.read_bytes())
    assert len(machine["case_grades"]) == 100 and len(machine["family_grades"]) == 34
    for n in range(100):
        assert f'data-case="C{n:03}"' in html
    for n in range(34):
        assert f"F{n:03}" in parsed.content


@pytest.mark.parametrize(
    "precision,traps,exponent_limit",
    [
        (28, False, 999999),
        (2, False, 999999),
        (2, True, 4),
        (256, True, 4),
    ],
)
@pytest.mark.parametrize(
    "value,percent",
    [
        (
            "999999999999999999999999999999000000000000.000000000000",
            "99999999999999999999999999999900000000000000%",
        ),
        ("0.8273", "82.73%"),
        ("0.000000000001", "0.0000000001%"),
        ("-0.001230000000", "-0.123%"),
        ("1.230000000000", "123%"),
        ("12", "1200%"),
        ("0.000000000000", "0%"),
        # Defensive full protocol-text bound, not a claim of reachable business gold.
        ("9" * 126 + ".0", "9" * 126 + "00%"),
    ],
)
def test_ratio_percent_is_exact_and_preserves_machine_data_in_any_decimal_context(
    tmp_path,
    precision,
    traps,
    exponent_limit,
    value,
    percent,
):
    from decimal import ROUND_DOWN, localcontext

    result = {"status": "VALUE", "value": value, "unit": "ratio", "reason": None}
    grade = case(value_check={"verdict": "PASS", "expected": result, "actual": result})
    saved = report((grade,))
    before = canonical_json(saved)
    with localcontext() as context:
        context.prec = precision
        context.rounding = ROUND_DOWN
        context.Emin = -exponent_limit
        context.Emax = exponent_limit
        for signal in context.traps:
            context.traps[signal] = traps
        context.clear_flags()
        flags_before = dict(context.flags)
        paths, _, parsed = render(saved, tmp_path)
        assert percent in parsed.text
        assert f'value="{value}"' in parsed.content
        assert paths.json_path.read_bytes() == before
        assert canonical_json(saved) == before
        assert dict(context.flags) == flags_before
        assert context.prec == precision and context.rounding == ROUND_DOWN
        assert context.Emin == -exponent_limit and context.Emax == exponent_limit
        assert all(flag is traps for flag in context.traps.values())
