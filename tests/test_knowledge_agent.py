"""知识库检索 + 诊断工作流 + 历史复用 + 大模型降级/增强的测试。"""

import time

from cncops import datasource
from cncops.agent import DiagnosisAgent, _feedback_weight, feedback
from cncops.knowledge import get_kb
from cncops.llm_client import LLMClient
from cncops.memory import get_memory
from cncops.render import render_markdown


def _wait_alarm(device_id: str, rounds: int = 15) -> None:
    for _ in range(rounds):
        if datasource.read_snapshot(device_id)["alarm_codes"]:
            return
        time.sleep(0.05)


def test_kb_search_top3_and_uncovered():
    kb = get_kb()
    hits = kb.search("主轴发烫 冷却液 温度高", device_type="cnc", alarm_codes=["ALM-101"], top_k=3)
    assert 1 <= len(hits) <= 3
    assert hits[0].entry.id == "KB-CNC-001"

    empty = kb.search("设备外壳掉漆了", device_type="cnc", top_k=3)
    assert kb.is_uncovered(empty) or empty[0].entry.id.startswith("KB-GEN")


def test_workflow_output_has_all_seven_sections():
    """固定格式：七段结构必须齐全。"""
    datasource.inject_fault("CNC-02", "spindle_bearing_wear")
    _wait_alarm("CNC-02")
    report = DiagnosisAgent().diagnose("二号机声音不对还发烫")
    text = render_markdown(report)
    for section in ["## 一、异常结论", "## 二、数据证据", "## 三、可能原因排序",
                    "## 四、分级排查步骤", "## 五、安全警告", "## 六、知识库来源",
                    "## 七、该设备历史记录"]:
        assert section in text, f"缺少输出段落：{section}"
    assert report.abnormal_evidence, "应给出超标数据证据"
    assert report.causes and report.steps and report.warnings
    assert report.record_id, "诊断结果必须写入记忆库"


def test_steps_come_from_knowledge_base():
    """排查步骤必须来自知识库原文（不得由大模型或规则凭空生成）。"""
    datasource.inject_fault("CNC-02", "spindle_bearing_wear")
    _wait_alarm("CNC-02")
    report = DiagnosisAgent().diagnose("二号机声音不对还发烫")
    kb_texts = {s["text"] for e in get_kb().entries for s in e.steps}
    for step in report.steps:
        assert step["text"] in kb_texts, f"步骤不在知识库中：{step['text']}"


def test_history_reference_and_feedback_effect():
    """同类故障第二次出现时引用上次记录；未解决的反馈会触发升级建议。"""
    datasource.inject_fault("CNC-01", "spindle_bearing_wear")
    _wait_alarm("CNC-01")
    r1 = DiagnosisAgent().diagnose("机器不太对劲")
    assert r1.record_id
    feedback(r1.record_id, resolved=False, comment="换了刀具还是响")

    r2 = DiagnosisAgent().diagnose("一号机又开始了，跟上次一样")
    assert r2.history_ref is not None, "第二次同类故障应引用上一次处理记录"
    assert r2.history_ref["id"] == r1.record_id
    assert any("上次同类故障" in s["text"] for s in r2.steps), "未解决的历史应触发升级步骤"

    # 反馈影响排序权重
    ok, ng = get_memory().fault_feedback_stats("CNC-01", r1.causes[0].kb_id)
    assert (ok, ng) == (0, 1)
    weight, _ = _feedback_weight("CNC-01", r1.causes[0].kb_id)
    assert weight < 1.0, "未解决的反馈应降低该原因的下次排序权重"


class StubLLM(LLMClient):
    """测试替身：模拟大模型返回，避免测试依赖真实网络。"""

    def __init__(self, payload):
        self.payload = payload

    @property
    def available(self):
        return self.payload is not None

    @property
    def reason(self):
        return "stub"

    def chat_json(self, system, user):  # noqa: D102
        return self.payload


def test_llm_enhanced_mode_reorders_but_steps_still_from_kb():
    """大模型增强：可以改变结论与原因排序，但步骤仍必须出自知识库。"""
    datasource.inject_fault("CNC-02", "spindle_bearing_wear")
    _wait_alarm("CNC-02")
    payload = {
        "understood": {"device": "CNC-02", "symptoms": ["异响", "发烫"]},
        "conclusion": "主轴振动与温度同时越限，优先怀疑主轴轴承磨损导致发热。",
        "causes": [{"kb_id": "KB-CNC-002", "rank": 1, "why": "用户主诉为声音异常"},
                   {"kb_id": "KB-CNC-001", "rank": 2, "why": "温度同时越限"}],
        "narrative": "先按振动排查，同时监控温度。",
    }
    report = DiagnosisAgent(llm=StubLLM(payload)).diagnose("二号机声音不对还发烫")
    assert report.mode == "llm" and report.degraded is False
    assert report.causes[0].kb_id == "KB-CNC-002", "大模型排序应生效"
    kb_texts = {s["text"] for e in get_kb().entries for s in e.steps}
    assert all(s["text"] in kb_texts for s in report.steps), "步骤仍必须来自知识库"


def test_llm_invalid_output_falls_back_to_rule_mode():
    """大模型输出越界或不可解析时，必须回退规则版并保证格式不变。"""
    datasource.inject_fault("CNC-02", "spindle_bearing_wear")
    _wait_alarm("CNC-02")
    bad_payload = {"conclusion": "随便说说", "causes": [{"kb_id": "KB-编造-001", "rank": 1, "why": "模型臆造"}]}
    report = DiagnosisAgent(llm=StubLLM(bad_payload)).diagnose("二号机声音不对还发烫")
    assert report.mode == "rule", "校验失败必须回退规则版"
    assert report.degraded is True
    assert any("校验" in r for r in report.degraded_reasons)
    text = render_markdown(report)
    assert "## 四、分级排查步骤" in text and "当前为降级模式" in text


def test_degrade_mode_without_llm_key():
    """无 LLM Key 时自动进入降级模式，功能完整、格式不变。"""
    datasource.inject_fault("AIR-01", "air_pressure_low")
    _wait_alarm("AIR-01")
    report = DiagnosisAgent().diagnose("空压机没劲儿，压力表上不去")
    assert report.mode == "rule"
    assert report.degraded is True
    assert report.abnormal_evidence and report.causes and report.steps
    text = render_markdown(report)
    assert "## 二、数据证据" in text and "当前为降级模式" in text
