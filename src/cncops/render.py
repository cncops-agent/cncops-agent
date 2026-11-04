"""固定格式渲染：异常结论 / 数据证据 / 可能原因排序 / L1-L3 分级步骤 / 安全警告 / 知识库来源 / 历史记录。

格式是硬约束：无论走大模型还是降级模式，七段结构完全一致，
降级模式只会在头部多一行"当前为降级模式"标注。
"""

from __future__ import annotations

from typing import List

from .agent import DiagnosisReport
from .safety import LEVEL_NAME

MODE_TEXT = {"llm": "大模型增强模式", "rule": "降级模式（关键词匹配 + 阈值规则 + 知识库检索）"}


def render_markdown(report: DiagnosisReport) -> str:
    lines: List[str] = []
    head = f"# 设备诊断报告 · {report.device_id} {report.device_name}"
    lines.append(head)
    mode_line = f"- 生成时间：{report.ts} ｜ 模式：{MODE_TEXT.get(report.mode, report.mode)} ｜ 数据源：{report.data_source}"
    if report.mode == "rule":
        mode_line += " ｜ **当前为降级模式**"
    lines.append(mode_line)
    alarm_text = "、".join(report.alarm_codes) or "无"
    lines.append(f"- 设备类型：{report.device_type} ｜ 报警码：{alarm_text} ｜ 记录号：#{report.record_id}")
    lines.append(f"- 用户描述：{report.query}")
    if report.device_selection_note:
        lines.append(f"- 设备判定：{report.device_selection_note}")
    if report.degraded_reasons:
        lines.append("- 降级原因：" + "；".join(report.degraded_reasons))

    # 一、异常结论
    lines.append("\n## 一、异常结论")
    lines.append(report.conclusion)
    if report.narrative:
        lines.append(f"\n> {report.narrative}")
    if report.parsed.get("vague"):
        if report.abnormal_evidence:
            lines.append("\n（说明：描述较模糊，已按实时数据超标项反推故障方向。）")
        else:
            lines.append("\n（说明：描述较模糊且实时数据未见异常，已按观察建议处理，"
                         "建议补充工况信息后复测。）")

    # 二、数据证据
    lines.append("\n## 二、数据证据（实际值 vs 阈值）")
    abnormal = report.abnormal_evidence
    if abnormal:
        lines.append("| 测点 | 实际值 | 单位 | 预警阈值 | 报警阈值 | 判定 |")
        lines.append("|---|---|---|---|---|---|")
        for e in abnormal:
            lines.append(f"| {e['label']} | {e['value']} | {e['unit']} | {e['warn']} | {e['alarm']} | "
                         f"{'报警' if e['severity'] == 'alarm' else '预警'} |")
        lines.append("")
        for e in abnormal:
            lines.append(f"- {e['text']}")
    else:
        lines.append("- 未检出超标项，关键测点均在阈值内。")
        for e in report.evidence[:4]:
            lines.append(f"- {e['text']}")
    if report.alarm_messages:
        lines.append("\n派生报警码（由实时数据计算，非独立产生）：" + "；".join(report.alarm_messages))

    # 三、可能原因排序
    lines.append("\n## 三、可能原因排序（带依据）")
    if report.causes:
        for idx, c in enumerate(report.causes[:5], 1):
            lines.append(f"{idx}. 【{c.kb_id}】{c.title} ｜ 可能性：{c.probability * 100:.0f}%")
            lines.append(f"   - 首要怀疑：{c.text}")
            for j, cand in enumerate(c.candidates, 1):
                lines.append(f"      ({j}) {cand['text']}（依据：{cand['basis']}）")
            lines.append(f"   - 排序依据：{c.basis}")
    else:
        lines.append("- 无可用候选原因。")

    # 四、分级排查步骤
    lines.append("\n## 四、分级排查步骤（L1 操作员 / L2 维修工 / L3 工程师）")
    if not report.steps:
        lines.append("- 知识库未覆盖，无分级步骤。")
    else:
        for level in ("L1", "L2", "L3"):
            steps = [s for s in report.steps if s["level"] == level]
            if not steps:
                continue
            lines.append(f"\n### {LEVEL_NAME[level]}")
            for i, s in enumerate(steps, 1):
                flag = " ⚠ 危险作业" if s.get("danger_hits") else ""
                lines.append(f"{i}. {s['text']}{flag}")
                lines.append(f"   - 来源：{s.get('kb_id', '-')}")

    # 五、安全警告
    lines.append("\n## 五、安全警告")
    for w in report.warnings:
        lines.append(f"- ⚠ {w}")

    # 六、知识库来源
    lines.append("\n## 六、知识库来源")
    if report.uncovered:
        lines.append("- **知识库未覆盖当前现象**，以下条目的步骤仅作为通用安全建议，不构成针对性处置方案。")
    if report.kb_hits:
        lines.append("| 条目 | 标题 | 检索得分 | 命中原因 | 文件 |")
        lines.append("|---|---|---|---|---|")
        for h in report.kb_hits:
            src = h.get("source_file", "")
            lines.append(f"| {h['kb_id']} | {h['title']} | {h['score']:.2f} | "
                         f"{'；'.join(h.get('reasons', [])) or '-'} | {src} |")
    else:
        lines.append("- 无命中条目。")

    # 七、历史记录引用
    lines.append("\n## 七、该设备历史记录")
    if report.history_ref:
        h = report.history_ref
        state = {1: "已解决", 0: "未解决", None: "未反馈"}.get(h.get("resolved"), "未反馈")
        lines.append(f"- **同类故障复发**：记录 #{h.get('id')}（{h.get('created_at')}）"
                     f"【{h.get('fault_id')} {h.get('fault_title', '')}】，上次结论：{h.get('conclusion')}，处理结果：{state}")
        if h.get("resolved") == 0:
            lines.append("- 上次处理未解决，本次已按规则提升处置等级，建议直接由工程师复核上次记录。")
    else:
        lines.append("- 该设备此前未出现同类故障（同指纹）记录。")
    if report.device_history:
        lines.append("\n最近诊断记录：")
        for h in report.device_history[:3]:
            state = {1: "已解决", 0: "未解决", None: "未反馈"}.get(h.get("resolved"), "未反馈")
            lines.append(f"- #{h.get('id')} {h.get('created_at')} 【{h.get('fault_id')}】{h.get('conclusion')} ｜ {state}")
    return "\n".join(lines)


def render_text(report: DiagnosisReport) -> str:
    """纯文本版（便于终端与日志）。"""
    return render_markdown(report)
