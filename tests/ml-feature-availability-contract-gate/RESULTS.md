# Validation results

Date: 2026-10-14 (Asia/Calcutta).
Branch: `feat/ml-feature-availability-contract-gate`.
Environment: Windows, Python 3.13.1, PyYAML 6.0.3.

## Executed

| Check | Result |
| --- | --- |
| Embedded-validator and blueprint tests | 32 tests passed in 12.571 seconds; includes parameterized negative cases |
| All flow YAML parsing and allowed-tag checks | 1,411 files, zero failures |
| New blueprint duplicate YAML keys | None; strict loader passed |
| Metadata/default fixtures and gate structure | Passed in the 32-test suite |
| Git whitespace check | `git diff --check` passed |

The test suite runs the Python script extracted from the blueprint in subprocesses.
It does not substitute a separate validator implementation. The default audit
has status APPROVED, 4 rows, 4 jointly usable rows, fraction 1.0, no failures,
and UTC evaluation time `2026-01-02T12:00:00+00:00`. Repeated runs produce identical
reports. The disjoint-null fixture passes both column null budgets but rejects
with joint coverage 0.5 against a 0.75 minimum. Exact age/fraction boundaries pass.

PyYAML was installed into a temporary directory outside the repository. The
initial sandbox download was network-blocked; the approved installation and
test executions succeeded. No runtime dependencies were added to the blueprint.

## Repository compliance review

- Correct hyphenated filename/ID and namespace.
- Useful staging, validation, conditional approval/rejection tasks; no padding logs.
- `If` flow task, disabled UTC schedule, non-core Python scripts plugin.
- All required `extend` fields; metaDescription within 160 characters.
- Allowed `AI` and `Data` tags; explicit `ee: false`, `demo: true`.
- Deterministic, self-contained demo; configurable evaluation time.
- Input types/defaults/constraints, prerequisites, outputs, failure behavior,
  usage examples, limits, and plugin documentation links included.
- Audit output is declared on the validator before the intentional failure task.
- Approval writer exists only in the positive branch and references the staged
  dataset and checksum. No shared/latest manifest or external publication.
- No claim of historical point-in-time leakage detection.
- Narrow `.gitignore` exception makes test scripts visible despite the repository's
  existing `*.py` rule.

## Unverified and submission readiness

Local implementation checks pass. Runtime validation is still required before
claiming end-to-end submission readiness. Docker and the Kestra CLI were not
installed, no Kestra environment configuration was present, and no connection
was established to localhost:8080. No Kestra execution was performed.

Outstanding: plugin-schema validation, container launch, Pebble rendering,
storage transfer/upload, audit availability after failed executions, runtime
absence of approval on rejection, FILE output resolution, and parent Subflow
failure propagation. The runtime checklist is in README.md.

This version accepts bounded CSV text inputs, treats only blank/whitespace
feature cells as null, and does not validate feature data types or historical
leakage. Approval retains tolerated unusable rows. Infrastructure failure before
audit upload can leave no report; failures cannot intentionally take the approval
branch. Freshness approval applies at the recorded time and must be rechecked
when training is delayed. Training integration must consume the current
execution's approval and verify its dataset checksum.

No commit, push, or pull request was created.

## Runtime preparation follow-up (2026-10-14)

Corrected the blueprint's Python plugin link to `plugin-script-python`.
Added `runtime-parent.yaml`, the real-API driver `run_runtime.py`, local
preparation/script tests, and exact deployment/execution instructions in
[RUNTIME.md](RUNTIME.md).

Commands executed from repository root using the existing temporary dependency:

```powershell
$env:PYTHONPATH = Join-Path $env:TEMP 'blueprints-feature-gate-test-deps'
python -B tests/ml-feature-availability-contract-gate/test_gate.py
python -B tests/ml-feature-availability-contract-gate/test_runtime_preparation.py
python -B tests/ml-feature-availability-contract-gate/run_runtime.py --prepare-only
python -B tests/ml-feature-availability-contract-gate/check_catalog.py
git diff --check
```

| Check | Actual result |
| --- | --- |
| Existing suite after URL correction | 32 tests passed in 10.928 seconds |
| New preparation and parent-script suite | 9 tests passed in 3.455 seconds |
| Offline deployment preparation | Exit 0; generated gate.yaml, parent.yaml, summary.json; no API calls |
| Full catalog check | 1,411 YAML files, zero failures |
| Tracked/untracked whitespace checks and Python syntax parsing | Passed |
| Real Kestra executions | Zero; not run |

Offline evidence was written outside the repository to
`C:\Users\Admin\AppData\Local\Temp\kestra-feature-gate-7v5mpd7l` with namespace
`company.ml.validation.rd454ecae857640d5b4a4cc523d03318d`. Its summary is PREPARED
with `runtime_verified: false`; this is not a successful runtime test result.

The new local tests use constructed manifest/audit artifacts to run the embedded
verifier and toy trainer. They confirm rejection of tampered bytes and a local
baseline prediction of 39.0 over four rows, but do not simulate or verify Kestra
execution states, storage, Pebble, or Subflow propagation.

Docker, Podman, Kestra CLI, and Java remain unavailable. Read-only HTTP probes of
`http://127.0.0.1:8080/api/v1/configs` and port 8081 both returned connection refused
(WinError 10061). No Docker/Java/images/large dependencies were installed.
The prepared real-API suite must still be run on a provisioned instance; it will
record evidence for success, null rejection, and disjoint joint-coverage rejection.
Submission remains blocked on that real execution evidence.
