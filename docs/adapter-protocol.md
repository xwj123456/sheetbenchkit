# Adapter protocol

An adapter is an explicitly supplied trusted command. It may wrap deterministic code or an AI system. No provider SDK, credentials, or live-provider validation are included.

## Invocation

On macOS/Linux:

```sh
sheetbenchkit run --suite suite --results results --repeats 1 --timeout 60 \
  --config-id my-adapter -- python adapter.py
```

The runner starts one process per case/attempt, passes argv without a shell, writes one UTF-8 JSON TaskEnvelope followed by a newline to stdin, and closes stdin. Read the complete stdin document. Return one UTF-8 JSON AgentResult on stdout and exit zero. Put diagnostic logs on stderr. The deterministic example uses:

```sh
python -m sheetbenchkit.examples.independent_runner
```

`--config-id` is a caller label. Repeats are explicit and are not retries. Windows `run` returns ERROR/exit 2 before adapter startup.

## TaskEnvelope on stdin

| Field | Meaning |
| --- | --- |
| `schema_version` | `"1"`. |
| `case_id`, `metric_id` | Identities to echo in the result. |
| `task` | Stable confirmed business instructions. |
| `inputs` | Input descriptors with `file_id`, `relative_path`, `format`, `sha256`, and `expected_missing`. |
| `source_ids` | Declared logical source IDs. |
| `output_protocol` | Format instructions, complete `result_schema`, machine-readable `contract`, and explicit `unit_labels`. |

During execution, the field named `inputs[].relative_path` contains an **absolute filesystem path** to that case's frozen input. This runtime binding does not alter the stable task or frozen hashes. Use these supplied paths rather than guessing from the current directory, file ordering, or case ID. The contract supplies exact file IDs, sheets, header rows, closed row ranges, fields, optional text `eq`, units, and rounding. Gold is not included in the envelope, but the runner does not prevent an adapter from accessing public gold elsewhere.

## AgentResult on stdout

An illustrative result for fixed case S01 is:

```json
{
  "schema_version": "1",
  "case_id": "S01",
  "metric_id": "metric",
  "status": "VALUE",
  "value": "100",
  "unit": "units",
  "reason": null,
  "bindings": [
    {
      "source": "chosen",
      "file_id": "target",
      "sheet": "Actual",
      "header_row": 1,
      "rows": [2, 2],
      "fields": ["amount"]
    }
  ]
}
```

Echo the actual envelope identities. VALUE needs a normalized decimal string of at most 128 characters, a nonempty exact unit, and complete bindings. Use fixed output places from the contract; do not use JSON numbers, scientific notation, percent scaling, NaN, or Infinity. `reason` must be absent or null for VALUE. Bindings include fields used by filters as well as calculations; order is immaterial, names and ranges are exact.

For an expected refusal, return `status: "ABSTAIN"`, `value: null`, `bindings: []` or legal attempted bindings, and a reason from the schema:

```json
{
  "schema_version": "1",
  "case_id": "N15",
  "metric_id": "metric",
  "status": "ABSTAIN",
  "value": null,
  "unit": null,
  "reason": "MISSING_VALUE",
  "bindings": []
}
```

Allowed reasons are MISSING_SOURCE, MISSING_FIELD, AMBIGUOUS_FIELD, MISSING_VALUE, INVALID_VALUE, UNSUPPORTED_CELL, INVALID_FILTER_VALUE, UNSUPPORTED_UNIT_CONVERSION, ZERO_DENOMINATOR, and UNSUPPORTED_INPUT_LIMIT. A legal reason must also match that case's gold. ABSTAIN is not a generic success response.

The [result schema](../src/sheetbenchkit/schemas/result.json) rejects additional fields. Do not wrap the result in another object, add usage metadata, print Markdown fences, concatenate multiple JSON objects, or mix logs with stdout. Strict decoding rejects duplicate keys and nonstandard nonfinite literals. Invalid JSON becomes NO_RESULT/INVALID_JSON; valid JSON that violates the result protocol becomes FAIL/PROTOCOL_VIOLATION. No extraction or repair is performed.

## Captured observations

`run` writes an ObservationBatch with `schema_version`, `runs`, and `observations`. Each run declares `config_id` and `repeats`. Each observation stores `case_id`, `attempt`, `config_id`, `execution_status`, original `raw_stdout`/`raw_stderr`, `usage`, and `runtime_metadata`. The [observation schema](../src/sheetbenchkit/schemas/observations.json) defines saved-batch input for offline grading.

Execution statuses are SUCCESS, TIMEOUT, NONZERO_EXIT, START_ERROR, and OUTPUT_LIMIT. Timeout/nonzero exit/output limit yield NO_RESULT; START_ERROR yields ERROR. An exit-zero process can still fail result validation. Stream caps retain a bounded prefix with explicit truncation metadata; a valid-looking prefix is not accepted as a complete result. Invalid UTF-8 uses a bytes representation with exact captured bytes in base64 metadata rather than lossy replacement.

Runner `usage` is always null because AgentResult has no telemetry channel. Cost is unknown. External capture systems can supply usage in saved observations; the grader preserves it without authenticating it. Keep every declared case/attempt, including missing results. See [benchmark semantics](benchmark.md) for denominator and verdict rules.

The runner controls its POSIX process group, not adapter filesystem/network access, escaped sessions, internal retries, or provider bills. Review [SECURITY.md](../SECURITY.md) before executing or publishing adapter artifacts.
