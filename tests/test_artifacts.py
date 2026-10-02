"""Freeze tests catch partial publication, stale evidence, and mutable/hash-unstable artifacts."""

import dataclasses
import hashlib
import json

import pytest
from test_contracts import api, contract, decode, manifest


def canonical(document):
    return json.dumps(
        document, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def candidate(case_id="base-001", origin="preset_independent"):
    data = b"amount\n1\n2\n"
    spec_doc = contract()
    task = {"description": "Sum exact amount in target rows 2..3; CNY; HALF_UP places=2."}
    confirmation = {
        "reviewed_contract_sha256": hashlib.sha256(canonical(spec_doc)).hexdigest(),
        "task_sha256": hashlib.sha256(canonical(task)).hexdigest(),
        "reviewer": "independent-fixture",
    }
    doc = manifest()
    doc["case_id"] = case_id
    doc["inputs"][0]["sha256"] = hashlib.sha256(data).hexdigest()
    doc["business_confirmation"] = confirmation
    spec = decode("contract", spec_doc)
    man = decode("manifest", doc)
    bundle = api("models", "InputBundle")({}, {"work": data}, doc["unit_labels"], {})
    validated = api("models", "ValidatedCase")(spec, man, bundle, task)
    binding = api("models", "Binding")("target", "work", "@csv", 1, (2, 3), ("amount",))
    expected = api("models", "ExpectedResult")("VALUE", "3.00", "CNY", None, (binding,))
    return api("models", "CandidateCase")(
        validated, expected, origin, confirmation, {"method": "independent hand sum 1+2=3"}
    )


def test_invalid_contract_and_atomic_freeze(tmp_path):
    item = candidate()
    freeze = api("artifacts", "freeze_suite")
    error = api("models", "CaseError")
    for items in [
        (item, item),
        (
            dataclasses.replace(
                item, confirmation={**item.confirmation, "reviewed_contract_sha256": "b" * 64}
            ),
        ),
        (dataclasses.replace(item, evidence={}),),
        (dataclasses.replace(item, confirmation={**item.confirmation, "task_sha256": "b" * 64}),),
    ]:
        destination = tmp_path / "invalid"
        with pytest.raises(error):
            freeze(items, (), destination)
        assert not destination.exists()
        assert list(tmp_path.iterdir()) == []
    destination = tmp_path / "valid"
    suite = freeze((item,), (), destination)
    assert suite.cases[0].case_id == "base-001"
    gold = api("contracts", "decode_document")(
        "gold", (destination / suite.cases[0].gold_path).read_bytes()
    )
    assert gold.origin == "preset_independent"
    assert gold.expected.value == "3.00"
    persisted = json.loads((destination / suite.cases[0].manifest_path).read_bytes())
    path = destination / "cases/base-001" / persisted["inputs"][0]["relative_path"]
    assert path.read_bytes() == b"amount\n1\n2\n"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == persisted["inputs"][0]["sha256"]
    original = (destination / "suite.json").read_bytes()
    with pytest.raises(error):
        freeze((item,), (), destination)
    assert (destination / "suite.json").read_bytes() == original


def test_freeze_checks_snapshots_expected_bindings_and_family_refs(tmp_path):
    item = candidate()
    freeze = api("artifacts", "freeze_suite")
    bundle = dataclasses.replace(item.validated.inputs, snapshots={"work": b"changed"})
    stale = dataclasses.replace(item, validated=dataclasses.replace(item.validated, inputs=bundle))
    binding = dataclasses.replace(item.expected.bindings[0], file_id="wrong")
    wrong = dataclasses.replace(
        item, expected=dataclasses.replace(item.expected, bindings=(binding,))
    )
    family = api("models", "FamilySpec")("family", "base-001", "missing", "other", {})
    for candidates, families in [((stale,), ()), ((wrong,), ()), ((item,), (family,)), ((), ())]:
        with pytest.raises(api("models", "CaseError")):
            freeze(candidates, families, tmp_path / "invalid")
        assert not (tmp_path / "invalid").exists()


def test_canonical_hash_stability_and_runtime_exclusion():
    canonical_json = api("artifacts", "canonical_json")
    content_hash = api("artifacts", "content_hash")
    assert canonical_json({"z": 0, "a": "中文"}) == b'{"a":"\xe4\xb8\xad\xe6\x96\x87","z":0}'
    assert content_hash({"a": 1, "runtime_metadata": {"elapsed": 2}, "content_sha256": "old"}) == (
        hashlib.sha256(b'{"a":1}').hexdigest()
    )
    assert content_hash({"a": 2}) != content_hash({"a": 1})
    with pytest.raises(api("models", "CaseError")):
        canonical_json({"invalid": float("nan")})


def test_freeze_deterministic_and_deeply_immutable(tmp_path):
    item = candidate(origin="generated_contract")
    freeze = api("artifacts", "freeze_suite")
    first = freeze((item,), (), tmp_path / "one")
    second = freeze((item,), (), tmp_path / "two")
    assert first.content_sha256 == second.content_sha256
    assert (tmp_path / "one/suite.json").read_bytes() == (tmp_path / "two/suite.json").read_bytes()
    with pytest.raises(TypeError):
        item.validated.task["description"] = "changed"
    with pytest.raises(TypeError):
        item.evidence["method"] = "changed"


def test_freeze_rejects_runtime_identity_in_stable_task(tmp_path):
    item = candidate()
    task = {**item.validated.task, "case_id": "base-001", "inputs": [{"sha256": "a" * 64}]}
    confirmation = {**item.confirmation, "task_sha256": hashlib.sha256(canonical(task)).hexdigest()}
    case = dataclasses.replace(
        item.validated,
        task=task,
        manifest=dataclasses.replace(item.validated.manifest, business_confirmation=confirmation),
    )
    item = dataclasses.replace(item, validated=case, confirmation=confirmation)
    with pytest.raises(api("models", "CaseError")):
        api("artifacts", "freeze_suite")((item,), (), tmp_path / "invalid")
    assert not (tmp_path / "invalid").exists()


def test_freeze_io_failure_removes_staging_and_preserves_other_files(tmp_path, monkeypatch):
    from pathlib import Path

    sentinel = tmp_path / "unrelated.txt"
    sentinel.write_text("preserve")
    original = Path.open

    def failing_open(self, *args, **kwargs):
        if self.name == "gold.json" and args == ("xb",):
            raise OSError("injected write failure")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", failing_open)
    with pytest.raises(api("models", "CaseError"), match="FREEZE_IO_ERROR"):
        api("artifacts", "freeze_suite")((candidate(),), (), tmp_path / "suite")
    assert not (tmp_path / "suite").exists()
    assert sentinel.read_text() == "preserve"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["unrelated.txt"]


def test_freeze_publication_never_overwrites_racing_destination(tmp_path, monkeypatch):
    import sheetbenchkit.artifacts as artifacts

    publish = artifacts._publish

    def racing_publish(staging, destination):
        destination.mkdir()
        publish(staging, destination)

    monkeypatch.setattr(artifacts, "_publish", racing_publish)
    with pytest.raises(api("models", "CaseError"), match="FREEZE_IO_ERROR"):
        artifacts.freeze_suite((candidate(),), (), tmp_path / "suite")
    assert (tmp_path / "suite").is_dir()
    assert list((tmp_path / "suite").iterdir()) == []
    assert sorted(p.name for p in tmp_path.iterdir()) == ["suite"]


def test_expected_missing_remains_absent_and_null_hashed(tmp_path):
    item = candidate()
    entry = dataclasses.replace(
        item.validated.manifest.inputs[0], expected_missing=True, sha256=None
    )
    manifest_ = dataclasses.replace(
        item.validated.manifest, inputs=(entry,), intentional_boundaries=("MISSING_SOURCE",)
    )
    case = dataclasses.replace(
        item.validated,
        manifest=manifest_,
        inputs=dataclasses.replace(item.validated.inputs, snapshots={}),
    )
    expected = dataclasses.replace(
        item.expected, status="ABSTAIN", value=None, unit=None, reason="MISSING_SOURCE", bindings=()
    )
    suite = api("artifacts", "freeze_suite")(
        (dataclasses.replace(item, validated=case, expected=expected),), (), tmp_path / "missing"
    )
    persisted = api("contracts", "decode_document")(
        "manifest", (tmp_path / "missing" / suite.cases[0].manifest_path).read_bytes()
    )
    assert persisted.inputs[0].sha256 is None
    assert persisted.inputs[0].expected_missing
    assert not (tmp_path / "missing/cases/base-001/inputs/work.csv").exists()


def test_invalid_task_metadata_reports_case_error_without_publication(tmp_path):
    item = candidate()
    case = dataclasses.replace(item.validated, task={1: "invalid JSON key"})
    with pytest.raises(api("models", "CaseError"), match="INVALID_JSON"):
        api("artifacts", "freeze_suite")(
            (dataclasses.replace(item, validated=case),), (), tmp_path / "invalid"
        )
    assert not (tmp_path / "invalid").exists()


@pytest.mark.parametrize("suffix", ["\n", "\r\n", "\r", "\t", "!", "\u2028", "\x00"])
@pytest.mark.parametrize("mutation", ["case_id", "sum_value", "count_value", "input_hash"])
def test_freeze_rejects_protocol_trailing_characters_without_publication(
    tmp_path, mutation, suffix
):
    item = candidate()
    if mutation == "case_id":
        case = dataclasses.replace(
            item.validated,
            manifest=dataclasses.replace(item.validated.manifest, case_id="base-001" + suffix),
        )
        item = dataclasses.replace(item, validated=case)
    elif mutation == "sum_value":
        item = dataclasses.replace(
            item, expected=dataclasses.replace(item.expected, value="3.00" + suffix)
        )
    elif mutation == "input_hash":
        manifest_ = item.validated.manifest
        entry = dataclasses.replace(manifest_.inputs[0], sha256=manifest_.inputs[0].sha256 + suffix)
        case = dataclasses.replace(
            item.validated, manifest=dataclasses.replace(manifest_, inputs=(entry,))
        )
        item = dataclasses.replace(item, validated=case)
    else:
        spec = dataclasses.replace(
            item.validated.spec, metric=api("models", "CountSpec")("count", "target")
        )
        confirmation = {
            **item.confirmation,
            "reviewed_contract_sha256": hashlib.sha256(
                api("artifacts", "canonical_json")(spec)
            ).hexdigest(),
        }
        manifest_ = dataclasses.replace(item.validated.manifest, business_confirmation=confirmation)
        case = dataclasses.replace(item.validated, spec=spec, manifest=manifest_)
        binding = dataclasses.replace(item.expected.bindings[0], fields=())
        expected = dataclasses.replace(
            item.expected, value="2" + suffix, unit="count", bindings=(binding,)
        )
        item = dataclasses.replace(
            item, validated=case, expected=expected, confirmation=confirmation
        )
    destination = tmp_path / "invalid"
    with pytest.raises(api("models", "CaseError")):
        api("artifacts", "freeze_suite")((item,), (), destination)
    assert not destination.exists()
    assert list(tmp_path.iterdir()) == []


def test_freeze_preserves_boundary_id_hashes_and_count_decimal(tmp_path):
    boundary_id = "A" + "a0_-" * 15 + "Z9_"
    item = candidate(case_id=boundary_id)
    spec = dataclasses.replace(
        item.validated.spec, metric=api("models", "CountSpec")("count", "target")
    )
    confirmation = {
        **item.confirmation,
        "reviewed_contract_sha256": hashlib.sha256(
            api("artifacts", "canonical_json")(spec)
        ).hexdigest(),
    }
    manifest_ = dataclasses.replace(item.validated.manifest, business_confirmation=confirmation)
    case = dataclasses.replace(item.validated, spec=spec, manifest=manifest_)
    binding = dataclasses.replace(item.expected.bindings[0], fields=())
    expected = dataclasses.replace(item.expected, value="2", unit="count", bindings=(binding,))
    item = dataclasses.replace(item, validated=case, expected=expected, confirmation=confirmation)
    destination = tmp_path / "valid"
    suite = api("artifacts", "freeze_suite")((item,), (), destination)
    assert suite.cases[0].case_id == boundary_id
    gold = api("contracts", "decode_document")(
        "gold", (destination / suite.cases[0].gold_path).read_bytes()
    )
    assert gold.expected.value == "2"
    assert gold.confirmation["reviewed_contract_sha256"] == confirmation["reviewed_contract_sha256"]
