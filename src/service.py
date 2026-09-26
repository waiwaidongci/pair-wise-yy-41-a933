from __future__ import annotations

from typing import Any, Dict, Optional

from . import advisory
from .domain import (ConflictError, NotFoundError, ValidationError, ensure_role,
                     normalize_severity, require_number, require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, ENTITY, RECORD_ROLES, TITLE,
                    VIEW_ROLES, completion_blockers, escalation_required,
                    priority_score, response_deadline_hours, role_for_transition,
                    validate_transition)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    def create_item(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        title = require_text(payload.get("title"), "title", 200)
        description = require_text(payload.get("description"), "description")
        severity = normalize_severity(payload.get("severity"))
        quantity = require_number(payload.get("quantity", 0), "quantity")
        threshold = require_number(payload.get("threshold", 1), "threshold", 0.000001)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "priority": priority_score(severity, quantity, threshold),
        })
        return self.enrich(item)

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValueError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        record = self.repository.add_record(item_id, kind, detail, status,
                                            external_ref, actor)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
        })
        return record

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        blockers = completion_blockers(target, self.repository.open_record_count(item_id))
        if blockers:
            from .domain import ConflictError
            raise ConflictError("；".join(blockers))
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        return self.enrich(updated)

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    def register_reading(self, item_id: int, payload: Dict[str, Any], actor: str,
                         role: str) -> Dict[str, Any]:
        ensure_role(role, advisory.READING_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 50)
        cleaned = advisory.validate_payload(kind, payload)
        risk_level = advisory.evaluate_reading(kind, cleaned)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        defect_status = "open" if kind == "inspection_defect" else None
        reading = self.repository.add_reading(item_id, kind, cleaned, risk_level,
                                              defect_status, external_ref, actor)
        self.repository.append_audit("reading", ENTITY, item_id, actor, {
            "reading_id": reading["id"], "kind": kind, "risk_level": risk_level,
        })
        return {"reading": reading, "advisory": self._recompute(item_id, actor)}

    def review_reading(self, item_id: int, reading_id: int, payload: Dict[str, Any],
                       actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, advisory.REVIEW_ROLES)
        actor = require_text(actor, "actor", 100)
        decision = require_text(payload.get("decision"), "decision", 20)
        if decision not in ("confirm", "void"):
            raise ValidationError("decision必须是confirm或void")
        note = payload.get("note")
        if note is not None:
            note = require_text(note, "note", 500)
        reading = self.repository.get_reading(item_id, reading_id)
        if reading["status"] != "active":
            raise ConflictError("该数据已复核，不能重复复核")
        updated = self.repository.review_reading(reading_id, decision, note, actor)
        self.repository.append_audit("review", ENTITY, item_id, actor, {
            "reading_id": reading_id, "decision": decision,
        })
        return {"reading": updated, "advisory": self._recompute(item_id, actor)}

    def close_defect(self, item_id: int, reading_id: int, actor: str,
                     role: str) -> Dict[str, Any]:
        ensure_role(role, advisory.REVIEW_ROLES)
        actor = require_text(actor, "actor", 100)
        reading = self.repository.get_reading(item_id, reading_id)
        if reading["kind"] != "inspection_defect":
            raise ValidationError("只有巡检缺陷可以关闭")
        if reading["status"] == "void":
            raise ConflictError("该数据已作废")
        if reading["defect_status"] != "open":
            raise ConflictError("缺陷已关闭")
        updated = self.repository.close_defect(reading_id, actor)
        self.repository.append_audit("close_defect", ENTITY, item_id, actor, {
            "reading_id": reading_id,
        })
        return {"reading": updated, "advisory": self._recompute(item_id, actor)}

    def sign_advisory(self, item_id: int, payload: Dict[str, Any], actor: str,
                      role: str) -> Dict[str, Any]:
        ensure_role(role, advisory.SIGN_ROLES)
        actor = require_text(actor, "actor", 100)
        decision = require_text(payload.get("decision"), "decision", 20)
        if decision not in ("approve", "reject"):
            raise ValidationError("decision必须是approve或reject")
        comment = payload.get("comment")
        if comment is not None:
            comment = require_text(comment, "comment", 500)
        latest = self.repository.latest_advisory(item_id)
        if latest is None:
            raise NotFoundError("暂无建议版本")
        if latest["status"] not in ("signing", "pending_dispute"):
            raise ConflictError("当前版本不可会签")
        party = advisory.party_for_role(role)
        eng = decision if party == "eng" else latest["eng_decision"]
        road = decision if party == "road" else latest["road_decision"]
        status = advisory.signoff_status(eng, road)
        updated = self.repository.update_signoff(latest["id"], party, decision,
                                                 comment, actor, status)
        self.repository.append_audit("sign", ENTITY, item_id, actor, {
            "advisory_id": latest["id"], "version": latest["version"],
            "party": party, "decision": decision, "status": status,
        })
        return updated

    def restore(self, item_id: int, payload: Dict[str, Any], actor: str,
                role: str) -> Dict[str, Any]:
        ensure_role(role, advisory.RESTORE_ROLES)
        actor = require_text(actor, "actor", 100)
        notice_removed = payload.get("notice_removed") is True
        latest = self.repository.latest_advisory(item_id)
        if latest is None:
            raise NotFoundError("暂无建议版本")
        if latest["level"] == "normal" and latest["status"] == "released":
            raise ConflictError("当前已是正常通行，无需恢复")
        blockers = advisory.restore_blockers(
            self.repository.open_defect_count(item_id), notice_removed)
        if blockers:
            raise ConflictError("；".join(blockers))
        self.repository.supersede_advisory(latest["id"])
        restored = self.repository.create_advisory(
            item_id, latest["version"] + 1, "normal", [], "released",
            "缺陷全部关闭，路政确认撤除通告，恢复通行", actor)
        self.repository.append_audit("restore", ENTITY, item_id, actor, {
            "advisory_id": restored["id"], "version": restored["version"],
            "supersedes": latest["version"], "notice_removed": True,
        })
        return restored

    def list_readings(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_readings(item_id)

    def current_advisory(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        latest = self.repository.latest_advisory(item_id)
        if latest is None:
            raise NotFoundError("暂无建议版本")
        latest["open_defects"] = self.repository.open_defect_count(item_id)
        return latest

    def list_advisories(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_advisories(item_id)

    def _recompute(self, item_id: int, actor: str) -> Dict[str, Any]:
        """新增或复核数据后重算；结论变化时旧建议失效留档，生成新会签版本。"""
        readings = self.repository.list_readings(item_id)
        level, triggers = advisory.evaluate(readings)
        latest = self.repository.latest_advisory(item_id)
        if (latest is not None and latest["level"] == level
                and [t["reading_id"] for t in latest["triggers"]]
                == [t["reading_id"] for t in triggers]):
            return latest
        if latest is not None:
            self.repository.supersede_advisory(latest["id"])
        version_no = latest["version"] + 1 if latest is not None else 1
        created = self.repository.create_advisory(item_id, version_no, level,
                                                  triggers, "signing", None, actor)
        self.repository.append_audit("advisory", ENTITY, item_id, actor, {
            "advisory_id": created["id"], "version": version_no, "level": level,
            "triggers": triggers,
            "supersedes": latest["version"] if latest is not None else None,
        })
        return created

    @staticmethod
    def enrich(item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        return result
