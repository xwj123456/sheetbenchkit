"""Strict JSON codecs and fixed, offline Draft 2020-12 contracts."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from decimal import Decimal
from functools import lru_cache
from importlib.resources import files
from pathlib import Path, PurePosixPath
from typing import Any

from jsonschema import Draft202012Validator

from . import models as m

SCHEMA_NAMES = frozenset({"contract", "manifest", "suite", "gold", "result", "observations"})


def strict_json_loads(raw: str | bytes) -> Any:
    """Reject duplicate keys, nonfinite numbers, invalid UTF-8, and trailing JSON."""

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise m.CaseError("DUPLICATE_JSON_KEY", key)
            result[key] = value
        return result

    def bad_constant(value):
        raise m.CaseError("INVALID_JSON", value)

    def number(value):
        result = float(value)
        if not math.isfinite(result):
            raise m.CaseError("INVALID_JSON", "nonfinite number")
        return result

    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return json.loads(
            raw, object_pairs_hook=pairs, parse_constant=bad_constant, parse_float=number
        )
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise m.CaseError("INVALID_JSON", str(exc)) from exc


def to_document(value: Any) -> Any:
    """Convert immutable models to plain JSON; no arbitrary object coercion."""
    if is_dataclass(value) and not isinstance(value, type):
        return {
            f.name: to_document(getattr(value, f.name))
            for f in fields(value)
            if not (f.name == "filter" and getattr(value, f.name) is None)
        }
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise m.CaseError("INVALID_JSON", "object keys must be strings")
        return {key: to_document(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [to_document(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if value is None or type(value) in (str, int, bool):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    raise m.CaseError("INVALID_JSON", f"unsupported data type {type(value).__name__}")


@lru_cache(maxsize=6)
def _validator(name: str) -> Draft202012Validator:
    if name not in SCHEMA_NAMES:
        raise m.CaseError("UNKNOWN_SCHEMA", name)
    schema = strict_json_loads(
        files("sheetbenchkit").joinpath("schemas", name + ".json").read_bytes()
    )

    def check_refs(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "$ref" and (not isinstance(item, str) or not item.startswith("#/$defs/")):
                    raise m.CaseError("EXTERNAL_SCHEMA_REF", item)
                check_refs(item)
        elif isinstance(value, list):
            for item in value:
                check_refs(item)

    check_refs(schema)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def validate_schema(name: str, document: Any) -> None:
    """Only built-in schemas are accepted; metadata must remain finite JSON data."""
    document = to_document(document)
    error = next(_validator(name).iter_errors(document), None)
    if error:
        raise m.CaseError(
            "INVALID_" + name.upper(), {"path": list(error.absolute_path), "message": error.message}
        )
    _semantic(name, document)


def validate_relative_path(value: str) -> None:
    path = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or ":" in value
        or "\x00" in value
        or path.is_absolute()
        or any(part in ("", ".", "..") for part in value.split("/"))
    ):
        raise m.CaseError("INVALID_PATH", value)


def _range(source: Mapping[str, Any]) -> None:
    start, end = source["rows"]
    if start > end or start <= source["header_row"]:
        raise m.CaseError("INVALID_ROWS", source["rows"])


def _unique(values, reason):
    values = list(values)
    if len(values) != len(set(values)):
        raise m.CaseError(reason)


def _result(document):
    if document["status"] == "VALUE" and document["value"].startswith("-"):
        if Decimal(document["value"]) == 0:
            raise m.CaseError("INVALID_RESULT", "negative zero")
    _unique((binding["source"] for binding in document["bindings"]), "DUPLICATE_BINDING")
    for binding in document["bindings"]:
        _range(binding)


def _semantic(name, document):
    if name == "contract":
        for source in document["sources"].values():
            _range(source)
        metric = document["metric"]
        aggregates = (
            [metric["numerator"], metric["denominator"]] if metric["op"] == "ratio" else ([metric])
        )
        for aggregate in aggregates:
            if aggregate["source"] not in document["sources"]:
                raise m.CaseError("UNKNOWN_SOURCE", aggregate["source"])
    elif name == "manifest":
        _unique((entry["file_id"] for entry in document["inputs"]), "DUPLICATE_INPUT_ID")
        _unique((entry["relative_path"] for entry in document["inputs"]), "DUPLICATE_INPUT_PATH")
        file_ids = {entry["file_id"] for entry in document["inputs"]}
        for entry in document["inputs"]:
            validate_relative_path(entry["relative_path"])
        if set(document["unit_labels"]) - file_ids:
            raise m.CaseError("UNKNOWN_UNIT_FILE")
        for recipe in document["variant_recipes"]:
            _range(recipe)
            if recipe["file_id"] not in file_ids:
                raise m.CaseError("UNKNOWN_RECIPE_FILE")
            kind = recipe["kind"]
            if kind == "column_shuffle":
                if any(
                    recipe[field] is not None
                    for field in ("field", "row", "numeric_delta", "text_replacement")
                ):
                    raise m.CaseError("INVALID_RECIPE", "unused fields must be null")
            else:
                if (
                    not recipe["field"]
                    or recipe["row"] is None
                    or not (recipe["rows"][0] <= recipe["row"] <= recipe["rows"][1])
                ):
                    raise m.CaseError("INVALID_RECIPE", "field/row required and in range")
                if kind.endswith("_numeric"):
                    if recipe["numeric_delta"] is None or recipe["text_replacement"] is not None:
                        raise m.CaseError("INVALID_RECIPE", "numeric mutation shape")
                elif recipe["text_replacement"] is None or recipe["numeric_delta"] is not None:
                    raise m.CaseError("INVALID_RECIPE", "filter mutation shape")
    elif name in ("result", "gold"):
        _result(document if name == "result" else document["expected"])
    elif name == "suite":
        ids = {case["case_id"] for case in document["cases"]}
        _unique((case["case_id"] for case in document["cases"]), "DUPLICATE_CASE_ID")
        paths = []
        for case in document["cases"]:
            for key in ("contract_path", "manifest_path", "task_path", "gold_path"):
                validate_relative_path(case[key])
                paths.append(case[key])
        _unique(paths, "DUPLICATE_ARTIFACT_PATH")
        _unique((family["family_id"] for family in document["families"]), "DUPLICATE_FAMILY_ID")
        for family in document["families"]:
            members = [family["base"], family["target"], family["distractor"]]
            if len(set(members)) != 3 or not set(members) <= ids:
                raise m.CaseError("INVALID_FAMILY_REFERENCE", family["family_id"])
    elif name == "observations":
        _unique((run["config_id"] for run in document["runs"]), "DUPLICATE_CONFIG_ID")
        runs = {run["config_id"]: run["repeats"] for run in document["runs"]}
        slots = []
        for obs in document["observations"]:
            if obs["config_id"] not in runs or obs["attempt"] > runs[obs["config_id"]]:
                raise m.CaseError("UNPLANNED_OBSERVATION")
            slots.append((obs["config_id"], obs["case_id"], obs["attempt"]))
        _unique(slots, "DUPLICATE_OBSERVATION")


def validate_documents(spec: m.RuleSpec, manifest: m.CaseManifest) -> None:
    validate_schema("contract", spec)
    validate_schema("manifest", manifest)
    entries = {entry.file_id: entry for entry in manifest.inputs}
    for source in spec.sources.values():
        if source.file_id not in entries:
            raise m.CaseError("UNKNOWN_INPUT", source.file_id)
        if entries[source.file_id].format == "csv" and source.sheet != "@csv":
            raise m.CaseError("INVALID_CSV_SHEET", source.sheet)


def _aggregate(document, root=False):
    document = dict(document)
    filter_ = document.pop("filter", None)
    document["filter"] = m.FilterSpec(**filter_) if filter_ is not None else None
    if document["op"] == "count":
        return m.CountSpec(**document)
    return (m.RootSumSpec if root else m.AggregateSumSpec)(**document)


def _expected(document):
    return m.ExpectedResult(
        document["status"],
        document["value"],
        document.get("unit"),
        document.get("reason"),
        tuple(m.Binding(**binding) for binding in document["bindings"]),
    )


def _normalize_integer_slots(name: str, document: Any, exact: Any) -> Any:
    """Normalize only integer nodes of the validated bundled schema.

    The second parse preserves integer-slot lexemes before binary float rounding;
    arbitrary JSON metadata keeps the ordinary finite floats from the first parse.
    """
    validator = _validator(name)
    root: Any = validator.schema

    def visit(value: Any, precise: Any, schema: Any) -> Any:
        if not isinstance(schema, dict):
            return value
        if "$ref" in schema:
            schema = root["$defs"][schema["$ref"].removeprefix("#/$defs/")]
        for keyword in ("oneOf", "anyOf"):
            for branch in schema.get(keyword, ()):
                if validator.evolve(schema=branch).is_valid(value):
                    value = visit(value, precise, branch)
                    break
        if schema.get("type") == "integer":
            if isinstance(precise, Decimal):
                if precise != precise.to_integral_value():
                    raise m.CaseError("INVALID_" + name.upper(), "nonintegral integer slot")
                return int(precise)
            return value
        if isinstance(value, dict):
            properties = schema.get("properties", {})
            return {
                key: visit(
                    item,
                    precise[key],
                    properties.get(key, schema.get("additionalProperties", True)),
                )
                for key, item in value.items()
            }
        if isinstance(value, list):
            prefix = schema.get("prefixItems", ())
            return [
                visit(
                    item,
                    precise[index],
                    prefix[index] if index < len(prefix) else schema.get("items", True),
                )
                for index, item in enumerate(value)
            ]
        return value

    normalized = visit(document, exact, root)
    validate_schema(name, normalized)
    return normalized


def decode_document(name: str, raw: str | bytes) -> Any:
    """Decode one persisted document into its immutable public model."""
    doc = strict_json_loads(raw)
    validate_schema(name, doc)
    # Strict parsing/schema validation above already rejected malformed/duplicate JSON.
    doc = _normalize_integer_slots(name, doc, json.loads(raw, parse_float=Decimal))
    if name == "contract":
        metric = doc["metric"]
        if metric["op"] == "ratio":
            metric = m.RatioSpec(
                metric["op"],
                _aggregate(metric["numerator"]),
                _aggregate(metric["denominator"]),
                metric["places"],
                metric["rounding"],
            )
        else:
            metric = _aggregate(metric, root=True)
        return m.RuleSpec(
            doc["schema_version"],
            doc["metric_id"],
            {key: m.SourceSpec(**value) for key, value in doc["sources"].items()},
            metric,
        )
    if name == "manifest":
        return m.CaseManifest(
            **{
                **doc,
                "inputs": tuple(m.InputEntry(**x) for x in doc["inputs"]),
                "variant_recipes": tuple(m.MutationRecipe(**x) for x in doc["variant_recipes"]),
            }
        )
    if name == "result":
        expected = _expected(doc)
        return m.AgentResult(
            doc["schema_version"],
            doc["case_id"],
            doc["metric_id"],
            expected.status,
            expected.value,
            expected.unit,
            expected.reason,
            expected.bindings,
        )
    if name == "gold":
        return m.GoldRecord(**{**doc, "expected": _expected(doc["expected"])})
    if name == "suite":
        return m.Suite(
            **{
                **doc,
                "cases": tuple(m.CaseRef(**x) for x in doc["cases"]),
                "families": tuple(m.FamilySpec(**x) for x in doc["families"]),
            }
        )
    return m.ObservationBatch(
        doc["schema_version"],
        tuple(m.RunSpec(**x) for x in doc["runs"]),
        tuple(m.Observation(**x) for x in doc["observations"]),
    )


def validate_case(spec: m.RuleSpec, manifest: m.CaseManifest, root: Path) -> m.ValidatedCase:
    """Load case-local inputs and existing confirmed stable business task JSON."""
    from .artifacts import _check_stable_task, canonical_json, sha256_bytes
    from .loaders import MAX_FILE_BYTES, _contained_path, load_inputs

    inputs = load_inputs(spec, manifest, root)
    path = _contained_path(Path(root), "task.json")
    if not path.is_file():
        raise m.CaseError("TASK_IO_ERROR", "regular task.json file required")
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_FILE_BYTES + 1)
        if not path.resolve().is_relative_to(Path(root).resolve()):
            raise m.CaseError("PATH_ESCAPE", "task.json")
    except (OSError, RuntimeError) as exc:
        raise m.CaseError("TASK_IO_ERROR", "task.json") from exc
    if len(raw) > MAX_FILE_BYTES:
        raise m.CaseError("TASK_LIMIT")
    task = strict_json_loads(raw)
    if not isinstance(task, dict) or not task:
        raise m.CaseError("INVALID_TASK", "nonempty stable business JSON object required")
    task_hash = sha256_bytes(canonical_json(task))
    _check_stable_task(task)
    if (
        manifest.business_confirmation.get("reviewed_contract_sha256")
        != sha256_bytes(canonical_json(spec))
        or manifest.business_confirmation.get("task_sha256") != task_hash
    ):
        raise m.CaseError("CONFIRMATION_HASH_MISMATCH", manifest.case_id)
    return m.ValidatedCase(spec, manifest, inputs, task)
