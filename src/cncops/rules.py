"""阈值规则与证据生成：把实时数据换算成可溯源的数据证据。

两个关键设计：
1. 报警码由本模块从实时数据**派生**，不做独立随机，保证"报警码与数据一致"（防误报）；
2. 阈值只从 config/thresholds.yaml 读取，业务代码中不出现任何硬编码数值。
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional

from .catalog import Device, get_device
from .config import load_yaml, thresholds_for
from . import paths

SEVERITY_ORDER = {"normal": 0, "warn": 1, "alarm": 2}


@dataclass
class Evidence:
    """单条数据证据：实际值 vs 阈值。"""

    point: str
    label: str
    value: float
    unit: str
    direction: str
    warn: Optional[float]
    alarm: Optional[float]
    severity: str          # normal | warn | alarm
    text: str
    hint: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _alarm_table() -> List[dict]:
    data = load_yaml(str(paths.ALARMS_YAML)) or {}
    return list(data.get("codes", []))


def evaluate(value: float, rule: Dict[str, Any]) -> str:
    """按方向判定单个测点的严重等级。"""
    direction = rule.get("direction", "above")
    warn = rule.get("warn")
    alarm = rule.get("alarm")
    if direction == "above":
        if alarm is not None and value >= alarm:
            return "alarm"
        if warn is not None and value >= warn:
            return "warn"
        return "normal"
    if direction == "below":
        if alarm is not None and value <= alarm:
            return "alarm"
        if warn is not None and value <= warn:
            return "warn"
        return "normal"
    if direction == "deviation":
        nominal = rule.get("nominal") or 0.0
        if not nominal:
            return "normal"
        dev = abs(value - nominal) / abs(nominal)
        if alarm is not None and dev >= alarm:
            return "alarm"
        if warn is not None and dev >= warn:
            return "warn"
        return "normal"
    return "normal"


def _evidence_text(label: str, value: float, unit: str, rule: Dict[str, Any], severity: str) -> str:
    direction = rule.get("direction", "above")
    warn, alarm = rule.get("warn"), rule.get("alarm")
    if severity == "normal":
        return f"{label} {value:.2f}{unit}（正常，报警阈值 {alarm}{unit}）"
    hit = alarm if severity == "alarm" else warn
    if direction == "above":
        over = (value - hit) / abs(hit) * 100 if hit else 0
        return f"{label} {value:.2f}{unit} ≥ {'报警' if severity == 'alarm' else '预警'}阈值 {hit}{unit}，超出 {over:.0f}%"
    if direction == "below":
        over = (hit - value) / abs(hit) * 100 if hit else 0
        return f"{label} {value:.2f}{unit} ≤ {'报警' if severity == 'alarm' else '预警'}阈值 {hit}{unit}，低于 {over:.0f}%"
    nominal = rule.get("nominal") or 0.0
    dev = abs(value - nominal) / abs(nominal) * 100 if nominal else 0
    return f"{label} {value:.2f}{unit} 与额定值 {nominal:.0f}{unit} 偏差 {dev:.0f}%（{'报警' if severity == 'alarm' else '预警'}阈值 {hit}）"


def build_evidence(snapshot: dict) -> List[Evidence]:
    """把快照与阈值表比对，生成全部测点的证据（含正常项，供"未见异常"场景使用）。"""
    dev: Optional[Device] = get_device(snapshot["device_id"])
    if dev is None:
        return []
    rules = thresholds_for(dev.type)
    values = snapshot.get("values", {})
    evidences: List[Evidence] = []
    for key, rule in rules.items():
        if key not in values:
            continue
        value = float(values[key])
        severity = evaluate(value, rule)
        evidences.append(Evidence(
            point=key,
            label=rule.get("label", key),
            value=round(value, 3),
            unit=rule.get("unit", ""),
            direction=rule.get("direction", "above"),
            warn=rule.get("warn"),
            alarm=rule.get("alarm"),
            severity=severity,
            text=_evidence_text(rule.get("label", key), value, rule.get("unit", ""), rule, severity),
            hint=rule.get("hint", ""),
        ))
    evidences.sort(key=lambda e: (-SEVERITY_ORDER[e.severity], e.point))
    return evidences


def abnormal(evidences: List[Evidence], min_severity: str = "warn") -> List[Evidence]:
    """取超标项（默认含预警级）。"""
    floor = SEVERITY_ORDER[min_severity]
    return [e for e in evidences if SEVERITY_ORDER[e.severity] >= floor]


def derive_alarm_codes(snapshot: dict, evidences: List[Evidence]) -> List[dict]:
    """从证据派生报警码：只有数据真的越限才会产生对应报警码。"""
    dev = get_device(snapshot["device_id"])
    if dev is None:
        return []
    sev_by_point = {e.point: e.severity for e in evidences}
    codes: List[dict] = []
    for item in _alarm_table():
        if dev.type not in item.get("device_types", []):
            continue
        point = item.get("point")
        severity = sev_by_point.get(point, "normal")
        min_sev = item.get("min_severity", "alarm")
        if SEVERITY_ORDER[severity] >= SEVERITY_ORDER[min_sev]:
            codes.append({"code": item["code"], "message": item.get("message", ""), "point": point})
    codes.sort(key=lambda c: c["code"])
    return codes


def overall_status(evidences: List[Evidence]) -> str:
    if any(e.severity == "alarm" for e in evidences):
        return "ALARM"
    if any(e.severity == "warn" for e in evidences):
        return "WARN"
    return "RUN"


def attach_derived(snapshot: dict) -> dict:
    """在快照上补齐派生字段（状态与报警码），保证二者与数据同源。"""
    evidences = build_evidence(snapshot)
    codes = derive_alarm_codes(snapshot, evidences)
    snapshot = dict(snapshot)
    snapshot["status"] = overall_status(evidences)
    snapshot["alarm_codes"] = [c["code"] for c in codes]
    snapshot["alarm_messages"] = [f"{c['code']} {c['message']}" for c in codes]
    snapshot["evidences"] = [e.to_dict() for e in evidences]
    return snapshot


def signature(device_id: str, evidences: List[Evidence], alarm_codes: List[str]) -> str:
    """故障指纹：用于判断"同类故障再次发生"，从而引用上一次处理记录。"""
    abn = sorted({e.point for e in evidences if e.severity != "normal"})
    return f"{device_id}|{'/'.join(sorted(alarm_codes))}|{'/'.join(abn)}"
