# Contributing

Use small changes with a concrete regression example and a clear statement of the resulting behavior. Discuss changes to contracts, refusal rules, gold provenance, resource limits, or scoring denominators before changing their meaning.

## Development

Use Python 3.12 or 3.13 and a local environment. With uv installed:

```sh
uv sync --group dev
uv run pytest -q
uv run ruff check src tests
uv run mypy src/sheetbenchkit
uv run python -m build
```

For a focused change, first run the relevant tests. Release validation also needs the full checks, an installed wheel tested outside the checkout without `PYTHONPATH`, and the platform matrix. A local pass is not evidence that every OS, a live provider, or an external user has been tested. Windows core CLI tests must remain active; POSIX execution tests cannot establish Windows runner support.

Keep temporary suites, observations, build-test environments, and evidence outside the source tree. Preserve original fixtures and unrelated work. Do not commit credentials, customer spreadsheets, local paths, captured private output, or generated reports containing sensitive data.

## Fixtures and scoring

Every fixture needs an explicit contract, manifest, stable task, and frozen gold with a review basis. Verify expected truth independently before comparing with an adapter or reference implementation. The fixed preset records independent AI review with Decimal and integer long-division checks; do not replace that description with unsupported human signatures.

Treat input, task, contract, and gold edits as a new freeze/confirmation operation. Do not patch hashes or expected values while grading to make an observation pass. Generated contract-derived gold must remain distinguishable from independently reviewed preset gold.

Add meaningful tests for changed behavior and important boundaries. Negative controls should isolate one declared fault and return protocol-valid results so a business failure is distinguishable from JSON rejection or execution failure. Retain both M03 field-selection subtypes and both M08 scale/unit subtypes. Binding declarations and triplet consistency must stay separate from claims about internal source use.

## Pull requests

Describe the triggering input, the change, verification commands/results, and remaining limitations. Include reproducible synthetic data where needed. Preserve planned attempts and missing outputs in scoring; do not select the best repeat or infer zero usage/cost from absent telemetry.

For security-sensitive findings, follow [SECURITY.md](SECURITY.md) before posting details publicly. Contributions are distributed under the project's [Apache-2.0 license](LICENSE).
