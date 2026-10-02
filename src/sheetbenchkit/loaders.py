"""Bounded, immutable CSV/XLSX snapshots; no formula evaluation or remote reads.

business_limits maps source_id to a tuple of {"reason": str, "details": JSON object}.
Unbound file issues use file:<file_id>; these cannot collide with valid source IDs.
A rejected source never gets a fabricated empty Table. Missing/ambiguous source or
field conditions require the same reason in manifest.intentional_boundaries.
CSV carries exact text (or blank); XLSX retains its intrinsic cell kind.
"""

from __future__ import annotations

import hashlib
import io
import lzma
import re
import zipfile
import zlib
from collections.abc import Iterator
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from xml.etree.ElementTree import ParseError

from defusedxml import ElementTree as safe_xml  # type: ignore[import-untyped]
from defusedxml.common import DefusedXmlException  # type: ignore[import-untyped]
from openpyxl import load_workbook  # type: ignore[import-untyped]
from openpyxl.utils.cell import get_column_letter  # type: ignore[import-untyped]

from . import models as m
from .contracts import validate_documents, validate_relative_path

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_ROWS = 1000
MAX_COLUMNS = 100
MAX_ZIP_MEMBERS = 200
MAX_MEMBER_BYTES = 16 * 1024 * 1024
MAX_EXPANDED_BYTES = 64 * 1024 * 1024
_CSV_DELIMITER = re.compile('[,\r\n"]')
_CELL_ADDRESS = re.compile(r"([A-Z]{1,3})([1-9][0-9]{0,6})\Z")


def _contained_path(root: Path, relative: str) -> Path:
    validate_relative_path(relative)
    try:
        base = root.resolve(strict=True)
        path = base / relative
        if not path.resolve().is_relative_to(base):
            raise m.CaseError("PATH_ESCAPE", relative)
        return path
    except (OSError, RuntimeError) as exc:
        raise m.CaseError("INPUT_IO_ERROR", relative) from exc


def _snapshot(root: Path, entry: m.InputEntry) -> bytes | None:
    path = _contained_path(root, entry.relative_path)
    if entry.expected_missing:
        if path.exists() or path.is_symlink():
            raise m.CaseError("EXPECTED_MISSING_PRESENT", entry.file_id)
        return None
    if not path.is_file():
        raise m.CaseError("INPUT_IO_ERROR", "regular file required: " + entry.relative_path)
    try:
        # One immutable read; the extra byte is only an over-limit sentinel.
        with path.open("rb") as handle:
            data = handle.read(MAX_FILE_BYTES + 1)
        if not path.resolve().is_relative_to(root.resolve()):
            raise m.CaseError("PATH_ESCAPE", entry.relative_path)
    except (OSError, RuntimeError) as exc:
        raise m.CaseError("INPUT_IO_ERROR", entry.relative_path) from exc
    if len(data) > MAX_FILE_BYTES:
        # There is no full hash-verified snapshot under the approved <=10 MiB rule.
        return data
    if hashlib.sha256(data).hexdigest() != entry.sha256:
        raise m.CaseError("INPUT_HASH_MISMATCH", entry.file_id)
    return data


def _issue(reason: str, **details: Any) -> dict[str, Any]:
    return {"reason": reason, "details": details}


def _csv_records(text: str) -> Iterator[tuple[tuple[str, ...], int]]:
    """Strict quote grammar, logical records, and bounded retained header width.

    Keep at most 101 fields while still checking the actual width of every record.
    This avoids csv.field_size_limit's process-global state and unbounded row lists.
    """
    pos, length = 0, len(text)
    while pos < length:
        fields: list[str] = []
        width = 0
        while True:
            start = pos
            if pos < length and text[pos] == '"':
                pos += 1
                while True:
                    quote = text.find('"', pos)
                    if quote < 0:
                        raise m.CaseError("MALFORMED_CSV", "unclosed quoted field")
                    if quote + 1 < length and text[quote + 1] == '"':
                        pos = quote + 2
                        continue
                    pos = quote + 1
                    if pos < length and text[pos] not in ",\r\n":
                        raise m.CaseError("MALFORMED_CSV", "text after closing quote")
                    value = text[start + 1 : quote].replace('""', '"')
                    break
            else:
                match = _CSV_DELIMITER.search(text, pos)
                pos = match.start() if match else length
                if pos < length and text[pos] == '"':
                    raise m.CaseError("MALFORMED_CSV", "quote in unquoted field")
                value = text[start:pos]
            width += 1
            if width <= MAX_COLUMNS + 1:
                fields.append(value)
            if pos >= length:
                yield tuple(fields), width
                break
            delimiter = text[pos]
            pos += 1
            if delimiter == ",":
                continue
            if delimiter == "\r" and pos < length and text[pos] == "\n":
                pos += 1
            yield tuple(fields), width
            break


def _csv_tables(data: bytes, sources: dict[str, m.SourceSpec]):
    try:
        text = data.decode("utf-8-sig")
    except UnicodeError as exc:
        raise m.CaseError("INVALID_UTF8") from exc
    wanted = {s.header_row for s in sources.values()}
    for source in sources.values():
        if source.rows[1] - source.rows[0] + 1 <= MAX_ROWS:
            wanted.update(range(source.rows[0], source.rows[1] + 1))
    kept: dict[int, tuple[str, ...]] = {}
    selected_widths: dict[int, int] = {}
    width: int | None = None
    for index, (record, actual_width) in enumerate(_csv_records(text), start=1):
        if width is None:
            width = actual_width
        elif actual_width != width:
            raise m.CaseError("NONRECTANGULAR_CSV", {"record": index})
        if index in wanted:
            kept[index] = record
            selected_widths[index] = actual_width
    tables, limits = {}, {}
    reads = {
        key: selected_widths.get(source.header_row, 0)
        + (
            sum(selected_widths.get(i, 0) for i in range(source.rows[0], source.rows[1] + 1))
            if source.rows[1] - source.rows[0] + 1 <= MAX_ROWS
            else 0
        )
        for key, source in sources.items()
    }
    if not sources and (width or 0) > MAX_COLUMNS:
        limits[""] = [_issue("UNSUPPORTED_INPUT_LIMIT", budget="table_columns")]
    for source_id, source in sources.items():
        if source.rows[1] - source.rows[0] + 1 > MAX_ROWS or (width or 0) > MAX_COLUMNS:
            limits[source_id] = [_issue("UNSUPPORTED_INPUT_LIMIT", budget="table_range")]
        elif source.header_row not in kept or source.rows[1] not in kept:
            raise m.CaseError("INVALID_CSV_RANGE", {"source": source_id, "rows": source.rows})
        else:
            rows = tuple(
                tuple(
                    m.Cell(
                        "text" if value else "blank",
                        value if value else None,
                        f"{get_column_letter(column)}{index}",
                    )
                    for column, value in enumerate(kept[index], start=1)
                )
                for index in range(source.rows[0], source.rows[1] + 1)
            )
            tables[source_id] = m.Table(kept[source.header_row], rows)
    return tables, limits, reads


def _zip_budget(archive: zipfile.ZipFile) -> dict[str, Any] | None:
    members = archive.infolist()
    names = [member.filename for member in members]
    if len(names) != len(set(names)):
        raise m.CaseError("DUPLICATE_ZIP_MEMBER")
    if len(members) > MAX_ZIP_MEMBERS:
        return _issue("UNSUPPORTED_INPUT_LIMIT", budget="zip_members")
    total = 0
    for member in members:
        expanded = 0
        with archive.open(member) as handle:
            while block := handle.read(64 * 1024):
                expanded += len(block)
                total += len(block)
                if expanded > MAX_MEMBER_BYTES:
                    return _issue("UNSUPPORTED_INPUT_LIMIT", budget="zip_member_bytes")
                if total > MAX_EXPANDED_BYTES:
                    return _issue("UNSUPPORTED_INPUT_LIMIT", budget="zip_total_bytes")
    return None


def _xml_widths(archive: zipfile.ZipFile, wanted: set[int]) -> dict[str, dict[int, int]]:
    """Defuse every XML part before openpyxl; inspect physical cells, never dimension."""
    result: dict[str, dict[int, int]] = {}
    for member in archive.infolist():
        if not member.filename.endswith((".xml", ".rels")):
            continue
        widths: dict[int, int] = {}
        row_index, last_row, column = 0, 0, 0
        worksheet = False
        parents = []
        with archive.open(member) as handle:
            events = safe_xml.iterparse(
                handle,
                events=("start", "end"),
                forbid_dtd=True,
                forbid_entities=True,
                forbid_external=True,
            )
            for event, element in events:
                tag = element.tag.rsplit("}", 1)[-1]
                if event == "start":
                    parents.append(element)
                    if tag == "worksheet":
                        worksheet = True
                    elif worksheet and tag == "row":
                        row_index = int(element.attrib.get("r", last_row + 1))
                        if row_index <= last_row or row_index > 1_048_576:
                            raise m.CaseError("INVALID_XLSX", "row order/coordinate")
                        last_row, column = row_index, 0
                    elif worksheet and tag == "c":
                        address = element.attrib.get("r")
                        if address:
                            match = _CELL_ADDRESS.fullmatch(address)
                            if not match or int(match[2]) != row_index:
                                raise m.CaseError("INVALID_XLSX", "cell coordinate")
                            actual_column = 0
                            for letter in match[1]:
                                actual_column = actual_column * 26 + ord(letter) - 64
                        else:
                            actual_column = column + 1
                        if actual_column <= column or actual_column > 16384:
                            raise m.CaseError("INVALID_XLSX", "cell order/coordinate")
                        column = actual_column
                        if row_index in wanted:
                            widths[row_index] = column
                else:
                    element.clear()
                    parents.pop()
                    if parents:
                        parents[-1].remove(element)
            if worksheet:
                result[member.filename] = widths
    return result


def _xlsx_cell(cell: Any, coordinate: str) -> m.Cell:
    value = cell.value
    if value is None:
        return m.Cell("blank", None, coordinate)
    if cell.data_type == "f":
        # Array/data-table formulas also retain formula kind; never use cached values.
        formula = value if isinstance(value, str) else getattr(value, "text", None)
        return m.Cell("formula", formula if formula is not None else dict(value), coordinate)
    if cell.data_type == "e":
        return m.Cell("error", value, coordinate)
    if isinstance(value, bool):
        return m.Cell("bool", value, coordinate)
    if isinstance(value, (datetime, date, time)):
        return m.Cell("date", value.isoformat(), coordinate)
    if isinstance(value, timedelta):
        return m.Cell("date", str(value), coordinate)
    if isinstance(value, (int, float, Decimal)):
        return m.Cell("number", str(Decimal(str(value))), coordinate)
    if isinstance(value, str):
        return m.Cell("text" if value else "blank", value if value else None, coordinate)
    raise m.CaseError("INVALID_XLSX", "unknown cell representation")


def _xlsx_tables(data: bytes, sources: dict[str, m.SourceSpec]):
    tables, limits, reads = {}, {}, {}
    workbook = None
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            budget = _zip_budget(archive)
            if budget:
                return {}, {source_id: [budget] for source_id in (tuple(sources) or ("",))}, {}
            wanted = {s.header_row for s in sources.values()}
            for source in sources.values():
                if source.rows[1] - source.rows[0] + 1 <= MAX_ROWS:
                    wanted.update(range(source.rows[0], source.rows[1] + 1))
            widths = _xml_widths(archive, wanted)
        workbook = load_workbook(
            io.BytesIO(data), read_only=True, data_only=False, keep_links=False
        )
        for source_id, source in sources.items():
            if source.rows[1] - source.rows[0] + 1 > MAX_ROWS:
                if source.sheet in workbook.sheetnames:
                    worksheet = workbook[source.sheet]
                    reads[source_id] = widths.get(worksheet._worksheet_path, {}).get(
                        source.header_row, 0
                    )
                limits[source_id] = [_issue("UNSUPPORTED_INPUT_LIMIT", budget="table_rows")]
                continue
            if source.sheet not in workbook.sheetnames:
                limits[source_id] = [_issue("MISSING_SOURCE", sheet=source.sheet)]
                continue
            worksheet = workbook[source.sheet]
            # openpyxl's part path maps the actual selected worksheet, including renamed sheets.
            physical = widths.get(worksheet._worksheet_path, {})
            width = physical.get(source.header_row, 0)
            reads[source_id] = width
            reads[source_id] += sum(
                physical.get(i, 0) for i in range(source.rows[0], source.rows[1] + 1)
            )
            data_width = max(
                (physical.get(i, 0) for i in range(*(source.rows[0], source.rows[1] + 1))),
                default=0,
            )
            if max(width, data_width) > MAX_COLUMNS:
                limits[source_id] = [_issue("UNSUPPORTED_INPUT_LIMIT", budget="table_columns")]
                continue
            if width == 0:
                limits[source_id] = [_issue("MISSING_FIELD", sheet=source.sheet)]
                continue
            if data_width > width:
                raise m.CaseError("NONRECTANGULAR_XLSX", source_id)
            header_iter = worksheet.iter_rows(
                min_row=source.header_row, max_row=source.header_row, min_col=1, max_col=width
            )
            try:
                header = next(header_iter)
            finally:
                header_iter.close()
            if any(
                cell.data_type not in ("s", "inlineStr") or not isinstance(cell.value, str)
                for cell in header
            ):
                limits[source_id] = [_issue("UNSUPPORTED_CELL", location="header")]
                continue
            row_iter = worksheet.iter_rows(
                min_row=source.rows[0], max_row=source.rows[1], min_col=1, max_col=width
            )
            try:
                rows = [
                    tuple(
                        _xlsx_cell(cell, f"{get_column_letter(col)}{index}")
                        for col, cell in enumerate(row, start=1)
                    )
                    for index, row in enumerate(row_iter, start=source.rows[0])
                ]
            finally:
                row_iter.close()
            # openpyxl does not materialize trailing absent rows; the explicit range does.
            for index in range(source.rows[0] + len(rows), source.rows[1] + 1):
                rows.append(
                    tuple(
                        m.Cell("blank", None, f"{get_column_letter(col)}{index}")
                        for col in range(1, width + 1)
                    )
                )
            tables[source_id] = m.Table(tuple(cell.value for cell in header), tuple(rows))
            # Supported declared rectangles include materialized blank cells as before.
            reads[source_id] = width * (1 + len(rows))
        return tables, limits, reads
    except m.CaseError:
        raise
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        IndexError,
        RuntimeError,
        zipfile.BadZipFile,
        ParseError,
        DefusedXmlException,
        zlib.error,
        lzma.LZMAError,
        OverflowError,
    ) as exc:
        raise m.CaseError("INVALID_XLSX", type(exc).__name__) from exc
    finally:
        if workbook is not None:
            workbook.close()


def _required_fields(spec: m.RuleSpec) -> dict[str, set[str]]:
    metric = spec.metric
    aggregates = (
        (metric.numerator, metric.denominator) if isinstance(metric, m.RatioSpec) else (metric,)
    )
    needed: dict[str, set[str]] = {}
    for aggregate in aggregates:
        fields = needed.setdefault(aggregate.source, set())
        if isinstance(aggregate, m.AggregateSumSpec):
            fields.add(aggregate.field)
        if aggregate.filter:
            fields.add(aggregate.filter.field)
    return needed


def load_inputs(spec: m.RuleSpec, manifest: m.CaseManifest, root: Path) -> m.InputBundle:
    """Validate documents and load exactly their declared sources from a case root."""
    validate_documents(spec, manifest)
    tables: dict[str, m.Table] = {}
    snapshots: dict[str, bytes] = {}
    selected_reads: dict[str, int] = {}
    limits: dict[str, list[dict[str, Any]]] = {}
    for entry in manifest.inputs:
        sources = {
            key: value for key, value in spec.sources.items() if value.file_id == entry.file_id
        }
        data = _snapshot(Path(root), entry)
        if data is None:
            if "MISSING_SOURCE" not in manifest.intentional_boundaries:
                raise m.CaseError("UNDECLARED_MISSING_SOURCE", entry.file_id)
            limits.update(
                {
                    key: [_issue("MISSING_SOURCE", file_id=entry.file_id)]
                    for key in (tuple(sources) or ("file:" + entry.file_id,))
                }
            )
        elif len(data) > MAX_FILE_BYTES:
            limits.update(
                {
                    key: [
                        _issue(
                            "UNSUPPORTED_INPUT_LIMIT",
                            budget="file_bytes",
                            snapshot_unavailable=True,
                            hash_verified=False,
                        )
                    ]
                    for key in (tuple(sources) or ("file:" + entry.file_id,))
                }
            )
        else:
            snapshots[entry.file_id] = data
            loaded, rejected, reads = (
                _csv_tables(data, sources) if entry.format == "csv" else _xlsx_tables(data, sources)
            )
            tables.update(loaded)
            selected_reads.update(reads)
            limits.update(
                {
                    ("file:" + entry.file_id if key == "" else key): value
                    for key, value in rejected.items()
                }
            )
    for source_id, fields in _required_fields(spec).items():
        if source_id not in tables:
            continue
        headers = tables[source_id].headers
        if len(set(headers)) != len(headers):
            limits.setdefault(source_id, []).append(_issue("AMBIGUOUS_FIELD", headers=headers))
        absent = sorted(fields - set(headers))
        if absent:
            limits.setdefault(source_id, []).append(_issue("MISSING_FIELD", fields=absent))
    for issues in limits.values():
        for issue in issues:
            if issue["reason"] in {"MISSING_SOURCE", "MISSING_FIELD", "AMBIGUOUS_FIELD"}:
                if issue["reason"] not in manifest.intentional_boundaries:
                    raise m.CaseError("UNDECLARED_BOUNDARY", issue)
    return m.InputBundle(tables, snapshots, manifest.unit_labels, limits, selected_reads)
