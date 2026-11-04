"""当前变更的 API 契约测试（2026-10-25 返工轮新增）。

守护的变更点：
- ``src/cncops/api.py`` diagnose 响应 ``llm_used`` 字段（修复前任何接口都不返回该字段，
  增强模式是否生效无法从响应层验证）；
- ``src/cncops/api.py`` 历史详情序列化 ``llm_used`` 字段；
- 报告卡「可能原因排序」块（web/static/js/ui.js + app.css）的**数据契约**——
  该块的条件渲染依赖 ``causes`` 数组的字段齐全性与排序，浏览器级 DOM 断言由
  ``scripts/shot_diagnosis.py``（.cause-row，15/15）覆盖，本文件守护接口侧契约。

约定沿用 tests/conftest.py（独立临时库 + 默认关闭大模型）与
tests/test_knowledge_agent.py 的 StubLLM 测试替身模式（selftest G 段明文不 mock，
故增强模式的接口级验证放在本层）。
"""

import time

import pytest
from fastapi.testclient import TestClient

from cncops import agent as agent_mod
from cncops import datasource
from cncops.api import app
from cncops.llm_client import LLMClient

OVERHEAT_QUERY = "一号机主轴发烫，温度高得厉害"
DANGER_QUERY = "一号机主轴有焦糊味还冒烟"

ENHANCED_PAYLOAD = {
    "understood": {"device": "CNC-01", "symptoms": ["发烫", "温度高"]},
    "conclusion": "主轴温度越限，优先怀疑主轴过热导致发热。",
    "causes": [{"kb_id": "KB-CNC-001", "rank": 1, "why": "用户主诉主轴温度高"}],
    "narrative": "先复测主轴温度，再检查冷却回路。",
}


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


def _wait_alarm(device_id: str, rounds: int = 15) -> None:
    for _ in range(rounds):
        if datasource.read_snapshot(device_id)["alarm_codes"]:
            return
        time.sleep(0.05)


def _diagnose_overheat(c: TestClient) -> dict:
    """注入主轴过热并等待报警，走真实 HTTP 栈诊断，返回响应 JSON。"""
    datasource.inject_fault("CNC-01", "spindle_overheat")
    _wait_alarm("CNC-01")
    r = c.post("/api/diagnose", json={"query": OVERHEAT_QUERY})
    assert r.status_code == 200, f"诊断接口应 200，实得 {r.status_code}"
    return r.json()


# ---------------------------------------------------------------- llm_used 契约

def test_diagnose_llm_used_false_in_rule_mode():
    """降级模式（无 Key，conftest 已关闭 LLM）：响应必须显式声明 llm_used=False。

    修复前该字段根本不返回，调用方只能靠 mode 字符串猜测；
    且若派生写错（如降级也标 True），界面会假报"已增强"。
    """
    with TestClient(app) as c:
        rep = _diagnose_overheat(c)
        assert rep["mode"] == "rule"
        assert rep["degraded"] is True
        assert rep["llm_used"] is False


def test_diagnose_llm_used_true_in_enhanced_mode(monkeypatch):
    """增强模式：LLMClient 构造替换为 StubLLM，响应必须 llm_used=True。"""
    monkeypatch.setattr(agent_mod, "LLMClient", lambda: StubLLM(ENHANCED_PAYLOAD))
    with TestClient(app) as c:
        rep = _diagnose_overheat(c)
        assert rep["mode"] == "llm"
        assert rep["degraded"] is False
        assert rep["llm_used"] is True


def test_llm_used_always_consistent_with_mode(monkeypatch):
    """不变量：llm_used 必须严格等于 (mode == "llm")，两种模式各验一次。

    该派生式是 api.py diagnose 响应的实现细节，单独固化防止未来改出
    "mode 与 llm_used 互相矛盾"的第三态。
    """
    with TestClient(app) as c:
        rule_rep = _diagnose_overheat(c)
        assert rule_rep["llm_used"] == (rule_rep["mode"] == "llm")

        monkeypatch.setattr(agent_mod, "LLMClient", lambda: StubLLM(ENHANCED_PAYLOAD))
        llm_rep = _diagnose_overheat(c)
        assert llm_rep["mode"] == "llm"
        assert llm_rep["llm_used"] == (llm_rep["mode"] == "llm")


def test_history_detail_exposes_llm_used_rule_mode():
    """历史详情序列化必须返回 llm_used（布尔，False=规则模式）。"""
    with TestClient(app) as c:
        rep = _diagnose_overheat(c)
        detail = c.get(f"/api/history/{rep['record_id']}")
        assert detail.status_code == 200
        body = detail.json()
        assert "llm_used" in body and isinstance(body["llm_used"], bool)
        assert body["llm_used"] is False


def test_history_detail_llm_used_roundtrip_for_enhanced_record(monkeypatch):
    """增强模式的记录：落库 llm_used=1，历史详情回读必须为 True（端到端往返）。"""
    monkeypatch.setattr(agent_mod, "LLMClient", lambda: StubLLM(ENHANCED_PAYLOAD))
    with TestClient(app) as c:
        rep = _diagnose_overheat(c)
        assert rep["mode"] == "llm"
        detail = c.get(f"/api/history/{rep['record_id']}").json()
        assert detail["llm_used"] is True


# ---------------------------------------------------------------- 原因排序块数据契约

def test_diagnose_causes_contract_for_report_card():
    """「可能原因排序」块的接口契约：字段齐全、概率归一、Top-1 正确且排在首位。

    块按数组顺序渲染序号 1/2/3，故顺序即可能性顺序；
    缺 title/text/basis 任一字段都会让块静默缺内容（escape 后为空）。
    """
    with TestClient(app) as c:
        rep = _diagnose_overheat(c)
        causes = rep["causes"]
        assert causes, "命中已知故障时必须给出候选原因"
        for cause in causes:
            for field in ("kb_id", "title", "probability", "text", "basis"):
                assert field in cause, f"原因缺少报告卡渲染字段：{field}"
            assert 0.0 <= cause["probability"] <= 1.0
            assert cause["text"], "每条原因必须有正文"
        assert causes[0]["kb_id"] == "KB-CNC-001", "主轴过热的 Top-1 必须是 F001 条目"


def test_uncovered_diagnose_causes_stay_generic():
    """未覆盖问诊：causes 只允许通用兜底条目，不得把具体故障猜测塞进排序块。"""
    with TestClient(app) as c:
        r = c.post("/api/diagnose", json={"query": DANGER_QUERY})
        assert r.status_code == 200
        rep = r.json()
        assert rep["uncovered"] is True
        assert rep["causes"], "未覆盖时也应保留通用兜底原因供排序块展示"
        assert all(cause["kb_id"].startswith("KB-GEN") for cause in rep["causes"]), \
            f"未覆盖时出现非通用原因：{[c['kb_id'] for c in rep['causes']]}"
        assert rep["llm_used"] is False
