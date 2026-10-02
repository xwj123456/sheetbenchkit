"""Canonical hashes and atomic publication of confirmed, immutable suites."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import shutil
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from . import models as m
from .contracts import to_document, validate_documents, validate_schema


def canonical_json(value: Any) -> bytes:
    """UTF-8, sorted keys, fixed separators, finite JSON; no runtime coercion."""
    try:
        return json.dumps(
            to_document(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise m.CaseError("INVALID_JSON", str(exc)) from exc


def sha256_bytes(snapshot: bytes) -> str:
    if not isinstance(snapshot, bytes):
        raise m.CaseError("INVALID_SNAPSHOT", "expected immutable bytes")
    return hashlib.sha256(snapshot).hexdigest()


def content_hash(value: Any) -> str:
    """Exclude the document's own content hash and top-level runtime-only data.

    Nested business objects are retained verbatim, including their input hashes.
    File hashes always use sha256_bytes(canonical_json(document)) instead.
    """
    document = to_document(value)
    if isinstance(document, dict):
        document = {
            key: item
            for key, item in document.items()
            if key
            not in {"content_sha256", "runtime_metadata", "timestamp", "elapsed_s", "duration_s"}
        }
    return sha256_bytes(canonical_json(document))


def _check_expected(candidate: m.CandidateCase) -> None:
    spec, expected = candidate.validated.spec, candidate.expected
    probe = m.AgentResult(
        "1",
        candidate.validated.manifest.case_id,
        spec.metric_id,
        expected.status,
        expected.value,
        expected.unit,
        expected.reason,
        expected.bindings,
    )
    validate_schema("result", probe)
    metric = spec.metric
    aggregates = (
        (metric.numerator, metric.denominator) if isinstance(metric, m.RatioSpec) else (metric,)
    )
    needed: dict[str, set[str]] = {}
    for aggregate in aggregates:
        field_names = needed.setdefault(aggregate.source, set())
        if isinstance(aggregate, m.AggregateSumSpec):
            field_names.add(aggregate.field)
        if aggregate.filter is not None:
            field_names.add(aggregate.filter.field)
    seen = set()
    for binding in expected.bindings:
        if binding.source not in spec.sources:
            raise m.CaseError("INVALID_GOLD_BINDING", binding.source)
        source = spec.sources[binding.source]
        if (binding.file_id, binding.sheet, binding.header_row, binding.rows) != (
            source.file_id,
            source.sheet,
            source.header_row,
            source.rows,
        ):
            raise m.CaseError("INVALID_GOLD_BINDING", binding.source)
        if set(binding.fields) != needed.get(binding.source, set()):
            raise m.CaseError("INVALID_GOLD_BINDING", "fields")
        seen.add(binding.source)
    if expected.status == "VALUE":
        if seen != set(needed):
            raise m.CaseError("INCOMPLETE_GOLD_BINDINGS")
        unit = (
            "ratio"
            if isinstance(metric, m.RatioSpec)
            else "count"
            if isinstance(metric, m.CountSpec)
            else metric.unit
        )
        places = 0 if isinstance(metric, m.CountSpec) else metric.places
        value = expected.value
        if expected.unit != unit or value is None:
            raise m.CaseError("INVALID_GOLD_UNIT")
        actual_places = len(value.split(".")[1]) if "." in value else 0
        if actual_places != places or (places == 0 and "." in value):
            raise m.CaseError("INVALID_GOLD_PLACES")


def _prepare(candidate: m.CandidateCase) -> tuple[m.CaseManifest, dict[str, bytes], m.GoldRecord]:
    case = candidate.validated
    validate_documents(case.spec, case.manifest)
    contract_sha = sha256_bytes(canonical_json(case.spec))
    if not isinstance(case.task, Mapping) or not case.task:
        raise m.CaseError("INVALID_TASK", "nonempty stable business JSON object required")
    task_sha = sha256_bytes(canonical_json(case.task))
    _check_stable_task(case.task)
    for confirmation in (candidate.confirmation, case.manifest.business_confirmation):
        if (
            confirmation.get("reviewed_contract_sha256") != contract_sha
            or confirmation.get("task_sha256") != task_sha
        ):
            raise m.CaseError("CONFIRMATION_HASH_MISMATCH", case.manifest.case_id)
    if candidate.confirmation != case.manifest.business_confirmation:
        raise m.CaseError("CONFIRMATION_MISMATCH", case.manifest.case_id)
    _check_expected(candidate)
    entries = []
    snapshots = {}
    file_ids = {entry.file_id for entry in case.manifest.inputs}
    if set(case.inputs.snapshots) - file_ids:
        raise m.CaseError("UNKNOWN_SNAPSHOT")
    for entry in case.manifest.inputs:
        snapshot = case.inputs.snapshots.get(entry.file_id)
        if entry.expected_missing:
            if snapshot is not None:
                raise m.CaseError("EXPECTED_MISSING_PRESENT", entry.file_id)
            if "MISSING_SOURCE" not in case.manifest.intentional_boundaries:
                raise m.CaseError("UNDECLARED_MISSING_SOURCE", entry.file_id)
            entries.append(replace(entry, relative_path=f"inputs/{entry.file_id}.{entry.format}"))
            continue
        if snapshot is None or sha256_bytes(snapshot) != entry.sha256:
            raise m.CaseError("INPUT_HASH_MISMATCH", entry.file_id)
        path = f"inputs/{entry.file_id}.{entry.format}"
        snapshots[path] = snapshot
        entries.append(replace(entry, relative_path=path, sha256=sha256_bytes(snapshot)))
    manifest = replace(case.manifest, inputs=tuple(entries))
    validate_documents(case.spec, manifest)
    gold = m.GoldRecord(
        manifest.case_id,
        contract_sha,
        sha256_bytes(canonical_json(manifest)),
        candidate.expected,
        candidate.origin,
        candidate.confirmation,
        candidate.evidence,
    )
    validate_schema("gold", gold)
    return manifest, snapshots, gold


def _check_stable_task(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in {
                "case_id",
                "relative_path",
                "absolute_path",
                "sha256",
                "gold",
                "expected",
                "raw_stdout",
                "raw_stderr",
                "runtime_metadata",
                "timestamp",
                "elapsed_s",
                "duration_s",
            } or key.endswith("_sha256"):
                raise m.CaseError("UNSTABLE_TASK_FIELD", key)
            _check_stable_task(item)
    elif isinstance(value, (tuple, list)):
        for item in value:
            _check_stable_task(item)


def _publish(staging: Path, destination: Path) -> None:
    """Atomic no-replace rename, including a destination-created-during-freeze race."""
    source_bytes, target_bytes = os.fsencode(staging), os.fsencode(destination)
    if sys.platform in ("darwin", "linux"):
        libc = ctypes.CDLL(None, use_errno=True)
        if sys.platform == "darwin":
            function = libc.renamex_np
            function.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
            args = (source_bytes, target_bytes, 4)  # RENAME_EXCL
        else:
            function = libc.renameat2
            function.argtypes = [
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            ]
            args = (-100, source_bytes, -100, target_bytes, 1)  # RENAME_NOREPLACE
        function.restype = ctypes.c_int
        if function(*args) != 0:
            errno = ctypes.get_errno()
            raise OSError(errno, os.strerror(errno), str(destination))
    elif os.name == "nt":
        os.rename(staging, destination)  # Windows rename never replaces an existing destination.
    else:
        raise m.CaseError("UNSUPPORTED_ATOMIC_PUBLISH", sys.platform)


def freeze_suite(
    candidates: tuple[m.CandidateCase, ...], families: tuple[m.FamilySpec, ...], destination: Path
) -> m.Suite:
    """Publish only supplied, confirmed gold; never evaluate/reference/recompute it."""
    destination = Path(destination)
    if destination.exists() or destination.is_symlink():
        raise m.CaseError("DESTINATION_EXISTS", str(destination))
    if not 1 <= len(candidates) <= 100:
        raise m.CaseError("SUITE_LIMIT", "suite must contain 1..100 cases")
    ids = [item.validated.manifest.case_id for item in candidates]
    if len(ids) != len(set(ids)):
        raise m.CaseError("DUPLICATE_CASE_ID")
    ordered = sorted(candidates, key=lambda item: item.validated.manifest.case_id)
    prepared = [(item, *_prepare(item)) for item in ordered]
    byte_count = sum(
        len(snapshot) for _, _, snapshots, _ in prepared for snapshot in snapshots.values()
    )
    cell_count = sum(_read_cells(candidate.validated.inputs) for candidate in ordered)
    if byte_count > 128 * 1024 * 1024 or cell_count > 1_000_000:
        raise m.CaseError("SUITE_LIMIT", "aggregate snapshot/cell budget exceeded")
    documents = {}
    refs = []
    for candidate, manifest, snapshots, gold in prepared:
        case_id = manifest.case_id
        prefix = f"cases/{case_id}/"
        paths = {kind: prefix + kind + ".json" for kind in ("contract", "manifest", "task", "gold")}
        values = {
            "contract": candidate.validated.spec,
            "manifest": manifest,
            "task": candidate.validated.task,
            "gold": gold,
        }
        hashes = {}
        for kind, value in values.items():
            snapshot = canonical_json(value)
            documents[paths[kind]] = snapshot
            hashes[kind] = sha256_bytes(snapshot)
        for path, snapshot in snapshots.items():
            documents[prefix + path] = snapshot
        refs.append(
            m.CaseRef(
                case_id,
                paths["contract"],
                hashes["contract"],
                paths["manifest"],
                hashes["manifest"],
                paths["task"],
                hashes["task"],
                paths["gold"],
                hashes["gold"],
            )
        )
    families = tuple(sorted(families, key=lambda item: item.family_id))
    suite_id = "suite_" + content_hash({"cases": refs, "families": families})[:24]
    suite = m.Suite("1", suite_id, tuple(refs), families, "0" * 64)
    suite = replace(suite, content_sha256=content_hash(suite))
    validate_schema("suite", suite)
    documents["suite.json"] = canonical_json(suite)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}.staging-", dir=destination.parent))
    try:
        for path, snapshot in documents.items():
            target = staging / path
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as handle:
                handle.write(snapshot)
                handle.flush()
                os.fsync(handle.fileno())
        _publish(staging, destination)
    except Exception as exc:
        shutil.rmtree(staging)
        if isinstance(exc, m.CaseError):
            raise
        raise m.CaseError("FREEZE_IO_ERROR", str(exc)) from exc
    return suite


MAX_SUITE_BYTES = 128 * 1024 * 1024
MAX_SUITE_CELLS = 1_000_000


def _read_cells(inputs: m.InputBundle) -> int:
    """Actual selected-table reads include each source's headers and data cells."""
    return sum(inputs.selected_read_cells.values()) + sum(
        len(table.headers) + sum(len(row) for row in table.rows)
        for source_id, table in inputs.tables.items()
        if source_id not in inputs.selected_read_cells
    )


def _frozen_hash(case: m.ValidatedCase, gold: m.GoldRecord) -> str:
    return content_hash(
        {"contract": case.spec, "manifest": case.manifest, "task": case.task, "gold": gold}
    )


def _verify_frozen(case: m.FrozenCase) -> None:
    """Validate supplied frozen evidence without recomputing any business result."""
    validated, gold = case.validated, case.gold
    validate_schema("gold", gold)
    if gold.case_id != validated.manifest.case_id:
        raise m.CaseError("GOLD_IDENTITY_MISMATCH")
    _, _, reconstructed = _prepare(
        m.CandidateCase(validated, gold.expected, gold.origin, gold.confirmation, gold.evidence)
    )
    if (
        gold.contract_sha256 != reconstructed.contract_sha256
        or gold.manifest_sha256 != sha256_bytes(canonical_json(validated.manifest))
    ):
        raise m.CaseError("GOLD_HASH_MISMATCH", gold.case_id)
    if case.content_sha256 != _frozen_hash(validated, gold):
        raise m.CaseError("FROZEN_HASH_MISMATCH", gold.case_id)


def _read_artifact(root: Path, relative: str) -> bytes:
    from .loaders import MAX_FILE_BYTES, _contained_path

    path = _contained_path(root, relative)
    if not path.is_file():
        raise m.CaseError("ARTIFACT_IO_ERROR", relative)
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_FILE_BYTES + 1)
        if not path.resolve().is_relative_to(root.resolve()):
            raise m.CaseError("PATH_ESCAPE", relative)
    except (OSError, RuntimeError) as exc:
        raise m.CaseError("ARTIFACT_IO_ERROR", relative) from exc
    if len(raw) > MAX_FILE_BYTES:
        raise m.CaseError("ARTIFACT_LIMIT", relative)
    return raw


def preflight_suite(suite: m.Suite, root: Path) -> tuple[m.FrozenCase, ...]:
    """Check the entire persisted suite before execution or grading; never derive gold.

    Hashes cover exact artifact bytes. Inputs are loaded once into verified snapshots,
    and aggregate budgets are independently counted from those loaded snapshots/tables.
    """
    from .contracts import decode_document, strict_json_loads
    from .loaders import load_inputs

    if not 1 <= len(suite.cases) <= 100:
        raise m.CaseError("SUITE_LIMIT", "suite must contain 1..100 cases")
    validate_schema("suite", suite)
    raw_suite = _read_artifact(Path(root), "suite.json")
    persisted = decode_document("suite", raw_suite)
    if raw_suite != canonical_json(suite) or persisted != suite:
        raise m.CaseError("SUITE_DOCUMENT_MISMATCH")
    if suite.content_sha256 != content_hash(suite):
        raise m.CaseError("SUITE_HASH_MISMATCH")
    cases = []
    byte_count = cell_count = 0
    for ref in suite.cases:
        documents = {}
        for name in ("contract", "manifest", "task", "gold"):
            relative = getattr(ref, name + "_path")
            raw = _read_artifact(Path(root), relative)
            documents[name] = (
                strict_json_loads(raw) if name == "task" else decode_document(name, raw)
            )
            if sha256_bytes(raw) != getattr(ref, name + "_sha256"):
                raise m.CaseError("ARTIFACT_HASH_MISMATCH", {"case_id": ref.case_id, "kind": name})
            if relative != f"cases/{ref.case_id}/{name}.json":
                raise m.CaseError("ARTIFACT_LAYOUT_MISMATCH", relative)
        spec, manifest, task, gold = (
            documents[name] for name in ("contract", "manifest", "task", "gold")
        )
        if manifest.case_id != ref.case_id or gold.case_id != ref.case_id:
            raise m.CaseError("CASE_IDENTITY_MISMATCH", ref.case_id)
        case_root = Path(root) / "cases" / ref.case_id
        inputs = load_inputs(spec, manifest, case_root)
        for entry in manifest.inputs:
            if not entry.expected_missing and entry.file_id not in inputs.snapshots:
                raise m.CaseError(
                    "UNVERIFIED_SNAPSHOT",
                    {
                        "case_id": ref.case_id,
                        "file_id": entry.file_id,
                        "hash_verified": False,
                        "snapshot_unavailable": True,
                    },
                )
        byte_count += sum(len(snapshot) for snapshot in inputs.snapshots.values())
        cell_count += _read_cells(inputs)
        if byte_count > MAX_SUITE_BYTES or cell_count > MAX_SUITE_CELLS:
            raise m.CaseError(
                "SUITE_LIMIT", {"snapshot_bytes": byte_count, "read_cells": cell_count}
            )
        validated = m.ValidatedCase(spec, manifest, inputs, task)
        frozen = m.FrozenCase(validated, gold, _frozen_hash(validated, gold))
        _verify_frozen(frozen)
        cases.append(frozen)
    return tuple(cases)
