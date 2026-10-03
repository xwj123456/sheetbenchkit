# SheetBenchKit

Contract-driven regression tests for spreadsheet agents.

[中文说明](README.zh-CN.md) · [Custom cases](docs/custom-cases.md) · [Report preview](docs/report-preview.md)

SheetBenchKit freezes spreadsheet inputs, explicit calculation contracts, and expected results, then grades saved adapter output offline. It checks exact values, units, refusals, and declared source bindings separately. Controlled input triplets test whether outputs respond consistently to target and distractor changes.

The bundled preset contains **30 synthetic inputs: 23 VALUE and 7 ABSTAIN**, including four three-input families. These are regression fixtures, not 30 independent tasks or a real-model benchmark. Public gold is inspectable and provides no anti-cheating guarantee.

## Quickstart

Install Git and Python 3.12 or 3.13 first. The following commands start from a directory that does not already contain a `sheetbenchkit` checkout. Installation may download Python dependencies; the demos themselves make no model calls and require no API key.

### macOS and Linux

```sh
git clone https://github.com/xwj123456/sheetbenchkit.git
cd sheetbenchkit
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install .
```

If you use Python 3.13, replace `python3.12` with `python3.13`. Keep generated artifacts outside the checkout and run the bundled deterministic adapter:

```sh
mkdir -p ../sheetbenchkit-demo
cd ../sheetbenchkit-demo

python -m sheetbenchkit generate --demo --seed 42 --output suite
python -m sheetbenchkit validate --suite suite
python -m sheetbenchkit run --suite suite --results results --config-id independent -- \
  python -m sheetbenchkit.examples.independent_runner
python -m sheetbenchkit grade --suite suite --observations results/observations.json --output regraded
```

Use a new destination for `generate`; it refuses to overwrite an existing suite. This demo selects 22 bases by their declared roles and generates 30 cases with four families. With the included deterministic adapter, all 30 cases and four families should pass. `run` saves `observations.json`, `report.json`, and `report.html`; `grade` produces the two reports from those saved observations. Open `results/report.html` locally to inspect results and captured evidence. The example reads the supplied files and calculates the contract with independent exact arithmetic.

### Windows PowerShell

```powershell
git clone https://github.com/xwj123456/sheetbenchkit.git
cd sheetbenchkit
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install .

.\.venv\Scripts\python.exe -m sheetbenchkit validate --suite docs/examples/report-preview/suite
.\.venv\Scripts\python.exe -m sheetbenchkit grade --suite docs/examples/report-preview/suite --observations docs/examples/report-preview/observations.json --output ../sheetbenchkit-preview
$LASTEXITCODE
```

Use `py -3.13` for Python 3.13. These commands use the environment's executable directly, so activation is optional. The saved preview deliberately contains one PASS, one FAIL, and one NO_RESULT. **Its `grade` exit code is 1 by design**; open `../sheetbenchkit-preview/report.html` to see the three outcomes. No adapter or model is started.

Installation, `generate`, `validate`, and `grade` are supported on Windows. **`run` is unsupported on Windows and returns exit code 2 before starting an adapter.** Use saved observations for offline grading, or use macOS/Linux to execute an adapter. Python 3.12 and 3.13 are the CI matrix; the minimum version declaration does not establish test coverage for every newer version.

### Install a downloaded wheel

After downloading the wheel from [GitHub Releases](https://github.com/xwj123456/sheetbenchkit/releases), install it in an existing Python 3.12/3.13 virtual environment:

```sh
python -m pip install ./sheetbenchkit-0.1.0-py3-none-any.whl
```

This project does not require a PyPI release. The wheel includes the packaged 30-case demo and example adapters. To reproduce the saved report preview or read the repository documentation locally, also clone the repository or extract the corresponding source archive; `docs/` is not installed by the wheel.

## See the report

![Actual SheetBenchKit report rendered from constructed synthetic saved outputs](docs/assets/report-preview.png)

The [report preview](docs/report-preview.md) explains the three cases and gives a reproducible offline command. Its saved outputs are constructed examples, not observations from an AI provider. Usage and cost remain unknown.

To test your own CSV/XLSX inputs, follow the [custom-case tutorial](docs/custom-cases.md). It walks through defining a contract, independently reviewing expected results, freezing inputs, and validating the resulting suite.

## Commands

Each command is available as `sheetbenchkit COMMAND` or `python -m sheetbenchkit COMMAND`.

| Command | Arguments | Purpose |
| --- | --- | --- |
| `validate` | `--suite DIR` | Verify contracts, frozen artifacts, input hashes, and suite budgets. |
| `generate` | `(--base DIR \| --demo) --seed N --output DIR` | Generate variants from a confirmed frozen suite or packaged demo. |
| `run` | `--suite DIR --results DIR [--repeats N] [--timeout N] [--config-id ID] -- ADAPTER ARG...` | Execute explicitly supplied trusted argv, capture output, then grade. |
| `grade` | `--suite DIR --observations FILE --output DIR` | Grade existing observations without starting adapters or using a network. |

`run` defaults to one attempt per case and a 60-second timeout per invocation. Repeats range from 1 to 10; there are no automatic retries or best-result selection. `--config-id` is a caller label, not authenticated model identity. An adapter may wrap an AI system, but the toolkit includes no live-provider integration or model-performance result. See the [adapter protocol](docs/adapter-protocol.md).

Exit codes are `0` for complete required passes, `1` for FAIL, NO_RESULT, or incomplete required family checks, and `2` for ERROR or invalid input/report. ERROR takes priority. Missing observations remain in the planned denominator.

## Scope and trust

Version 0.1.0 supports CSV/XLSX, exact file/sheet/header/range selectors, an optional single-field text equality filter, sum, row count, and ratios of two aggregates. It uses exact arithmetic and final HALF_UP rounding. It does not evaluate formulas, join tables, convert units, skip invalid selected values, or execute custom grading code.

`grade` compares frozen gold with saved text without repairing it. `run` executes trusted commands without a shell and bounds POSIX process groups, time, and captured output. **It is not a sandbox**: adapters retain filesystem, network, environment, and provider access. Runtime paths and captured logs can be sensitive; review artifacts before sharing.

Read [benchmark semantics and limits](docs/benchmark.md), [architecture](docs/architecture.md), [contribution guidance](CONTRIBUTING.md), [security guidance](SECURITY.md), and the [changelog](CHANGELOG.md). Licensed under [Apache-2.0](LICENSE).
