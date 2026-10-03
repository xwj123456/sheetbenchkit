# Build your own frozen CSV/XLSX cases

This walkthrough starts with physical files, an explicit business contract, and supplied fixed gold. It does not load the bundled v1 cases or use an adapter/reference evaluator to generate expected answers. The starter contains two synthetic cases; copy its definitions for your own business question.

Use Python 3.12 or newer in the environment where SheetBenchKit is installed. See the [installation instructions](../README.md#quickstart) if needed. Every command below is a single line, so paths can also contain spaces or Chinese characters.

## 1. Inspect the synthetic input and fixed answers

Create a new input directory:

```sh
python -m sheetbenchkit.examples.custom_suite demo-inputs --output "./custom-inputs"
```

The command writes `sales.csv`, `budget.xlsx`, and `review.json`. It refuses an existing output directory. The files are synthetic and contain no customer data.

`sales.csv` contains:

```csv
status,amount
Confirmed,10.25
Pending,999.00
Confirmed,5.50
```

`budget.xlsx`, sheet `Budget`, contains:

| Physical row | project | actual | planned |
| --- | --- | --- | --- |
| 1 | project | actual | planned |
| 2 | Alpha | 40 | 120 |
| 3 | Beta | 10 | 80 |

The two contracts and independently inspectable literal answers are:

| Case | Exact source selection and rule | Fixed gold |
| --- | --- | --- |
| `custom_csv_sum` | CSV logical header record 1, data records 2–4; sum `amount` where `status` equals `Confirmed`, case-sensitive; CNY, final HALF_UP to 2 places. | `10.25 + 5.50 = 15.75`, unit `CNY`; exclude Pending `999.00`. |
| `custom_xlsx_ratio` | XLSX sheet `Budget`, header row 1, data rows 2–3; sum `actual` divided by sum `planned`; both columns CNY, final HALF_UP to 4 places. | `(40 + 10) / (120 + 80) = 50 / 200 = 0.2500`, unit `ratio`. |

CSV rows in the contract are logical records, not line numbers inside a quoted multiline field. XLSX row numbers are physical worksheet rows. The ratio gold is `0.2500`, not `25` or `25%`.

Open `review.json` before freezing. It contains:

```json
{
  "schema_version": "1",
  "review_note": "Describe how the fixed answers were established and the review scope.",
  "input_sha256": {
    "sales.csv": "<64 lowercase hexadecimal characters for the actual file bytes>",
    "budget.xlsx": "<64 lowercase hexadecimal characters for the actual file bytes>"
  },
  "expected_values": {
    "custom_csv_sum": "15.75",
    "custom_xlsx_ratio": "0.2500"
  },
  "derivations": {
    "custom_csv_sum": "10.25 + 5.50 = 15.75 CNY; Pending 999.00 is excluded.",
    "custom_xlsx_ratio": "(40 + 10) / (120 + 80) = 0.2500 ratio."
  }
}
```

The generated file has real hashes; the placeholders above are explanatory and cannot be passed to the module. The demo's gold comes from these fixed arithmetic literals. No human signature or customer review is claimed. The module records supplied review evidence as `not_authenticated`.

## 2. Freeze the declared input and review

```sh
python -m sheetbenchkit.examples.custom_suite freeze --inputs "./custom-inputs" --review "./custom-inputs/review.json" --output "./custom-suite"
python -m sheetbenchkit validate --suite "./custom-suite"
```

The expected validation message reports two frozen cases. The output includes:

```text
custom-suite/
  suite.json
  cases/
    custom_csv_sum/
      contract.json
      manifest.json
      task.json
      gold.json
      inputs/work.csv
    custom_xlsx_ratio/
      contract.json
      manifest.json
      task.json
      gold.json
      inputs/work.xlsx
```

The manifest hashes and `inputs/work.*` bind the original file bytes. XLSX is copied as bytes; it is not re-saved while freezing. `task.json` contains the stable business question, without runtime paths or gold. `gold.json` preserves the supplied expected value, derivation, review note, and input hash. These are two standalone cases, with no source-variant family requirement.

An existing destination is rejected, including an empty directory. Use a new output directory for every reviewed version. Input/review errors occur before publication; the public `freeze_suite` API handles atomic publication without replacing an existing destination.

Validation verifies schemas, source selections, snapshots, hashes, and required declarations. **It does not independently prove that a supplied expected answer is mathematically or commercially correct.** A syntactically valid wrong answer can be frozen. Review the answer independently before using it as a benchmark.

## 3. Run an independent adapter, then grade saved observations

On Linux or macOS, run the shipped independent example against the actual frozen files:

```sh
python -m sheetbenchkit run --suite "./custom-suite" --results "./custom-results" --config-id independent-example -- python -m sheetbenchkit.examples.independent_runner
```

The example adapter reads CSV with the standard library and XLSX with openpyxl. It uses separate calculation code, receives a gold-free task envelope, and never sees the expected answer. It is deterministic local example code, not a language model. For these inputs, `report.json` should contain two PASS results and no FAIL, NO_RESULT, or ERROR; open `custom-results/report.html` for the report. Exit code `0` means the complete required plan passed.

Grade the saved batch again without executing an adapter:

```sh
python -m sheetbenchkit grade --suite "./custom-suite" --observations "./custom-results/observations.json" --output "./custom-regraded"
```

This command works on Windows as well as Linux/macOS. On Windows, copy the **same frozen suite** and its saved `observations.json` from a supported POSIX run; do not regenerate different XLSX bytes and substitute them into the suite. Creating inputs, freezing, validating, and grading are supported on Windows. The toolkit's `run` command requires POSIX owned process groups and returns `UNSUPPORTED_PLATFORM` on Windows before starting an adapter.

Saved observations must follow the [adapter protocol](adapter-protocol.md). Keep every planned case/configuration/attempt; missing slots are NO_RESULT. The independent example has no token/cost telemetry and records unknown usage, not zero cost.

## 4. Replace the input with your own data

For the same two starter questions, provide your own `sales.csv` and `budget.xlsx` in an input directory. Preserve the declared filenames, exact headers, worksheet name, row ranges, and units. Additional rows outside the declared range are not automatically included.

Before freezing a changed input:

1. Inspect the exact source cells and confirm the business task and contract.
2. Compute the expected result independently, using exact arithmetic and the declared final rounding. Do not copy the adapter output as gold.
3. Write the reviewed literal into `expected_values`, update its `derivations`, and describe the actual review scope in `review_note`.
4. After review, calculate SHA256 for the actual original input bytes and update `input_sha256`.
5. Freeze to a new directory, validate it, and run your independent checks.

To print hashes without modifying either file:

```sh
python -c "import hashlib,json; from pathlib import Path; p=Path('./custom-inputs'); print(json.dumps({n:hashlib.sha256((p/n).read_bytes()).hexdigest() for n in ('sales.csv','budget.xlsx')},indent=2))"
```

Any input-byte change, including XLSX metadata from a re-save, makes the previous review fail with `REVIEW_INPUT_HASH_MISMATCH`. Updating a hash alone does not re-review the answer. The module only verifies that a supplied review refers to these bytes; it does not authenticate the reviewer or certify their calculation. A later independent test must investigate disagreements rather than replacing gold with whatever the adapter returned.

If you edit a file inside an already frozen suite, `validate` and grading reject the stale input hash. Keep the frozen artifact immutable; change the original inputs, review a new version, and freeze into a new directory.

## 5. Adapt the starter to another question

The implementation is deliberately an example module, not a general import command. Its [source](../src/sheetbenchkit/examples/custom_suite.py) shows the complete existing API sequence:

```python
# Use explicit contract/task/manifest documents and independently reviewed gold.
spec = decode_document("contract", canonical_json(contract_document))
manifest = decode_document("manifest", canonical_json(manifest_document))
validated = validate_case(spec, manifest, case_root)  # Includes physical inputs and task.json.
candidate = CandidateCase(validated, expected, "preset_independent", confirmation, evidence)
suite = freeze_suite((candidate,), (), new_destination)
```

Import these names from `sheetbenchkit.contracts`, `sheetbenchkit.artifacts`, and `sheetbenchkit.models` as shown in the runnable module. In a source checkout, copy the module into your own project and edit `_definitions()` for the worksheet/record range, exact fields, filter, unit, operation, and stable task. Also update the review shape and explicit expected-result bindings for your cases. The packaged example runs unchanged from an ordinary wheel; modifying installed package files is unnecessary.

`validate_case` reads the declared case-local input and a `task.json` file. The business confirmation must bind the canonical contract and task hashes. `CandidateCase` then supplies fixed expected values and evidence; `freeze_suite` does not call a reference evaluator or recalculate them. The existing `preset_independent` origin denotes supplied independently established fixed gold, including this custom starter; it is not a claim that your files came from the packaged v1 suite or that a human reviewer was authenticated.

Keep within the [v1 contract and resource limits](benchmark.md#resource-limits): one metric per case, sum/count/ratio operations, bounded CSV/XLSX inputs, exact field matching, and final HALF_UP rounding. Joins, arbitrary formulas, unit conversion, and formula evaluation need a separate design. Legitimate refusal cases need an explicit reviewed ABSTAIN result and applicable declared boundaries; do not silently treat missing values as zero.

中文提示：这个示例先冻结原始文件和明确的业务规则，再保存独立复核的固定答案。输入变更后必须重新复核答案并绑定新哈希；冻结或校验成功，只证明文件与声明一致，不能替代答案正确性或真实业务验收。
