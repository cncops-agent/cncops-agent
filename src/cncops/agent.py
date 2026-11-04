"""诊断智能体：固定六步工作流，禁止跳步。

工作流（顺序固定，任何一步失败都会记录降级原因而不是中断）：
  ① 解析口语（设备别名 + 症状同义词扩展）
  ② get_device_data 取实时数据
  ③ 与阈值对比生成数据证据（含报警码派生）
  ④ search_kb 检索知识库 Top3
  ⑤ get_history 查该设备历史 / 同类故障记录
  ⑥ 按固定格式输出诊断（步骤恒取自知识库原文）

大模型只参与：口语理解补充、原因排序、结论语言组织；
排查步骤一律取自知识库条目原文，LLM 输出需通过校验，校验失败回退规则版。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from . import datasource, rules, safety, tools
from .catalog import Device, get_device
from .config import settings
from .knowledge import KbEntry, KbHit, get_kb
from .llm_client import LLMClient
from .memory import get_memory
from .parser import ParsedQuery, parse

GENERIC_ENTRY_ID = "KB-GEN-000"   # 知识库未覆盖时的通用安全条目
OBSERVE_ENTRY_ID = "KB-GEN-001"   # 数据正常时的观察建议条目

SYSTEM_PROMPT = (
    "你是工业设备运维诊断助手，服务对象是一线维修工。\n"
    "严格约束：\n"
    "1. 只能使用【实时数据证据】与【知识库候选条目】中的信息，禁止编造任何排查步骤；\n"
    "2. 排查步骤不由你输出，你只输出结论、原因排序与给用户的解释；\n"
    "3. 涉及拆机、带电、高压、主轴拆装、液压的操作必须标注需要专业工程师；\n"
    "4. 只输出 JSON，不要输出任何额外文字。\n"
    "输出格式：\n"
    '{"understood":{"device":"设备号","symptoms":["症状1"]},'
    '"conclusion":"一句话异常结论",'
    '"causes":[{"kb_id":"KB-XXX","rank":1,"why":"依据说明"}],'
    '"narrative":"给维修工的通俗解释"}'
)


@dataclass
class Cause:
    """一个候选故障条目（含该条目下的具体原因候选，均来自知识库原文）。"""

    kb_id: str
    title: str
    text: str
    basis: str
    score: float
    probability: float = 0.0
    feedback: str = ""
    candidates: List[Dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"kb_id": self.kb_id, "title": self.title, "text": self.text,
                "basis": self.basis, "score": round(self.score, 4),
                "probability": round(self.probability, 3), "feedback": self.feedback,
                "candidates": self.candidates}


@dataclass
class DiagnosisReport:
    device_id: str = ""
    device_name: str = ""
    device_type: str = ""
    ts: str = ""
    query: str = ""
    parsed: Dict[str, Any] = field(default_factory=dict)
    mode: str = "rule"                       # rule（降级/规则） | llm（大模型增强）
    degraded: bool = True
    degraded_reasons: List[str] = field(default_factory=list)
    llm_notes: List[str] = field(default_factory=list)
    conclusion: str = ""
    narrative: str = ""
    evidence: List[Dict[str, Any]] = field(default_factory=list)
    abnormal_evidence: List[Dict[str, Any]] = field(default_factory=list)
    alarm_codes: List[str] = field(default_factory=list)
    alarm_messages: List[str] = field(default_factory=list)
    data_source: str = ""
    causes: List[Cause] = field(default_factory=list)
    steps: List[Dict[str, Any]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    kb_hits: List[Dict[str, Any]] = field(default_factory=list)
    uncovered: bool = False
    history_ref: Optional[Dict[str, Any]] = None
    device_history: List[Dict[str, Any]] = field(default_factory=list)
    signature: str = ""
    record_id: Optional[int] = None
    device_selection_note: str = ""

    def to_dict(self) -> dict:
        data = dict(self.__dict__)
        data["causes"] = [c.to_dict() for c in self.causes]
        return data


# ---------------------------------------------------------------- 排序与依据
def _feedback_weight(device_id: str, kb_id: str) -> tuple:
    """读取历史反馈，返回 (权重系数, 说明文本)。"""
    try:
        ok, ng = get_memory().fault_feedback_stats(device_id, kb_id)
    except Exception:
        return 1.0, "历史反馈不可用"
    if ok == 0 and ng == 0:
        return 1.0, "无历史反馈"
    weight = 1.0 + 0.15 * ok - 0.25 * ng
    weight = max(0.4, min(1.6, weight))
    return weight, f"历史反馈：解决了 {ok} 次 / 未解决 {ng} 次"


def rank_causes(hits: List[KbHit], snapshot: dict, evidences: List[rules.Evidence]) -> List[Cause]:
    """规则版原因排序（按知识库条目聚合）：基础概率 × 反馈权重 × 证据匹配 × 检索得分。"""
    alarm_codes = set(snapshot.get("alarm_codes", []))
    abn_points = {e.point for e in evidences if e.severity != "normal"}
    causes: List[Cause] = []
    for hit in hits:
        entry = hit.entry
        score = entry.base_probability
        weight, fb_text = _feedback_weight(snapshot["device_id"], entry.id)
        score *= weight
        hit_codes = alarm_codes & set(entry.alarm_codes)
        code_note = "无"
        if hit_codes:
            score *= 1.25
            # 关键区分信号：该条目能解释"观测到的报警码"的比例。
            # 组合故障（如振动+过热同时报警）应排在只能解释其中一个码的单一条目之前，
            # 否则条目先验概率会把证据更完整的条目压下去（自测 A-03 暴露的问题）。
            coverage = len(hit_codes) / max(len(alarm_codes), 1)
            score *= (1.0 + 0.30 * coverage)
            code_note = (f"{'、'.join(sorted(hit_codes))}（可解释观测报警码 "
                         f"{len(hit_codes)}/{len(alarm_codes)}）")
        if abn_points & set(entry.points):
            score *= 1.15
        score *= (0.8 + 0.4 * hit.score)
        basis = "；".join(p for p in [
            f"实时数据：{_evidence_brief(evidences)}",
            f"检索得分：{hit.score:.2f}（{'；'.join(hit.reasons)}）",
            f"命中报警码：{code_note}",
            fb_text,
        ] if p)
        candidates = [{"text": c.get("text", ""), "basis": c.get("basis", "")} for c in entry.causes]
        causes.append(Cause(kb_id=entry.id, title=entry.title,
                            text=candidates[0]["text"] if candidates else entry.conclusion,
                            basis=basis, score=score, feedback=fb_text, candidates=candidates))
    causes.sort(key=lambda c: c.score, reverse=True)
    total = sum(c.score for c in causes) or 1.0
    for c in causes:
        c.probability = c.score / total
    return causes


def _evidence_brief(evidences: List[rules.Evidence]) -> str:
    abn = [e for e in evidences if e.severity != "normal"]
    if not abn:
        return "无超标项"
    return "，".join(f"{e.label}={e.value}{e.unit}" for e in abn[:3])


# ---------------------------------------------------------------- 大模型校验
def _validate_llm(payload: Dict[str, Any], allowed_ids: List[str]) -> List[str]:
    """校验大模型输出：越界即失败（由上层回退规则版，绝不采用未校验内容）。"""
    errors: List[str] = []
    if not isinstance(payload, dict):
        return ["返回不是 JSON 对象"]
    causes = payload.get("causes")
    if not isinstance(causes, list) or not causes:
        errors.append("缺少 causes 或为空")
        return errors
    for item in causes:
        if not isinstance(item, dict) or item.get("kb_id") not in allowed_ids:
            errors.append(f"原因条目越界：{item.get('kb_id') if isinstance(item, dict) else item}")
            break
    if not str(payload.get("conclusion", "")).strip():
        errors.append("缺少 conclusion")
    ranks = [c.get("rank") for c in causes if isinstance(c, dict)]
    if any(not isinstance(r, int) for r in ranks):
        errors.append("rank 不是整数")
    return errors


# ---------------------------------------------------------------- 主流程
class DiagnosisAgent:
    def __init__(self, llm: Optional[LLMClient] = None) -> None:
        self.cfg = settings()
        self.llm = llm or LLMClient()
        self.kb = get_kb()

    def diagnose(self, text: str, device_id: Optional[str] = None, top_k: int = 3,
                 operator: str = "") -> DiagnosisReport:
        report = DiagnosisReport(query=text, ts=datetime.now().isoformat(timespec="seconds"))

        # ① 解析口语 -------------------------------------------------------
        parsed: ParsedQuery = parse(text)
        report.parsed = parsed.to_dict()

        # ② 取实时数据 -----------------------------------------------------
        snapshot, note = tools.get_device_data(device_id, parsed)
        report.device_id = snapshot["device_id"]
        report.device_name = snapshot["device_name"]
        report.device_type = snapshot["device_type"]
        report.data_source = snapshot.get("source", "")
        report.device_selection_note = note
        if snapshot.get("degraded"):
            report.degraded_reasons.append(snapshot["degraded"])

        # ③ 阈值对比 → 证据 ------------------------------------------------
        evidences = rules.build_evidence(snapshot)
        report.evidence = [e.to_dict() for e in evidences]
        report.abnormal_evidence = [e.to_dict() for e in rules.abnormal(evidences)]
        report.alarm_codes = list(snapshot.get("alarm_codes", []))
        report.alarm_messages = list(snapshot.get("alarm_messages", []))
        report.signature = rules.signature(report.device_id, evidences, report.alarm_codes)

        # ④ 知识库检索 Top3 ------------------------------------------------
        kb_query = tools.build_kb_query(text, snapshot, parsed)
        hits = tools.search_kb(kb_query, device_type=report.device_type,
                               alarm_codes=report.alarm_codes,
                               points=[e["point"] for e in report.abnormal_evidence],
                               top_k=top_k)
        uncovered = self.kb.is_uncovered(hits)
        # 用户描述了冒烟/焦糊味等危险现象，但命中的专业条目里没有任何一条覆盖它 → 视为未覆盖
        if not uncovered and parsed.danger_terms:
            danger_covered = any(
                any(d in s for s in h.entry.symptoms for d in parsed.danger_terms)
                for h in hits if not h.entry.id.startswith("KB-GEN"))
            if not danger_covered:
                uncovered = True

        # 选择兜底条目：数据正常且无危险描述 → 观察建议；未覆盖 → 通用安全原则
        no_abn = not report.abnormal_evidence
        if no_abn and not parsed.danger_terms:
            uncovered, fallback_id = False, OBSERVE_ENTRY_ID
        elif uncovered:
            fallback_id = GENERIC_ENTRY_ID
        else:
            fallback_id = None
        if fallback_id:
            fb_entry = self.kb.get(fallback_id)
            if fb_entry:
                hits = [h for h in hits if h.entry.id != fallback_id]
                hits.insert(0, KbHit(fb_entry, 0.30, reasons=["按当前数据与描述回退到通用条目"]))
                hits = hits[:top_k]
        report.uncovered = uncovered
        report.kb_hits = [h.to_dict() for h in hits]
        allowed_ids = [h.entry.id for h in hits]

        # ⑤ 查历史 ----------------------------------------------------------
        history = tools.get_history(report.device_id, report.signature)
        report.device_history = history.get("device", [])
        same_sig = history.get("same_signature", [])
        report.history_ref = same_sig[0] if same_sig else None

        # ⑥ 生成结论与原因排序（先出规则版，再尝试大模型增强）--------------
        causes = rank_causes(hits, snapshot, evidences)
        if fallback_id:
            # 未覆盖/数据正常时不做凭空的故障猜测，只保留通用条目
            causes = [c for c in causes if c.kb_id == fallback_id] or causes
        report.causes = causes
        top_entry = self._entry_of(causes[0].kb_id, hits) if causes else None
        report.conclusion = top_entry.conclusion if top_entry else "未检索到可用知识条目，按通用安全原则处置。"
        if report.uncovered:
            report.conclusion = "知识库未覆盖当前现象，以下仅提供通用安全处置原则，请联系工程师现场确认。"
            if not report.abnormal_evidence:
                report.conclusion += "（实时数据未越过阈值，无法从数据侧定位。）"
        elif not report.abnormal_evidence:
            report.conclusion = "实时数据未越过任何阈值，暂未检出异常；按观察建议持续跟踪。"

        applied = self._try_llm(report, text, snapshot, evidences, hits, allowed_ids, causes)
        if not applied:
            report.mode = "rule"
            report.degraded = True

        # 步骤来源：知识库原文（大模型不得生成步骤）
        top_id = fallback_id or (report.causes[0].kb_id if report.causes else GENERIC_ENTRY_ID)
        entry = self._entry_of(top_id, hits) or self.kb.get(GENERIC_ENTRY_ID)
        steps: List[Dict[str, Any]] = []
        entry_safety: List[str] = []
        if entry:
            steps = [dict(s, kb_id=entry.id) for s in entry.steps]
            entry_safety = list(entry.safety)
            # 多个报警码并存时，若第二名条目得分接近，补充其 L1 步骤作并列排查（每步仍带来源 id）
            if len(report.causes) >= 2 and not report.uncovered:
                second = self._entry_of(report.causes[1].kb_id, hits)
                if second and second.id != entry.id and report.causes[1].score >= 0.75 * report.causes[0].score:
                    steps += [dict(s, kb_id=second.id) for s in second.steps
                              if str(s.get("level", "")).upper() == "L1"]
                    entry_safety += list(second.safety)
        report.steps = safety.escalate_steps(steps)
        if report.history_ref and report.history_ref.get("resolved") == 0:
            report.steps.append({
                "level": "L3",
                "text": "上次同类故障按建议处理后仍未解决，建议直接升级：由工程师接管并复核上次处置记录。",
                "danger_hits": [],
                "safety": [],
                "kb_id": report.history_ref.get("fault_id", ""),
            })
            entry_safety.append("上次同类故障未解决，禁止反复复位报警继续生产。")
        report.warnings = safety.collect_warnings(entry_safety, report.steps)
        if parsed.danger_terms:
            report.warnings.insert(1, "描述中出现危险现象（" + "、".join(parsed.danger_terms) +
                                   "）：立即停机断电，禁止带电查看，由工程师处置。")

        # 记忆库落库（每次诊断都沉淀）
        try:
            report.record_id = get_memory().save_diagnosis({
                "created_at": report.ts,
                "device_id": report.device_id,
                "device_name": report.device_name,
                "device_type": report.device_type,
                "query": text,
                "parsed": report.parsed,
                "snapshot": {k: v for k, v in snapshot.items() if k != "evidences"},
                "evidence": report.abnormal_evidence,
                "conclusion": report.conclusion,
                "fault_id": top_id,
                "fault_title": entry.title if entry else "",
                "kb_ids": allowed_ids,
                # 步骤原文（含工具/物料/耗时）一并落库，历史页才能还原当时的完整勾选清单
                "steps": report.steps,
                "max_level": max((s["level"] for s in report.steps), default="L1"),
                "signature": report.signature,
                "mode": report.mode,
                "llm_used": report.mode == "llm",
                # 多工厂/多租户/操作人维度：工厂由设备目录推导，租户取部署环境，操作人来自调用方
                "factory_id": "",
                "operator": operator,
                "alarm_codes": report.alarm_codes,
            })
        except Exception as exc:
            report.llm_notes.append(f"记忆库写入失败：{exc}")
        return report

    # ---------- 辅助 ----------
    @staticmethod
    def _entry_of(kb_id: str, hits: List[KbHit]) -> Optional[KbEntry]:
        for h in hits:
            if h.entry.id == kb_id:
                return h.entry
        return get_kb().get(kb_id)

    def _try_llm(self, report: DiagnosisReport, text: str, snapshot: dict,
                 evidences: List[rules.Evidence], hits: List[KbHit],
                 allowed_ids: List[str], rule_causes: List[Cause]) -> bool:
        """尝试用大模型增强（只做理解、排序、语言组织）；失败或校验不通过则回退。"""
        if not self.llm.available:
            report.degraded_reasons.append(self.llm.reason)
            return False
        payload = {
            "device": {"id": report.device_id, "name": report.device_name, "type": report.device_type},
            "user_query": text,
            "parsed": report.parsed,
            "alarm_codes": report.alarm_codes,
            "evidence": report.abnormal_evidence or report.evidence[:3],
            "history": [
                {"fault_id": h.get("fault_id"), "conclusion": h.get("conclusion"),
                 "resolved": h.get("resolved")} for h in (report.history_ref and [report.history_ref] or [])
            ],
            "kb_candidates": [
                {"kb_id": h.entry.id, "title": h.entry.title, "conclusion": h.entry.conclusion,
                 "causes": h.entry.causes, "score": round(h.score, 3)} for h in hits
            ],
        }
        data = self.llm.chat_json(SYSTEM_PROMPT, json.dumps(payload, ensure_ascii=False))
        if data is None:
            report.degraded_reasons.append("大模型调用失败或超时，已回退规则版输出")
            report.llm_notes.append("LLM 未返回结果（超时/网络/不可解析）")
            return False
        errors = _validate_llm(data, allowed_ids)
        if errors:
            report.degraded_reasons.append("大模型输出未通过校验，已回退规则版输出：" + "；".join(errors))
            report.llm_notes.extend(errors)
            return False

        # 校验通过：仅采纳结论、原因排序与解释，步骤仍取自知识库
        report.mode = "llm"
        report.degraded = False
        report.conclusion = str(data.get("conclusion", report.conclusion)).strip() or report.conclusion
        report.narrative = str(data.get("narrative", "")).strip()
        understood = data.get("understood") or {}
        if isinstance(understood, dict) and understood.get("symptoms"):
            report.parsed["llm_symptoms"] = list(understood["symptoms"])[:5]

        order = {c.get("kb_id"): c.get("rank", 99) for c in data["causes"]}
        why = {c.get("kb_id"): str(c.get("why", "")) for c in data["causes"]}
        reordered = sorted(rule_causes, key=lambda c: (order.get(c.kb_id, 99), -c.score))
        for c in reordered:
            if why.get(c.kb_id):
                c.basis = f"{c.basis}；大模型补充：{why[c.kb_id]}"
        report.causes = reordered
        return True


def diagnose(text: str, device_id: Optional[str] = None, top_k: int = 3,
             operator: str = "") -> DiagnosisReport:
    """便捷入口：执行一次完整诊断。"""
    return DiagnosisAgent().diagnose(text, device_id=device_id, top_k=top_k, operator=operator)


def feedback(record_id: int, resolved: bool, comment: str = "", operator: str = "",
             handling_min: Optional[int] = None, downtime_min: Optional[int] = None) -> int:
    """用户反馈回填：'解决了/没解决' 写入记忆库并影响下次同类诊断排序。"""
    return get_memory().add_feedback(record_id, resolved, comment, operator=operator,
                                     handling_min=handling_min, downtime_min=downtime_min)
