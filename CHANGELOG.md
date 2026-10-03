# Changelog

## 0.1.0

Initial release of contract-driven, offline regression tests for spreadsheet agents.

- Frozen CSV/XLSX inputs, explicit calculation contracts, hash validation, and recorded expected results with declared review provenance.
- Exact sum, row count, and aggregate-ratio calculations with final HALF_UP rounding and explicit refusal rules.
- A reviewed synthetic preset with 30 inputs: 23 VALUE and 7 ABSTAIN, including four controlled three-input source families.
- Deterministic input variants, explicit trusted-argv execution on macOS/Linux, saved observations, and offline grading through `generate`, `validate`, `run`, and `grade`.
- Static HTML and JSON reports with separate value and declared-binding checks, planned-attempt denominators, raw output evidence, and unknown usage/cost when telemetry is absent.
- Python 3.12/3.13 CI for Linux, macOS, and Windows. Windows supports generation, validation, and saved-output grading; external-adapter `run` returns an unsupported-platform error before startup.
- Complete source-install instructions in English and Chinese, a reproducible three-case synthetic report preview, and a tutorial for independently reviewed custom CSV/XLSX cases.

The included adapters perform deterministic calculations. This release does not include a live AI-provider integration, real-model benchmark, or external adoption evidence. Public gold is inspectable, and source bindings are output declarations rather than proof of internal data use.
