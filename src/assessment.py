from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .domain import ValidationError, require_number, require_text

# 风险等级，按严重程度升序：观察、限载、限行、封闭
LEVELS = ["observe", "limit_load", "restrict", "close"]
LEVEL_LABELS = {"observe": "观察", "limit_load": "限载", "restrict": "限行", "close": "封闭"}
LEVEL_RANK = {level: index for index, level in enumerate(LEVELS)}

CATEGORIES = ["sensor_peak", "inspection_defect", "vehicle_load", "weather"]
CATEGORY_LABELS = {
    "sensor_peak": "传感峰值",
    "inspection_defect": "巡检缺陷",
    "vehicle_load": "车型轴重",
    "weather": "桥面天气",
}

DEFECT_GRADES = ["minor", "moderate", "major", "critical"]
WEATHER_CONDITIONS = ["wind", "rain", "snow", "ice", "fog", "heat"]
WEATHER_ALERTS = ["blue", "yellow", "orange", "red"]

# 传感峰值/阈值 比值 -> 等级
SENSOR_BANDS = ((1.5, "close"), (1.2, "restrict"), (1.0, "limit_load"), (0.8, "observe"))
# 轴重/限重 比值 -> 等级
AXLE_BANDS = ((1.3, "restrict"), (1.0, "limit_load"), (0.9, "observe"))
DEFECT_LEVEL = {"minor": "observe", "moderate": "limit_load", "major": "restrict", "critical": "close"}
WEATHER_LEVEL = {"blue": "observe", "yellow": "limit_load", "orange": "restrict", "red": "close"}


def _band_level(ratio: float, bands: Tuple[Tuple[float, str], ...]) -> Optional[str]:
    for cutoff, level in bands:
        if ratio >= cutoff:
            return level
    return None


def validate_payload(category: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """按类别校验并规范化登记数据，未知类别或缺字段直接拒绝。"""
    if category not in CATEGORIES:
        raise ValidationError("category不在允许范围内")
    if not isinstance(payload, dict):
        raise ValidationError("payload必须是JSON对象")
    if category == "sensor_peak":
        return {
            "peak_value": require_number(payload.get("peak_value"), "peak_value"),
            "threshold": require_number(payload.get("threshold"), "threshold", 0.000001),
        }
    if category == "inspection_defect":
        grade = require_text(payload.get("defect_grade"), "defect_grade", 20)
        if grade not in DEFECT_GRADES:
            raise ValidationError("defect_grade不在允许范围内")
        return {
            "defect_grade": grade,
            "description": require_text(payload.get("description"), "description", 500),
        }
    if category == "vehicle_load":
        return {
            "vehicle_type": require_text(payload.get("vehicle_type"), "vehicle_type", 100),
            "axle_weight": require_number(payload.get("axle_weight"), "axle_weight"),
            "limit_weight": require_number(payload.get("limit_weight"), "limit_weight", 0.000001),
        }
    condition = require_text(payload.get("condition"), "condition", 20)
    if condition not in WEATHER_CONDITIONS:
        raise ValidationError("condition不在允许范围内")
    alert = require_text(payload.get("alert"), "alert", 20)
    if alert not in WEATHER_ALERTS:
        raise ValidationError("alert不在允许范围内")
    return {"condition": condition, "alert": alert}


def contribution(category: str, payload: Dict[str, Any]) -> Tuple[Optional[str], str]:
    """单条登记数据对应的建议等级和可读说明，无风险返回(None, 说明)。"""
    if category == "sensor_peak":
        ratio = payload["peak_value"] / payload["threshold"]
        level = _band_level(ratio, SENSOR_BANDS)
        summary = f"传感峰值{payload['peak_value']:g}/阈值{payload['threshold']:g}，比值{ratio:.2f}"
        return level, summary
    if category == "inspection_defect":
        level = DEFECT_LEVEL[payload["defect_grade"]]
        return level, f"巡检缺陷[{payload['defect_grade']}]：{payload['description']}"
    if category == "vehicle_load":
        ratio = payload["axle_weight"] / payload["limit_weight"]
        level = _band_level(ratio, AXLE_BANDS)
        summary = (f"{payload['vehicle_type']}轴重{payload['axle_weight']:g}"
                   f"/限重{payload['limit_weight']:g}，比值{ratio:.2f}")
        return level, summary
    level = WEATHER_LEVEL[payload["alert"]]
    return level, f"桥面天气[{payload['condition']}]预警[{payload['alert']}]"


def evaluate(readings: List[Dict[str, Any]]) -> Dict[str, Any]:
    """按最高风险汇总：返回最终等级、触发项（最高等级的来源）和全部参评项。"""
    contributions = []
    for reading in readings:
        level, summary = contribution(reading["category"], reading["payload"])
        if level is None:
            continue
        contributions.append({
            "reading_id": reading["id"],
            "category": reading["category"],
            "category_label": CATEGORY_LABELS[reading["category"]],
            "level": level,
            "summary": summary,
        })
    if not contributions:
        return {"level": "observe", "triggers": [], "contributions": []}
    top = max(LEVEL_RANK[item["level"]] for item in contributions)
    level = LEVELS[top]
    triggers = [item for item in contributions if item["level"] == level]
    return {"level": level, "triggers": triggers, "contributions": contributions}
