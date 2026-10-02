# Architecture

SheetBenchKit separates case construction, execution, and grading so saved output can be evaluated without rerunning an adapter or recalculating gold.

## Data flow

1. A contract defines one metric and explicit sources. A manifest identifies input bytes and declared boundaries. A stable task records the business instructions.
2. Validation reads bounded CSV/XLSX snapshots and checks schemas, contained paths, hashes, selectors, and declared limits.
3. Freezing binds reviewed expected results to the contract, manifest, task, and input hashes. Generation creates deterministic permitted mutations, recalculates their contract-derived gold, and freezes a new suite.
4. Optional execution preflights the entire suite, supplies each adapter with a gold-free task envelope, and records each planned invocation's original output and execution metadata.
5. Grading compares observations with frozen gold. Reports serialize the saved grades and display their evidence; rendering does not recalculate results.

The persisted layout is:

```text
suite/
  suite.json
  cases/<case_id>/
    contract.json
    manifest.json
    task.json
    gold.json
    inputs/...
```

A separate `observations.json` contains the declared run plan and observations. Reports contain `report.json` and a local `report.html`.

## Modules

| Module | Responsibility |
| --- | --- |
| `models`, `contracts` | Immutable data models, strict JSON codecs, and bundled Draft 2020-12 schemas. |
| `loaders` | Bounded file snapshots, CSV/XLSX parsing, and source tables. |
| `reference` | Exact contract evaluation used during case construction. |
| `variants`, `artifacts` | Deterministic mutations, confirmation records, freezing, persistence, and preflight. |
| `runner` | Gold-free envelopes and explicit trusted POSIX subprocess execution. |
| `grader` | Pure comparison of frozen expected results and recorded output. |
| `report`, `cli` | Saved-state reports and the four public commands. |
| `examples.independent_runner` | Example file-reading adapter with independent calculation logic. |

Schemas, templates, and the fixed demo are package resources resolved with `importlib.resources`. Running an installed package does not require the checkout as the current directory.

## Identity and integrity

Canonical JSON uses UTF-8, sorted keys, fixed separators, and no nonfinite values. Content hashes include business content and exclude their own hash field and runtime timing. Input hashes bind actual bytes. Confirmation records link stable task and contract hashes; they are declarations of review, not reviewer authentication.

Generation selects every explicit family base and every case that is not already a target/distractor variant. Explicit base roles take precedence. For the bundled suite, four family bases and 18 standalone bases produce 30 cases. An existing generation destination is refused.

A changed input, contract, task, or gold requires a new confirmed freeze. Preflight failures are infrastructure ERROR, not adapter calculation failures. `run` saves observations and re-preflights frozen artifacts before grading, so an adapter cannot silently update its own expected answer.

## Execution and offline grading

`run` passes an argv list without a shell and starts a new process group for each case/attempt. Time and stream capture are bounded; group ownership is not filesystem or network isolation. The runner records execution independently of result validity and never retries automatically.

`grade` starts no adapters, uses no network or model credentials, and executes no output or custom grader. It preserves raw text, usage, and runtime metadata as evidence. Invalid JSON is not extracted from prose or repaired. The report escapes dynamic content as text and loads no external assets.

See [benchmark semantics](benchmark.md) for limits and scoring, and the [adapter protocol](adapter-protocol.md) for the execution interface.
