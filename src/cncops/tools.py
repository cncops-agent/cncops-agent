"""智能体可用工具：所有外部调用都在这一层做超时与异常处理，保证上层永远拿得到结果。

工具清单（与固定工作流一一对应）：
- get_device_data：取设备实时数据（OPC-UA / 内置仿真，失败自动降级）
- search_kb：知识库检索 Top3
- get_history：查该设备历史与同类故障记录
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from . import datasource
from .knowledge import KbHit, get_kb
from .memory import get_memory
from .parser import ParsedQuery
from . import rules

SEVERITY_SCORE = {"alarm": 2, "warn": 1, "normal": 0}


def _anomaly_score(snapshot: dict) -> int:
    """异常显著性打分：用于在用户没说清设备时挑选最可疑的设备。"""
    return sum(SEVERITY_SCORE.get(e.get("severity", "normal"), 0) for e in snapshot.get("evidences", []))


def pick_device_by_anomaly() -> Tuple[Optional[str], str]:
    """按异常最显著挑选设备（用户未指明设备时的保守推断）。"""
    snaps = datasource.read_all()
    if not snaps:
        return None, "未取到任何设备数据"
    snaps.sort(key=_anomaly_score, reverse=True)
    best = snaps[0]
    if _anomaly_score(best) == 0:
        return best["device_id"], f"全部设备数据均正常，默认选择 {best['device_id']} 做点检"
    return best["device_id"], f"未指明设备，按异常最显著原则选择 {best['device_id']}（异常项最多）"


def get_device_data(device_id: Optional[str] = None, parsed: Optional[ParsedQuery] = None) -> Tuple[dict, str]:
    """取实时数据：优先用户点名的设备，其次解析结果，最后按异常显著性推断。"""
    note = ""
    target = device_id or (parsed.device_id if parsed else None)
    if not target:
        target, note = pick_device_by_anomaly()
    snapshot = datasource.read_snapshot(target) if target else None
    if snapshot is None:
        raise ValueError(f"无法读取设备数据：{target}")
    if not note:
        note = f"按描述锁定设备 {snapshot['device_id']}" + (
            f"（别名命中：{parsed.device_alias_hit}）" if parsed and parsed.device_alias_hit else "")
    return snapshot, note


def search_kb(query: str, device_type: Optional[str] = None,
              alarm_codes: Optional[List[str]] = None,
              points: Optional[List[str]] = None, top_k: int = 3) -> List[KbHit]:
    """知识库检索 Top-K（离线可用，异常时返回空列表由上层判定未覆盖）。"""
    try:
        return get_kb().search(query, device_type=device_type, alarm_codes=alarm_codes,
                               points=points, top_k=top_k)
    except Exception:
        return []


def get_history(device_id: str, signature: str = "", limit: int = 5) -> Dict[str, List[dict]]:
    """查历史：该设备最近记录 + 同类故障（同指纹）记录。"""
    try:
        mem = get_memory()
        same = mem.same_signature_history(device_id, signature, limit=3) if signature else []
        return {"same_signature": same, "device": mem.device_history(device_id, limit=limit)}
    except Exception:
        return {"same_signature": [], "device": []}


def build_kb_query(raw_text: str, snapshot: dict, parsed: Optional[ParsedQuery] = None) -> str:
    """构造检索词：原始描述 + 同义词扩展 + 数据派生的报警与超标项。

    这一步让"机器不太对劲"这类模糊描述也能被数据驱动着检索到正确条目。
    """
    parts = [raw_text or ""]
    if parsed:
        parts.extend(parsed.expanded_terms)
    parts.extend(snapshot.get("alarm_messages", []))
    for ev in snapshot.get("evidences", []):
        if ev.get("severity") != "normal":
            parts.append(ev.get("label", ""))
    return " ".join(p for p in parts if p)
