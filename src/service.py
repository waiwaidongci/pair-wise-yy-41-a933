from __future__ import annotations

from typing import Any, Dict, Optional

from . import assessment
from .domain import (ConflictError, PermissionDenied, ValidationError,
                     ensure_role, normalize_severity, require_number,
                     require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, DEFECT_CLOSE_ROLES, ENTITY,
                    NOTICE_ROLES, READING_ROLES, RECORD_ROLES, RELEASE_ROLES,
                    REVIEW_ROLES, SIGN_SIDE, TITLE, VIEW_ROLES,
                    completion_blockers, escalation_required, priority_score,
                    response_deadline_hours, role_for_transition,
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
        blockers = completion_blockers(
            target, self.repository.open_record_count(item_id),
            open_defects=self.repository.open_defect_count(item_id),
            notice_withdrawn=self.repository.record_kind_count(
                item_id, "notice_withdrawal") > 0)
        if blockers:
            raise ConflictError("；".join(blockers))
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        return self.enrich(updated)

    def register_reading(self, item_id: int, payload: Dict[str, Any], actor: str,
                         role: str) -> Dict[str, Any]:
        """登记传感峰值、巡检缺陷、车型轴重或桥面天气，随后重算建议。"""
        actor = require_text(actor, "actor", 100)
        category = require_text(payload.get("category"), "category", 30)
        if category not in assessment.CATEGORIES:
            raise ValidationError("category不在允许范围内")
        ensure_role(role, READING_ROLES[category])
        data = assessment.validate_payload(category, payload)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        reading = self.repository.add_reading(item_id, category, data, external_ref, actor)
        self.repository.append_audit("reading", ENTITY, item_id, actor, {
            "reading_id": reading["id"], "category": category,
        })
        recommendation = self._recompute(item_id, actor)
        return {"reading": reading, "recommendation": recommendation}

    def review_reading(self, item_id: int, reading_id: int, actor: str,
                       role: str) -> Dict[str, Any]:
        """复核登记数据，复核后重算建议。"""
        ensure_role(role, REVIEW_ROLES)
        actor = require_text(actor, "actor", 100)
        reading = self.repository.get_reading(reading_id)
        if reading["item_id"] != item_id:
            raise ConflictError("登记数据不属于该项目")
        reading = self.repository.mark_reading_reviewed(reading_id, actor)
        self.repository.append_audit("review", ENTITY, item_id, actor, {
            "reading_id": reading_id, "review_count": reading["review_count"],
        })
        recommendation = self._recompute(item_id, actor)
        return {"reading": reading, "recommendation": recommendation}

    def close_reading(self, item_id: int, reading_id: int, actor: str,
                      role: str) -> Dict[str, Any]:
        """关闭巡检缺陷，关闭后重算建议。"""
        ensure_role(role, DEFECT_CLOSE_ROLES)
        actor = require_text(actor, "actor", 100)
        reading = self.repository.get_reading(reading_id)
        if reading["item_id"] != item_id:
            raise ConflictError("登记数据不属于该项目")
        if reading["category"] != "inspection_defect":
            raise ConflictError("只有巡检缺陷可以关闭")
        reading = self.repository.close_reading(reading_id)
        self.repository.append_audit("close_reading", ENTITY, item_id, actor, {
            "reading_id": reading_id,
        })
        recommendation = self._recompute(item_id, actor)
        return {"reading": reading, "recommendation": recommendation}

    def _recompute(self, item_id: int, actor: str) -> Dict[str, Any]:
        """按当前参评数据重算：旧建议失效留档，生成写明触发项的新版本。"""
        result = assessment.evaluate(self.repository.evaluation_readings(item_id))
        recommendation = self.repository.create_recommendation(
            item_id, result["level"], result["triggers"], actor)
        self.repository.append_audit("recompute", ENTITY, item_id, actor, {
            "recommendation_id": recommendation["id"],
            "version": recommendation["version"],
            "level": recommendation["level"],
            "triggers": result["triggers"],
        })
        return recommendation

    def sign_recommendation(self, item_id: int, rec_id: int, level: str, actor: str,
                            role: str) -> Dict[str, Any]:
        """工程或路政会签；双方意见一致才生效，不一致保持待决。"""
        side = SIGN_SIDE.get(role)
        if side is None:
            raise PermissionDenied("当前角色无权执行该操作")
        actor = require_text(actor, "actor", 100)
        level = require_text(level, "level", 20)
        if level not in assessment.LEVELS:
            raise ValidationError("level不在允许范围内")
        recommendation = self.repository.get_recommendation(rec_id)
        if recommendation["item_id"] != item_id:
            raise ConflictError("建议版本不属于该项目")
        recommendation = self.repository.sign_recommendation(rec_id, side, level, actor)
        self.repository.append_audit("sign", ENTITY, item_id, actor, {
            "recommendation_id": rec_id, "side": side, "level": level,
            "status": recommendation["status"],
        })
        return recommendation

    def release_recommendation(self, item_id: int, rec_id: int, actor: str,
                               role: str) -> Dict[str, Any]:
        """放行建议：须会签一致，限行与封闭还须绑定交通通告记录。"""
        ensure_role(role, RELEASE_ROLES)
        actor = require_text(actor, "actor", 100)
        recommendation = self.repository.get_recommendation(rec_id)
        if recommendation["item_id"] != item_id:
            raise ConflictError("建议版本不属于该项目")
        if recommendation["status"] != "effective":
            raise ConflictError("会签未一致，不能直接放行")
        if recommendation["level"] in ("restrict", "close") and \
                self.repository.record_kind_count(item_id, "traffic_notice") == 0:
            raise ConflictError("限行与封闭决策必须绑定交通通告记录")
        recommendation = self.repository.release_recommendation(rec_id)
        self.repository.append_audit("release", ENTITY, item_id, actor, {
            "recommendation_id": rec_id, "level": recommendation["level"],
        })
        return recommendation

    def issue_notice(self, item_id: int, payload: Dict[str, Any], actor: str,
                     role: str) -> Dict[str, Any]:
        """路政登记交通通告，作为限行或封闭决策的绑定记录。"""
        return self._notice(item_id, payload, actor, role, "traffic_notice")

    def withdraw_notice(self, item_id: int, payload: Dict[str, Any], actor: str,
                        role: str) -> Dict[str, Any]:
        """路政确认撤除通告，是恢复放行的前置条件。"""
        return self._notice(item_id, payload, actor, role, "notice_withdrawal")

    def _notice(self, item_id: int, payload: Dict[str, Any], actor: str, role: str,
                kind: str) -> Dict[str, Any]:
        ensure_role(role, NOTICE_ROLES)
        actor = require_text(actor, "actor", 100)
        detail = require_text(payload.get("detail"), "detail")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        record = self.repository.add_record(item_id, kind, detail, "closed",
                                            external_ref, actor)
        self.repository.append_audit(kind, ENTITY, item_id, actor, {
            "record_id": record["id"],
        })
        return record

    def list_readings(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_readings(item_id)

    def list_recommendations(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_recommendations(item_id)

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        result = self.enrich(self.repository.get_item(item_id))
        result["recommendation"] = self.repository.current_recommendation(item_id)
        return result


    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

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
