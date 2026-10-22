"""Run the actual embedded validator; no Kestra execution is simulated."""
import copy
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
FLOW_PATH = ROOT / "flows/ml-feature-availability-contract-gate.yaml"


class UniqueLoader(yaml.SafeLoader):
    pass


def mapping(loader, node, deep=False):
    pairs = loader.construct_pairs(node, deep=deep)
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate YAML key: {key}")
        result[key] = value
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
FLOW = yaml.load(FLOW_PATH.read_text(encoding="utf-8"), Loader=UniqueLoader)
SCRIPT = FLOW["tasks"][1]["script"]
CSV = (HERE / "fixtures/candidate.csv").read_text(encoding="utf-8")
CONTRACT = json.loads((HERE / "fixtures/contract.json").read_text(encoding="utf-8"))
TIME = "2026-01-02T12:00:00Z"


def edit_csv(changes=(), remove=None):
    rows = list(csv.reader(io.StringIO(CSV)))
    for row, column, value in changes:
        rows[row][rows[0].index(column)] = value
    if remove:
        index = rows[0].index(remove)
        for row in rows:
            row.pop(index)
    output = io.StringIO(newline="")
    csv.writer(output, lineterminator="\n").writerows(rows)
    return output.getvalue()


class GateTests(unittest.TestCase):
    def run_gate(self, candidate=CSV, contract=None, evaluation_time=TIME, raw_contract=None):
        contract = copy.deepcopy(CONTRACT) if contract is None else contract
        raw = raw_contract if raw_contract is not None else json.dumps(contract)
        with tempfile.TemporaryDirectory(prefix="feature-gate-") as directory:
            directory = Path(directory)
            (directory / "validator.py").write_text(SCRIPT, encoding="utf-8")
            if candidate is not None:
                (directory / "candidate.csv").write_bytes(candidate.encode("utf-8"))
            (directory / "contract.json").write_text(raw, encoding="utf-8")
            result = subprocess.run(
                [sys.executable, "-B", "validator.py"], cwd=directory,
                env={**os.environ, "EVALUATION_TIME": evaluation_time,
                     "DATASET_URI": "kestra:///test/execution/candidate.csv", "EXECUTION_ID": "test-execution"},
                capture_output=True, text=True, timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads((directory / "audit.json").read_text(encoding="utf-8"))
            emitted = json.loads(result.stdout.strip()[2:-2])["outputs"]
            self.assertIs(emitted["approved"], report["approved"])
            self.assertIs(report["approved"], not bool(report["failure_reasons"]))
            self.assertEqual(report["status"], "APPROVED" if report["approved"] else "REJECTED")
            self.assertEqual(list(directory.glob("*approval*")), [])
            invalid_time = any(r["code"] == "INVALID_EVALUATION_TIME" for r in report["failure_reasons"])
            if invalid_time:
                self.assertIsNone(report["evaluation_time"])
                self.assertIsNone(report["dataset"]["sha256"])
            elif candidate is not None:
                self.assertEqual(report["dataset"]["sha256"], hashlib.sha256(candidate.encode("utf-8")).hexdigest())
                self.assertEqual(report["contract_sha256"], hashlib.sha256(raw.encode("utf-8")).hexdigest())
            return report

    def assert_rejected(self, report, code):
        self.assertFalse(report["approved"])
        self.assertIn(code, [reason["code"] for reason in report["failure_reasons"]])

    def test_default_success_and_repeatability(self):
        first = self.run_gate()
        self.assertEqual(first, self.run_gate())
        self.assertTrue(first["approved"])
        self.assertEqual(first["row_count"], 4)
        self.assertEqual(first["joint_usable_fraction"], 1)
        self.assertTrue(first["evaluation_complete"])
        self.assertEqual(first["features"][1]["null_count"], 0)  # zero is not null

    def test_missing_feature(self):
        report = self.run_gate(edit_csv(remove="customer_age"))
        self.assert_rejected(report, "MISSING_COLUMNS")
        self.assertEqual(report["failure_reasons"][0]["columns"], ["customer_age"])
        self.assertFalse(report["evaluation_complete"])

    def test_missing_timestamp_column(self):
        self.assert_rejected(self.run_gate(edit_csv(remove="profile_updated_at")), "MISSING_COLUMNS")

    def test_excessive_nulls(self):
        report = self.run_gate(edit_csv([(1, "customer_age", "  ")]))
        self.assert_rejected(report, "EXCESSIVE_NULL")
        self.assertEqual(report["features"][0]["null_fraction"], 0.25)
        self.assertEqual(report["joint_usable_fraction"], 0.75)

    def test_stale_feature(self):
        report = self.run_gate(edit_csv([(1, "transactions_updated_at", "2025-12-01T00:00:00Z")]))
        self.assert_rejected(report, "EXCESSIVE_STALE")
        self.assertEqual(report["features"][1]["stale_fraction"], 0.25)

    def test_independent_freshness_budgets(self):
        candidate = edit_csv([(1, "profile_updated_at", "2025-12-31T12:00:00Z"),
                              (1, "transactions_updated_at", "2025-12-31T12:00:00Z")])
        report = self.run_gate(candidate)
        self.assertEqual(report["features"][0]["stale_count"], 0)
        self.assertEqual(report["features"][1]["stale_count"], 1)

    def test_disjoint_missing_values_fail_only_joint_gate(self):
        contract = copy.deepcopy(CONTRACT)
        for feature in contract["features"]:
            feature["max_null_fraction"] = 0.25
        contract["min_joint_usable_fraction"] = 0.75
        report = self.run_gate(edit_csv([(1, "customer_age", ""), (2, "transaction_count_30d", "")]), contract)
        self.assertEqual(report["joint_usable_fraction"], 0.5)
        self.assertEqual([r["code"] for r in report["failure_reasons"]], ["INSUFFICIENT_JOINT_COVERAGE"])

    def test_null_and_joint_threshold_equality(self):
        contract = copy.deepcopy(CONTRACT)
        contract["features"][0]["max_null_fraction"] = 0.25
        contract["min_joint_usable_fraction"] = 0.75
        self.assertTrue(self.run_gate(edit_csv([(1, "customer_age", "")]), contract)["approved"])

    def test_stale_fraction_threshold_equality(self):
        contract = copy.deepcopy(CONTRACT)
        contract["features"][1]["max_stale_fraction"] = 0.25
        contract["min_joint_usable_fraction"] = 0.75
        self.assertTrue(self.run_gate(edit_csv([(1, "transactions_updated_at", "2025-12-01T00:00:00Z")]), contract)["approved"])

    def test_age_boundary_and_one_microsecond_over(self):
        self.assertTrue(self.run_gate(edit_csv([(1, "transactions_updated_at", "2026-01-01T12:00:00Z")]))["approved"])
        self.assert_rejected(self.run_gate(edit_csv([(1, "transactions_updated_at", "2026-01-01T11:59:59.999999Z")])), "EXCESSIVE_STALE")

    def test_insufficient_rows(self):
        self.assert_rejected(self.run_gate("\n".join(CSV.splitlines()[:-1]) + "\n"), "INSUFFICIENT_ROWS")

    def test_empty_and_header_only(self):
        for candidate in ("", CSV.splitlines()[0] + "\n"):
            with self.subTest(candidate=candidate):
                self.assert_rejected(self.run_gate(candidate), "EMPTY_DATASET")

    def test_invalid_timestamps(self):
        for value in ("", "garbage", "2026-01-02", "2026-01-02T10:00:00", "2026-02-30T10:00:00Z",
                      "2026-01-02T10:00:00+25:00", "2026-01-02T10:00:00-00:00", "2026-01-02T10:00:60Z"):
            with self.subTest(value=value):
                self.assert_rejected(self.run_gate(edit_csv([(1, "transactions_updated_at", value)])), "INVALID_TIMESTAMP")

    def test_invalid_timestamp_on_null_cell_is_hard_failure(self):
        contract = copy.deepcopy(CONTRACT)
        contract["features"][0]["max_null_fraction"] = 1
        contract["min_joint_usable_fraction"] = 0
        self.assert_rejected(self.run_gate(edit_csv([(1, "customer_age", ""), (1, "profile_updated_at", "")]), contract), "INVALID_TIMESTAMP")

    def test_future_timestamp(self):
        self.assert_rejected(self.run_gate(edit_csv([(1, "transactions_updated_at", "2026-01-02T12:00:00.000001Z")])), "FUTURE_TIMESTAMP")

    def test_offset_equivalence(self):
        candidate = edit_csv([(1, "transactions_updated_at", "2026-01-02T16:30:00+05:30")])
        self.assertTrue(self.run_gate(candidate)["approved"])

    def test_invalid_evaluation_time(self):
        self.assert_rejected(self.run_gate(evaluation_time="yesterday"), "INVALID_EVALUATION_TIME")

    def test_independent_null_budgets(self):
        contract = copy.deepcopy(CONTRACT)
        contract["features"][0]["max_null_fraction"] = 0.25
        contract["min_joint_usable_fraction"] = 0.75
        report = self.run_gate(edit_csv([(1, "customer_age", ""), (1, "transaction_count_30d", "")]), contract)
        self.assertEqual([r["feature"] for r in report["failure_reasons"]], ["transaction_count_30d"])

    def test_fraction_just_outside_boundary(self):
        candidate = edit_csv([(1, "customer_age", "")])
        contract = copy.deepcopy(CONTRACT)
        contract["features"][0]["max_null_fraction"] = 0.249999
        contract["min_joint_usable_fraction"] = 0.75
        self.assert_rejected(self.run_gate(candidate, contract), "EXCESSIVE_NULL")
        contract["features"][0]["max_null_fraction"] = 0.25
        contract["min_joint_usable_fraction"] = 0.750001
        self.assert_rejected(self.run_gate(candidate, contract), "INSUFFICIENT_JOINT_COVERAGE")

    def test_missing_contract_field(self):
        contract = copy.deepcopy(CONTRACT)
        del contract["features"][0]["timestamp_field"]
        report = self.run_gate(contract=contract)
        self.assert_rejected(report, "INVALID_CONTRACT")
        self.assertEqual(report["evaluation_time"], "2026-01-02T12:00:00+00:00")

    def test_current_time_captured(self):
        report = self.run_gate(evaluation_time="")
        self.assertIsNotNone(report["evaluation_time"])
        self.assertTrue(report["evaluation_time"].endswith("+00:00"))

    def test_invalid_contracts(self):
        for key, value in (("version", 2), ("version", True), ("min_rows", 0), ("min_rows", 1.5),
                           ("min_rows", True), ("min_joint_usable_fraction", -0.1),
                           ("min_joint_usable_fraction", 1.1), ("features", []), ("extra", "typo")):
            contract = copy.deepcopy(CONTRACT)
            contract[key] = value
            with self.subTest(key=key, value=value):
                self.assert_rejected(self.run_gate(contract=contract), "INVALID_CONTRACT")

    def test_invalid_feature_contracts(self):
        for key, value in (("name", ""), ("name", " customer_age"), ("timestamp_field", "customer_age"),
                           ("max_age_hours", -1), ("max_age_hours", "24"), ("max_null_fraction", True),
                           ("max_stale_fraction", 1.01), ("extra", 1)):
            contract = copy.deepcopy(CONTRACT)
            contract["features"][0][key] = value
            with self.subTest(key=key, value=value):
                self.assert_rejected(self.run_gate(contract=contract), "INVALID_CONTRACT")

    def test_duplicate_features(self):
        contract = copy.deepcopy(CONTRACT)
        contract["features"].append(contract["features"][0])
        self.assert_rejected(self.run_gate(contract=contract), "INVALID_CONTRACT")

    def test_bad_json_and_shapes(self):
        for raw in ("{", "[]", "null", '{"version":1,"version":1}', '{"version":NaN}', '{"version":Infinity}'):
            with self.subTest(raw=raw):
                self.assert_rejected(self.run_gate(raw_contract=raw), "INVALID_CONTRACT")

    def test_malformed_csv(self):
        for candidate in (CSV + "broken,row\n", CSV + '\"unterminated', CSV.replace("customer_id", "customer_age", 1)):
            report = self.run_gate(candidate)
            self.assertFalse(report["approved"])
            self.assertFalse(report["evaluation_complete"])

    def test_missing_file_fails_closed_with_audit(self):
        self.assert_rejected(self.run_gate(candidate=None), "DATASET_READ_ERROR")

    def test_extra_columns_and_bom(self):
        self.assertTrue(self.run_gate("\ufeff" + CSV)["approved"])

    def test_overlap_null_and_stale_counts(self):
        report = self.run_gate(edit_csv([(1, "customer_age", ""), (1, "profile_updated_at", "2025-01-01T00:00:00Z")]))
        self.assertEqual(report["features"][0]["null_count"], 1)
        self.assertEqual(report["features"][0]["stale_count"], 1)
        self.assertEqual(report["joint_usable_rows"], 3)

    def test_dataset_hash_changes_with_bytes(self):
        self.assertNotEqual(self.run_gate()["dataset"]["sha256"], self.run_gate(CSV.replace("\n", "\r\n"))["dataset"]["sha256"])


class BlueprintTests(unittest.TestCase):
    def test_metadata_and_defaults(self):
        self.assertEqual(FLOW["id"], FLOW_PATH.stem)
        self.assertTrue(FLOW["namespace"])
        metadata = FLOW["extend"]
        self.assertTrue(set(("title", "metaTitle", "shortDescription", "description", "metaDescription", "tags", "ee", "demo")) <= set(metadata))
        self.assertLessEqual(len(metadata["metaDescription"]), 160)
        self.assertLessEqual(len(metadata["tags"]), 3)
        self.assertTrue(set(metadata["tags"]) <= {"Data", "Infrastructure", "Business", "Cloud", "Core", "AI", "Getting Started", "System"})
        inputs = {item["id"]: item["defaults"] for item in FLOW["inputs"]}
        self.assertEqual(inputs["candidate_csv"], CSV)
        self.assertEqual(inputs["feature_contract"], CONTRACT)
        self.assertEqual(inputs["evaluation_time"], TIME)

    def test_gate_structure_and_artifact_binding(self):
        # Structural checks only: these do not execute Pebble or the Kestra engine.
        stage, validator, gate = FLOW["tasks"]
        self.assertEqual(stage["id"], "stage_candidate")
        self.assertEqual(validator["inputFiles"]["candidate.csv"], "{{ outputs.stage_candidate.uri }}")
        self.assertEqual(validator["env"]["DATASET_URI"], "{{ outputs.stage_candidate.uri }}")
        self.assertEqual(validator["outputFiles"], ["audit.json"])
        self.assertEqual(gate["type"], "io.kestra.plugin.core.flow.If")
        self.assertEqual(gate["condition"], "{{ outputs.validate_availability.vars.approved == true }}")
        self.assertEqual([t["id"] for t in gate["then"]], ["release_approval"])
        self.assertEqual([t["type"] for t in gate["else"]], ["io.kestra.plugin.core.execution.Fail"])
        self.assertIn("audit.dataset | toJson", gate["then"][0]["content"])
        self.assertIn("audit.contract_sha256 | toJson", gate["then"][0]["content"])
        self.assertTrue(validator["type"].startswith("io.kestra.plugin.scripts.python."))
        self.assertTrue(FLOW["triggers"][0]["disabled"])
        self.assertEqual(FLOW["triggers"][0]["timezone"], "UTC")
        self.assertNotIn("allowFailure", validator)
        self.assertNotIn("allowFailure", gate)


if __name__ == "__main__":
    unittest.main(verbosity=2)
