import tempfile, unittest
from pathlib import Path
from src.domain import ConflictError, PermissionDenied, ValidationError
from src.repository import Repository
from src.service import Service
from src.rules import STATES, TRANSITION_ROLES


class RecommendationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.item = self.service.create_item(
            {"title": "某跨江大桥", "description": "限行建议台", "severity": "warning",
             "quantity": 5, "threshold": 10, "external_ref": "BR-1"},
            "creator", "sensor_operator")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _register(self, payload, actor="op", role="sensor_operator"):
        return self.service.register_reading(self.item["id"], payload, actor, role)

    def test_highest_risk_level_and_triggers_recorded(self):
        self._register({"category": "sensor_peak", "peak_value": 11, "threshold": 10})
        self._register({"category": "vehicle_load", "vehicle_type": "三轴货车",
                        "axle_weight": 30, "limit_weight": 25}, "auth", "traffic_authority")
        result = self._register({"category": "weather", "condition": "wind", "alert": "red"})
        rec = result["recommendation"]
        self.assertEqual(rec["level"], "close")
        self.assertEqual(rec["status"], "pending")
        self.assertEqual(len(rec["triggers"]), 1)
        self.assertEqual(rec["triggers"][0]["category"], "weather")
        self.assertIn("red", rec["triggers"][0]["summary"])

    def test_register_and_review_recompute_and_supersede(self):
        first = self._register({"category": "sensor_peak", "peak_value": 9,
                                "threshold": 10})["recommendation"]
        second = self._register({"category": "sensor_peak", "peak_value": 13,
                                 "threshold": 10})["recommendation"]
        self.assertEqual(second["version"], first["version"] + 1)
        self.assertEqual(second["level"], "restrict")
        history = self.service.list_recommendations(self.item["id"], "viewer")
        by_version = {rec["version"]: rec for rec in history}
        self.assertEqual(by_version[first["version"]]["status"], "superseded")
        self.assertEqual(by_version[second["version"]]["status"], "pending")
        reading_id = second["triggers"][0]["reading_id"]
        third = self.service.review_reading(
            self.item["id"], reading_id, "eng", "bridge_engineer")["recommendation"]
        self.assertEqual(third["version"], second["version"] + 1)
        history = self.service.list_recommendations(self.item["id"], "viewer")
        superseded = [rec for rec in history if rec["status"] == "superseded"]
        self.assertEqual(len(superseded), 2)

    def test_countersign_dispute_then_release(self):
        rec = self._register({"category": "sensor_peak", "peak_value": 13,
                              "threshold": 10})["recommendation"]
        with self.assertRaises(PermissionDenied):
            self.service.sign_recommendation(self.item["id"], rec["id"], "restrict",
                                             "bad", "viewer")
        rec = self.service.sign_recommendation(self.item["id"], rec["id"], "limit_load",
                                               "eng", "bridge_engineer")
        rec = self.service.sign_recommendation(self.item["id"], rec["id"], "restrict",
                                               "auth", "traffic_authority")
        self.assertEqual(rec["status"], "disputed")
        with self.assertRaises(ConflictError):
            self.service.release_recommendation(self.item["id"], rec["id"],
                                                "auth", "traffic_authority")
        rec = self.service.sign_recommendation(self.item["id"], rec["id"], "restrict",
                                               "eng2", "bridge_engineer")
        self.assertEqual(rec["status"], "effective")
        self.assertEqual(rec["level"], "restrict")
        with self.assertRaises(ConflictError):
            self.service.release_recommendation(self.item["id"], rec["id"],
                                                "auth", "traffic_authority")
        self.service.issue_notice(self.item["id"], {"detail": "限行通告已发布"},
                                  "auth", "traffic_authority")
        rec = self.service.release_recommendation(self.item["id"], rec["id"],
                                                  "auth", "traffic_authority")
        self.assertEqual(rec["status"], "released")

    def test_restore_requires_defect_closed_and_notice_withdrawn(self):
        defect = self.service.register_reading(
            self.item["id"], {"category": "inspection_defect", "defect_grade": "major",
                              "description": "支座开裂"}, "eng", "bridge_engineer")["reading"]
        current = self.service.get_item(self.item["id"], "viewer")
        for target in STATES[1:-1]:
            current = self.service.transition(current["id"], target, current["version"],
                                              "reviewer", TRANSITION_ROLES[target][0])
        with self.assertRaises(ConflictError):
            self.service.transition(current["id"], STATES[-1], current["version"],
                                    "reviewer", TRANSITION_ROLES[STATES[-1]][0])
        self.service.close_reading(self.item["id"], defect["id"], "eng", "bridge_engineer")
        current = self.service.get_item(self.item["id"], "viewer")
        with self.assertRaises(ConflictError):
            self.service.transition(current["id"], STATES[-1], current["version"],
                                    "reviewer", TRANSITION_ROLES[STATES[-1]][0])
        self.service.withdraw_notice(self.item["id"], {"detail": "通告已撤除"},
                                     "auth", "traffic_authority")
        final = self.service.transition(current["id"], STATES[-1], current["version"],
                                        "reviewer", TRANSITION_ROLES[STATES[-1]][0])
        self.assertEqual(final["status"], STATES[-1])
        self.assertTrue(self.repo.verify_audit_chain())

    def test_reading_role_and_validation_guards(self):
        with self.assertRaises(PermissionDenied):
            self._register({"category": "inspection_defect", "defect_grade": "major",
                            "description": "裂缝"}, "op", "sensor_operator")
        with self.assertRaises(ValidationError):
            self._register({"category": "sensor_peak", "peak_value": -1, "threshold": 10})
        with self.assertRaises(ValidationError):
            self._register({"category": "unknown", "peak_value": 1, "threshold": 10})


if __name__ == "__main__":
    unittest.main()
