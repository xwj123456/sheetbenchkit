"""Offline, escaped presentation of saved grades; never evaluates or repairs results."""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Mapping
from html import escape
from importlib.resources import files
from pathlib import Path
from string import Template
from typing import Any

from .artifacts import canonical_json
from .contracts import to_document
from .models import CaseGrade, ReportPaths, SuiteReport

_STATUSES = ("PASS", "FAIL", "NO_RESULT", "ERROR")
_LABELS = {
    "PASS": "通过",
    "FAIL": "断言失败",
    "NO_RESULT": "缺少结果",
    "ERROR": "工具或案例错误",
    "NOT_CHECKED": "未检查",
}
_MISSING = object()


def _text(value: Any = _MISSING) -> str:
    if value is _MISSING:
        return "未提供"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    return escape(str(value), quote=True)


def _json(value: Any = _MISSING) -> str:
    if value is _MISSING:
        return "未提供"
    return escape(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), quote=True)


def _badge(status: Any) -> str:
    css = status if status in _LABELS else "UNKNOWN"
    label = f" · {_LABELS[status]}" if status in _LABELS else ""
    return f'<span class="badge {css}">{_text(status)}{label}</span>'


def _block(label: str, value: Any = _MISSING, *, folded: bool = False) -> str:
    content = f"<pre>{_json(value)}</pre>"
    if folded:
        return f"<details><summary>{escape(label)}</summary>{content}</details>"
    return f'<div class="field"><h4>{escape(label)}</h4>{content}</div>'


def _percent(value: str) -> str | None:
    """Shift bounded fixed decimal text exactly; no ambient Decimal arithmetic."""
    if len(value) > 128 or not re.fullmatch(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", value):
        return None
    sign = "-" if value.startswith("-") else ""
    integer, _, fraction = value.lstrip("-").partition(".")
    digits = integer + fraction.ljust(2, "0")
    point = len(integer) + 2
    whole = digits[:point].lstrip("0") or "0"
    tail = digits[point:].rstrip("0")
    if whole == "0" and not tail:
        sign = ""
    return sign + whole + ("." + tail if tail else "") + "%"


def _result(value: Any) -> str:
    if not isinstance(value, Mapping):
        return _block("保存值", value)
    content = []
    if value.get("unit") == "ratio" and isinstance(value.get("value"), str):
        percent = _percent(value["value"])
        if percent is not None:
            content.append(f'<p class="mono">{_text(percent)}</p>')
    if "value" in value or "unit" in value:
        original = (
            'value="' + _text(value["value"]) + '"'
            if isinstance(value.get("value"), str)
            else "value=" + _text(value.get("value", _MISSING))
        )
        content.append(
            f'<p class="mono">{original} · unit={_text(value.get("unit", _MISSING))}</p>'
        )
    content.append(f"<pre>{_json(value)}</pre>")
    return "".join(content)


def _check(value: Any) -> str:
    if not isinstance(value, Mapping):
        return _block("保存检查", value)
    return (
        _badge(value.get("verdict", "未提供"))
        + "<h4>expected（保存金标）</h4>"
        + _result(value.get("expected", _MISSING))
        + "<h4>actual（保存结果）</h4>"
        + _result(value.get("actual", _MISSING))
        + _block("完整检查记录", value, folded=True)
    )


def _table(caption: str, headers: tuple[str, ...], rows: list[list[str]]) -> str:
    heading = "".join(f'<th scope="col">{escape(h)}</th>' for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>" for row in rows)
    if not rows:
        body = f'<tr><td colspan="{len(headers)}">没有保存记录</td></tr>'
    return (
        '<p class="scroll-note">表格可横向滚动查看全部列</p>'
        f'<div class="table-scroll" tabindex="0" role="region" aria-label="{escape(caption)}">'
        f"<table><caption>{escape(caption)}</caption><thead><tr>{heading}</tr></thead>"
        f"<tbody>{body}</tbody></table></div>"
    )


def _stats(stats: Mapping[str, Any], protocol_valid: int | None = None) -> str:
    labels = {
        "planned": "planned · 计划评测槽位",
        "attempted": "attempted · 计划评测槽位",
        "observed_count": "observed_count · 已保存观测",
        "missing_count": "missing_count · 缺失观测",
    }
    cells = [
        f"<div><dt>{label}</dt><dd>{_text(stats.get(key, _MISSING))}</dd></div>"
        for key, label in labels.items()
    ]
    if protocol_valid is not None:
        cells.append(
            f"<div><dt>结构合法 · 保存 protocol_valid=true 的案例数</dt>"
            f"<dd>{protocol_valid}</dd></div>"
        )
    statuses = stats.get("status_counts", {})
    cells.extend(
        f"<div><dt>{_badge(status)}</dt><dd>{_text(statuses.get(status, _MISSING))}</dd></div>"
        for status in _STATUSES
    )
    return '<dl class="stats">' + "".join(cells) + "</dl>"


def _identity(grade: Mapping[str, Any]) -> str:
    return (
        '<p class="mono">'
        + " · ".join(
            f"{name}={_text(grade.get(name, _MISSING))}"
            for name in ("config_id", "case_id", "metric_id", "attempt")
        )
        + "</p>"
    )


def _stream(evidence: Mapping[str, Any], stream: str, missing: bool) -> str:
    metadata = evidence.get("runtime_metadata") or {}
    incomplete = metadata.get("capture_complete") is False or any(
        metadata.get(key) is True for key in ("stdout_truncated", "stderr_truncated")
    )
    note = (
        "捕获不完整，可能截断"
        if incomplete
        else ("完整捕获文本" if metadata.get("capture_complete") is True else "捕获完整性未提供")
    )
    label = f"保存的 {stream}（{note}）"
    raw = evidence.get("raw_" + stream, _MISSING)
    prefix = "<p>缺少观测；原始输出未提供。保存占位文本如下。</p>" if missing else ""
    if metadata.get(stream + "_encoding_error") is True:
        prefix += "<p>非 UTF-8：展示 ASCII bytes repr；base64 保留精确捕获字节，仅作为文本。</p>"
        prefix += _block(
            stream + " representation / base64",
            {
                "representation": metadata.get(stream + "_representation"),
                "base64": metadata.get("raw_" + stream + "_base64"),
            },
        )
    # Preserve the actual stream, including blank lines, without JSON string escaping.
    return prefix + f"<details><summary>{escape(label)}</summary><pre>{_text(raw)}</pre></details>"


def _paths(evidence: Mapping[str, Any]) -> str:
    paths = evidence.get("input_paths", _MISSING)
    content = (
        "".join(f"<pre>{_text(path)}</pre>" for path in paths)
        if isinstance(paths, list)
        else f"<pre>{_text(paths)}</pre>"
    )
    return (
        '<div class="field"><h4>输入定位 · input_paths（相对路径，纯文本）</h4>'
        + content
        + "</div>"
    )


def _evidence(grade: Mapping[str, Any], *, open_default: bool) -> str:
    evidence = grade["evidence"]
    suite_scope = evidence.get("scope") == "suite"
    attr = ' data-scope="suite"' if suite_scope else f' data-case="{_text(grade["case_id"])}"'
    opened = " open" if open_default else ""
    missing = grade.get("reason") == "MISSING_OUTPUT"
    title = _badge(grade["verdict"]) + " · " + _text(grade["case_id"])
    prefix = "<p>套件诊断；不是案例、观测或执行槽位。</p>" if suite_scope else ""
    return (
        f'<details class="evidence"{opened}{attr}><summary>{title}</summary>'
        + prefix
        + _identity(grade)
        + '<p>reason: <span class="mono">'
        + _text(grade["reason"])
        + "</span></p><p>expected_status: "
        + _text(grade["expected_status"])
        + "</p>"
        + "<h4>value_check</h4>"
        + _check(grade["value_check"])
        + "<h4>binding_check</h4>"
        + _check(grade["binding_check"])
        + _paths(evidence)
        + _block(
            "输入与合同 hash",
            {
                key: value
                for key, value in evidence.items()
                if key == "input_hashes" or key.endswith("sha256")
            },
        )
        + '<p>execution_status: <span class="mono">'
        + _text(evidence.get("execution_status", _MISSING))
        + "</span></p>"
        + ("<p>缺少观测；启动情况 unknown。</p>" if missing else "")
        + _stream(evidence, "stdout", missing)
        + _stream(evidence, "stderr", missing)
        + _block("usage（null 表示未知）", evidence.get("usage", _MISSING), folded=True)
        + _block("runtime metadata", evidence.get("runtime_metadata", _MISSING), folded=True)
        + _block("完整 evidence · 全部保存字段", evidence, folded=True)
        + "</details>"
    )


def _case_key(grade: Mapping[str, Any]) -> tuple[str, int, str]:
    return grade["config_id"], grade["attempt"], grade["case_id"]


def _body(document: Mapping[str, Any]) -> str:
    genuine = sorted(
        (g for g in document["case_grades"] if g["evidence"].get("scope") == "case"), key=_case_key
    )
    diagnostics = [g for g in document["case_grades"] if g["evidence"].get("scope") == "suite"]
    out = [
        '<header><h1>SheetBenchKit · 测试报告</h1><p class="mono">suite_id: '
        + _text(document["suite_id"])
        + "</p><p>valid: <strong>"
        + _text(document["valid"])
        + "</strong> · 有效性不代表所有业务断言通过。</p></header>"
    ]
    if not document["valid"]:
        out.append(
            '<p class="quarantine">数据隔离：套件或案例存在工具错误。保留已知计划；'
            "被结构拒绝的数据未进入正常评分。零观测或零缺失不表示确认没有启动；"
            "启动情况 unknown。"
            "不展示正常性能比例。</p>"
        )
    out.append(_stats(document))
    out.append(
        "<p>attempted 是计划评测槽位，与 planned 相等；已保存观测与缺失分别列出。"
        "缺观测不代表已确认启动或未启动。</p>"
    )
    out.append(
        "<section><h2>按配置统计</h2><p>仅整理保存计数；结构合法数与原因汇总为保存标记的展示计数。"
        "未重新判分；无推断的成功率。</p>"
    )
    for config, stats in document["per_config"].items():
        relevant = [g for g in genuine if g["config_id"] == config]
        protocol = sum(g["evidence"].get("protocol_valid") is True for g in relevant)
        reasons = Counter(g["reason"] for g in relevant if g["reason"] is not None)
        out.append(
            f'<article data-config="{_text(config)}"><h3 class="mono">config_id: '
            + _text(config)
            + "</h3>"
            + _stats(stats, protocol)
            + _block("保存案例原因 · 展示计数", dict(reasons))
            + _block("完整配置统计", stats, folded=True)
            + "</article>"
        )
    out.append(_block("保存运行计划 runs", document["runs"], folded=True) + "</section>")
    out.append(
        "<section><h2>需检查的结果与证据</h2><p>异常视图不新增案例；"
        "NOT_CHECKED 描述检查范围，合法 ABSTAIN 的 binding_check 未检查不表示案例失败。</p>"
    )
    for diagnostic in diagnostics:
        out.append(_evidence(diagnostic, open_default=True))
    exceptions = sorted(
        (g for g in genuine if g["verdict"] != "PASS"),
        key=lambda g: ({"ERROR": 0, "FAIL": 1, "NO_RESULT": 2}.get(g["verdict"], 3), _case_key(g)),
    )
    if not exceptions and not diagnostics:
        out.append("<p>没有已记录的异常；完整结果见下方。</p>")
    out.extend(_evidence(g, open_default=True) for g in exceptions)
    unchecked = [
        g
        for g in genuine
        if any(
            isinstance(g[k], Mapping) and g[k].get("verdict") == "NOT_CHECKED"
            for k in ("value_check", "binding_check")
        )
    ]
    if unchecked:
        out.append("<h3>未检查范围 · 不是新增案例终态</h3>")
        for g in unchecked:
            out.append(
                _identity(g)
                + "<p>value_check: "
                + _badge(g["value_check"]["verdict"])
                + " · binding_check: "
                + _badge(g["binding_check"]["verdict"])
                + "</p>"
            )
    out.append("</section><section><h2>完整案例结果</h2>")
    rows = [
        [
            _identity(g),
            _text(g["case_id"]) + "<br>metric: " + _text(g["metric_id"]),
            _text(g["category"]) + "<br>金标状态: " + _text(g["expected_status"]),
            _badge(g["verdict"]),
            '<span class="mono">' + _text(g["reason"]) + "</span>",
            _check(g["value_check"]),
            _check(g["binding_check"]),
        ]
        for g in genuine
    ]
    out.append(
        _table(
            "全部真实案例 · 套件诊断见异常区",
            (
                "config / attempt",
                "case / metric",
                "category / 金标状态",
                "verdict",
                "reason",
                "value_check",
                "binding_check",
            ),
            rows,
        )
    )
    out.append("<h3>全部案例证据</h3>")
    out.extend(_evidence(g, open_default=False) for g in genuine)
    out.append("</section><section><h2>金标状态与类别分层</h2>")
    for field, caption in (
        ("expected_status", "金标状态 VALUE / ABSTAIN"),
        ("category", "类别 category"),
    ):
        rows = [
            [
                _text(label),
                *[
                    '<span class="num">' + _text(stats.get(key, _MISSING)) + "</span>"
                    for key in ("planned", "attempted", "observed_count", "missing_count")
                ],
                *[
                    '<span class="num">'
                    + _text(stats.get("status_counts", {}).get(s, _MISSING))
                    + "</span>"
                    for s in _STATUSES
                ],
            ]
            for label, stats in document["strata"].get(field, {}).items()
        ]
        out.append(
            _table(
                caption,
                ("保存标签", "planned", "attempted", "observed", "missing", *_STATUSES),
                rows,
            )
        )
    out.append(_block("完整保存分层", document["strata"], folded=True))
    out.append(
        "</section><section><h2>来源 family 检查</h2>"
        "<p>来源声明、数值判分与扰动关系分别报告；"
        "不能证明模型内部实际来源。family 断言不改变案例分母。</p>"
    )
    family_counts = dict(Counter(g["verdict"] for g in document["family_grades"]))
    out.append(_block("保存 family 状态 · 展示计数（NOT_CHECKED 单列）", family_counts))
    out.append(
        _table(
            "所有保存 family 检查",
            ("family_id", "config_id", "attempt", "verdict", "reason"),
            [
                [
                    _text(g["family_id"]),
                    _text(g["config_id"]),
                    '<span class="num">' + _text(g["attempt"]) + "</span>",
                    _badge(g["verdict"]),
                    _text(g["reason"]),
                ]
                for g in document["family_grades"]
            ],
        )
    )
    out.append(
        "</section><section><h2>usage 与运行信息</h2><p>费用：unknown（缺少价格依据）。"
        "usage=null 表示未知；合法 0 保留为 0。"
        "运行 metadata 是保存数据，不用于猜测模型身份。</p>"
    )
    for g in genuine:
        out.append(
            "<article>"
            + _identity(g)
            + _block("usage", g["evidence"].get("usage", _MISSING))
            + _block("runtime metadata", g["evidence"].get("runtime_metadata", _MISSING))
            + "</article>"
        )
    if not genuine:
        out.append("<p>没有真实案例的运行信息；启动情况 unknown。</p>")
    out.append(
        "</section><footer><p>静态保存结果 · 无脚本、无外部资源、无导航。"
        "完整机器数据保存在同目录 report.json。</p></footer>"
    )
    return "".join(out)


def write_report(report: SuiteReport, output_dir: Path) -> ReportPaths:
    """Write unchanged canonical report.json plus a self-contained static report.html."""
    document = to_document(report)
    machine = canonical_json(document)
    template = files("sheetbenchkit").joinpath("templates/report.html").read_text(encoding="utf-8")
    html = Template(template).substitute(body=_body(document))
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = ReportPaths(output_dir / "report.json", output_dir / "report.html")
    paths.json_path.write_bytes(machine)
    paths.html_path.write_text(html, encoding="utf-8")
    return paths


def exit_code(report: SuiteReport) -> int:
    """ERROR has priority; zero requires nonempty complete saved case/family PASS."""
    if (
        not report.valid
        or report.status_counts.get("ERROR", 0)
        or any(g.verdict == "ERROR" for g in report.case_grades)
        or any(g.verdict == "ERROR" for g in report.family_grades)
    ):
        return 2
    genuine: tuple[CaseGrade, ...] = tuple(
        g for g in report.case_grades if g.evidence.get("scope") == "case"
    )
    if (
        not report.runs
        or report.planned <= 0
        or len(genuine) != report.planned
        or report.attempted != report.planned
        or report.missing_count
        or any(g.verdict != "PASS" for g in genuine)
        or any(g.verdict != "PASS" for g in report.family_grades)
        or any(report.status_counts.get(s, 0) for s in ("FAIL", "NO_RESULT"))
    ):
        return 1
    return 0
