# ML Feature Availability Contract Gate: validation and usage

The blueprint is [`../../flows/ml-feature-availability-contract-gate.yaml`](../../flows/ml-feature-availability-contract-gate.yaml).
Its `extend.description` contains the complete input contract, timestamp/null
semantics, plugin prerequisites, outputs, and integration instructions.

## Run the blueprint

Import the YAML into Kestra with the Python scripts plugin and Docker runner.
Run the defaults: four rows pass at the fixed evaluation time
`2026-01-02T12:00:00Z`. The same candidate and contract are in `fixtures/`.
No external credentials or Python libraries are needed by the flow.

Supply CSV text through `candidate_csv` and a JSON object through
`feature_contract`. For a current production batch, set `evaluation_time` to
an explicit RFC3339 time or an empty string to capture current UTC once.
The fixed demo time is deliberately not appropriate for live data. The schedule
ships disabled; wire real candidate inputs before enabling automation.

Blank a `customer_age` cell to exercise rejection. To isolate the joint coverage
rule, allow `max_null_fraction: 0.25` for both features and set
`min_joint_usable_fraction: 0.75`. Blank age on row 1 and transaction count on
row 2. Each feature passes individually, but joint coverage is 0.5 and rejects.

The validator uses standard-library streaming CSV processing. DuckDB would add
an unnecessary dependency for these row-wise checks. The Python scripts plugin
satisfies the non-core plugin requirement. `stage_candidate`,
`validate_availability`, and branch-specific `release_approval`/`reject_candidate`
perform useful work; `availability_gate` is the flow task.

## Run local checks

Use Python 3.11 or newer (the runtime image uses Python 3.13). From repository root,
install the test-only dependency into an environment of your choice, then run:

```text
python -m pip install -r tests/ml-feature-availability-contract-gate/requirements.txt
python -B tests/ml-feature-availability-contract-gate/test_gate.py
python -B tests/ml-feature-availability-contract-gate/check_catalog.py
git diff --check
```

The tests extract the actual Python script from the YAML and execute it in fresh
temporary directories. They verify audits, emitted verdicts, file checksums,
reproducibility, metadata, and branch structure. All temporary fixtures are
removed after each run. Base fixtures are checked against blueprint defaults;
negative cases are deterministic mutations of those fixtures in `test_gate.py`.
The test harness does not emulate Kestra, Pebble, or Docker.

Coverage includes missing feature/timestamp columns, independent thresholds,
nulls, staleness, joint coverage, malformed contracts and CSV, missing files,
empty datasets, invalid/future timestamps, UTC offsets, timestamp boundaries,
fraction boundaries, minimum rows, and byte-sensitive dataset hashes.

## Approval and downstream training

`outputs.validate_availability.outputFiles['audit.json']` is uploaded before the
intentional `Fail` task. On failure inspect this task artifact; flow-level outputs
may not be evaluated. Missing/uncomputed metrics in a partial report are not
passing results. Infrastructure failures before upload may leave no audit.

Only the successful `If` branch writes `outputs.release_approval.uri`. Its JSON
manifest binds the staged dataset URI/SHA-256, evaluation time, contract version
and serialized-contract SHA-256, execution ID, and audit URI. There is no shared
"latest approval" key that a rejected execution could leave behind.

The caller must invoke the gate as a waiting Subflow with failure propagation
(`wait: true`, `transmitFailed: true`) and start training only after success.
Read that execution's manifest, fetch its exact dataset URI, and verify the
checksum. Do not silently reuse a previous approval. A standalone scheduled
gate cannot stop an independently scheduled training job.

Approval covers the full dataset; tolerated unusable rows are not removed.
Trainers requiring complete cases must use strict thresholds or an explicit
downstream policy. This gate assesses batch readiness, not historical
point-in-time leakage or feature correctness. Delayed training should revalidate
freshness. CSV execution inputs are intended for bounded batches, and storage
retention must preserve the approved dataset and audit for the consumer.

## Required Kestra runtime checks before submission

- Import into a supported Kestra version and validate plugin schemas.
- Run defaults; validate both emitted JSON artifacts and their checksums.
- Run missing-column, null, stale, joint-coverage, malformed-contract, empty,
  and boundary fixtures through the engine, not just the Python subprocess.
- Confirm failed executions retain the audit and never execute `release_approval`.
- Check Pebble rendering of JSON, boolean outputs, FILE outputs, and `inputFiles`.
- In a parent flow verify a rejected waiting Subflow prevents training; verify
  success consumes the current manifest and exact dataset artifact.
- Exercise a worker/storage failure and retry; neither may reuse old approval.
- Confirm Docker image availability and disabled schedule behavior.

See `RESULTS.md` for the checks actually performed and outstanding limitations.

## Prepared end-to-end validation

See [RUNTIME.md](RUNTIME.md) for the minimal parent Subflow, temporary-instance
deployment steps, authentication, exact API execution commands, and evidence
checks. `run_runtime.py --prepare-only` prepares YAML without API calls;
`run_runtime.py --run` requires a real Kestra instance and verifies passing,
null-rejected, and joint-coverage-rejected parent/child executions. Local checks
in `test_runtime_preparation.py` are explicitly not Kestra runtime evidence.
