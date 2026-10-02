"""Boundary tests catch accepting ambiguous, executable, or malformed contracts."""

import copy
import dataclasses
import importlib
import json

import pytest


def api(module, name):
    result = getattr(importlib.import_module(f"sheetbenchkit.{module}"), name, None)
    assert callable(result), f"Missing Task 1 capability: {module}.{name}"
    return result


def contract():
    return {
        "schema_version": "1",
        "metric_id": "amount",
        "sources": {
            "target": {"file_id": "work", "sheet": "@csv", "header_row": 1, "rows": [2, 3]}
        },
        "metric": {
            "op": "sum",
            "source": "target",
            "field": "amount",
            "unit": "CNY",
            "places": 2,
            "rounding": "HALF_UP",
        },
    }


def manifest():
    return {
        "schema_version": "1",
        "case_id": "base-001",
        "category": "数值",
        "inputs": [
            {
                "file_id": "work",
                "relative_path": "work.csv",
                "format": "csv",
                "sha256": "a" * 64,
                "expected_missing": False,
            }
        ],
        "unit_labels": {"work": {"@csv": {"amount": "CNY"}}},
        "intentional_boundaries": [],
        "business_confirmation": {},
        "variant_recipes": [],
    }


def decode(name, document):
    return api("contracts", "decode_document")(name, json.dumps(document, ensure_ascii=False))


def test_duplicate_identity_and_keys():
    error = api("models", "CaseError")
    strict = api("contracts", "strict_json_loads")
    for raw in [
        '{"a":1,"a":2}',
        '{"nested":{"a":1,"a":2}}',
        '{"a":NaN}',
        '{"a":Infinity}',
        '{"a":1e999}',
        b'"\xff"',
    ]:
        with pytest.raises(error):
            strict(raw)
    assert strict('{"zero":0,"text":"原文"}') == {"zero": 0, "text": "原文"}
    batch = {
        "schema_version": "1",
        "runs": [{"config_id": "adapter", "repeats": 1}],
        "observations": [],
    }
    batch["runs"] *= 2
    with pytest.raises(error):
        decode("observations", batch)


def test_invalid_contract_and_atomic_freeze():
    error = api("models", "CaseError")
    valid = decode("contract", contract())
    api("contracts", "validate_documents")(valid, decode("manifest", manifest()))
    for mutation in ["rows", "places", "op", "three", "root_missing", "nested_rounding"]:
        doc = copy.deepcopy(contract())
        if mutation == "rows":
            doc["sources"]["target"]["rows"] = [5, 2]
        elif mutation == "places":
            doc["metric"]["places"] = 13
        elif mutation == "op":
            doc["metric"]["op"] = "eval"
        elif mutation == "three":
            doc["sources"]["second"] = doc["sources"]["third"] = doc["sources"]["target"]
        elif mutation == "root_missing":
            del doc["metric"]["places"]
        else:
            doc["metric"] = {
                "op": "ratio",
                "numerator": doc["metric"],
                "denominator": {"op": "count", "source": "target"},
                "places": 4,
                "rounding": "HALF_UP",
            }
        with pytest.raises(error):
            decode("contract", doc)
    bad = dataclasses.replace(
        valid, sources={"target": dataclasses.replace(valid.sources["target"], rows=(5, 2))}
    )
    with pytest.raises(error):
        api("contracts", "validate_documents")(bad, decode("manifest", manifest()))


@pytest.mark.parametrize("mutation", ["unknown", "ref", "filter", "row_header", "file_ref"])
def test_contract_closed_shapes_and_references(mutation):
    doc = contract()
    if mutation == "unknown":
        doc["metric"]["code"] = "os.system(...)"
    elif mutation == "ref":
        doc["metric"] = {"$ref": "https://invalid.example/schema"}
    elif mutation == "filter":
        doc["metric"]["filter"] = {"field": "state", "eq": 1}
    elif mutation == "row_header":
        doc["sources"]["target"]["rows"] = [1, 2]
    else:
        doc["sources"]["target"]["file_id"] = "other"
    with pytest.raises(api("models", "CaseError")):
        api("contracts", "validate_documents")(
            decode("contract", doc), decode("manifest", manifest())
        )


def test_ratio_has_unrounded_children_and_optional_filters():
    doc = contract()
    child = doc["metric"]
    del child["places"], child["rounding"]
    doc["metric"] = {
        "op": "ratio",
        "numerator": child,
        "denominator": {"op": "count", "source": "target"},
        "places": 4,
        "rounding": "HALF_UP",
    }
    spec = decode("contract", doc)
    assert spec.metric.numerator.filter is None
    assert not hasattr(spec.metric.numerator, "places")


def test_result_protocol_and_immutable_json():
    binding = {
        "source": "target",
        "file_id": "work",
        "sheet": "@csv",
        "header_row": 1,
        "rows": [2, 3],
        "fields": ["amount"],
    }
    result = {
        "schema_version": "1",
        "case_id": "base-001",
        "metric_id": "amount",
        "status": "VALUE",
        "value": "0.00",
        "unit": "CNY",
        "bindings": [binding],
    }
    decoded = decode("result", result)
    assert decoded.value == "0.00"
    with pytest.raises(dataclasses.FrozenInstanceError):
        decoded.value = "1.00"
    for bad_value in ["NaN", "01.0", "1e2", "+1", "-0.00", "1" * 129]:
        with pytest.raises(api("models", "CaseError")):
            decode("result", {**result, "value": bad_value})
    with pytest.raises(api("models", "CaseError")):
        decode("result", {**result, "bindings": []})
    with pytest.raises(api("models", "CaseError")):
        decode(
            "result",
            {**result, "status": "ABSTAIN", "value": None, "bindings": [], "reason": "GUESS"},
        )
    spec = decode("contract", contract())
    with pytest.raises(TypeError):
        spec.sources["other"] = spec.sources["target"]


@pytest.mark.parametrize("path", ["../out.csv", "/tmp/out.csv", "https://x/a", "a\\b.csv"])
def test_manifest_rejects_unsafe_paths(path):
    doc = manifest()
    doc["inputs"][0]["relative_path"] = path
    with pytest.raises(api("models", "CaseError")):
        decode("manifest", doc)


def test_observations_preserve_raw_stdout_and_reject_bad_slots():
    obs = {
        "case_id": "base-001",
        "attempt": 1,
        "config_id": "adapter",
        "execution_status": "SUCCESS",
        "raw_stdout": '  {"a":1}\n',
        "raw_stderr": "",
        "usage": {"tokens": 0},
        "runtime_metadata": {"elapsed": 1.5},
    }
    batch = {
        "schema_version": "1",
        "runs": [{"config_id": "adapter", "repeats": 1}],
        "observations": [obs],
    }
    decoded = decode("observations", batch)
    assert decoded.observations[0].raw_stdout == '  {"a":1}\n'
    with pytest.raises(TypeError):
        decoded.observations[0].usage["tokens"] = 2
    for observations in [[obs, obs], [{**obs, "attempt": 2}], [{**obs, "config_id": "unknown"}]]:
        with pytest.raises(api("models", "CaseError")):
            decode("observations", {**batch, "observations": observations})
    with pytest.raises(api("models", "CaseError")):
        decode("observations", {**batch, "runs": []})


def protocol_document(name):
    binding = {
        "source": "target",
        "file_id": "work",
        "sheet": "@csv",
        "header_row": 1,
        "rows": [2, 3],
        "fields": ["amount"],
    }
    expected = {
        "status": "VALUE",
        "value": "3.00",
        "unit": "CNY",
        "reason": None,
        "bindings": [binding],
    }
    if name == "contract":
        return contract()
    if name == "manifest":
        return manifest()
    if name == "result":
        return {"schema_version": "1", "case_id": "base-001", "metric_id": "amount", **expected}
    if name == "gold":
        return {
            "case_id": "base-001",
            "contract_sha256": "a" * 64,
            "manifest_sha256": "b" * 64,
            "expected": expected,
            "origin": "preset_independent",
            "confirmation": {"reviewed_contract_sha256": "a" * 64, "task_sha256": "c" * 64},
            "evidence": {"method": "independent hand sum"},
        }
    if name == "suite":
        case_ref = {"case_id": "base-001"}
        for kind in ("contract", "manifest", "task", "gold"):
            case_ref[kind + "_path"] = f"cases/base-001/{kind}.json"
            case_ref[kind + "_sha256"] = "a" * 64
        return {
            "schema_version": "1",
            "suite_id": "suite_base",
            "cases": [case_ref],
            "families": [],
            "content_sha256": "b" * 64,
        }
    return {
        "schema_version": "1",
        "runs": [{"config_id": "adapter", "repeats": 1}],
        "observations": [
            {
                "case_id": "base-001",
                "config_id": "adapter",
                "attempt": 1,
                "execution_status": "SUCCESS",
                "raw_stdout": "",
                "raw_stderr": "",
                "usage": None,
                "runtime_metadata": {},
            }
        ],
    }


def nested_parent(document, path):
    for part in path[:-1]:
        document = document[part]
    return document, path[-1]


@pytest.mark.parametrize("suffix", ["\n", "\r\n", "\r", "\t", " ", "!", "\u2028", "\x00"])
@pytest.mark.parametrize(
    "name,path",
    [
        ("contract", ("metric_id",)),
        ("manifest", ("case_id",)),
        ("manifest", ("inputs", 0, "file_id")),
        ("manifest", ("inputs", 0, "sha256")),
        ("observations", ("runs", 0, "config_id")),
        ("observations", ("observations", 0, "case_id")),
        ("result", ("case_id",)),
        ("result", ("metric_id",)),
        ("result", ("value",)),
        ("result", ("bindings", 0, "source")),
        ("result", ("bindings", 0, "file_id")),
        ("gold", ("case_id",)),
        ("gold", ("contract_sha256",)),
        ("gold", ("manifest_sha256",)),
        ("gold", ("confirmation", "reviewed_contract_sha256")),
        ("gold", ("confirmation", "task_sha256")),
        ("gold", ("expected", "value")),
        ("gold", ("expected", "bindings", 0, "source")),
        ("suite", ("suite_id",)),
        ("suite", ("content_sha256",)),
        ("suite", ("cases", 0, "case_id")),
        ("suite", ("cases", 0, "contract_sha256")),
        ("suite", ("cases", 0, "manifest_sha256")),
        ("suite", ("cases", 0, "task_sha256")),
        ("suite", ("cases", 0, "gold_sha256")),
    ],
)
def test_decode_rejects_protocol_trailing_characters(name, path, suffix):
    document = protocol_document(name)
    parent, key = nested_parent(document, path)
    parent[key] += suffix
    if name == "observations" and path == ("runs", 0, "config_id"):
        document["observations"][0]["config_id"] = parent[key]
    elif name == "manifest" and path == ("inputs", 0, "file_id"):
        document["unit_labels"] = {parent[key]: document["unit_labels"]["work"]}
    with pytest.raises(api("models", "CaseError")):
        decode(name, document)


@pytest.mark.parametrize(
    "name", ["contract", "manifest", "observations", "result", "gold", "suite"]
)
@pytest.mark.parametrize("boundary_id", ["A", "A" + "a0_-" * 15 + "Z9_"])
def test_decode_preserves_valid_boundary_ids_and_exact_hashes(name, boundary_id):
    document = protocol_document(name)
    identity_key = {
        "contract": "metric_id",
        "manifest": "case_id",
        "result": "case_id",
        "gold": "case_id",
        "suite": "suite_id",
    }.get(name)
    if identity_key is None:
        document["runs"][0]["config_id"] = boundary_id
        document["observations"][0]["config_id"] = boundary_id
        decoded = decode(name, document)
        assert decoded.runs[0].config_id == boundary_id
    else:
        document[identity_key] = boundary_id
        decoded = decode(name, document)
        assert getattr(decoded, identity_key) == boundary_id
    if name == "manifest":
        assert decoded.inputs[0].sha256 == "a" * 64
    elif name == "gold":
        assert decoded.contract_sha256 == "a" * 64
    elif name == "suite":
        assert decoded.cases[0].gold_sha256 == "a" * 64


@pytest.mark.parametrize("value", ["0", "2", "3.00", "-3.00", "0.000000000001", "9" * 128])
def test_decode_preserves_valid_normalized_decimals(value):
    document = protocol_document("result")
    document["value"] = value
    assert decode("result", document).value == value
