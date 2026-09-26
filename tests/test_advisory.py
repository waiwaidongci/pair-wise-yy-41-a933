import tempfile
import unittest
from pathlib import Path

from src.domain import ConflictError, PermissionDenied, ValidationError
from src.repository import Repository
from src.service import Service


class AdvisoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.item = self.service.create_item(
            {"title": "某跨江大桥", "description": "限行建议台测试", "severity": "warning",
             "quantity": 5, "threshold": 10, "external_ref": "BR-1"},
            "creator", "sensor_operator")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _reading(self, payload, actor="operator", role="sensor_operator"):
        return self.service.register_reading(self.item["id"], payload, actor, role)

    def test_highest_risk_triggers_and_archive(self):
        r1 = self._reading({"kind": "sensor_peak", "sensor": "应变计",
                            "value": 11, "threshold": 10})
        self.assertEqual(r1["advisory"]["level"], "observe")
        r2 = self._reading({"kind": "inspection_defect", "grade": "serious",
                            "location": "2号墩"}, "engineer", "bridge_engineer")
        adv = r2["advisory"]
        self.assertEqual(adv["level"], "restrict")
        self.assertEqual(adv["version"], 2)
        self.assertEqual(len(adv["triggers"]), 1)
        self.assertEqual(adv["triggers"][0]["kind"], "inspection_defect")
        r3 = self._reading({"kind": "vehicle_load", "vehicle_type": "六轴货车",
                            "axle_load": 80, "limit": 49})
        self.assertEqual(r3["advisory"]["level"], "closure")
        history = self.service.list_advisories(self.item["id"], "viewer")
        self.assertEqual(len(history), 3)
        self.assertEqual(history[0]["status"], "signing")
        self.assertTrue(all(v["status"] == "superseded" for v in history[1:]))
        self.assertTrue(self.repo.verify_audit_chain())

    def test_recompute_after_review_void(self):
        r = self._reading({"kind": "inspection_defect", "grade": "critical",
                           "location": "主梁"}, "engineer", "bridge_engineer")
        self.assertEqual(r["advisory"]["level"], "closure")
        reading_id = r["reading"]["id"]
        res = self.service.review_reading(
            self.item["id"], reading_id, {"decision": "void", "note": "误报"},
            "engineer", "bridge_engineer")
        self.assertEqual(res["reading"]["status"], "void")
        self.assertEqual(res["advisory"]["level"], "normal")
        with self.assertRaises(ConflictError):
            self.service.review_reading(self.item["id"], reading_id,
                                        {"decision": "confirm"}, "engineer",
                                        "bridge_engineer")

    def test_unchanged_recompute_keeps_version(self):
        first = self._reading({"kind": "weather", "condition": "rain"})
        second = self._reading({"kind": "sensor_peak", "value": 5, "threshold": 10})
        self.assertEqual(first["advisory"]["version"], 1)
        self.assertEqual(second["advisory"]["version"], 1)
        self.assertEqual(
            len(self.service.list_advisories(self.item["id"], "viewer")), 1)

    def test_dual_signoff_release(self):
        self._reading({"kind": "inspection_defect", "grade": "moderate",
                       "location": "桥台"}, "engineer", "bridge_engineer")
        adv = self.service.sign_advisory(
            self.item["id"], {"decision": "approve", "comment": "同意"},
            "eng1", "bridge_engineer")
        self.assertEqual(adv["status"], "signing")
        adv = self.service.sign_advisory(
            self.item["id"], {"decision": "approve"}, "road1", "traffic_authority")
        self.assertEqual(adv["status"], "released")
        with self.assertRaises(ConflictError):
            self.service.sign_advisory(self.item["id"], {"decision": "reject"},
                                       "eng1", "bridge_engineer")

    def test_signoff_dispute_stays_pending(self):
        self._reading({"kind": "vehicle_load", "vehicle_type": "挂车",
                       "axle_load": 70, "limit": 49})
        self.service.sign_advisory(self.item["id"], {"decision": "approve"},
                                   "eng1", "bridge_engineer")
        adv = self.service.sign_advisory(
            self.item["id"], {"decision": "reject", "comment": "需复核"},
            "road1", "traffic_authority")
        self.assertEqual(adv["status"], "pending_dispute")
        adv = self.service.sign_advisory(
            self.item["id"], {"decision": "approve"}, "road1", "traffic_authority")
        self.assertEqual(adv["status"], "released")

    def test_restore_gate(self):
        r = self._reading({"kind": "inspection_defect", "grade": "serious",
                           "location": "伸缩缝"}, "engineer", "bridge_engineer")
        reading_id = r["reading"]["id"]
        with self.assertRaises(ConflictError):
            self.service.restore(self.item["id"], {"notice_removed": True},
                                 "road1", "traffic_authority")
        self.service.close_defect(self.item["id"], reading_id, "eng1",
                                  "bridge_engineer")
        with self.assertRaises(ConflictError):
            self.service.restore(self.item["id"], {"notice_removed": False},
                                 "road1", "traffic_authority")
        adv = self.service.restore(self.item["id"], {"notice_removed": True},
                                   "road1", "traffic_authority")
        self.assertEqual(adv["level"], "normal")
        self.assertEqual(adv["status"], "released")
        with self.assertRaises(ConflictError):
            self.service.restore(self.item["id"], {"notice_removed": True},
                                 "road1", "traffic_authority")

    def test_permissions(self):
        with self.assertRaises(PermissionDenied):
            self._reading({"kind": "weather", "condition": "rain"}, "v", "viewer")
        r = self._reading({"kind": "weather", "condition": "storm"})
        with self.assertRaises(PermissionDenied):
            self.service.sign_advisory(self.item["id"], {"decision": "approve"},
                                       "op", "sensor_operator")
        with self.assertRaises(PermissionDenied):
            self.service.restore(self.item["id"], {"notice_removed": True},
                                 "eng", "bridge_engineer")
        with self.assertRaises(PermissionDenied):
            self.service.review_reading(self.item["id"], r["reading"]["id"],
                                        {"decision": "confirm"}, "op",
                                        "sensor_operator")

    def test_validation(self):
        with self.assertRaises(ValidationError):
            self._reading({"kind": "weather", "condition": "hail"})
        with self.assertRaises(ValidationError):
            self._reading({"kind": "unknown_kind"})
        with self.assertRaises(ValidationError):
            self._reading({"kind": "vehicle_load", "vehicle_type": "货车",
                           "axle_load": -1, "limit": 49})


if __name__ == "__main__":
    unittest.main()
