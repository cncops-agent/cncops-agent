"""安全分级：把"拆机 / 带电 / 高压 / 主轴拆装 / 液压 / 带压"类操作强制升级到 L3。

铁律：分级与安全警告由本模块确定性产出，处于渲染链最后一步，
不受大模型输出影响，也不会因为用户催促而降级或省略。
"""

from __future__ import annotations

from typing import Dict, List

# 命中任一关键词的步骤，强制升级为 L3（需工程师）
DANGEROUS_PATTERNS: List[str] = [
    "拆机", "拆解", "拆装", "拆卸", "拆下", "打开机头", "机头内部", "主轴拆",
    "主轴轴承更换", "动平衡校正", "更换轴承",
    "带电", "高压", "电气柜", "配电", "变频器", "伺服", "保护整定", "接线端子",
    "液压", "带压管路", "带压作业", "安全阀整定", "减压阀整定",
]

GLOBAL_WARNINGS: List[str] = [
    "本系统只提供诊断与建议，不向设备下发任何控制指令。",
    "任何操作前须执行断电挂牌上锁（LOTO），并遵守本岗位安全规程。",
    "涉及拆机、带电、高压、主轴拆装、液压与带压部件的作业，必须由专业工程师执行。",
]

LEVEL_NAME = {"L1": "L1 操作员（无需工具，可现场确认）",
              "L2": "L2 维修工（停机后可处置的常规维修）",
              "L3": "L3 工程师（需专业工程师或厂家）"}


def is_dangerous(text: str) -> List[str]:
    """返回命中的危险关键词；空列表表示安全。"""
    return [p for p in DANGEROUS_PATTERNS if p in text]


def escalate_steps(steps: List[Dict[str, str]]) -> List[Dict[str, object]]:
    """对知识库步骤做安全升级：危险步骤一律升到 L3 并附带安全警告。"""
    escalated: List[Dict[str, object]] = []
    for step in steps:
        text = str(step.get("text", ""))
        level = str(step.get("level", "L2")).upper()
        hits = is_dangerous(text)
        if hits:
            level = "L3"
        item = dict(step)          # 保留来源字段（kb_id 等），只改写分级与安全信息
        item.update({
            "level": level,
            "text": text,
            "danger_hits": hits,
            "safety": ["该步骤涉及：" + "、".join(hits) + "，须由专业工程师执行并断电挂牌上锁（LOTO）。"] if hits else [],
        })
        escalated.append(item)
    return escalated


def collect_warnings(entries_safety: List[str], steps: List[Dict[str, object]]) -> List[str]:
    """汇总全局警告 + 条目自带警告 + 危险步骤警告（去重，保持顺序）。"""
    warnings: List[str] = list(GLOBAL_WARNINGS)
    for item in entries_safety or []:
        if item and item not in warnings:
            warnings.append(item)
    for step in steps:
        for w in step.get("safety", []) or []:  # type: ignore[union-attr]
            if w and w not in warnings:
                warnings.append(w)
    return warnings
