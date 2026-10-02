# SheetBenchKit

Contract-driven regression tests for spreadsheet agents.

SheetBenchKit freezes spreadsheet inputs, explicit calculation contracts, and expected results, then grades recorded adapter output offline. It checks exact values, units, refusals, and declared source bindings separately. Controlled input triplets test whether outputs respond consistently to target and distractor changes.

The bundled preset contains **30 synthetic inputs: 23 VALUE and 7 ABSTAIN**, including four three-input families. These are regression fixtures, not 30 independent tasks or a real-model benchmark. Public gold is inspectable and provides no anti-cheating guarantee.

## Quickstart

Requires Python 3.12 or newer. Install from a source checkout in a virtual environment:

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install .
```

Alternatively, install a locally built wheel with `python -m pip install /path/to/sheetbenchkit-0.1.0-py3-none-any.whl`. These instructions use source or wheel installation; they do not depend on a PyPI release.

On macOS or Linux, keep generated artifacts in a separate working directory and run the bundled deterministic adapter:

```sh
mkdir -p ../sheetbenchkit-demo
cd ../sheetbenchkit-demo

sheetbenchkit generate --demo --seed 42 --output suite
sheetbenchkit validate --suite suite
sheetbenchkit run --suite suite --results results --config-id independent -- \
  python -m sheetbenchkit.examples.independent_runner
sheetbenchkit grade --suite suite --observations results/observations.json --output regraded
```

`generate` needs a new destination. The demo selects 22 bases by their declared roles and generates 30 cases with four families. `run` saves `observations.json`, `report.json`, and `report.html`; `grade` produces the two reports from those saved observations. Open `results/report.html` locally to inspect results and captured evidence. The example adapter reads the supplied files and computes the contract with independent exact arithmetic; it makes no model calls.

All four commands also work through the module entry point: replace `sheetbenchkit` with `python -m sheetbenchkit`, for example:

```sh
python -m sheetbenchkit grade --suite suite --observations results/observations.json --output module-report
```

On Windows, create/activate the environment with `py -3.12 -m venv .venv` and `.venv\Scripts\Activate.ps1` in PowerShell. Installation, `generate`, `validate`, and `grade` are supported. **`run` is unsupported on Windows and returns exit code 2 before starting an adapter.** Grade observations captured elsewhere, or use macOS/Linux for execution. Python 3.12 and 3.13 are the intended CI matrix; the minimum version declaration does not establish test coverage for every newer Python version.

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

Version 1 supports CSV/XLSX, exact file/sheet/header/range selectors, an optional single-field text equality filter, sum, row count, and ratios of two aggregates. It uses exact arithmetic and final HALF_UP rounding. It does not evaluate formulas, join tables, convert units, skip invalid selected values, or execute custom grading code.

`grade` compares frozen gold with captured text without repairing it. `run` executes trusted commands without a shell and bounds POSIX process groups, time, and captured output. **It is not a sandbox**: adapters retain filesystem, network, environment, and provider access. Runtime paths and captured logs can be sensitive; review artifacts before sharing.

Read [benchmark semantics and limits](docs/benchmark.md), [architecture](docs/architecture.md), [contribution guidance](CONTRIBUTING.md), and [security guidance](SECURITY.md). Licensed under [Apache-2.0](LICENSE).
