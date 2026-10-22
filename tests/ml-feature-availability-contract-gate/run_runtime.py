"""Prepare flows offline or validate them through a real Kestra REST API.

No simulated execution mode. --run creates two flows in a unique namespace and
starts three parent executions. It never overwrites/deletes existing flows.
"""
import argparse
import base64
import copy
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
GATE_ID = "ml-feature-availability-contract-gate"
PARENT_ID = GATE_ID + "-parent-test"
TERMINAL = {"SUCCESS", "FAILED", "KILLED", "CANCELLED", "WARNING"}


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, allow_nan=False), encoding="utf-8")


def prepare(directory, namespace):
    """Change only catalog metadata and namespace in the deployed gate copy."""
    gate = yaml.safe_load((ROOT / "flows" / (GATE_ID + ".yaml")).read_text(encoding="utf-8"))
    parent = yaml.safe_load((HERE / "runtime-parent.yaml").read_text(encoding="utf-8"))
    gate.pop("extend")  # Catalog metadata is not a runtime flow property.
    for filename, flow in (("gate.yaml", gate), ("parent.yaml", parent)):
        flow["namespace"] = namespace
        (directory / filename).write_text(yaml.safe_dump(flow, sort_keys=False), encoding="utf-8")
    return gate, parent


def scenarios():
    candidate = (HERE / "fixtures/candidate.csv").read_text(encoding="utf-8")
    contract = json.loads((HERE / "fixtures/contract.json").read_text(encoding="utf-8"))
    rows = list(csv.reader(io.StringIO(candidate)))
    rows[1][1] = ""  # Required customer_age missing on the first data row.
    output = io.StringIO(newline="")
    csv.writer(output, lineterminator="\n").writerows(rows)
    rejected_csv = output.getvalue()
    joint_contract = copy.deepcopy(contract)
    for feature in joint_contract["features"]:
        feature["max_null_fraction"] = 0.25
    joint_contract["min_joint_usable_fraction"] = 0.75
    rows[2][3] = ""  # Disjoint nulls: 75% each, but 50% jointly usable.
    output = io.StringIO(newline="")
    csv.writer(output, lineterminator="\n").writerows(rows)
    return [
        ("success", candidate, contract, None),
        ("rejected_null", rejected_csv, contract, "EXCESSIVE_NULL"),
        ("rejected_joint", output.getvalue(), joint_contract, "INSUFFICIENT_JOINT_COVERAGE"),
    ]


def multipart(fields):
    boundary = "kestra-feature-gate-" + uuid.uuid4().hex
    body = bytearray()
    for key, value in fields.items():
        body.extend((f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n'
                     'Content-Type: text/plain; charset=utf-8\r\n\r\n').encode())
        body.extend(value.encode("utf-8"))
        body.extend(b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode())
    return bytes(body), "multipart/form-data; boundary=" + boundary


class Api:
    def __init__(self, base_url, tenant):
        parsed = urllib.parse.urlsplit(base_url)
        require(parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username
                and not parsed.password and not parsed.query and not parsed.fragment,
                "Use an HTTP(S) base URL without embedded credentials, query, or fragment")
        self.root = base_url.rstrip("/") + "/api/v1/" + urllib.parse.quote(tenant, safe="")
        self.headers = {}
        token = os.environ.get("KESTRA_API_TOKEN")
        username, password = os.environ.get("KESTRA_USERNAME"), os.environ.get("KESTRA_PASSWORD")
        if token:
            self.headers["Authorization"] = "Bearer " + token
        elif username and password:
            encoded = base64.b64encode((username + ":" + password).encode()).decode()
            self.headers["Authorization"] = "Basic " + encoded
        elif username or password:
            raise ValueError("Set both KESTRA_USERNAME and KESTRA_PASSWORD")

    def request(self, method, path, data=None, content_type=None, raw=False):
        headers = {**self.headers, "Accept": "application/octet-stream" if raw else "application/json"}
        if content_type:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(self.root + path, data=data, headers=headers, method=method)
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read()
        return body if raw else json.loads(body) if body else None

    def evaluate(self, execution_id, expression):
        response = self.request("POST", f"/executions/{execution_id}/actions/eval",
                                expression.encode(), "text/plain")
        require(not response.get("error"), "Kestra expression evaluation failed: " + str(response.get("error")))
        return json.loads(response["result"])

    def output(self, execution_id, reference):
        return self.evaluate(execution_id, "{{ " + reference + " | toJson }}")

    def download(self, execution_id, uri):
        require(isinstance(uri, str) and uri.startswith("kestra:"), "Expected Kestra internal-storage URI")
        query = urllib.parse.urlencode({"path": uri})
        return self.request("GET", f"/executions/{execution_id}/file?{query}", raw=True)

    def wait(self, execution_id, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            execution = self.request("GET", "/executions/" + execution_id)
            state = execution["state"]["current"]
            if state in TERMINAL:
                return execution
            if state in {"PAUSED", "BREAKPOINT"}:
                raise RuntimeError("Execution paused unexpectedly: " + execution_id)
            time.sleep(2)
        raise TimeoutError("Execution did not finish: " + execution_id)


def task_succeeded(execution, task_id):
    runs = [r for r in execution.get("taskRunList", []) if r["taskId"] == task_id]
    return bool(runs) and all(r["state"]["current"] == "SUCCESS" for r in runs)


def task_not_executed(execution, task_id):
    return all(r["state"]["current"] == "SKIPPED"
               for r in execution.get("taskRunList", []) if r["taskId"] == task_id)


def find_child(api, namespace, parent_id):
    # Failed Subflow output extraction may omit executionId. Inspect execution
    # records and the explicit correlation label, not failed flow-level outputs.
    query = urllib.parse.urlencode({"namespace": namespace, "flowId": GATE_ID, "page": 1, "size": 100})
    matches = []
    for item in api.request("GET", "/executions?" + query)["results"]:
        child = api.request("GET", "/executions/" + item["id"])
        labels = child.get("labels") or []
        if isinstance(labels, list):
            labels = {label["key"]: label["value"] for label in labels}
        if labels.get("parent_test_execution") == parent_id:
            matches.append(child)
    require(len(matches) == 1, f"Expected one correlated child for {parent_id}, found {len(matches)}")
    return matches[0]


def verify_case(api, directory, parent, child, candidate, failure_code):
    parent_id, child_id = parent["id"], child["id"]
    write_json(directory / "parent-execution.json", parent)
    write_json(directory / "child-execution.json", child)
    expected_state = "FAILED" if failure_code else "SUCCESS"
    require(parent["state"]["current"] == expected_state, "Unexpected parent execution state")
    require(child["state"]["current"] == expected_state, "Unexpected child execution state")
    require(task_succeeded(child, "validate_availability"), "Validator did not finish/upload its report")
    audit_uri = api.output(child_id, "outputs.validate_availability.outputFiles['audit.json']")
    raw_audit = api.download(child_id, audit_uri)
    (directory / "audit.json").write_bytes(raw_audit)
    audit = json.loads(raw_audit)
    dataset_uri = api.output(child_id, "outputs.stage_candidate.uri")
    dataset = api.download(child_id, dataset_uri)
    (directory / "validated-dataset.csv").write_bytes(dataset)
    digest = hashlib.sha256(dataset).hexdigest()
    require(dataset == candidate.encode("utf-8"), "Staged bytes differ from submitted CSV")
    require(audit["dataset"] == {"uri": dataset_uri, "sha256": digest}, "Audit dataset binding mismatch")
    require(audit["execution_id"] == child_id, "Audit belongs to a different execution")
    require(audit["evaluation_complete"] is True, "Unexpected partial audit")
    if failure_code:
        require(audit["approved"] is False and audit["status"] == "REJECTED", "Rejection audit invalid")
        require(failure_code in {r["code"] for r in audit["failure_reasons"]}, "Expected failure reason missing")
        require(task_not_executed(child, "release_approval"), "Rejected child executed approval task")
        require(task_not_executed(parent, "verify_approval"), "Rejected parent executed verification")
        require(task_not_executed(parent, "training_smoke"), "Rejected parent executed training")
        require(api.evaluate(child_id, "{{ outputs.release_approval is defined }}") is False,
                "Rejected child exposes an approval output")
        require(api.evaluate(parent_id, "{{ outputs.training_smoke is defined }}") is False,
                "Rejected parent exposes a training output")
        if failure_code == "INSUFFICIENT_JOINT_COVERAGE":
            require(audit["joint_usable_fraction"] == 0.5, "Unexpected joint coverage")
            require({r["code"] for r in audit["failure_reasons"]} == {failure_code},
                    "Joint fixture should pass independent feature budgets")
    else:
        require(audit["approved"] is True and not audit["failure_reasons"], "Passing audit invalid")
        require(task_succeeded(child, "release_approval"), "Approval was not published")
        require(task_succeeded(parent, "verify_approval"), "Parent did not verify the manifest")
        require(task_succeeded(parent, "training_smoke"), "Downstream training did not execute")
        manifest_uri = api.output(child_id, "outputs.release_approval.uri")
        raw_manifest = api.download(child_id, manifest_uri)
        (directory / "approval-manifest.json").write_bytes(raw_manifest)
        manifest = json.loads(raw_manifest)
        require(manifest["approved"] is True, "Manifest not approved")
        require(manifest["dataset"] == audit["dataset"], "Manifest dataset mismatch")
        require(manifest["audit_uri"] == audit_uri and manifest["execution_id"] == child_id, "Manifest lineage mismatch")
        require(manifest["contract_sha256"] == audit["contract_sha256"], "Contract hash mismatch")
        require(manifest["evaluation_time"] == audit["evaluation_time"], "Evaluation time mismatch")
        parent_manifest = api.output(parent_id, "outputs.gate.outputs.approval_manifest")
        require(parent_manifest == manifest_uri, "Subflow returned a different manifest")
        model_uri = api.output(parent_id, "outputs.training_smoke.outputFiles['baseline-model.json']")
        raw_model = api.download(parent_id, model_uri)
        (directory / "baseline-model.json").write_bytes(raw_model)
        model = json.loads(raw_model)
        require(model["dataset_sha256"] == digest and model["gate_execution_id"] == child_id,
                "Training consumed another dataset/execution")
        require(model["training_rows"] == 4 and model["prediction"] == 39.0, "Unexpected toy model")
    return {"parent_id": parent_id, "child_id": child_id, "state": expected_state,
            "dataset_sha256": digest, "audit_uri": audit_uri, "checks_passed": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--prepare-only", action="store_true", help="Write deployment YAML without API calls")
    mode.add_argument("--run", action="store_true", help="Deploy and run tests on the specified real instance")
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--tenant", default="main")
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--evidence-dir", type=Path, help="New or empty directory; defaults to OS temporary directory")
    args = parser.parse_args()
    directory = args.evidence_dir or Path(tempfile.mkdtemp(prefix="kestra-feature-gate-"))
    directory.mkdir(parents=True, exist_ok=True)
    require(not any(directory.iterdir()), "Evidence directory must be empty; previous evidence is never overwritten")
    namespace = "company.ml.validation.r" + uuid.uuid4().hex
    gate, parent = prepare(directory, namespace)
    summary = {"mode": "REAL_KESTRA_API" if args.run else "PREPARE_ONLY",
               "namespace": namespace, "runtime_verified": False, "cases": [], "status": "PREPARED"}
    write_json(directory / "summary.json", summary)
    print("Evidence:", directory.resolve(), flush=True)
    print("Namespace:", namespace, flush=True)
    if args.prepare_only:
        print("Prepared only. No API calls or Kestra executions performed.")
        return 0
    try:
        api = Api(args.base_url, args.tenant)
        for filename, flow in (("gate.yaml", gate), ("parent.yaml", parent)):
            source = (directory / filename).read_bytes()
            violations = api.request("POST", "/flows/validate", source, "application/x-yaml")
            write_json(directory / (filename + ".validation.json"), violations)
            require(isinstance(violations, list), "Unexpected flow validation API response")
            require(not any(v.get("errors") for v in violations), "Kestra flow validation errors; inspect evidence")
            # POST creates one flow; never use bulk namespace replacement APIs.
            created = api.request("POST", "/flows", source, "application/x-yaml")
            require(created["id"] == flow["id"] and created["namespace"] == namespace, "Unexpected deployment result")
        for name, candidate, contract, failure_code in scenarios():
            case_dir = directory / name
            case_dir.mkdir()
            fields = {"candidate_csv": candidate, "feature_contract": json.dumps(contract),
                      "evaluation_time": "2026-01-02T12:00:00Z"}
            payload, content_type = multipart(fields)
            started = api.request("POST", f"/executions/{namespace}/{PARENT_ID}", payload, content_type)
            write_json(case_dir / "started.json", started)
            print(name + ": parent " + started["id"], flush=True)
            execution = api.wait(started["id"], args.timeout)
            write_json(case_dir / "parent-execution.json", execution)
            child = find_child(api, namespace, execution["id"])
            result = verify_case(api, case_dir, execution, child, candidate, failure_code)
            summary["cases"].append({"name": name, **result})
            write_json(directory / "summary.json", summary)
            print(name + ": verified against real Kestra execution", flush=True)
        summary.update(status="PASSED", runtime_verified=True)
        return 0
    except Exception as error:
        summary.update(status="FAILED_OR_BLOCKED", error_type=type(error).__name__, error=str(error))
        print("Runtime validation FAILED OR BLOCKED:", error, file=sys.stderr)
        return 1
    finally:
        write_json(directory / "summary.json", summary)


if __name__ == "__main__":
    sys.exit(main())
