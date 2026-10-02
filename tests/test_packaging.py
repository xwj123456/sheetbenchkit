"""Real distributable installation, resource bytes and CLI behavior outside the checkout."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src/sheetbenchkit"
PUBLIC_ROOTS = {
    "src", "tests", "docs", ".github", ".gitattributes", ".gitignore", "pyproject.toml",
    "uv.lock", "LICENSE", "NOTICE", "README.md", "CONTRIBUTING.md", "SECURITY.md", "PKG-INFO",
}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def resource_hashes():
    return {
        str(p.relative_to(PACKAGE)).replace(os.sep, "/"): digest(p.read_bytes())
        for folder in ("data/v1", "schemas", "templates")
        for p in sorted((PACKAGE / folder).rglob("*"))
        if p.is_file()
    }


PROBE = r'''
import hashlib, importlib, json, pkgutil, sys
from importlib import metadata, resources
from pathlib import Path
import sheetbenchkit
package = Path(sheetbenchkit.__file__).resolve()
assert package.is_relative_to(Path(sys.prefix).resolve()), (package, sys.prefix)
distribution = metadata.distribution("sheetbenchkit")
direct = json.loads(distribution.read_text("direct_url.json"))
assert not direct.get("dir_info", {}).get("editable", False)
assert direct["url"].endswith(".whl"), direct
for module in pkgutil.walk_packages(sheetbenchkit.__path__, "sheetbenchkit."):
    if not module.name.endswith(".__main__"):
        importlib.import_module(module.name)
def collect(root, prefix):
    result = {}
    for child in root.iterdir():
        name = prefix + "/" + child.name
        if child.is_dir():
            result.update(collect(child, name))
        else:
            result[name] = hashlib.sha256(child.read_bytes()).hexdigest()
    return result
hashes = {}
for folder in ("data/v1", "schemas", "templates"):
    hashes.update(collect(resources.files("sheetbenchkit").joinpath(folder), folder))
licenses = [p for p in distribution.files if str(p).endswith(".dist-info/licenses/LICENSE")]
assert len(licenses) == 1, licenses
print(json.dumps({"package": str(package), "prefix": sys.prefix, "hashes": hashes,
    "license": hashlib.sha256(distribution.locate_file(licenses[0]).read_bytes()).hexdigest(),
    "readme": distribution.metadata.get_payload(),
    "content_type": distribution.metadata.get("Description-Content-Type")}))
'''

# This is test-only capture. It creates gold-free envelopes and invokes the installed
# independent adapter on real input files, including on Windows where toolkit run is unsupported.
CAPTURE = r'''
import json, subprocess, sys
from pathlib import Path
from sheetbenchkit import artifacts as a, models as m
from sheetbenchkit.contracts import decode_document, to_document
from sheetbenchkit.runner import make_task_envelope
root = Path(sys.argv[1]).resolve()
suite = decode_document("suite", (root / "suite.json").read_bytes())
cases = a.preflight_suite(suite, root)
observations = []
for ref, case in zip(suite.cases, cases, strict=True):
    envelope = to_document(make_task_envelope(case))
    for entry in envelope["inputs"]:
        entry["relative_path"] = str((root / ref.contract_path).parent / entry["relative_path"])
    result = subprocess.run([sys.executable, "-m", "sheetbenchkit.examples.independent_runner"],
        input=json.dumps(envelope).encode(), capture_output=True, timeout=10, check=True)
    observations.append(m.Observation(ref.case_id, 1, "installed", "SUCCESS",
        result.stdout.decode("utf-8"), result.stderr.decode("utf-8"), None, {"capture": "test"}))
Path(sys.argv[2]).write_bytes(a.canonical_json(m.ObservationBatch("1",
    (m.RunSpec("installed", 1),), tuple(observations))))
'''


@pytest.fixture(scope="module")
def installed():
    # tempfile is intentionally independent of pytest --basetemp and the source root.
    with tempfile.TemporaryDirectory(prefix="sheetbenchkit-installed-") as directory:
        temporary = Path(directory).resolve()
        assert not temporary.is_relative_to(ROOT)
        cwd = temporary / "无关 工作目录"
        cwd.mkdir()
        env = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "PYTHONHOME"}}
        env["PYTHONUTF8"] = "1"
        transcript = []

        def execute(argv, *, code=0, where=cwd):
            result = subprocess.run(
                [str(arg) for arg in argv], cwd=where, env=env, capture_output=True, timeout=180
            )
            record = {"argv": [str(arg) for arg in argv], "cwd": str(where),
                      "returncode": result.returncode,
                      "stdout": result.stdout.decode("utf-8"),
                      "stderr": result.stderr.decode("utf-8")}
            transcript.append(record)
            print(json.dumps(record, ensure_ascii=False))
            assert result.returncode == code, record
            return result

        before = resource_hashes()
        project = temporary / "build-source"
        project.mkdir()
        for name in PUBLIC_ROOTS - {"PKG-INFO"}:
            source = ROOT / name
            if source.is_dir():
                shutil.copytree(source, project / name,
                                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"))
            else:
                shutil.copy2(source, project / name)
        scratch = project / "private-scratch"
        scratch.mkdir()
        (scratch / "notes.txt").write_text("Unpublished build scratch.\n", encoding="utf-8")
        artifacts = temporary / "dist"
        execute([sys.executable, "-m", "build", "--outdir", artifacts, project])
        wheel, = artifacts.glob("*.whl")
        sdist, = artifacts.glob("*.tar.gz")
        venv = temporary / "fresh 环境"
        execute([sys.executable, "-m", "venv", venv])
        bin_dir = venv / ("Scripts" if sys.platform == "win32" else "bin")
        python = bin_dir / ("python.exe" if sys.platform == "win32" else "python")
        console = bin_dir / ("sheetbenchkit.exe" if sys.platform == "win32" else "sheetbenchkit")
        execute([python, "-m", "pip", "install", "--disable-pip-version-check", wheel])
        probe = json.loads(execute([python, "-c", PROBE]).stdout)
        evidence = os.environ.get("SHEETBENCHKIT_PACKAGING_EVIDENCE")
        try:
            yield {"temporary": temporary, "cwd": cwd, "execute": execute,
                   "python": python, "console": console, "probe": probe,
                   "before": before, "sdist": sdist, "wheel": wheel}
        finally:
            assert resource_hashes() == before, "installation/demo changed source preset bytes"
            if evidence:
                destination = Path(evidence)
                destination.mkdir(parents=True, exist_ok=True)
                (destination / "installed-transcript.json").write_text(
                    json.dumps(transcript, indent=2, ensure_ascii=False), encoding="utf-8"
                )
                (destination / "installed-resources.json").write_text(
                    json.dumps(probe, indent=2, ensure_ascii=False), encoding="utf-8"
                )
                for artifact in (wheel, sdist):
                    shutil.copy2(artifact, destination / artifact.name)
                (destination / "SHA256SUMS").write_text(
                    "".join(f"{digest(p.read_bytes())}  {p.name}\n" for p in (wheel, sdist)),
                    encoding="utf-8",
                )
                for path in cwd.rglob("*"):
                    if path.is_file() and (path.name in {"report.json", "report.html",
                                                        "observations.json"}
                                           or path.name.endswith("-saved.json")):
                        target = destination / "demo-results" / path.relative_to(cwd)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(path, target)


def test_installed_resources_license_and_readme_match_source_bytes(installed):
    probe = installed["probe"]
    assert sum(name.startswith("data/v1/") for name in installed["before"]) == 155
    assert probe["hashes"] == installed["before"]
    assert probe["license"] == digest((ROOT / "LICENSE").read_bytes())
    assert probe["content_type"] == "text/markdown"
    assert probe["readme"] == (ROOT / "README.md").read_text(encoding="utf-8")
    # Both artifacts must carry the same fixture bytes and legal/source documentation.
    with tarfile.open(installed["sdist"]) as archive:
        prefix = archive.getnames()[0].split("/")[0]
        roots = {name.split("/")[1] for name in archive.getnames() if "/" in name}
        assert roots <= PUBLIC_ROOTS, f"Unpublished sdist roots: {roots - PUBLIC_ROOTS}"
        expected_files = {}
        for name in PUBLIC_ROOTS - {"PKG-INFO"}:
            path = ROOT / name
            paths = path.rglob("*") if path.is_dir() else (path,)
            expected_files.update({
                p.relative_to(ROOT).as_posix(): p.read_bytes()
                for p in paths if p.is_file() and "__pycache__" not in p.parts
                and p.suffix not in {".pyc", ".pyo"}
            })
        actual_files = {
            member.name.removeprefix(prefix + "/"): archive.extractfile(member).read()
            for member in archive.getmembers() if member.isfile()
            and member.name != prefix + "/PKG-INFO"
        }
        assert actual_files == expected_files, "sdist must retain the complete public closure"
        for name, expected in installed["before"].items():
            member = archive.extractfile(f"{prefix}/src/sheetbenchkit/{name}")
            assert member is not None and digest(member.read()) == expected
        for name in ("LICENSE", "README.md"):
            member = archive.extractfile(f"{prefix}/{name}")
            assert member is not None and member.read() == (ROOT / name).read_bytes()


def assert_report(path):
    report = json.loads(path.read_bytes())
    assert report["planned"] == report["attempted"] == report["observed_count"] == 30
    assert report["missing_count"] == 0
    assert report["status_counts"] == {"PASS": 30, "FAIL": 0, "NO_RESULT": 0, "ERROR": 0}
    assert len(report["family_grades"]) == 4
    assert all(item["verdict"] == "PASS" for item in report["family_grades"])
    assert path.with_suffix(".html").is_file()


@pytest.mark.parametrize("entry", ["console", "module"])
def test_installed_demo_outside_checkout(installed, entry):
    execute = installed["execute"]
    python = installed["python"]
    command = [installed["console"]] if entry == "console" else [python, "-m", "sheetbenchkit"]
    suite = installed["cwd"] / f"{entry}-suite"
    observations = installed["cwd"] / f"{entry}-saved.json"
    output = installed["cwd"] / f"{entry}-grade"
    execute([*command, "generate", "--demo", "--seed", "42", "--output", suite])
    result = execute([*command, "validate", "--suite", suite])
    assert b"30 frozen cases" in result.stdout
    execute([python, "-c", CAPTURE, suite, observations])
    execute([*command, "grade", "--suite", suite, "--observations", observations,
             "--output", output])
    assert_report(output / "report.json")
    results = installed["cwd"] / f"{entry}-run"
    adapter = [python, "-m", "sheetbenchkit.examples.independent_runner"]
    run = [*command, "run", "--suite", suite, "--results", results,
           "--config-id", "independent", "--timeout", "5", "--"]
    if sys.platform == "win32":
        marker = installed["cwd"] / f"{entry}-start-marker"
        marker.write_text("untouched", encoding="utf-8")
        start = "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('started')"
        result = execute([*run, python, "-c", start, marker], code=2)
        assert b"UNSUPPORTED_PLATFORM" in result.stderr
        assert marker.read_text(encoding="utf-8") == "untouched"
        assert not results.exists()
    else:
        execute([*run, *adapter])
        assert_report(results / "report.json")
