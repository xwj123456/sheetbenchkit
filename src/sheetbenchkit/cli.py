"""Explicit local commands; grading saved observations never executes adapters or gold code."""

from __future__ import annotations

import argparse
import sys
from importlib import resources
from pathlib import Path
from typing import cast

from . import artifacts as a
from . import models as m
from .contracts import decode_document, validate_schema


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sheetbenchkit")
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="verify an entire frozen suite offline")
    validate.add_argument("--suite", type=Path, required=True)
    generate = commands.add_parser("generate", help="generate confirmed contract variants offline")
    base = generate.add_mutually_exclusive_group(required=True)
    base.add_argument("--base", type=Path, help="confirmed frozen suite directory")
    base.add_argument("--demo", action="store_true", help="use packaged data/v1 confirmed bases")
    generate.add_argument("--seed", type=int, required=True)
    generate.add_argument("--output", type=Path, required=True)
    grade = commands.add_parser("grade", help="grade saved observations and write local reports")
    grade.add_argument("--suite", type=Path, required=True)
    grade.add_argument("--observations", type=Path, required=True)
    grade.add_argument("--output", type=Path, required=True)
    run = commands.add_parser(
        "run", help="execute trusted argv after --, save and grade observations"
    )
    run.add_argument("--suite", type=Path, required=True)
    run.add_argument("--results", type=Path, required=True)
    run.add_argument("--repeats", type=int, default=1)
    run.add_argument(
        "--timeout", type=int, default=60, help="positive integer seconds (default: 60)"
    )
    run.add_argument(
        "--config-id", default="adapter", help="caller label, not authenticated identity"
    )
    return parser


def _suite(root: Path) -> m.Suite:
    return cast(m.Suite, decode_document("suite", a._read_artifact(root, "suite.json")))


def _protected(suite: m.Suite, cases: tuple[m.FrozenCase, ...], root: Path) -> set[Path]:
    paths = {root / "suite.json"}
    for ref, case in zip(suite.cases, cases, strict=True):
        paths.update(
            root / getattr(ref, kind + "_path") for kind in ("contract", "manifest", "task", "gold")
        )
        paths.update(
            root / "cases" / ref.case_id / entry.relative_path
            for entry in case.validated.manifest.inputs
        )
    return {path.resolve() for path in paths}


def _output(output: Path, names: tuple[str, ...], protected: set[Path]) -> None:
    """Check actual targets before any write; never follow an artifact symlink."""
    package = Path(__file__).resolve().parent
    for name in names:
        target = output / name
        resolved = target.resolve()
        if (
            target.is_symlink()
            or resolved in protected
            or (target.exists() and resolved.is_relative_to(package))
            or (target.is_file() and target.stat().st_nlink > 1)
        ):
            raise m.CaseError("OUTPUT_PATH_CONFLICT", str(target))
        if target.exists() and not target.is_file():
            raise m.CaseError("OUTPUT_PATH_CONFLICT", str(target))
    output.mkdir(parents=True, exist_ok=True)


def _report(
    suite: m.Suite, cases: tuple[m.FrozenCase, ...], batch: m.ObservationBatch, output: Path
) -> int:
    from .grader import grade_suite
    from .report import exit_code, write_report

    report = grade_suite(suite, cases, batch)
    write_report(report, output)
    code = exit_code(report)
    if code == 2:
        reasons = {
            grade.reason or "ERROR" for grade in report.case_grades if grade.verdict == "ERROR"
        }
        reasons.update(
            grade.reason or "ERROR" for grade in report.family_grades if grade.verdict == "ERROR"
        )
        print("ERROR " + ", ".join(sorted(reasons or {"INVALID_REPORT"})), file=sys.stderr)
    return code


def _generate(root: Path, seed: int, destination: Path) -> int:
    suite = _suite(root)
    cases = a.preflight_suite(suite, root)
    # A family explicitly names its already-generated perturbations. Keep its base
    # and every other standalone case, regardless of case IDs or preset numbering.
    variants = {
        identity for family in suite.families for identity in (family.target, family.distractor)
    }
    explicit_bases = {family.base for family in suite.families}
    bases = tuple(
        case.validated
        for case in cases
        if case.validated.manifest.case_id in explicit_bases
        or case.validated.manifest.case_id not in variants
    )
    from .variants import generate_suite

    generate_suite(bases, seed, destination)
    return 0


def main(argv: list[str] | None = None) -> int:
    """Return a stable process exit code; input/IO errors have stderr diagnostics."""
    args = list(sys.argv[1:] if argv is None else argv)
    adapter: tuple[str, ...] = ()
    if args and args[0] == "run" and "--" in args:
        separator = args.index("--")
        adapter = tuple(args[separator + 1 :])
        args = args[:separator]
    try:
        options = _parser().parse_args(args)
    except SystemExit as exc:
        return int(exc.code or 0)
    try:
        if options.command == "generate":
            if options.demo:
                # as_file also supports installed zipped resources and extracts only
                # to its own temporary directory; all hashes are still preflighted.
                resource = resources.files("sheetbenchkit").joinpath("data/v1")
                with resources.as_file(resource) as root:
                    return _generate(root, options.seed, options.output)
            return _generate(options.base, options.seed, options.output)
        if options.command == "run":
            if sys.platform == "win32":
                raise m.CaseError("UNSUPPORTED_PLATFORM", "run requires POSIX owned process groups")
            if not adapter or not adapter[0] or any("\x00" in arg for arg in adapter):
                raise m.CaseError("INVALID_ADAPTER_ARGV", "provide trusted ADAPTER ARG... after --")
            if options.timeout <= 0:
                raise m.CaseError("INVALID_TIMEOUT", "positive integer seconds required")
            validate_schema(
                "observations",
                m.ObservationBatch("1", (m.RunSpec(options.config_id, options.repeats),), ()),
            )
        root = options.suite.resolve()
        suite = _suite(root)
        cases = a.preflight_suite(suite, root)
        if options.command == "validate":
            print(f"Validated {suite.suite_id}: {len(cases)} frozen cases")
            return 0
        protected = _protected(suite, cases, root)
        if options.command == "grade":
            path = options.observations
            batch = cast(m.ObservationBatch, decode_document("observations", path.read_bytes()))
            _output(options.output, ("report.json", "report.html"), protected | {path.resolve()})
            return _report(suite, cases, batch, options.output)
        from .runner import run_suite

        _output(options.results, ("observations.json", "report.json", "report.html"), protected)
        batch = run_suite(
            suite,
            root,
            adapter,
            repeats=options.repeats,
            timeout_s=options.timeout,
            config_id=options.config_id,
        )
        # Persist every planned attempt and captured stream before the second
        # preflight. A trusted adapter can change files; that invalidates grading.
        _output(options.results, ("observations.json",), protected)
        (options.results / "observations.json").write_bytes(a.canonical_json(batch))
        cases = a.preflight_suite(suite, root)
        _output(options.results, ("report.json", "report.html"), protected)
        return _report(suite, cases, batch, options.results)
    except m.CaseError as exc:
        print(f"ERROR {exc.reason}: {exc.details}", file=sys.stderr)
        return 2
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"ERROR CLI_IO_ERROR: {exc}", file=sys.stderr)
        return 2
