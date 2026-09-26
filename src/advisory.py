from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .domain import ValidationError, require_number, require_text

# 限行建议等级，按风险从低到高；取最高风险作为建议等级
LEVELS = ['normal', 'observe', 'load_limit', 'restrict', 'closure']
LEVEL_RANK = {level: index for index, level in enumerate(LEVELS)}

# 可登记的数据类别：传感峰值、巡检缺陷、车型轴重、桥面天气
KINDS = ['sensor_peak', 'inspection_defect', 'vehicle_load', 'weather']
READING_STATES = ['active', 'reviewed', 'void']
ADVISORY_STATES = ['signing', 'pending_dispute', 'released', 'rejected', 'superseded']

DEFECT_LEVEL = {'minor': 'observe', 'moderate': 'load_limit',
                'serious': 'restrict', 'critical': 'closure'}
WEATHER_LEVEL = {'clear': 'normal', 'rain': 'observe', 'snow': 'load_limit',
                 'strong_wind': 'load_limit', 'ice': 'restrict', 'storm': 'closure'}
SENSOR_BANDS = [(2.0, 'closure'), (1.5, 'restrict'), (1.2, 'load_limit'), (1.0, 'observe')]
VEHICLE_BANDS = [(1.6, 'closure'), (1.3, 'restrict'), (1.15, 'load_limit'), (1.0, 'observe')]

# 会签双方：工程=bridge_engineer，路政=traffic_authority
SIGN_PARTIES = {'eng': 'bridge_engineer', 'road': 'traffic_authority'}
READING_ROLES = {'sensor_operator', 'bridge_engineer'}
REVIEW_ROLES = {'bridge_engineer'}
SIGN_ROLES = set(SIGN_PARTIES.values())
RESTORE_ROLES = {'traffic_authority'}


def _optional_text(value, field, max_length):
    if value is None:
        return None
    return require_text(value, field, max_length)


def validate_payload(kind: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    if kind == 'sensor_peak':
        return {
            'sensor': _optional_text(payload.get('sensor'), 'sensor', 100),
            'value': require_number(payload.get('value'), 'value'),
            'threshold': require_number(payload.get('threshold'), 'threshold', 0.000001),
        }
    if kind == 'inspection_defect':
        grade = require_text(payload.get('grade'), 'grade', 50)
        if grade not in DEFECT_LEVEL:
            raise ValidationError('grade不在允许范围内')
        return {
            'grade': grade,
            'location': require_text(payload.get('location'), 'location', 200),
            'description': _optional_text(payload.get('description'), 'description', 2000),
        }
    if kind == 'vehicle_load':
        return {
            'vehicle_type': require_text(payload.get('vehicle_type'), 'vehicle_type', 100),
            'axle_load': require_number(payload.get('axle_load'), 'axle_load'),
            'limit': require_number(payload.get('limit'), 'limit', 0.000001),
        }
    if kind == 'weather':
        condition = require_text(payload.get('condition'), 'condition', 50)
        if condition not in WEATHER_LEVEL:
            raise ValidationError('condition不在允许范围内')
        return {
            'condition': condition,
            'note': _optional_text(payload.get('note'), 'note', 500),
        }
    raise ValidationError('未知的数据类别')


def _band_level(ratio: float, bands) -> str:
    for minimum, level in bands:
        if ratio >= minimum:
            return level
    return 'normal'


def evaluate_reading(kind: str, payload: Dict[str, Any]) -> str:
    if kind == 'sensor_peak':
        return _band_level(payload['value'] / payload['threshold'], SENSOR_BANDS)
    if kind == 'inspection_defect':
        return DEFECT_LEVEL[payload['grade']]
    if kind == 'vehicle_load':
        return _band_level(payload['axle_load'] / payload['limit'], VEHICLE_BANDS)
    if kind == 'weather':
        return WEATHER_LEVEL[payload['condition']]
    raise ValidationError('未知的数据类别')


def summarize(kind: str, payload: Dict[str, Any]) -> str:
    if kind == 'sensor_peak':
        return f"传感峰值{payload['value']}/阈值{payload['threshold']}"
    if kind == 'inspection_defect':
        return f"巡检缺陷[{payload['grade']}] {payload['location']}"
    if kind == 'vehicle_load':
        return f"{payload['vehicle_type']}轴重{payload['axle_load']}t/限{payload['limit']}t"
    if kind == 'weather':
        return f"桥面天气[{payload['condition']}]"
    return kind


def evaluate(readings: List[Dict[str, Any]]) -> Tuple[str, List[Dict[str, Any]]]:
    """按最高风险合成建议等级，触发项为达到该等级的全部有效数据。"""
    best = 'normal'
    triggers: List[Dict[str, Any]] = []
    for reading in readings:
        if reading['status'] == 'void':
            continue
        if reading['kind'] == 'inspection_defect' and reading.get('defect_status') == 'closed':
            continue
        level = reading['risk_level']
        trigger = {'reading_id': reading['id'], 'kind': reading['kind'], 'level': level,
                   'summary': summarize(reading['kind'], reading['payload'])}
        if LEVEL_RANK[level] > LEVEL_RANK[best]:
            best = level
            triggers = [trigger]
        elif level == best and level != 'normal':
            triggers.append(trigger)
    return best, triggers


def signoff_status(eng: Optional[str], road: Optional[str]) -> str:
    """双方同意才发布；意见不一致先待决，不能直接放行。"""
    if eng == 'approve' and road == 'approve':
        return 'released'
    if eng == 'reject' and road == 'reject':
        return 'rejected'
    if eng and road:
        return 'pending_dispute'
    return 'signing'


def party_for_role(role: str) -> Optional[str]:
    for party, party_role in SIGN_PARTIES.items():
        if party_role == role:
            return party
    return None


def restore_blockers(open_defects: int, notice_removed: bool) -> List[str]:
    blockers = []
    if open_defects > 0:
        blockers.append(f"仍有{open_defects}项巡检缺陷未关闭")
    if not notice_removed:
        blockers.append("需路政确认撤除通告")
    return blockers
