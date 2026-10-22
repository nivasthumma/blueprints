# End-to-end Kestra validation

Status: prepared, not yet executed in Kestra. Local Python script checks are not
proof of Subflow behavior, Pebble rendering, Docker execution, or storage upload.
Nothing here installs Docker, Java, or Kestra on the host automatically.

## What runs

`runtime-parent.yaml` is a test flow, not an additional catalog blueprint:

1. `gate`: invokes the real blueprint using `wait: true`, `transmitFailed: true`.
2. `verify_approval`: reads the child flow's manifest and audit, downloads the
   referenced dataset, and checks SHA-256, dataset URI agreement, execution ID,
   audit URI, evaluation time, contract hash/version, and joint coverage.
3. `training_smoke`: trains a tiny constant-age baseline on those exact bytes,
   checks their checksum again, and writes `baseline-model.json`. This is a toy
   training step for the fixture, not a production model or general training API.

`run_runtime.py --run` deploys copies of both flows into a fresh generated
`company.ml.validation.r<uuid>` namespace. It removes only catalog `extend`
metadata from the gate and replaces its namespace. The validator, Docker runner,
branches, and outputs remain unchanged. It never uses namespace replacement or
deletion APIs and does not change the repository blueprint.

It then starts three independent parent executions:

| Case | Expected child/parent | Expected artifacts |
| --- | --- | --- |
| `success` | SUCCESS / SUCCESS | Audit, approval manifest, model; 4 rows and prediction 39.0 |
| `rejected_null` | FAILED / FAILED | Audit with EXCESSIVE_NULL; no approval task/output and no training task/output |
| `rejected_joint` | FAILED / FAILED | Audit with only INSUFFICIENT_JOINT_COVERAGE (0.5 < 0.75); no approval or training |

The driver inspects the failed child execution directly using an explicit parent
correlation label. It does not depend on successful extraction of a failed
Subflow's flow outputs. It downloads the audit after the execution has failed,
which is the persistence check. It verifies both task-run absence/skip state and
absence of approval/training outputs on rejection. With this flow, the only
manifest writer is the approval task. This is not an exhaustive storage listing
or a storage atomicity test during infrastructure failure.

## 1. Prepare offline (no API calls)

From repository root with Python 3.11+ and the test-only PyYAML dependency:

```powershell
python -m pip install -r tests/ml-feature-availability-contract-gate/requirements.txt
python -B tests/ml-feature-availability-contract-gate/test_gate.py
python -B tests/ml-feature-availability-contract-gate/test_runtime_preparation.py
python -B tests/ml-feature-availability-contract-gate/run_runtime.py --prepare-only
```

Skip installation when PyYAML is already available. In this workspace its existing
temporary installation is usable by setting:

```powershell
$env:PYTHONPATH = Join-Path $env:TEMP 'blueprints-feature-gate-test-deps'
```

The preparation command prints a temporary directory containing `gate.yaml`,
`parent.yaml`, and `summary.json` with `runtime_verified: false`. It does not
deploy anything. `--evidence-dir <new-or-empty-directory>` selects a different
location. Each actual run prepares its own unique namespace and evidence folder.

## 2. Start a temporary instance, only on a host with Docker already installed

These commands have NOT been executed in this workspace. Docker, Java, or large
image/dependency installation requires separate approval here. Use Linux or WSL
with access to an existing Docker daemon, as recommended by Kestra's Docker guide.

The following Bash commands start an isolated container on localhost:8080. They
use the documented full `latest` image so the Python plugin is bundled. Capture
the resolved image digest; the rolling tag alone is not reproducible evidence.

```bash
docker version
docker pull kestra/kestra:latest
docker pull python:3.13-slim
docker image inspect kestra/kestra:latest --format '{{json .RepoDigests}}'
docker image inspect python:3.13-slim --format '{{json .RepoDigests}}'
docker run --detach --rm --name kestra-feature-gate-test --user=root \
  --publish 127.0.0.1:8080:8080 \
  --volume /var/run/docker.sock:/var/run/docker.sock \
  --volume /tmp:/tmp \
  kestra/kestra:latest server local
docker logs --tail 100 kestra-feature-gate-test
```

The Docker socket enables the real Python Docker tasks; the shared `/tmp` path
is needed for their working directories. Use a disposable test host. No named
database/storage volumes are created, so stopping this `--rm` container removes
its embedded database and execution storage; export evidence first.

Open `http://localhost:8080` and complete the local instance's account setup if
prompted. Wait until the UI is ready. Keep default authentication; do not embed
credentials in YAML. If your existing instance uses another address, use
`--base-url` below. Tenant defaults to `main`; override with `--tenant` when needed.

For Basic Auth, set credentials only in the terminal running the driver. Bash:

```bash
read -r -p 'Kestra username/email: ' KESTRA_USERNAME
read -r -s -p 'Kestra password: ' KESTRA_PASSWORD
printf '\n'
export KESTRA_USERNAME KESTRA_PASSWORD
```

PowerShell alternative:

```powershell
$gateCredential = Get-Credential -Message 'Temporary Kestra account'
$env:KESTRA_USERNAME = $gateCredential.UserName
$env:KESTRA_PASSWORD = $gateCredential.GetNetworkCredential().Password
```

For an instance accepting API tokens, set `KESTRA_API_TOKEN` instead. The driver
prefers that token when present. Authentication values are not written to evidence.

## 3. Deploy and execute through the API

Run from repository root, with network access to the temporary instance:

```text
python -B tests/ml-feature-availability-contract-gate/run_runtime.py --run --base-url http://127.0.0.1:8080 --tenant main --timeout 600
```

The driver uses these supported routes:

| Method | Route below `/api/v1/{tenant}` | Purpose |
| --- | --- | --- |
| POST | `/flows/validate` | Validate runtime YAML; retain response including errors/warnings |
| POST | `/flows` | Create each individual flow in its new namespace |
| POST | `/executions/{namespace}/{flowId}` | Start parent using multipart input fields |
| GET | `/executions/{executionId}` | Poll/read parent and child execution records |
| GET | `/executions?namespace=...&flowId=...&page=1&size=100` | Find the correlated child, including after failure |
| POST | `/executions/{executionId}/actions/eval` | Read task outputs using Kestra's own expression evaluator |
| GET | `/executions/{executionId}/file?path=<encoded-kestra-uri>` | Download exact persisted artifact bytes |

No heavy SDK is used: API requests use Python's standard library. Flow YAML
serialization uses the existing PyYAML test dependency. The driver fails on
validation errors, unexpected API responses, timeouts, unexpected execution
states, missing evidence, or artifact mismatch. Review warnings in the saved
validation responses even when executions pass. A failed or blocked run exits 1
and leaves `runtime_verified: false`; only all three real cases passing yields
`status: PASSED`, `runtime_verified: true`, exit 0.

Inspect the evidence folder printed by the driver:

- Serialized deployed flows and flow-validation API responses.
- `started.json`, parent/child execution JSON, and downloaded audit/dataset for each case.
- Success-only approval manifest and baseline model.
- `summary.json`: actual execution IDs, case verdicts, checksums, and audit URIs.

Save the image digests printed above and the Kestra/plugin versions with this
evidence. No fixed runtime version is claimed until this is executed. The namespace
and execution IDs in evidence can be opened in the UI for corroboration.

## 4. Clean up the temporary instance after exporting evidence

```bash
docker stop kestra-feature-gate-test
unset KESTRA_USERNAME KESTRA_PASSWORD KESTRA_API_TOKEN
```

PowerShell credential cleanup, if used:

```powershell
Remove-Item Env:KESTRA_USERNAME, Env:KESTRA_PASSWORD, Env:KESTRA_API_TOKEN -ErrorAction SilentlyContinue
Remove-Variable gateCredential -ErrorAction SilentlyContinue
```

The driver does not delete executions or flows on an existing shared instance.
For that environment, retain the unique namespace from the summary and use your
normal cleanup process. Do not run the container stop command against a shared
instance.

## Evidence limits

`test_runtime_preparation.py` uses constructed local manifest/audit fixtures to
check the embedded verifier and toy trainer, including byte tampering. Those are
local script tests, not simulated successful Kestra executions. `--prepare-only`
performs no execution at all. Real Subflow propagation, artifact availability after
failure, output expressions, and manifest generation remain unverified until
`--run` completes against Kestra. Cancellation, worker/storage failures, retries,
and restart behavior need additional tests beyond these three cases.

## References inspected

- Repository: `flows/pass-data-between-subflows.yaml`, `flows/parallel-subflows.yaml`,
  `flows/carbon-aware-batch-job-scheduler.yaml`.
- [Subflow implementation and output contract](https://github.com/kestra-io/kestra/blob/develop/core/src/main/java/io/kestra/plugin/core/flow/Subflow.java)
- [Flow validation/create API](https://github.com/kestra-io/kestra/blob/develop/webserver/src/main/java/io/kestra/webserver/controllers/api/FlowController.java)
- [Execution/evaluation/file API](https://github.com/kestra-io/kestra/blob/develop/webserver/src/main/java/io/kestra/webserver/controllers/api/ExecutionController.java)
- [Official Docker installation](https://kestra.io/docs/installation/docker)
