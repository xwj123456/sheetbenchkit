# Benchmark semantics

## Fixed synthetic preset

The bundled v1 suite contains 30 unique fixture inputs: 23 expect VALUE and seven expect ABSTAIN. Four three-input families account for 12 of those 30 inputs. A fixture input is a scoring unit, not an independent task or user scenario.

| Area | Inputs | Coverage |
| --- | --- | --- |
| Controlled source families | 12 | File, sheet, exact field, and filter/count selection. |
| Values and missingness | 6 | Valid zero, cancellation, missing/invalid selected values, empty selection, HALF_UP. |
| Field bindings | 4 | Duplicate headers, exact names, column reordering, missing fields. |
| Ratios and aggregation | 4 | Ratio of totals, filtered counts, zero numerator/denominator. |
| Units | 4 | Exact output unit, ratio scale, unsupported conversions. |

Each family holds the contract and task fixed across a base, target-only edit, and distractor-only edit. The target edit must change the final rounded gold; the distractor edit must leave it unchanged. All three results must match gold and satisfy that relation for family PASS. This establishes observed consistency under these perturbations. It does not prove what an adapter read internally or identify a wrong source from a failure alone.

Bindings are self-reported declarations. Their check only asks whether the declared file, sheet, header, row range, and fields match the contract.

## Gold provenance and negative controls

The fixed preset's expected values were established from a reviewed fixed matrix before adapter/reference crosschecks. An independent AI tester reviewed the expectations with two methods: Decimal at precision 200 with final HALF_UP, and integer arithmetic with digit long division. [The provenance record](../src/sheetbenchkit/data/v1/provenance.json) includes operands, expectations, and review scope. It contains no individual human review signatures or customer-validation claim.

Preset gold uses `preset_independent`; seed-generated variants use `generated_contract` and the contract evaluator. These origins are distinct. Public gold enables inspection and regression checks, not protection against memorization.

Test-only negative controls cover 12 conceptual fault classes through 14 subtypes. M03 separately tests substring field selection and fixed column indexing; M08 separately tests ratio scaling and ignored units. Other controls cover wrong files/sheets/filters, treating missing or invalid values as zero, averaging row ratios, HALF_EVEN, universal refusal, constant zero, and incorrect declared bindings.

M01–M11 retain target-contract declarations while injecting a business fault. M12 changes only declarations with correct calculation and measures declaration enforcement. Required killer results must be protocol-valid FAILs; schema rejection, NO_RESULT, or ERROR is not evidence of business-fault detection. These controls are synthetic regression checks, not measured real-model recall or accuracy.

## Exact results and verdicts

VALUE must use the contract's exact normalized decimal string, unit, and complete bindings. Sum/ratio round only at the final step to the declared 0–12 places using HALF_UP. Text equality is exact and case-sensitive, without trimming or fuzzy matching; blank filter cells do not match. Count means selected rows, with unit `count`. Ratios use unit `ratio`: `0.5000` stays `0.5000` in JSON even if the report also displays `50%`.

ABSTAIN requires `value: null` and the exact expected refusal reason. It must match an intentional, validated boundary. Missing values cannot become zero or a partial sum; a validated empty selection may produce zero. Formula evaluation, unit conversion, joins, and arbitrary expressions are outside v1.

| Case verdict | Meaning |
| --- | --- |
| PASS | Required exact result and applicable binding checks match. |
| FAIL | Protocol-valid result is wrong, or valid JSON violates the result protocol. |
| NO_RESULT | A planned slot has no usable result, such as missing output, invalid JSON, timeout, nonzero exit, or output limit. |
| ERROR | Infrastructure, suite, input integrity, plan, or adapter-start error prevents valid evaluation. |

Nested `value_check` and `binding_check` have PASS, FAIL, or NOT_CHECKED. Legal expected ABSTAIN has binding NOT_CHECKED and can still receive case PASS. Family records may be NOT_CHECKED when the relationship cannot be established; this is not family PASS. ERROR invalidates the suite's score. Exit priority is ERROR/invalid report → 2; FAIL/NO_RESULT/incomplete required family → 1; complete required passes → 0.

## Attempts, evidence, and usage

An observation batch declares nonempty `runs`, each with a unique configuration label and 1–10 repeats. The grader constructs every `(config_id, case_id, attempt)` slot. `planned = case_count × sum(repeats)` and `attempted = planned` are the scoring denominator; attempted here means planned evaluation slots, not proven process starts. `observed_count` and `missing_count` are shown separately. Entire missing rounds/configurations remain NO_RESULT. Duplicate or out-of-plan slots are ERROR; no best-repeat selection occurs.

Raw stdout/stderr, hashes, execution status, usage, and runtime metadata remain evidence. The runner has no usage telemetry channel and records `usage: null`; tokens and cost are unknown, not zero. Saved batches may carry supplied usage, which is preserved without provider authentication. A configuration label does not authenticate model identity.

## Resource limits

These are supported input boundaries, not performance measurements.

| Resource | v1 limit |
| --- | --- |
| Case | One metric, at most two sources and four input files. |
| Input file | 10 MiB. |
| Selected table range | 1,000 data rows and 100 columns. |
| XLSX ZIP | 200 members; 16 MiB expanded per member; 64 MiB total expanded. |
| Suite | 100 cases; 128 MiB cumulative snapshots; 1,000,000 read cells, including headers. |
| Numeric input text | 80 characters; 30 coefficient digits after leading zeros; 30 fractional digits; explicit exponent −12 through 12. |
| Result value string | 128 characters. |
| Runner capture | 1 MiB stdout; 64 KiB stderr per invocation. |
| Runner time | 60 seconds per invocation by default; explicit positive integer override. |

Only finite numbers are parsed. Locale formats, thousands separators, and percentage literals are not inferred. Legal declared input limits can require ABSTAIN/UNSUPPORTED_INPUT_LIMIT; corrupt files, invalid cases, bad hashes, and suite-budget failures are ERROR. An oversized file without a complete hash-verified snapshot cannot be published as a clean frozen benchmark.

The toolkit reports synthetic contract regression results. It provides no live-provider benchmark, external adoption evidence, or general spreadsheet-agent capability estimate.
