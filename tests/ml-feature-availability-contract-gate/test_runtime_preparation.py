"""Local preparation/script tests only. These are NOT Kestra runtime tests."""
import copy
from email.parser import BytesParser
from email.policy import default
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml

import run_runtime as runtime
import test_gate as gate_tests

PARENT = yaml.safe_load((runtime.HERE / "runtime-parent.yaml").read_text(encoding="utf-8"))


class RuntimePreparationTests(unittest.TestCase):
    def test_deployment_copy_preserves_gate_behavior(self):
        with tempfile.TemporaryDirectory() as directory:
            gate, parent = runtime.prepare(Path(directory), "company.ml.validation.test")
            expected = copy.deepcopy(gate_tests.FLOW)
            del expected["extend"]
            expected["namespace"] = "company.ml.validation.test"
            self.assertEqual(gate, expected)
            self.assertEqual(parent["namespace"], gate["namespace"])
            for filename in ("gate.yaml", "parent.yaml"):
                self.assertIsInstance(yaml.safe_load((Path(directory) / filename).read_text(encoding="utf-8")), dict)

    def test_parent_waits_and_propagates_failure(self):
        gate, verify, training = PARENT["tasks"]
        self.assertEqual([t["id"] for t in PARENT["tasks"]], ["gate", "verify_approval", "training_smoke"])
        self.assertTrue(gate["wait"])
        self.assertTrue(gate["transmitFailed"])
        self.assertEqual(gate["flowId"], runtime.GATE_ID)
        self.assertEqual(gate["labels"]["parent_test_execution"], "{{ execution.id }}")
        self.assertNotIn("allowFailure", gate)
        self.assertNotIn("errors", PARENT)
        self.assertNotIn("finally", PARENT)
        for task in (verify, training):
            compile(task["script"], task["id"], "exec")

    def test_multipart_keeps_csv_and_json_intact(self):
        fields = {"candidate_csv": 'name,value\n"quoted, cell",café\n',
                  "feature_contract": json.dumps({"version": 1, "features": []})}
        payload, content_type = runtime.multipart(fields)
        message = BytesParser(policy=default).parsebytes(
            ("Content-Type: " + content_type + "\r\nMIME-Version: 1.0\r\n\r\n").encode() + payload)
        actual = {part.get_param("name", header="content-disposition"):
                  part.get_payload(decode=True).decode("utf-8") for part in message.iter_parts()}
        self.assertEqual(actual, fields)

    def test_runtime_scenarios_have_expected_local_verdicts(self):
        validator = gate_tests.GateTests()
        for name, candidate, contract, failure_code in runtime.scenarios():
            with self.subTest(name=name):
                report = validator.run_gate(candidate, contract)
                self.assertEqual(report["approved"], failure_code is None)
                if failure_code:
                    self.assertIn(failure_code, {r["code"] for r in report["failure_reasons"]})
                if name == "rejected_joint":
                    self.assertEqual([r["code"] for r in report["failure_reasons"]], [failure_code])

    def execute_parent_script(self, task_index, tamper=False):
        # Constructed local artifacts exercise only Python assertions/training.
        # No Pebble rendering, storage API, Subflow, or execution state is simulated.
        audit = gate_tests.GateTests().run_gate()
        manifest = {"approved": True, "execution_id": audit["execution_id"],
                    "dataset": audit["dataset"], "audit_uri": "kestra:///test/audit.json",
                    "contract_sha256": audit["contract_sha256"], "contract_version": 1,
                    "evaluation_time": audit["evaluation_time"], "joint_usable_fraction": 1.0}
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            runtime.write_json(directory / "manifest.json", manifest)
            runtime.write_json(directory / "audit.json", audit)
            raw = gate_tests.CSV + ("\n" if tamper else "")
            (directory / "candidate.csv").write_bytes(raw.encode())
            (directory / "script.py").write_text(PARENT["tasks"][task_index]["script"], encoding="utf-8")
            result = subprocess.run([sys.executable, "-B", "script.py"], cwd=directory,
                                    env={**os.environ, "CHILD_EXECUTION_ID": audit["execution_id"],
                                         "AUDIT_URI": manifest["audit_uri"],
                                         "EXPECTED_SHA256": audit["dataset"]["sha256"]},
                                    capture_output=True, text=True, timeout=20)
            model = directory / "baseline-model.json"
            return result, json.loads(model.read_text(encoding="utf-8")) if model.exists() else None

    def test_parent_verifier_with_local_artifacts(self):
        result, _ = self.execute_parent_script(1)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_parent_verifier_rejects_tampered_bytes_locally(self):
        result, _ = self.execute_parent_script(1, tamper=True)
        self.assertNotEqual(result.returncode, 0)

    def test_toy_training_script_locally(self):
        result, model = self.execute_parent_script(2)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(model["prediction"], 39.0)
        self.assertEqual(model["training_rows"], 4)

    def test_toy_training_rechecks_bytes_locally(self):
        result, model = self.execute_parent_script(2, tamper=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNone(model)

    def test_created_or_failed_task_is_not_counted_as_unexecuted(self):
        self.assertTrue(runtime.task_not_executed({"taskRunList": []}, "training_smoke"))
        for state in ("CREATED", "RUNNING", "SUCCESS", "FAILED"):
            self.assertFalse(runtime.task_not_executed({"taskRunList": [
                {"taskId": "training_smoke", "state": {"current": state}}]}, "training_smoke"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
