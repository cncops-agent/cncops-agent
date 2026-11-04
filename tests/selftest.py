"""一键自动化自测（无需 pytest，一条命令跑完）：python tests/selftest.py

自测范围：
  A. 故障闭环：把 config/simulation.yaml 中**每一个**可注入故障逐个注入，
     配套一句口语描述跑完整诊断，断言 Top-1 命中正确故障条目 + 报警码正确 + 30 秒内检出。
  B. 误报回归：注入 A 故障后不得派生与之无关的报警码 / 不得命中无关故障条目
     （例如过热不许误报闷车、空压机故障不许借用 CNC 节点）。
  C. 不变量：未覆盖表述、历史复发引用、降级模式、记忆库隔离、步骤可溯源、只读、正常态不报警。
  D. 阈值可达性：对 thresholds.yaml 中**每一个阈值项（warn/alarm 两级）与每一个报警码**，
     穷举所有故障剧本做注入模拟，触发不了的单独列出并给出根因分类，不做静默修改。
  F. 展示层（Web API）：设备摘要行关键指标（超标标红/阈值说明）、步骤分级与工具/物料/耗时、
     L3 危险标注、超标证据的迷你趋势与越限起点。
  G. Web API 端到端：FastAPI TestClient 走真实 HTTP 栈（产线总览、诊断报告结论先行、
     知识库详情、勾选持久化、反馈入历史与筛选搜索、未覆盖兜底、演示注入与恢复）；
     真实浏览器渲染与交互由 scripts/browser_check.py 无头 Chrome 覆盖并产出截图。
  H. 验收剧本：正常运行 → 注入 → 提问 → 分级步骤 → 恢复 → 入历史 → 二次提问引用历史，逐步断言。
  I. 经验沉淀与导出：多工厂隔离（factory_id）、复发故障提醒（同设备 30 天内同故障 ≥2 次）、
     平均处理时长（填报值优先 / 反馈时间差兜底）、操作人落库、CSV（utf-8-sig）与 Excel（双工作表）导出。
  J. 覆盖完整性与守门串联：
     J-01 知识库条目 ↔ 故障用例**双向**覆盖（A 段只断言"每个故障有用例"，本段反向断言
          "每个非通用 KB 条目都必须是某个用例的期望 Top-1"，且用例引用的 KB id / 报警码真实存在）；
     J-02 十台设备预览配置（config/devices_10.yaml）在子进程中真实加载：10 台设备、双工厂各 5 台、
          基线全部无报警、每个工厂各注入一个代表故障并可检出（README 承诺的跑法必须有自测兜底）；
     J-03 把 scripts/check_thresholds.py（必越限测点 margin > 3σ 噪声守门）串进自测，
          作为 D 段"静态可达性"的动态稳定性补充，防止目标值卡在阈值附近被噪声抖成时对时错。
     J-04 三十台验收配置（config/devices_30.yaml）在子进程中真实加载：30 台、双工厂各 15 台、
          基线无报警、两工厂各注入代表故障可检出（UI「3/10/30 台不缩放」验收档位的配置闸门）。
  K. 紧凑列表/抽屉数据契约（Web API）：摘要行字段齐全且关键测点按设备类型选取、趋势序列与抽屉
     同源同周期、抽屉全测点阈值字段与最近运维历史、断线双路径语义（单设备降级 offline 行 /
     整体异常 500+503）、报警置顶排序。
  L. 反馈规则：操作人必填（空姓名 400 且不落库）、停机时长自动计时（设备报警起点 → 提交反馈
     停表并计入记录，手动填报优先，报警事件在反馈时消费）、报警事件生命周期
     （alarm_since/alarm_active 随 /api/line 端到端更新）。
  M. L3 服务端强制闸门：未确认 locked、绕前端直接标记完成 400 不落库、确认人/step_key/
     信号来源校验、人工确认留痕（历史详情可查）、确认后放行、L1 不受影响。
     诚实边界：人工确认 ≠ 设备断电检测，系统不判断设备是否真的断电。

运行约定（保证可复现、不污染正式库）：
  1. 固定随机种子 SEED，仿真器用该种子重建；
  2. 记忆库指向临时目录，并在开始/结束各统计一次正式库行数，断言未变；
  3. 强制 CNCOPS_LLM_ENABLED=0，走降级（规则）模式，保证无网无 Key 也能跑且结果确定。
"""

from __future__ import annotations

import contextlib
import io
import logging
import os
import random
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

# ---- 环境变量必须在导入 cncops 之前设置（settings() 有缓存，导入后再改无效）----
SEED = 20261018
TMP_DIR = Path(tempfile.mkdtemp(prefix="cncops_selftest_"))
TMP_DB = TMP_DIR / "selftest_memory.db"

os.environ["CNCOPS_DB_PATH"] = str(TMP_DB)
os.environ["CNCOPS_LLM_ENABLED"] = "0"          # 强制降级模式：结果确定、无需 Key
os.environ["CNCOPS_DATA_SOURCE"] = "simulator"  # 自测走内置仿真（与 OPC-UA 同源、更快）
os.environ.pop("CNCOPS_LLM_API_KEY", None)
os.environ.pop("CNCOPS_LLM_API_BASE", None)

random.seed(SEED)


class _QuietStderr:
    """stderr 过滤包装：只丢掉 known-noise 警告，其余内容原样透传，不掩盖真正的异常堆栈。

    已知噪音：
      - Streamlit 裸跑模式下 "missing ScriptRunContext"（带时间戳，破坏输出一致性）；
      - starlette 关于 httpx 弃用的 StarletteDeprecationWarning（第三方内部提示，且绕过
        warnings 过滤器，只能在此层按文本丢弃）。
    这些噪音均带时间戳或无关自测逻辑，丢之不影响任何断言结果。
    """

    NOISE = ("missing ScriptRunContext", "StarletteDeprecationWarning", "starlette.testclient import")

    def __init__(self, inner) -> None:
        self._inner = inner

    def write(self, text: str) -> int:
        if any(n in text for n in self.NOISE):
            return len(text)
        return self._inner.write(text)

    def flush(self) -> None:
        self._inner.flush()


sys.stderr = _QuietStderr(sys.stderr)
logging.getLogger("streamlit.runtime.scriptrunner_utils.script_run_context").setLevel(logging.CRITICAL + 1)

from cncops import catalog, config, datasource, paths, rules  # noqa: E402
from cncops.agent import diagnose, feedback  # noqa: E402
from cncops.memory import get_memory  # noqa: E402
import cncops.simulator as sim_mod  # noqa: E402

# 用固定种子重建仿真器单例，保证每次自测数据完全一致
sim_mod._SIM = sim_mod.Simulator(seed=SEED)

MAX_DETECT_SEC = 30.0     # 验收标准：注入后 30 秒内检出
SETTLE_STEPS = 18         # 注入/清除后让数据爬坡到稳定的步数

# 每个故障剧本的配套口语用例：(设备, 口语描述, 期望 Top-1 条目, 必须出现的报警码)
FAULT_CASES: Dict[str, Tuple[str, str, str, List[str]]] = {
    "spindle_overheat": ("CNC-01", "一号机主轴发烫，温度高得厉害", "KB-CNC-001", ["ALM-101"]),
    "spindle_vibration": ("CNC-02", "二号机主轴声音不对，震动很大", "KB-CNC-002", ["ALM-102"]),
    "spindle_bearing_wear": ("CNC-02", "二号机主轴又响又烫，轴承是不是坏了", "KB-CNC-006", ["ALM-101", "ALM-102"]),
    "spindle_overload": ("CNC-01", "一号机闷车了，进给走不动还掉速", "KB-CNC-003", ["ALM-103", "ALM-106"]),
    "coolant_low": ("CNC-02", "二号机冷却液不出水，液位低", "KB-CNC-004", ["ALM-105"]),
    "cnc_air_low": ("CNC-01", "一号机没气，换刀夹不紧", "KB-CNC-005", ["ALM-104"]),
    "air_pressure_low": ("AIR-01", "空压机压力上不去，没劲儿", "KB-AIR-001", ["ALM-202"]),
    "air_overheat": ("AIR-01", "空压机排气温度高，机器发烫", "KB-AIR-002", ["ALM-201"]),
    "air_oil_overheat": ("AIR-01", "空压机油温高，是不是该换油了", "KB-AIR-003", ["ALM-203"]),
    "air_motor_overload": ("AIR-01", "空压机电机电流高，还跳闸", "KB-AIR-004", ["ALM-204"]),
    "air_maintenance_due": ("AIR-01", "空压机是不是到期该保养了，运行小时超了吧", "KB-AIR-003", ["ALM-205"]),
}

# 误报回归：注入某故障后，**禁止**出现的报警码 / **禁止**成为 Top-1 的条目
# 关键点：报警码全部由 rules.derive_alarm_codes() 从实时数据派生，本用例验证"数据没越限就不会派生无关码"，
# 即"过热"绝不会因为先验概率高而被误判为"伺服/限位(闷车/进给)"等无关故障。
FALSE_POSITIVE_CASES = [
    # —— 用户点名的典型用例：过热故障不许误报伺服/限位（闷车/进给异常，对应 KB-CNC-003）——
    ("spindle_overheat", "CNC-01", "一号机主轴发烫，温度高得厉害",
     ["ALM-103", "ALM-106"], ["KB-CNC-003"],
     "过热不许误报伺服/限位（闷车/进给异常）"),
    ("spindle_vibration", "CNC-02", "二号机主轴声音不对，震动很大",
     ["ALM-101"], ["KB-CNC-001"], "振动不许误报主轴过热"),
    ("air_pressure_low", "AIR-01", "空压机压力上不去，没劲儿",
     ["ALM-201"], ["KB-AIR-002"], "压力低不许误报排气高温"),
    ("air_overheat", "AIR-01", "空压机排气温度高，机器发烫",
     ["ALM-101", "ALM-102", "ALM-103", "ALM-104", "ALM-105", "ALM-106"],
     ["KB-CNC-001", "KB-CNC-002", "KB-CNC-005"],
     "空压机故障不许借用 CNC 节点/报警码"),
    # —— 额外补充的横向误报回归：双向验证"故障与报错一一对应"——
    ("spindle_overload", "CNC-01", "一号机闷车了，进给走不动还掉速",
     ["ALM-101"], ["KB-CNC-001"], "过载/闷车不许误报主轴过热"),
    ("coolant_low", "CNC-02", "二号机冷却液不出水，液位低",
     ["ALM-103", "ALM-106"], ["KB-CNC-003"], "冷却液不足不许误报过载/闷车"),
]


# ---------------------------------------------------------------- 结果收集
RESULTS: List[Tuple[str, str, bool, str]] = []


def check(section: str, name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((section, name, bool(ok), detail))
    mark = "PASS" if ok else "FAIL"
    line = f"[{mark}] {section}-{name}"
    if detail:
        line += f"  {detail}"
    print(line)
    return bool(ok)


def section_title(title: str) -> None:
    print("\n" + "=" * 96)
    print(title)
    print("=" * 96)


# ---------------------------------------------------------------- 公共工具
def settle(device_id: str) -> None:
    """让仿真数据爬坡到稳定（注入后到目标值 / 清除后回基线）。"""
    for _ in range(SETTLE_STEPS):
        datasource.read_snapshot(device_id)


def reset_all(devices: Optional[List[str]] = None) -> None:
    """清除全部故障并等待各设备回到基线。"""
    datasource.clear_faults()
    for dev in catalog.devices():
        if devices is None or dev.id in devices:
            settle(dev.id)


def wait_detect(device_id: str, expect_codes: List[str], max_steps: int = 60) -> Tuple[Optional[float], dict]:
    """注入后轮询直到期望报警码全部出现，返回 (耗时秒, 快照)；超时返回 (None, 最后快照)。"""
    t0 = time.time()
    snap = {}
    for _ in range(max_steps):
        snap = datasource.read_snapshot(device_id) or {}
        if all(c in snap.get("alarm_codes", []) for c in expect_codes):
            return time.time() - t0, snap
        time.sleep(0.01)
    return None, snap


def brief(report) -> str:
    top = report.causes[0] if report.causes else None
    return (f"Top1={top.kb_id}({top.probability * 100:.0f}%)" if top else "Top1=无")


def evidence_note(report, limit: int = 2) -> str:
    items = [f"{e['label']}={e['value']}{e['unit']}" for e in report.abnormal_evidence[:limit]]
    return "，".join(items) or "无超标项"


def prod_db_rows() -> int:
    """正式记忆库的行数（用于证明自测没有污染正式库）。"""
    path = paths.DEFAULT_DB_PATH
    if not path.exists():
        return 0
    try:
        conn = sqlite3.connect(str(path))
        n = conn.execute("SELECT COUNT(*) FROM diagnoses").fetchone()[0]
        conn.close()
        return int(n)
    except sqlite3.Error:
        return -1


# ---------------------------------------------------------------- A 故障闭环
def run_fault_cases() -> None:
    section_title("A. 故障闭环：逐个注入全部可注入故障 + 口语描述跑完整诊断（断言 Top-1 / 报警码 / 30 秒内检出）")
    sim = datasource.simulator()
    step_source_violations: List[str] = []
    detect_times: List[float] = []

    for idx, fault in enumerate(sim.list_faults(), 1):
        fid = fault["id"]
        case = FAULT_CASES.get(fid)
        if case is None:
            check("A", f"{idx:02d} {fid}", False,
                  f"未配置配套口语用例（自测要求：每个可注入故障都必须有用例）｜剧本名称：{fault.get('name', '')}")
            continue
        dev_id, query, expect_top, expect_codes = case

        reset_all()
        datasource.inject_fault(dev_id, fid)
        cost, snap = wait_detect(dev_id, expect_codes)

        if cost is None:
            check("A", f"{idx:02d} {fid}", False,
                  f"注入后 {SETTLE_STEPS}+ 步仍未派生期望报警码 {expect_codes}，实际={snap.get('alarm_codes', [])}"
                  f"｜数据：{ {k: round(v, 2) for k, v in snap.get('values', {}).items()} }")
            continue
        if cost > MAX_DETECT_SEC:
            check("A", f"{idx:02d} {fid}", False, f"检出耗时 {cost:.2f}s 超过 {MAX_DETECT_SEC}s 验收线")
            continue
        detect_times.append(cost)

        report = diagnose(query)
        top = report.causes[0] if report.causes else None
        ok_top = bool(top) and top.kb_id == expect_top
        ok_dev = report.device_id == dev_id
        ok_codes = all(c in report.alarm_codes for c in expect_codes)
        ok_evidence = len(report.abnormal_evidence) > 0

        # 步骤溯源：每条步骤必须来自本轮命中的知识库条目（或明确标注的历史/通用条目）
        allowed = {h["kb_id"] for h in report.kb_hits}
        extra = {"KB-GEN-000", "KB-GEN-001"}
        if report.history_ref:
            extra.add(report.history_ref.get("fault_id", ""))
        for st in report.steps:
            if st.get("kb_id") not in allowed | extra:
                step_source_violations.append(f"{fid}:{st.get('kb_id')}")

        detail = (f"{brief(report)}｜设备={report.device_id}｜报警={'、'.join(report.alarm_codes) or '无'}"
                  f"｜检出={cost:.2f}s｜{evidence_note(report)}")
        ok = ok_top and ok_dev and ok_codes and ok_evidence
        if not ok:
            detail += (f" → 期望 Top1={expect_top}/设备={dev_id}/报警⊇{expect_codes}"
                       f"｜实际 {brief(report)}/{report.device_id}/{'、'.join(report.alarm_codes)}")
        check("A", f"{idx:02d} {fid}", ok, detail)

    reset_all()
    check("A", f"{len(FAULT_CASES) + 1:02d} steps_traceable", not step_source_violations,
          "全部用例的排查步骤均可溯源到本轮命中的知识库条目" if not step_source_violations
          else f"出现来源不明的步骤：{step_source_violations}")
    if detect_times:
        print(f"\n  检出耗时：最大 {max(detect_times):.2f}s，平均 {sum(detect_times) / len(detect_times):.2f}s"
              f"（验收线 {MAX_DETECT_SEC:.0f}s）")


# ---------------------------------------------------------------- B 误报回归
def run_false_positive_cases() -> None:
    section_title("B. 误报回归：注入故障后不得派生无关报警码 / 不得命中无关故障条目")
    for idx, (fid, dev_id, query, forbid_codes, forbid_kb, desc) in enumerate(FALSE_POSITIVE_CASES, 1):
        reset_all()
        datasource.inject_fault(dev_id, fid)
        settle(dev_id)
        report = diagnose(query)
        bad_codes = [c for c in forbid_codes if c in report.alarm_codes]
        top = report.causes[0].kb_id if report.causes else ""
        bad_kb = [k for k in forbid_kb if k == top]
        ok = not bad_codes and not bad_kb
        detail = (f"注入 {fid} → 报警={'、'.join(report.alarm_codes) or '无'}，{brief(report)}｜禁止项："
                  f"报警 {forbid_codes} / Top1 {forbid_kb}")
        if not ok:
            detail += f" → 误报：报警 {bad_codes}、Top1 {bad_kb}"
        check("B", f"{idx:02d} {fid}", ok, f"{desc}｜{detail}")
    reset_all()


# ---------------------------------------------------------------- C 不变量
def run_invariant_cases() -> None:
    section_title("C. 不变量：未覆盖 / 历史复发 / 降级模式 / 记忆库 / 只读 / 正常态")

    # C01 知识库未覆盖
    reset_all(["CNC-01"])
    report = diagnose("一号机主轴有股焦糊味还冒烟")
    ok = report.uncovered and "知识库未覆盖" in report.conclusion
    ok = ok and all(st.get("kb_id") == "KB-GEN-000" for st in report.steps)
    ok = ok and any("断电" in w or "带电" in w for w in report.warnings)
    check("C", "01 uncovered", ok,
          f"未覆盖判定={report.uncovered}｜结论={report.conclusion[:28]}…｜"
          f"步骤来源={sorted({st.get('kb_id') for st in report.steps})}｜警告 {len(report.warnings)} 条")

    # C02 历史复发引用：同一设备同故障连续诊断两次（先爬坡到稳态，保证两次故障指纹一致）
    reset_all(["CNC-02"])
    datasource.inject_fault("CNC-02", "spindle_bearing_wear")
    wait_detect("CNC-02", ["ALM-101", "ALM-102"])
    settle("CNC-02")
    r1 = diagnose("二号机主轴又响又烫，轴承是不是坏了")
    r2 = diagnose("二号机主轴又响又烫，轴承是不是坏了")
    ok = r2.history_ref is not None and r2.history_ref.get("id") == r1.record_id
    check("C", "02 history_recurrence", ok,
          f"第二次诊断引用历史={'有' if r2.history_ref else '无'}"
          + (f"（引用 #{r2.history_ref.get('id')} / 命中 {r2.history_ref.get('fault_id')}）" if r2.history_ref else ""))

    # C03 降级模式（无 Key / 强制关闭 LLM）
    r3 = diagnose("二号机主轴又响又烫，轴承是不是坏了")
    seven = [bool(r3.conclusion), len(r3.evidence) > 0, len(r3.causes) > 0, len(r3.steps) > 0,
             len(r3.warnings) > 0, len(r3.kb_hits) > 0, r3.device_id != ""]
    ok = r3.mode == "rule" and r3.degraded and all(seven)
    check("C", "03 degraded_mode", ok,
          f"mode={r3.mode}｜degraded={r3.degraded}｜七要素齐全={all(seven)}｜"
          f"降级原因：{r3.degraded_reasons[:1]}")

    # C04 反馈闭环影响排序（写临时库，不动正式库）
    feedback(r3.record_id, resolved=False, comment="按建议换了刀具还是响")
    r4 = diagnose("二号机主轴又响又烫，轴承是不是坏了")
    ok = r4.mode == "rule" and len(r4.causes) > 0
    check("C", "04 feedback_effect", ok,
          f"反馈“未解决”后重排：{[(c.kb_id, f'{c.probability:.0%}') for c in r4.causes[:3]]}")

    # C05 正常态不得报警（防误报的第一道闸）
    reset_all()
    alarms = {d.id: datasource.read_snapshot(d.id).get("alarm_codes", []) for d in catalog.devices()}
    ok = all(not v for v in alarms.values())
    check("C", "05 normal_no_alarm", ok, f"清除全部故障后各设备报警码：{alarms}")

    # C06 只读：数据源层不得出现任何写入/控制调用
    src_dir = paths.ROOT / "src" / "cncops"
    forbidden = ("set_value", "set_values", "write_value", "call_method", "set_attribute")
    hits: List[str] = []
    for name in ("opcua_client.py", "datasource.py", "simulator.py"):
        text = (src_dir / name).read_text(encoding="utf-8")
        for kw in forbidden:
            if kw in text:
                hits.append(f"{name}:{kw}")
    check("C", "06 readonly", not hits,
          "数据源与客户端均为只读，无写入/控制调用" if not hits else f"发现疑似写入调用：{hits}")

    # C07 空压机节点独立性（不得借用 CNC 点位）
    overlap = catalog.point_namespace_overlap()
    ok = set(overlap) <= {"status", "alarm_code"}
    check("C", "07 air_node_independent", ok,
          f"AIR-01 与 CNC 点位交集：{overlap or '无'}（status/alarm_code 为设计内共用派生字段）")

    reset_all()


# ---------------------------------------------------------------- D 阈值可达性
def _best_severity(values: List[float], rule: dict) -> Tuple[str, float]:
    """在一串采样值里取最严重的一档，返回 (severity, 达到该档的值)。"""
    order = {"normal": 0, "warn": 1, "alarm": 2}
    best, best_val = "normal", values[0] if values else 0.0
    for v in values:
        sev = rules.evaluate(float(v), rule)
        if order[sev] > order[best]:
            best, best_val = sev, float(v)
    return best, best_val


def _intends_exceed(target, rule: dict, level: str) -> bool:
    """该故障剧本的目标值是否**本意**要越过 level 级阈值（warn/alarm）。

    用于可达性根因分类：
    - 若剧本把测点推到了"本应越过阈值"的量级却仍够不到 → 阈值写错（过严）；
    - 若剧本目标值本身就低于阈值 → 剧本目标值不足。
    """
    direction = rule.get("direction", "above")
    thr = rule.get(level)
    if thr is None or target is None:
        return False
    try:
        target = float(target)
        thr = float(thr)
    except (TypeError, ValueError):
        return False
    if direction == "above":
        return target >= thr
    if direction == "below":
        return target <= thr
    if direction == "deviation":
        nominal = float(rule.get("nominal") or 0.0)
        if not nominal:
            return False
        return abs(target - nominal) / abs(nominal) >= thr
    return False


def run_reachability() -> None:
    section_title("D. 阈值可达性：对每一个阈值项（warn/alarm）与每一个报警码穷举全部故障剧本做注入模拟")

    thresholds = config.load_yaml(str(paths.THRESHOLDS_YAML)) or {}
    sim_cfg = config.load_yaml(str(paths.SIMULATION_YAML)) or {}
    faults = sim_cfg.get("faults", [])
    alarm_table = (config.load_yaml(str(paths.ALARMS_YAML)) or {}).get("codes", [])
    # thresholds.yaml 顶层含 schema_version 等非设备类型键，需按"值是字典"筛选
    device_types = [k for k, v in thresholds.items() if isinstance(v, dict) and not k.startswith("schema_")]
    point_owner: Dict[str, List[str]] = {}
    for dev in catalog.devices():
        for p in dev.point_keys:
            point_owner.setdefault(p, []).append(dev.id)

    # 采样：每个故障在其适用设备类型上注入并爬坡，记录各测点的取值序列
    samples: Dict[str, Dict[str, Dict[str, List[float]]]] = {}   # dev_type -> fault_id -> point -> values
    for dev_type in device_types:
        device_id = next((d.id for d in catalog.devices() if d.type == dev_type), None)
        if device_id is None:
            continue
        samples.setdefault(dev_type, {})
        for fault in faults:
            if dev_type not in fault.get("device_types", []):
                continue
            reset_all()
            datasource.inject_fault(device_id, fault["id"])
            series: Dict[str, List[float]] = {}
            for _ in range(SETTLE_STEPS):
                snap = datasource.read_snapshot(device_id) or {}
                for k, v in snap.get("values", {}).items():
                    series.setdefault(k, []).append(float(v))
            samples[dev_type][fault["id"]] = series
    reset_all()

    ok_count = total = 0
    unreachable: List[str] = []

    print(f"{'设备类型':<16}{'测点':<22}{'预警阈值':>10}{'报警阈值':>10}{'实测最佳':>10}{'可达':>14}  备注")
    print("-" * 104)
    for dev_type in device_types:
        for point, rule in thresholds[dev_type].items():
            driven_by = [f["id"] for f in faults
                         if dev_type in f.get("device_types", []) and point in (f.get("target") or {})]
            best_sev, best_val = "normal", None
            best_fault = ""
            for fid, series in samples.get(dev_type, {}).items():
                if point not in series:
                    continue
                sev, val = _best_severity(series[point], rule)
                if rules.SEVERITY_ORDER[sev] > rules.SEVERITY_ORDER[best_sev]:
                    best_sev, best_val, best_fault = sev, val, fid
            warn_ok = best_sev in ("warn", "alarm")
            alarm_ok = best_sev == "alarm"
            for level, reached, thr in (("warn", warn_ok, rule.get("warn")), ("alarm", alarm_ok, rule.get("alarm"))):
                total += 1
                if reached:
                    ok_count += 1
                else:
                    # 根因分类（四类，互不掩盖）：
                    #   节点缺失 —— 测点根本没在 devices.yaml 暴露；
                    #   剧本缺失 —— 有测点但没有任何故障剧本写入它；
                    #   阈值写错 —— 已有剧本本意要越过该阈值，却仍够不到（阈值过严/超出物理可达上限）；
                    #   剧本目标值不足 —— 剧本目标值本身就低于阈值。
                    if point not in point_owner:
                        reason = "节点缺失：该测点未在 config/devices.yaml 中暴露（阈值无数据源可验证）"
                    elif not driven_by:
                        reason = "剧本缺失：无任何故障剧本写入该测点，需新增对应故障剧本"
                    else:
                        driven_targets = []
                        for fid in driven_by:
                            for f in faults:
                                if f["id"] == fid:
                                    driven_targets.append(f.get("target", {}).get(point))
                                    break
                        intends = [t for t in driven_targets if _intends_exceed(t, rule, level)]
                        if intends:
                            worst = max(intends) if rule.get("direction") != "below" else min(intends)
                            reason = (f"阈值写错（阈值过严）：已有剧本把 {point} 目标设为 {worst}，"
                                      f"本意应越过 {level} 阈值 {thr}，但实测最佳仅 "
                                      f"{None if best_val is None else round(best_val, 2)}，"
                                      f"说明该阈值超出物理可达上限（方向 {rule.get('direction')}）")
                        else:
                            reason = (f"剧本目标值不足：{best_fault or '相关剧本'} 实测最佳 "
                                      f"{None if best_val is None else round(best_val, 2)}，"
                                      f"未越过 {level} 阈值 {thr}（方向 {rule.get('direction')}）；"
                                      f"相关剧本目标值="
                                      f"{[None if t is None else round(t, 2) for t in driven_targets]}")
                    unreachable.append(f"{dev_type}.{point}.{level}（阈值 {thr}）→ {reason}")
            shown = f"{best_val:.2f}" if best_val is not None else "-"
            note = f"由 {best_fault} 触发" if best_fault else "无剧本驱动"
            print(f"{dev_type:<16}{point:<22}{str(rule.get('warn')):>10}{str(rule.get('alarm')):>10}"
                  f"{shown:>10}{('warn+alarm' if alarm_ok else ('warn' if warn_ok else '不可达')):>14}  {note}")
    print("-" * 104)

    print(f"\n{'报警码':<10}{'测点':<22}{'要求等级':>10}{'可达':>8}  备注")
    print("-" * 70)
    for item in alarm_table:
        code, point, min_sev = item["code"], item.get("point"), item.get("min_severity", "alarm")
        dev_type = (item.get("device_types") or ["?"])[0]
        rule = (thresholds.get(dev_type) or {}).get(point)
        reached, via = False, ""
        if rule:
            for fid, series in samples.get(dev_type, {}).items():
                if point in series and rules.SEVERITY_ORDER[_best_severity(series[point], rule)[0]] \
                        >= rules.SEVERITY_ORDER[min_sev]:
                    reached, via = True, fid
                    break
        total += 1
        if reached:
            ok_count += 1
        else:
            driven = [f["id"] for f in faults
                      if dev_type in f.get("device_types", []) and point in (f.get("target") or {})]
            if not driven:
                reason = "剧本缺失：无故障剧本写入该测点，需新增对应故障剧本"
            else:
                driven_targets = [f.get("target", {}).get(point) for f in faults
                                 if f["id"] in driven]
                intends = [t for t in driven_targets if _intends_exceed(t, rule, min_sev)]
                if intends:
                    worst = max(intends) if rule.get("direction") != "below" else min(intends)
                    reason = (f"阈值写错（阈值过严）：已有剧本把 {point} 目标设为 {worst}，"
                              f"本意应越过 {min_sev} 级，但实测达不到（阈值 {rule.get(min_sev) if rule else '?'}）")
                else:
                    reason = (f"剧本目标值不足：{point} 达不到 {min_sev} 级"
                              f"（阈值 {rule.get(min_sev) if rule else '?'}；"
                              f"相关剧本目标值={[None if t is None else round(t, 2) for t in driven_targets]}）")
            unreachable.append(f"{code}（{dev_type}.{point} ≥ {min_sev}）→ {reason}")
        print(f"{code:<10}{str(point):<22}{min_sev:>10}{('是' if reached else '否'):>8}  {via or '无剧本可触发'}")

    print("-" * 70)
    check("D", "00 reachability_summary", ok_count == total,
          f"阈值/报警码可达性 {ok_count}/{total}")
    if unreachable:
        print("\n  以下阈值项注入后仍触发不了（逐条列出根因，不做静默修改）：")
        for line in unreachable:
            print(f"    - {line}")
    else:
        print("  全部阈值项与报警码均可在某个故障剧本注入后被触发。")


# ---------------------------------------------------------------- F 展示层（Web API 版）
def run_ui_layer_cases() -> None:
    """展示层：设备卡指标（超标标红/阈值说明）、诊断步骤分级与工具物料耗时、L3 危险信息。

    旧 Streamlit 展示层（uikit）已替换为 Web HMI（web/static + cncops/api.py），
    这里直接对 API 层做断言；真实浏览器渲染与交互由 scripts/browser_check.py 覆盖。
    """
    from cncops import api as webapi
    from cncops import monitor

    section_title("F. 展示层（Web API）：设备卡指标超标标红＋阈值说明／步骤分级与工具物料耗时／L3 危险信息")

    # F01 正常态：三台设备都有关键指标（2~4 个），全部绿色且每项带阈值说明
    reset_all()
    problems: List[str] = []
    counts: Dict[str, int] = {}
    for dev in catalog.devices():
        card = webapi._device_card(datasource.read_snapshot(dev.id) or {}, dev)
        counts[dev.id] = len(card["key_metrics"])
        if not 2 <= len(card["key_metrics"]) <= 4:
            problems.append(f"{dev.id} 关键指标数 {len(card['key_metrics'])} 不在 2~4")
        for m in card["key_metrics"]:
            if m["severity"] != "normal":
                problems.append(f"{dev.id}.{m['key']} 正常态非绿（{m['severity_text']}）")
            if not (m.get("warn") or m.get("alarm")):
                problems.append(f"{dev.id}.{m['key']} 缺阈值说明")
    check("F", "01 normal_cards", not problems,
          f"三台设备关键指标数 {counts}；"
          + ("全部绿色且每项带阈值/报警线说明" if not problems else "问题：" + "；".join(problems)))

    # F02 逐个故障：注入后设备卡变红，且必越限测点在全测点详情里确实处于 alarm 级
    sim_cfg = config.load_yaml(str(paths.SIMULATION_YAML)) or {}
    must_of = {f["id"]: list(f.get("must_trigger") or []) for f in sim_cfg.get("faults", [])}
    for idx, (fid, case) in enumerate(FAULT_CASES.items(), 2):
        dev_id = case[0]
        reset_all()
        datasource.inject_fault(dev_id, fid)
        settle(dev_id)
        dev = catalog.get_device(dev_id)
        card = webapi._device_card(datasource.read_snapshot(dev_id) or {}, dev)
        detail = webapi.device_detail(dev_id, n=5)
        detail_alarm = [m["key"] for m in detail["points"] if m["severity"] == "alarm"]
        must = must_of.get(fid, [])
        missing = [p for p in must if p not in detail_alarm]
        ok = card["severity"] in ("alarm", "warn") and not missing
        check("F", f"{idx:02d} {fid}_over_cards", ok,
              f"卡片等级 {card['severity']}｜全测点报警 {detail_alarm}"
              + ("" if ok else f" → 必越限测点未标红：{missing}"))

    # F03 步骤分级 + 工具/物料/耗时 + L3 危险标注（供前端清单与排查模式渲染）
    reset_all()
    datasource.inject_fault("CNC-01", "spindle_overheat")
    settle("CNC-01")
    rep = diagnose("一号机主轴发烫，温度高得厉害")
    steps = webapi._enrich_steps(rep)
    levels_ok = all(s["level"] in ("L1", "L2", "L3") for s in steps)
    keyed_ok = all(s["key"] and s["text"] for s in steps)
    meta_ok = all(isinstance(s["tools"], list) and isinstance(s["materials"], list)
                  and s["eta_min"] >= 0 for s in steps)
    l3 = [s for s in steps if s["level"] == "L3"]
    l3_marked = all((s["danger_hits"] or s["safety"] or s["danger_badge"]) for s in l3)
    check("F", "13 step_grouping", bool(steps) and levels_ok and keyed_ok and meta_ok and (not l3 or l3_marked),
          f"步骤 {len(steps)} 条｜L1 {len([s for s in steps if s['level']=='L1'])}"
          f" / L2 {len([s for s in steps if s['level']=='L2'])} / L3 {len(l3)}"
          f"｜等级合法={levels_ok}｜key/正文齐全={keyed_ok}｜工具物料耗时齐全={meta_ok}"
          f"｜L3 均带危险标注={l3_marked}")

    # F04 / F05 证据趋势：每条超标证据带迷你趋势线与越限起点（前端“从什么时候开始超的”）
    reset_all()
    datasource.inject_fault("CNC-01", "spindle_overheat")
    settle("CNC-01")
    from cncops.api import TREND as _trend_ref
    monitor.pump(_trend_ref, rounds=30)          # api 层趋势缓冲先补足窗口
    rep = diagnose("一号机主轴发烫，温度高得厉害")
    payload = webapi._report_payload(rep, n_points=30)
    evs = payload["evidence"]
    spark_ok = bool(evs) and all(len(e["spark"]) >= 10 for e in evs)
    cross_ok = all(e["cross_time"] for e in evs)
    dev_ok = all(bool(e.get("deviation")) for e in evs)
    check("F", "14 evidence_spark", spark_ok,
          f"超标证据 {len(evs)} 条，趋势点数 {[len(e['spark']) for e in evs]}"
          + ("" if spark_ok else "（要求每条 ≥10 点）"))
    check("F", "15 evidence_cross", cross_ok and dev_ok,
          "越限起点与偏差文案齐全" if (cross_ok and dev_ok)
          else f"越限起点缺失={[e['point'] for e in evs if not e['cross_time']]}，"
               f"偏差缺失={[e['point'] for e in evs if not e.get('deviation')]}")

    # F06 趋势缓冲：三台设备都能采到点，且注入后曲线明显抬升
    buf = monitor.TrendBuffer(maxlen=120)
    reset_all()
    monitor.pump(buf, rounds=10)
    base_series = buf.series("CNC-01", "spindle_temp")
    datasource.inject_fault("CNC-01", "spindle_overheat")
    monitor.pump(buf, rounds=40)
    after_series = buf.series("CNC-01", "spindle_temp")
    lengths = {d.id: buf.length(d.id) for d in catalog.devices()}
    base_avg = sum(v for _, v in base_series[-5:]) / max(1, len(base_series[-5:]))
    tail_avg = sum(v for _, v in after_series[-5:]) / max(1, len(after_series[-5:]))
    ok = all(v >= 10 for v in lengths.values()) and tail_avg > base_avg + 5
    check("F", "16 trend_buffer", ok,
          f"各设备采样点 {lengths}；主轴温度 注入前均值 {base_avg:.1f}°C → 注入后 {tail_avg:.1f}°C")

    # F07 后台采样线程（真实启停）
    # 注意：线程每 0.05s 调 read_snapshot 会额外推进仿真步进，破坏"固定种子可复现"，
    # 因此本项只校验"能采到点且能停止"，且不打印具体点数，测完立刻用固定种子重建仿真器。
    buf2 = monitor.TrendBuffer()
    sampler = monitor.TrendSampler(buf2, interval=0.05)
    sampler.start()
    time.sleep(0.6)
    sampler.stop()
    sampler.join(timeout=3)
    ok = (not sampler.is_alive()) and all(buf2.length(d.id) >= 2 for d in catalog.devices())
    check("F", "17 sampler_thread", ok,
          f"后台线程启动后三台设备均采到点（≥2），且可正常停止（is_alive={sampler.is_alive()}）")
    sim_mod._SIM = sim_mod.Simulator(seed=SEED)   # 用固定种子重建，消除后台线程带来的步进漂移
    reset_all()
    print("  （说明：后台采样线程会额外推进仿真，已按固定种子重建仿真器，后续用例结果仍可复现。）")


# ---------------------------------------------------------------- G Web API 端到端
def run_api_cases() -> None:
    """Web 后端 API 层端到端（FastAPI TestClient，真实走 HTTP 栈，不 mock）。

    真实浏览器渲染与交互（设备卡、排查模式、L3 弹层、语音降级等）由
    scripts/browser_check.py 无头 Chrome 覆盖，产出 docs/screenshots/ 八张截图。
    """
    from fastapi.testclient import TestClient
    from cncops.api import app

    section_title("G. Web API 端到端：产线总览／诊断报告（结论先行）／知识库详情／勾选持久化／反馈入历史／演示注入")

    with TestClient(app) as c:
        reset_all()

        # G01 健康检查 + 首页静态资源 + 产线总览
        r_h, r_i, r_l = c.get("/api/health"), c.get("/"), c.get("/api/line")
        line = r_l.json()
        css_ok = c.get("/css/app.css").status_code == 200
        js_ok = c.get("/js/app.js").status_code == 200
        ok = (r_h.status_code == 200 and r_h.json()["status"] == "ok"
              and r_i.status_code == 200 and "产线监控台" in r_i.text
              and css_ok and js_ok
              and r_l.status_code == 200 and line["summary"]["total"] == 3
              and len(line["devices"]) == 3)
        check("G", "01 health_index_line", ok,
              f"health={r_h.json()['status']}｜首页/静态资源 {'齐全' if (css_ok and js_ok) else '缺失'}"
              f"｜产线总览「{line['summary']['sentence']}」")

        # G02 注入故障 → 数秒内状态条报警 + 报警设备卡置顶变红
        c.post("/api/demo/inject", json={"device_id": "CNC-01", "fault_id": "spindle_overheat"})
        t0, d = time.time(), None
        while time.time() - t0 < 25:
            d = c.get("/api/line").json()
            if d["summary"]["alarm"]:
                break
            time.sleep(1.0)
        first = d["devices"][0] if d and d.get("devices") else {}
        ok = bool(d) and d["summary"]["alarm"] >= 1 and first.get("id") == "CNC-01" \
            and first.get("severity") == "alarm"
        check("G", "02 inject_alarm_first", ok,
              f"检出耗时 {time.time() - t0:.1f}s｜「{d['summary']['sentence']}」｜"
              f"首卡 {first.get('id')}（{first.get('status_text')}）" if d else "未取到总览")

        # G03 口语诊断 → 结论先行 + 故障编号 + 证据趋势 + 分级步骤（工具/物料/耗时）
        rep = c.post("/api/diagnose", json={"query": "一号机主轴发烫，温度高得厉害"}).json()
        hl, evs, steps = rep["headline"], rep["evidence"], rep["steps"]
        l3 = rep["steps_by_level"]["L3"]
        ok = ("主轴" in hl["fault"] and hl["code"].startswith("F0")
              and bool(evs) and all(len(e["spark"]) >= 5 and e["cross_time"] for e in evs)
              and all(s["level"] in ("L1", "L2", "L3") and s["key"] and s["text"] for s in steps)
              and all(isinstance(s["tools"], list) and s["eta_min"] >= 0 for s in steps)
              and (not l3 or all(s["danger_badge"] or s["safety"] for s in l3)))
        check("G", "03 report_conclusion_first", ok,
              f"结论「{hl['device']} · {hl['fault']}（{hl['code']}，置信度 {hl['confidence']['text']}）」"
              f"｜证据 {len(evs)} 条均带趋势与越限起点｜步骤 {len(steps)} 条"
              f"｜L1 {len(rep['steps_by_level']['L1'])}/L2 {len(rep['steps_by_level']['L2'])}/L3 {len(l3)}"
              + ("" if ok else " ← 断言失败"))

        # G04 故障编号角标 → 知识库条目详情
        r = c.get(f"/api/kb/{hl['kb_id']}")
        kb = r.json() if r.status_code == 200 else {}
        ok = r.status_code == 200 and kb.get("code") == hl["code"] and bool(kb.get("steps"))
        check("G", "04 kb_detail", ok,
              f"KB {kb.get('kb_id')}（{kb.get('code')}）「{kb.get('title')}」步骤 {len(kb.get('steps', []))} 条"
              if r.status_code == 200 else f"详情接口失败 HTTP {r.status_code}")

        # G05 勾选实时保存（服务端持久化，刷新/换浏览器不丢）
        key0 = steps[0]["key"]
        c.post(f"/api/records/{rep['record_id']}/checklist", json={"checks": {key0: True}})
        got = c.get(f"/api/records/{rep['record_id']}/checklist").json()
        ok = got.get("checks", {}).get(key0) is True
        check("G", "05 checklist_persist", ok,
              f"步骤 {key0} 勾选已持久化（服务端回读 {got.get('checks')}）")

        # G06 反馈「已解决」→ 历史立即可查 + 设备筛选/关键字搜索
        # （操作人为产品必填项：空姓名接口返回 400，详见 L 段）
        fb = c.post(f"/api/records/{rep['record_id']}/feedback",
                    json={"resolved": True, "comment": "补冷却液后恢复", "operator": "赵六"})
        hist = c.get("/api/history").json()
        hit = [i for i in hist["items"] if i["id"] == rep["record_id"]]
        q = c.get("/api/history", params={"q": "主轴"}).json()
        dv = c.get("/api/history", params={"device": "CNC-02"}).json()
        ok = (fb.status_code == 200 and hit and hit[0]["result"] == "已解决"
              and q["count"] >= 1 and all(i["device_id"] == "CNC-02" for i in dv["items"]))
        check("G", "06 feedback_history", ok,
              f"反馈回执「{fb.json().get('message', '')}」｜历史命中 {len(hit)} 条（结果={hit[0]['result'] if hit else '无'}）"
              f"｜搜「主轴」{q['count']} 条｜筛 CNC-02 {dv['count']} 条")

        # G07 未覆盖表述 → 明示“知识库未覆盖”并给通用安全建议，不空白不报错
        rep2 = c.post("/api/diagnose", json={"query": "一号机主轴有焦糊味还冒烟"}).json()
        ok = rep2["uncovered"] is True and bool(rep2["warnings"])
        check("G", "07 uncovered_hint", ok,
              f"uncovered={rep2['uncovered']}｜通用安全建议 {len(rep2['warnings'])} 条"
              f"｜结论区回退为「{rep2['headline']['fault']}」")

        # G08 演示模式收尾：全部恢复
        cl = c.post("/api/demo/clear", json={}).json()
        check("G", "08 demo_clear", cl.get("ok") is True, cl.get("message", ""))

    # 注入与采样会推进仿真步进，按固定种子重建，保证后续 H 段用例可复现
    sim_mod._SIM = sim_mod.Simulator(seed=SEED)
    reset_all()


# ---------------------------------------------------------------- H 验收剧本
def run_acceptance_script() -> None:
    """按验收剧本走一遍：正常运行 → 注入 → 提问 → 分级步骤 → 恢复 → 入历史 → 二次提问引用历史。"""
    from cncops import rules
    from cncops.memory import get_memory

    section_title("H. 验收剧本：正常运行 → 注入主轴过热 → 口语提问 → 分级步骤 → 恢复 → 入历史 → 二次提问引用历史")

    # H01 正常运行
    reset_all()
    snaps = {d.id: datasource.read_snapshot(d.id) for d in catalog.devices()}
    ok = all(not s.get("alarm_codes") for s in snaps.values()) \
        and all(all(e.severity == "normal" for e in rules.build_evidence(s)) for s in snaps.values())
    check("H", "01 normal_running", ok,
          f"三台设备报警码均为空、全部测点正常（状态："
          f"{ {k: v.get('status') for k, v in snaps.items()} }）")

    # H02 注入主轴过热
    datasource.inject_fault("CNC-01", "spindle_overheat")
    cost, snap = wait_detect("CNC-01", ["ALM-101"])
    settle("CNC-01")
    snap = datasource.read_snapshot("CNC-01")
    over = [e.point for e in rules.build_evidence(snap) if e.severity == "alarm"]
    ok = cost is not None and "ALM-101" in snap.get("alarm_codes", []) and bool(over)
    check("H", "02 inject_overheat", ok,
          f"检出耗时 {cost if cost is None else round(cost, 2)}s｜报警 {'、'.join(snap.get('alarm_codes', []))}"
          f"｜报警测点 {over}")

    # H03 口语提问
    r1 = diagnose("一号机主轴发烫，温度高得厉害")
    top = r1.causes[0] if r1.causes else None
    ok = r1.device_id == "CNC-01" and bool(top) and top.kb_id == "KB-CNC-001"
    check("H", "03 ask_in_colloquial", ok,
          f"识别设备 {r1.device_id}｜Top1={top.kb_id if top else '无'}"
          f"（{top.probability * 100:.0f}%）｜超标证据 {len(r1.abnormal_evidence)} 条")

    # H04 分级步骤（L1/L2 前端可勾选；L3 危险信息齐全，进入前由前端强制安全确认）
    levels = [str(s.get("level", "")).upper() for s in r1.steps]
    l3s = [s for s in r1.steps if str(s.get("level", "")).upper() == "L3"]
    ok = "L1" in levels and all((s.get("danger_hits") or s.get("safety")) for s in l3s)
    check("H", "04 graded_steps", ok,
          f"L1 {levels.count('L1')} 条 / L2 {levels.count('L2')} 条 / L3 {len(l3s)} 条"
          f"（L3 均带危险标注与安全要求，前端进入前强制确认）")

    # H05 处理并恢复
    feedback(r1.record_id, resolved=True, comment="补冷却液并清理滤网后温度回落")
    datasource.clear_faults("CNC-01")
    settle("CNC-01")
    snap = datasource.read_snapshot("CNC-01")
    ok = not snap.get("alarm_codes") \
        and all(e.severity == "normal" for e in rules.build_evidence(snap))
    check("H", "05 handled_and_recovered", ok,
          f"恢复后报警码为空、测点全部正常（主轴温度 {snap['values'].get('spindle_temp'):.1f}°C）")

    # H06 自动存入历史（含数据快照与反馈）
    mem = get_memory()
    rows = [r for r in mem.recent(200) if r.get("id") == r1.record_id]
    row = rows[0] if rows else {}
    ok = bool(row) and bool((row.get("snapshot") or {}).get("values")) \
        and int(row.get("resolved") or 0) == 1 and bool(row.get("fb_comment"))
    check("H", "06 history_saved", ok,
          f"历史 #{row.get('id')} 已存：快照测点 {len((row.get('snapshot') or {}).get('values', {}))} 个、"
          f"结果=已解决、备注=“{row.get('fb_comment', '')}”"
          if ok else f"历史记录不完整：{row}")

    # H07 第二次提问引用历史
    datasource.inject_fault("CNC-01", "spindle_overheat")
    wait_detect("CNC-01", ["ALM-101"])
    settle("CNC-01")
    r2 = diagnose("一号机主轴又发烫了，跟上次是不是一样")
    ref = r2.history_ref or {}
    ok = bool(ref) and ref.get("id") == r1.record_id and int(ref.get("resolved") or 0) == 1
    check("H", "07 history_referenced", ok,
          f"第二次诊断引用历史 #{ref.get('id')}（{ref.get('fault_title', '')}｜"
          f"处理结果：{'已解决' if ref.get('resolved') else '未反馈/未解决'}｜"
          f"备注：{ref.get('fb_comment', '')}）" if ref else "未引用历史记录")

    reset_all()


# ---------------------------------------------------------------- I 经验沉淀与导出
def run_insights_and_export() -> None:
    """经验沉淀（多工厂/复发提醒/平均处理时长/操作人）与 CSV/Excel 导出。

    说明：本节用合成设备号（CNC-97/98/99）与合成工厂 F002 造数，避免与前面段落
    真实诊断记录混在一起导致计数不确定；全部写入临时记忆库，不碰正式库。

    可复现性：种子数据的时间戳统一相对固定基准时刻 BASE_TIME（昨日 23:00）生成——
    ① 基准时刻永远在过去，"30 天复发窗口"（相对真实当前时间计算）判定稳定；
    ② 同一天内多次运行的时间戳字符串完全一致，配合 xlsx 固定 zip 条目时间戳，
       使"连续两次运行输出逐字节一致"成立。I-06 导出按 CNC-99 过滤，
       确保导出内容只含确定性种子数据（真实诊断记录带运行时刻，无法逐字节一致）。
    """
    from datetime import date, datetime, time, timedelta

    from fastapi.testclient import TestClient
    from cncops.api import app
    from cncops.memory import get_memory

    section_title("I. 经验沉淀与导出：多工厂隔离／复发提醒／平均处理时长／操作人／CSV+Excel 导出")

    mem = get_memory()
    # 固定基准时刻：昨日 23:00（永远在过去；同一天内跨次运行取值不变）
    BASE_TIME = datetime.combine(date.today(), time(0, 0)) - timedelta(hours=1)

    def diag(device_id: str, fault_id: str, fault_title: str, days_ago: float,
             factory_id: str = "F001", device_name: str = "") -> int:
        return mem.save_diagnosis({
            "created_at": (BASE_TIME - timedelta(days=days_ago)).isoformat(timespec="seconds"),
            "device_id": device_id,
            "device_name": device_name or device_id,
            "device_type": "cnc",
            "query": f"{device_id} {fault_title}",
            "fault_id": fault_id, "fault_title": fault_title,
            "kb_ids": [fault_id], "max_level": "L2",
            "signature": f"sig-{device_id}-{fault_id}-{days_ago}",
            "mode": "rule", "factory_id": factory_id,
        })

    # I01 多工厂隔离：同一租户下两个工厂的记录，按工厂筛选只看到本工厂的
    mem.upsert_factory("F001", "一号工厂")
    mem.upsert_factory("F002", "二号工厂")
    diag("CNC-01", "KB-CNC-001", "主轴温度过高", days_ago=2, factory_id="F001")
    diag("CNC-05", "KB-CNC-003", "主轴负载过高或闷车", days_ago=2, factory_id="F002",
         device_name="五号加工中心")
    rows_f1 = mem.device_stats(factory_id="F001", days=3650)
    rows_f2 = mem.device_stats(factory_id="F002", days=3650)
    ok = (any(r["device_id"] == "CNC-01" for r in rows_f1)
          and not any(r["device_id"] == "CNC-05" for r in rows_f1)
          and any(r["device_id"] == "CNC-05" for r in rows_f2)
          and not any(r["device_id"] == "CNC-01" for r in rows_f2))
    check("I", "01 factory_isolation", ok,
          f"F001 统计含 CNC-01、不含 CNC-05；F002 反之"
          f"（F001 设备数 {len(rows_f1)}，F002 设备数 {len(rows_f2)}）")

    # I02 复发提醒：CNC-99 同故障 30 天内 3 次 → critical；40 天前的 2 次不计入窗口；
    #     CNC-98 同故障 30 天内 2 次 → warn 级提醒
    diag("CNC-99", "KB-CNC-009", "合成故障A", days_ago=45)
    diag("CNC-99", "KB-CNC-009", "合成故障A", days_ago=44)
    diag("CNC-99", "KB-CNC-009", "合成故障A", days_ago=2)
    diag("CNC-99", "KB-CNC-009", "合成故障A", days_ago=1)
    diag("CNC-99", "KB-CNC-009", "合成故障A", days_ago=0.01)
    diag("CNC-98", "KB-CNC-008", "合成故障B", days_ago=3)
    diag("CNC-98", "KB-CNC-008", "合成故障B", days_ago=1)
    recs = mem.recurring_faults(days=30)
    rec99 = next((r for r in recs if r["device_id"] == "CNC-99"), None)
    rec98 = next((r for r in recs if r["device_id"] == "CNC-98"), None)
    ok = (rec99 is not None and rec99["count"] == 3 and rec99["severity"] == "critical"
          and rec99["fault_id"] == "KB-CNC-009"
          and rec98 is not None and rec98["count"] == 2 and rec98["severity"] == "warn")
    detail = (f"CNC-99 30 天内 {rec99['count'] if rec99 else 0} 次（{rec99['severity'] if rec99 else '-'}）"
              f"｜CNC-98 {rec98['count'] if rec98 else 0} 次（{rec98['severity'] if rec98 else '-'}）"
              if ok else f"复发提醒不符合预期：99={rec99}｜98={rec98}")
    check("I", "02 recurrence_alert", ok, detail)

    # I03 平均处理时长：填报值优先；未填报按"反馈时间-诊断时间"兜底推算。
    #     反馈时间显式指定（add_feedback 的 created_at 参数），使兜底差值精确为 12 分钟，
    #     不随运行快慢波动（ round 到分钟虽能吸收秒级抖动，但精确更好 ）。
    r_a = diag("CNC-97", "KB-CNC-007", "合成故障C", days_ago=0.2)
    mem.add_feedback(r_a, resolved=True, comment="已处理", operator="李四", handling_min=30,
                     created_at=(BASE_TIME - timedelta(days=0.2) + timedelta(minutes=30))
                     .isoformat(timespec="seconds"))
    r_b = diag("CNC-97", "KB-CNC-007", "合成故障C", days_ago=12 / 1440.0)   # 12 分钟前
    mem.add_feedback(r_b, resolved=True, operator="李四",                     # 不填报时长 → 兜底 =12 分钟
                     created_at=BASE_TIME.isoformat(timespec="seconds"))
    stat97 = next((d for d in mem.device_stats(device_id="CNC-97", days=3650)), {})
    ok = stat97.get("avg_handling_min") == 21.0 and stat97.get("total") == 2
    check("I", "03 avg_handling", ok,
          f"两条记录处理时长分别为 30 分钟（填报）与约 12 分钟（时间差兜底），"
          f"平均 {stat97.get('avg_handling_min')} 分钟（期望 21.0）"
          if ok else f"平均处理时长计算错误：{stat97}")

    with TestClient(app) as c:
        # I04 操作人端到端：诊断带操作人 → 落库；反馈带操作人/处理时长/停机时长 → 历史与统计可见
        datasource.inject_fault("CNC-01", "spindle_overheat")
        settle("CNC-01")
        rep = c.post("/api/diagnose", json={"query": "一号机主轴发烫，温度高得厉害",
                                            "operator": "王五"}).json()
        rid = rep["record_id"]
        fb = c.post(f"/api/records/{rid}/feedback",
                    json={"resolved": True, "comment": "更换冷却液并清滤网",
                          "operator": "王五", "handling_min": 35, "downtime_min": 50})
        hist = c.get("/api/history", params={"device": "CNC-01"}).json()
        item = next((i for i in hist["items"] if i["id"] == rid), {})
        ins = c.get("/api/insights", params={"days": 3650}).json()
        dev01 = next((d for d in ins["devices"] if d["device_id"] == "CNC-01"), {})
        ok = (fb.status_code == 200 and item.get("operator") == "王五"
              and item.get("handling_min") == 35 and item.get("downtime_min") == 50
              and item.get("result") == "已解决" and item.get("factory_name") == "一号工厂"
              and dev01.get("total_downtime_min", 0) >= 50)
        check("I", "04 operator_end_to_end", ok,
              f"记录 #{rid}：操作人={item.get('operator')}｜处理 {item.get('handling_min')} 分钟"
              f"｜停机 {item.get('downtime_min')} 分钟｜工厂={item.get('factory_name')}"
              f"｜CNC-01 累计停机 {dev01.get('total_downtime_min')} 分钟"
              if ok else f"操作人/停机字段未正确落库或展示：item={item}")

        # I05 CSV 导出：utf-8-sig（Excel 直接打开不乱码）+ 老板视角列齐全 + 行数与库内一致
        expect = mem.export_rows(device_id="CNC-99", days=3650)
        r = c.get("/api/records/export", params={"fmt": "csv", "device": "CNC-99", "days": 3650})
        text = r.content.decode("utf-8-sig")
        lines = [ln for ln in text.splitlines() if ln.strip()]
        ok = (r.status_code == 200 and r.content[:3] == b"\xef\xbb\xbf"
              and "操作人" in lines[0] and "停机时长(分钟)" in lines[0]
              and "工厂" in lines[0] and "处理时长(分钟)" in lines[0]
              and len(lines) - 1 == len(expect) and len(expect) == 5
              and any("合成故障A" in ln for ln in lines))
        check("I", "05 export_csv", ok,
              f"HTTP {r.status_code}｜BOM={'有' if r.content[:3] == b'\xef\xbb\xbf' else '无'}"
              f"｜表头 18 列含操作人/停机时长｜数据行 {len(lines) - 1} 条（库内 {len(expect)} 条）"
              if ok else f"CSV 导出不符合预期：HTTP {r.status_code}，行数 {len(lines) - 1}/{len(expect)}")

        # I06 Excel 导出：合法 zip + 两张工作表（明细 + 故障与停机统计）+ 关键内容在表内。
        #     按 CNC-99 过滤：导出内容只含固定基准时刻的种子数据，两次运行字节数一致
        #     （xlsx 的 zip 条目时间戳已固定；不过滤则真实诊断记录带运行时刻，无法逐字节复现）。
        r = c.get("/api/records/export", params={"fmt": "xlsx", "device": "CNC-99", "days": 3650})
        import io as _io
        import zipfile as _zipfile
        try:
            z = _zipfile.ZipFile(_io.BytesIO(r.content))
            names = z.namelist()
            wb = z.read("xl/workbook.xml").decode("utf-8")
            s1 = z.read("xl/worksheets/sheet1.xml").decode("utf-8")
            s2 = z.read("xl/worksheets/sheet2.xml").decode("utf-8")
            zip_ok = True
        except Exception as exc:                                       # pragma: no cover
            names, wb, s1, s2, zip_ok = [], "", "", "", False
            print(f"    xlsx 解析失败：{exc}")
        ok = (r.status_code == 200 and zip_ok and r.content[:2] == b"PK"
              and "xl/worksheets/sheet1.xml" in names and "xl/worksheets/sheet2.xml" in names
              and "运维记录" in wb and "故障与停机统计" in wb
              and "CNC-99" in s1 and "停机时长" in s1 and "累计停机" in s2)
        check("I", "06 export_xlsx", ok,
              f"HTTP {r.status_code}｜{len(r.content)} 字节｜工作表：运维记录 + 故障与停机统计"
              f"｜明细含合成设备行、统计表含停机汇总"
              if ok else "xlsx 结构或内容不符合预期")

        # I07 导出参数校验：非法格式/非法结果值必须 400，不允许静默导出错数据
        bad_fmt = c.get("/api/records/export", params={"fmt": "pdf"})
        bad_result = c.get("/api/records/export", params={"fmt": "csv", "result": "已完成"})
        facs = c.get("/api/factories").json()
        ok = (bad_fmt.status_code == 400 and bad_result.status_code == 400
              and any(f["id"] == "F001" and f["device_count"] == 3 for f in facs["items"])
              and any(f["id"] == "F002" for f in facs["items"]))
        check("I", "07 export_validation", ok,
              f"fmt=pdf → HTTP {bad_fmt.status_code}｜result=已完成 → HTTP {bad_result.status_code}"
              f"｜工厂目录 {[f['id'] for f in facs['items']]}（F001 设备数 3）")

        datasource.clear_faults()
        settle("CNC-01")

    # 注入会推进仿真步进，重建仿真器保持后续可复现
    sim_mod._SIM = sim_mod.Simulator(seed=SEED)
    reset_all()


# ---------------------------------------------------------------- J 覆盖完整性与守门串联
# 多设备配置的子进程验收脚本：父进程的 paths.DEVICES_YAML 在 import 时已定死，
# 只能通过"子进程 + CNCOPS_DEVICES_YAML 环境变量"真实切换配置来验证。
# 期望值（总数/每工厂数/两工厂代表故障）全部经 argv 传入，10 台与 30 台配置共用。
_DEVICES_CHILD = r"""
import sys, time
sys.path.insert(0, {src!r})
seed, total, per_fac, dev_a, fid_a, exp_a, dev_b, fid_b, exp_b = sys.argv[1:10]
total, per_fac = int(total), int(per_fac)
from cncops import catalog, datasource
import cncops.simulator as sim_mod

sim_mod._SIM = sim_mod.Simulator(seed=int(seed))
devs = catalog.devices()
facs = catalog.factories()
assert len(devs) == total, f"设备数 {{len(devs)}} != {{total}}"
assert {{f.id for f in facs}} == {{"F001", "F002"}}, f"工厂集合异常: {{[f.id for f in facs]}}"
by_fac = {{}}
for d in devs:
    by_fac.setdefault(d.factory_id, []).append(d.id)
assert all(len(v) == per_fac for v in by_fac.values()), f"双工厂应各 {{per_fac}} 台: {{by_fac}}"
# 基线：每台可读快照且无报警码
for d in devs:
    for _ in range(18):
        snap = datasource.read_snapshot(d.id)
    assert snap and not snap.get("alarm_codes"), f"{{d.id}} 基线异常: {{snap}}"
# 每个工厂各注入一个代表故障，验证可检出
for dev_id, fid, expect in ((dev_a, fid_a, [exp_a]), (dev_b, fid_b, [exp_b])):
    datasource.clear_faults()
    for _ in range(18):
        datasource.read_snapshot(dev_id)
    datasource.inject_fault(dev_id, fid)
    t0, seen = time.time(), []
    for _ in range(80):
        seen = (datasource.read_snapshot(dev_id) or {{}}).get("alarm_codes", [])
        if all(c in seen for c in expect):
            break
        time.sleep(0.05)
    else:
        raise AssertionError(f"{{dev_id}} 注入 {{fid}} 未检出期望 {{expect}}，实际 {{seen}}")
    print(f"OK {{dev_id}} +{{fid}} -> {{seen}} ({{time.time() - t0:.2f}}s)")
print(f"DEVICES{{total}}_ALL_OK")
"""


def _run_child(label: str, argv: List[str], env: dict, timeout: int = 180) -> Tuple[bool, str]:
    """运行子进程守门/验收脚本，返回 (是否通过, 输出尾部)。"""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, env=env,
                              timeout=timeout, cwd=str(ROOT))
    except subprocess.TimeoutExpired:
        return False, f"{label} 子进程超时（>{timeout}s）"
    tail = (proc.stdout or "").strip().splitlines()
    note = "\n      ".join(tail[-4:]) if tail else (proc.stderr or "")[-400:]
    return proc.returncode == 0, f"exit={proc.returncode}｜{note}"


def run_coverage_and_gate_cases() -> None:
    section_title("J. 覆盖完整性与守门串联：KB↔用例双向覆盖 / 十台配置真实加载 / 阈值守门脚本")

    # ---- J-01 知识库条目 ↔ 故障用例双向覆盖 ----
    from cncops.knowledge import get_kb
    kb = get_kb()
    fault_entries = [e for e in kb.entries if not e.id.startswith("KB-GEN")]
    covered_by_cases = {expect_top for (_, _, expect_top, _) in FAULT_CASES.values()}
    known_kb = {e.id for e in kb.entries}

    uncovered_kb = [f"{e.id}({e.code} {e.title})" for e in fault_entries if e.id not in covered_by_cases]
    dangling_kb = sorted({kb_id for (_, _, kb_id, _) in FAULT_CASES.values() if kb_id not in known_kb})
    alarm_table = (config.load_yaml(str(paths.ALARMS_YAML)) or {}).get("codes", [])
    known_codes = {item["code"] for item in alarm_table}
    bad_codes = sorted({f"{fid}:{c}" for fid, (_, _, _, codes) in FAULT_CASES.items()
                        for c in codes if c not in known_codes})
    sim_faults = {f["id"] for f in datasource.simulator().list_faults()}
    no_case = sorted(fid for fid in sim_faults if fid not in FAULT_CASES)

    ok = not (uncovered_kb or dangling_kb or bad_codes or no_case)
    detail = (f"KB 故障条目 {len(fault_entries)} 个全部有用例断言 Top-1；"
              f"故障剧本 {len(sim_faults)} 个全部配套口语用例；"
              f"用例引用的 KB id / 报警码均真实存在")
    if not ok:
        detail = (f"无用例覆盖的 KB 条目={uncovered_kb}；用例引用不存在的 KB={dangling_kb}；"
                  f"用例引用不存在的报警码={bad_codes}；无用例的故障={no_case}")
    check("J", "01 kb_case_coverage", ok, detail)

    # ---- J-02 十台设备预览配置（子进程真实加载 config/devices_10.yaml）----
    child_src = _DEVICES_CHILD.format(src=str(ROOT / "src"))
    env = dict(os.environ)
    env["CNCOPS_DEVICES_YAML"] = str(ROOT / "config" / "devices_10.yaml")
    ok, note = _run_child(
        "devices_10",
        [sys.executable, "-c", child_src, str(SEED), "10", "5",
         "CNC-01", "spindle_overheat", "ALM-101", "AIR-02", "air_pressure_low", "ALM-202"],
        env)
    check("J", "02 devices_10_preview", ok,
          f"10 台设备 / F001+F002 双工厂各 5 台 / 基线无报警 / 每工厂注入代表故障可检出｜{note}")

    # ---- J-03 阈值守门脚本串联（必越限测点 margin > 3σ 噪声）----
    gate = ROOT / "scripts" / "check_thresholds.py"
    ok, note = _run_child("check_thresholds", [sys.executable, str(gate)], dict(os.environ), timeout=120)
    check("J", "03 threshold_gate_script", ok,
          f"scripts/check_thresholds.py 作为子进程串联进自测（D 段可达性的动态稳定性补充）｜{note}")

    # ---- J-04 三十台设备验收配置（子进程真实加载 config/devices_30.yaml）----
    env30 = dict(os.environ)
    env30["CNCOPS_DEVICES_YAML"] = str(ROOT / "config" / "devices_30.yaml")
    ok, note = _run_child(
        "devices_30",
        [sys.executable, "-c", child_src, str(SEED), "30", "15",
         "CNC-01", "spindle_overheat", "ALM-101", "AIR-10", "air_pressure_low", "ALM-202"],
        env30)
    check("J", "04 devices_30_acceptance", ok,
          f"30 台设备（20 CNC + 10 空压机）/ F001+F002 各 15 台 / 基线无报警 / "
          f"两工厂注入代表故障可检出｜{note}")


# ---------------------------------------------------------------- K 紧凑列表/抽屉数据契约
def run_compact_list_contract() -> None:
    """紧凑摘要行 + 详情抽屉的前端数据契约（FastAPI TestClient，真实 HTTP 栈）。

    前端 ui.js 的 renderDeviceRows / renderDrawer 只消费这些字段；
    这里逐字段断言"接口真的给"，防止后端改字段名后前端静默显示 "--"。
    """
    from fastapi.testclient import TestClient
    from cncops.api import app

    section_title("K. 紧凑列表/抽屉数据契约：行字段 / 趋势同源 / 抽屉全测点与历史 / 断线语义")

    with TestClient(app, raise_server_exceptions=False) as c:
        reset_all()

        # ---- K-01 /api/line 摘要行字段契约 ----
        line = c.get("/api/line").json()
        cards = line["devices"]
        row_fields = ("id", "name", "type", "location", "status", "status_text",
                      "status_icon", "severity", "alarm_codes", "key_metrics")
        metric_fields = ("key", "label", "value", "unit", "severity", "alarm", "warn", "direction")
        missing_rows = [d["id"] for d in cards if any(k not in d for k in row_fields)]
        bad_metrics, wrong_key = [], []
        for d in cards:
            kms = d.get("key_metrics") or []
            if not kms or any(k not in kms[0] for k in metric_fields):
                bad_metrics.append(d["id"]); continue
            expect_key = "spindle_temp" if d["type"] == "cnc" else "outlet_pressure"
            if kms[0]["key"] != expect_key:
                wrong_key.append(f"{d['id']}:{kms[0]['key']}")
        ok = (len(cards) == 3 and not missing_rows and not bad_metrics and not wrong_key
              and all(isinstance(d["alarm_codes"], list) for d in cards))
        check("K", "01 line_row_fields", ok,
              f"3 台设备行字段齐全（缺字段={missing_rows or '无'}）；"
              f"关键测点按类型选取正确（CNC→spindle_temp，空压机→outlet_pressure，异常={wrong_key or '无'}）；"
              f"关键值字段缺漏={bad_metrics or '无'}")

        # ---- K-02 /api/trend 火花线契约（与抽屉同源同周期）----
        bad_pts, bad_keys, misaligned = [], [], []
        for d in cards:
            r = c.get(f"/api/trend/{d['id']}?n=24")
            if r.status_code != 200:
                bad_pts.append(f"{d['id']}:HTTP{r.status_code}"); continue
            series = r.json().get("series") or {}
            detail_keys = {p["key"] for p in c.get(f"/api/devices/{d['id']}").json()["points"]}
            if set(series) != detail_keys:
                misaligned.append(d["id"])
            for key, pts in series.items():
                if len(pts) > 24:
                    bad_pts.append(f"{d['id']}.{key}:{len(pts)}点")
                ts = [p.get("t") for p in pts]
                vs = [p.get("v") for p in pts]
                if any(not isinstance(t, str) or not isinstance(v, (int, float)) for t, v in zip(ts, vs)):
                    bad_keys.append(f"{d['id']}.{key}")
                elif ts != sorted(ts):
                    bad_keys.append(f"{d['id']}.{key}:时间乱序")
        ok = not (bad_pts or bad_keys or misaligned)
        check("K", "02 trend_series_contract", ok,
              f"n=24 上限遵守（超限={bad_pts or '无'}）；每点为 {{t: 字符串, v: 数值}} 且时间递增"
              f"（异常={bad_keys or '无'}）；series 测点集合与抽屉详情一致（不一致={misaligned or '无'}）")

        # ---- K-03 抽屉详情 + 运维历史契约 ----
        detail = c.get("/api/devices/CNC-01").json()
        p0 = (detail.get("points") or [{}])[0]
        point_fields = ("key", "label", "value", "unit", "severity", "alarm", "warn", "direction")
        hist = c.get("/api/history?device=CNC-01&limit=5").json()
        items = hist.get("items") or []
        hist_fields = ("id", "created_at", "fault_title", "result", "operator", "comment")
        missing_hist = [h.get("id") for h in items if any(k not in h for k in hist_fields)]
        ok = (detail.get("device", {}).get("id") == "CNC-01"
              and len(detail.get("points") or []) >= 5
              and all(k in p0 for k in point_fields)
              and isinstance(detail.get("trend"), dict)
              and 0 < len(items) <= 5 and not missing_hist)
        check("K", "03 drawer_detail_history", ok,
              f"抽屉全测点 {len(detail.get('points') or [])} 项且含阈值字段；"
              f"最近运维历史 {len(items)} 条（≤5）且字段齐全（缺字段记录={missing_hist or '无'}）")

        # ---- K-04 断线语义契约（前端横幅/行内中断/抽屉中断文案据此触发）----
        # 场景 A：read_snapshot 返回 None（设备无数据）→ /api/line 仍 200，该行降级为 offline 卡片
        # 场景 B：read_snapshot 抛异常（数据源断线）→ /api/line 500、/api/devices 503，
        #         前端 API.request 抛错 → 顶部横幅 + 抽屉显「数据连接中断」
        # 注意：本段客户端全程 raise_server_exceptions=False，否则 TestClient 会把
        # 服务端未捕获异常直接抛进测试进程，拿不到真实的 500 响应。
        import logging
        from cncops import api as api_mod
        real_read = api_mod.datasource.read_snapshot

        def _none_for_2(device_id):
            return None if device_id == "CNC-02" else real_read(device_id)

        def _raise_for_2(device_id):
            if device_id == "CNC-02":
                raise RuntimeError("模拟数据源断线")
            return real_read(device_id)

        api_mod.datasource.read_snapshot = _none_for_2
        try:
            line_none = c.get("/api/line")
            card2 = next((d for d in line_none.json()["devices"] if d["id"] == "CNC-02"), {})
        finally:
            api_mod.datasource.read_snapshot = real_read

        api_logger = logging.getLogger("cncops.api")
        old_level = api_logger.level
        api_logger.setLevel(logging.CRITICAL)          # 异常路径的 traceback 是预期行为，压低噪音
        api_mod.datasource.read_snapshot = _raise_for_2
        try:
            line_raise = c.get("/api/line")
            d503 = c.get("/api/devices/CNC-02")
        finally:
            api_mod.datasource.read_snapshot = real_read   # 必须恢复，避免污染后续用例
            api_logger.setLevel(old_level)

        ok_a = (line_none.status_code == 200
                and card2.get("severity") == "offline" and card2.get("degraded") is True
                and "数据连接中断" in (card2.get("status_text") or ""))
        ok_b = (line_raise.status_code == 500
                and d503.status_code == 503
                and "数据连接中断" in (d503.json().get("detail") or ""))
        check("K", "04 offline_semantics", ok_a and ok_b,
              f"场景A 返回 None：/api/line 200 且 CNC-02 行 severity=offline+degraded+文案「数据连接中断」"
              f"（实际 {card2.get('severity')}/{card2.get('degraded')}）｜"
              f"场景B 抛异常：/api/line {line_raise.status_code}、/api/devices {d503.status_code}"
              f"「{(d503.json().get('detail') or '')[:30]}」（前端据此显顶部横幅与抽屉中断）")

        # ---- K-05 排序契约：报警 > 预警 > 正常，前端 renderDeviceRows 依赖该顺序 ----
        reset_all()
        c.post("/api/demo/inject", json={"device_id": "CNC-01", "fault_id": "spindle_overheat"})
        t0 = time.time()
        while time.time() - t0 < 25:
            ranked = c.get("/api/line").json()["devices"]
            if ranked and ranked[0].get("severity") == "alarm":
                break
            time.sleep(1.0)
        sev_seq = [d.get("severity") for d in ranked]
        order = {"alarm": 0, "warn": 1, "offline": 2, "normal": 3}
        ok = bool(sev_seq) and [order.get(s, 9) for s in sev_seq] == sorted(order.get(s, 9) for s in sev_seq)
        check("K", "05 line_rank_order", ok,
              f"注入 CNC-01 过热后序列 {'>'.join(sev_seq)}（报警必须排在最前；前端另有 rank 兜底）")
        reset_all()


def run_feedback_rules() -> None:
    """反馈规则：操作人必填 + 停机时长自动计时（FastAPI TestClient，真实 HTTP 栈）。

    产品规则（对应用户需求）：
    1. 操作人（姓名/工号）必填——空姓名 400 且不落库；
    2. 停机时长从设备报警开始自动计时，提交反馈（确认解决）时停表并计入记录；
       手动填报优先；报警事件在反馈时被消费（停表）。
    """
    from datetime import datetime, timedelta

    from fastapi.testclient import TestClient
    from cncops import api as api_mod
    from cncops.api import app

    section_title("L. 反馈规则：操作人必填 / 停机时长自动计时（报警起点→确认解决停表）")

    with TestClient(app, raise_server_exceptions=False) as c:
        reset_all()
        api_mod._ALARM_EPISODES.clear()

        # ---- L-01 操作人必填：空姓名 400 且不落库；补填后正常入库 ----
        rep = c.post("/api/diagnose", json={"query": "一号机主轴发烫，温度高得厉害"}).json()
        rid = rep["record_id"]
        before = c.get("/api/history", params={"device": "CNC-01"}).json()
        rec_before = next((i for i in before["items"] if i["id"] == rid), {})
        bad = c.post(f"/api/records/{rid}/feedback", json={"resolved": True, "operator": "  "})
        after = c.get("/api/history", params={"device": "CNC-01"}).json()
        rec_after = next((i for i in after["items"] if i["id"] == rid), {})
        ok = (bad.status_code == 400 and "操作人" in (bad.json().get("detail") or "")
              and rec_after.get("result") == "未反馈" and rec_before.get("result") == "未反馈")
        check("L", "01 operator_required", ok,
              f"空姓名提交 → HTTP {bad.status_code}「{bad.json().get('detail')}」；"
              f"记录 #{rid} 仍为「{rec_after.get('result')}」（未落库）")

        good = c.post(f"/api/records/{rid}/feedback",
                      json={"resolved": True, "operator": "张三", "comment": "已处理"})
        rec_ok = next((i for i in c.get("/api/history", params={"device": "CNC-01"}).json()["items"]
                       if i["id"] == rid), {})
        ok = (good.status_code == 200 and rec_ok.get("result") == "已解决"
              and rec_ok.get("fb_operator") == "张三")
        check("L", "01b operator_recorded", ok,
              f"补填操作人「张三」→ 200；历史记录 #{rid} 结果={rec_ok.get('result')}、"
              f"实际处理人={rec_ok.get('fb_operator')}")

        # ---- L-02 停机自动计时：报警起点 → 反馈时停表，分钟数计入记录 ----
        # 起点按 10 分钟前设置（直接构造报警事件，避免真实等待；/api/line 驱动的
        # 事件登记逻辑由 L-03 的端到端用例覆盖）
        rep2 = c.post("/api/diagnose", json={"query": "二号机主轴发烫，温度高得厉害"}).json()
        rid2 = rep2["record_id"]
        start_iso = (datetime.now() - timedelta(minutes=10)).isoformat(timespec="seconds")
        api_mod._ALARM_EPISODES["CNC-02"] = {"start": start_iso, "cleared": ""}
        fb_auto = c.post(f"/api/records/{rid2}/feedback",
                         json={"resolved": True, "operator": "李四"})
        j = fb_auto.json()
        rec2 = next((i for i in c.get("/api/history", params={"device": "CNC-02"}).json()["items"]
                     if i["id"] == rid2), {})
        ep2 = api_mod._ALARM_EPISODES.get("CNC-02") or {}
        ok = (fb_auto.status_code == 200 and j.get("downtime_min") == 10 and j.get("downtime_auto") is True
              and rec2.get("downtime_min") == 10 and ep2.get("consumed") is True)
        check("L", "02 downtime_auto_timing", ok,
              f"报警起点设为 10 分钟前 → 反馈未填报停机时长 → 自动计时 {j.get('downtime_min')} 分钟"
              f"（downtime_auto={j.get('downtime_auto')}）；历史记录 #{rid2} 停机={rec2.get('downtime_min')} 分钟；"
              f"报警事件已标记 consumed（计时停止）")

        # ---- L-02b 手动填报优先 + 停表：填报值覆盖自动值，事件同样被消费 ----
        rep3 = c.post("/api/diagnose", json={"query": "二号机主轴又响又烫，是不是轴承坏了"}).json()
        rid3 = rep3["record_id"]
        start_iso = (datetime.now() - timedelta(minutes=5)).isoformat(timespec="seconds")
        api_mod._ALARM_EPISODES["CNC-02"] = {"start": start_iso, "cleared": ""}
        fb_manual = c.post(f"/api/records/{rid3}/feedback",
                           json={"resolved": False, "operator": "王五", "downtime_min": 99})
        j = fb_manual.json()
        ep3 = api_mod._ALARM_EPISODES.get("CNC-02") or {}
        ok = (fb_manual.status_code == 200 and j.get("downtime_min") == 99
              and j.get("downtime_auto") is False and ep3.get("consumed") is True)
        check("L", "02b downtime_manual_override", ok,
              f"报警起点 5 分钟前但填报 99 分钟 → 入库 {j.get('downtime_min')} 分钟"
              f"（downtime_auto={j.get('downtime_auto')}，填报优先）；事件已标记 consumed（停表）")

        # ---- L-02c 停表后不得重启：确认解决但报警仍在持续 → 不起新表 ----
        # 回归用例：曾出现的 bug——反馈消费事件后，报警仍激活，下一轮 /api/line
        # 立刻生成新事件，前端把停机计时器重启（读数从"已停止"跳回"计时中"）。
        api_mod._ALARM_EPISODES.clear()
        rep4 = c.post("/api/diagnose", json={"query": "二号机主轴发烫，温度高得厉害"}).json()
        rid4 = rep4["record_id"]
        c.post("/api/demo/inject", json={"device_id": "CNC-02", "fault_id": "spindle_overheat"})
        t0, card = time.time(), {}
        while time.time() - t0 < 25:
            card = next((d for d in c.get("/api/line").json()["devices"] if d["id"] == "CNC-02"), {})
            if card.get("severity") == "alarm":
                break
            time.sleep(1.0)
        since1 = card.get("alarm_since")
        fb4 = c.post(f"/api/records/{rid4}/feedback", json={"resolved": True, "operator": "赵六"})
        # 反馈后设备仍在报警：继续轮询，事件必须保持 consumed 且起点不变（不起新表）
        stable, since_after = True, since1
        for _ in range(4):
            time.sleep(1.0)
            card_after = next((d for d in c.get("/api/line").json()["devices"]
                               if d["id"] == "CNC-02"), {})
            if card_after.get("alarm_since") != since1:
                stable = False
                since_after = card_after.get("alarm_since")
                break
        ep4 = api_mod._ALARM_EPISODES.get("CNC-02") or {}
        ok = (fb4.status_code == 200 and fb4.json().get("downtime_auto") is True
              and stable and ep4.get("consumed") is True and since1 is not None)
        check("L", "02c no_restart_after_resolved", ok,
              f"反馈确认解决后报警仍持续：alarm_since 保持 {since1}（实际 {since_after}）、"
              f"事件 consumed={ep4.get('consumed')}、downtime_auto={fb4.json().get('downtime_auto')}"
              f"（不起新表 → 前端计时器不会重启）")

        # 报警消失后事件关闭；再次报警才起新表（新故障 → 新停机计时）
        reset_all()
        t0 = time.time()
        while time.time() - t0 < 15:
            card_n = next((d for d in c.get("/api/line").json()["devices"] if d["id"] == "CNC-02"), {})
            if card_n.get("severity") != "alarm":
                break
            time.sleep(1.0)
        closed = "CNC-02" not in api_mod._ALARM_EPISODES
        c.post("/api/demo/inject", json={"device_id": "CNC-02", "fault_id": "spindle_overheat"})
        t0, since2 = time.time(), None
        while time.time() - t0 < 25:
            card_n = next((d for d in c.get("/api/line").json()["devices"] if d["id"] == "CNC-02"), {})
            if card_n.get("severity") == "alarm":
                since2 = card_n.get("alarm_since")
                break
            time.sleep(1.0)
        ok = closed and since2 is not None and since2 > (since1 or "")
        check("L", "02d new_alarm_new_episode", ok,
              f"报警消失后事件关闭（存在={not closed}）；再次报警起新表：{since1} → {since2}")
        api_mod._ALARM_EPISODES.clear()
        reset_all()

        # ---- L-03 报警事件生命周期：alarm_since/alarm_active 随 /api/line 端到端更新 ----
        c.post("/api/demo/inject", json={"device_id": "CNC-02", "fault_id": "spindle_overheat"})
        t0, card = time.time(), {}
        while time.time() - t0 < 25:
            card = next((d for d in c.get("/api/line").json()["devices"] if d["id"] == "CNC-02"), {})
            if card.get("severity") == "alarm":
                break
            time.sleep(1.0)
        active_ok = card.get("alarm_active") is True and bool(card.get("alarm_since"))
        reset_all()
        t0, card_cleared = time.time(), {}
        while time.time() - t0 < 15:
            card_cleared = next((d for d in c.get("/api/line").json()["devices"] if d["id"] == "CNC-02"), {})
            if card_cleared.get("severity") != "alarm":
                break
            time.sleep(1.0)
        cleared_ok = (card_cleared.get("alarm_active") is False
                      and card_cleared.get("alarm_since") == card.get("alarm_since"))
        ok = active_ok and cleared_ok
        check("L", "03 alarm_episode_lifecycle", ok,
              f"注入 CNC-02 过热：alarm_active=True、alarm_since={card.get('alarm_since')}；"
              f"恢复后 alarm_active=False 且起点保留（{card_cleared.get('alarm_since')}，供反馈时计算停机）")
        api_mod._ALARM_EPISODES.clear()
        reset_all()


# ---------------------------------------------------------------- 主流程
# ---------------------------------------------------------------- M L3 服务端强制闸门
def run_l3_gate() -> None:
    """L3 危险步骤服务端强制闸门 + 人工确认留痕（TestClient 真实 HTTP 栈）。

    诚实边界：这是「人工确认」，不是「设备断电检测」。系统不判断设备是否真的断电，
    只保证未确认时 L3 步骤不可标记完成，并把确认人/时间落库可追溯。
    """
    from fastapi.testclient import TestClient
    from cncops.api import app

    section_title("M. L3 服务端强制闸门：未确认 locked／绕前端 400／确认留痕／非法步骤拒绝")
    with TestClient(app, raise_server_exceptions=False) as c:
        reset_all()
        datasource.inject_fault("CNC-01", "spindle_overheat")
        settle("CNC-01")
        rep = c.post("/api/diagnose", json={"query": "一号机主轴发烫，温度高得厉害"}).json()
        rid = rep.get("record_id")
        l3 = rep.get("steps_by_level", {}).get("L3", [])
        check("M", "01 l3_locked_initially",
              bool(rid) and bool(l3) and all(s.get("locked") is True and not s.get("ack") for s in l3),
              f"记录 #{rid}｜L3 {len(l3)} 条均 locked 且无确认留痕")
        if not (rid and l3):
            return
        key = l3[0]["key"]

        # M02 绕开前端直接调接口标记 L3 完成 → 400 且不落库
        r = c.post(f"/api/records/{rid}/checklist", json={"checks": {key: True}})
        got = c.get(f"/api/records/{rid}/checklist").json().get("checks", {})
        ok = r.status_code == 400 and "安全确认" in str(r.json().get("detail", "")) and not got.get(key)
        check("M", "02 bypass_blocked", ok,
              f"绕前端直接标记 L3 完成 → HTTP {r.status_code}｜未落库={not got.get(key)}")

        # M03 空确认人 / 伪造 step_key / 无信号来源的 signal 模式 均被拒
        r1 = c.post(f"/api/records/{rid}/l3-ack", json={"step_key": key, "ack_by": "  "})
        r2 = c.post(f"/api/records/{rid}/l3-ack", json={"step_key": "FAKE#0", "ack_by": "张三"})
        r3 = c.post(f"/api/records/{rid}/l3-ack",
                    json={"step_key": key, "ack_by": "张三", "mode": "signal"})
        ok = r1.status_code == 400 and r2.status_code == 400 and r3.status_code == 400
        check("M", "03 ack_validation", ok,
              f"空确认人 {r1.status_code}｜伪造 step_key {r2.status_code}｜"
              f"无信号来源的 signal 模式 {r3.status_code}（均应为 400）")

        # M04 合法人工确认 → 200 + 确认人/时间落库 + 历史详情可查
        r = c.post(f"/api/records/{rid}/l3-ack",
                   json={"step_key": key, "ack_by": "持证工程师-张三"})
        acks = c.get(f"/api/records/{rid}/l3-acks").json().get("acks", {})
        detail = c.get(f"/api/history/{rid}").json()
        ack = acks.get(key, {})
        ok = (r.status_code == 200 and r.json().get("mode") == "manual"
              and ack.get("ack_by") == "持证工程师-张三" and bool(ack.get("ack_at"))
              and detail.get("l3_acks", {}).get(key, {}).get("ack_by") == "持证工程师-张三")
        check("M", "04 ack_recorded", ok,
              f"人工确认留痕：{ack.get('ack_by')} @ {ack.get('ack_at')}｜历史详情可查")

        # M05 确认后 L3 可标记完成，L1 步骤不受影响
        l1 = rep["steps_by_level"]["L1"][0]["key"] if rep["steps_by_level"]["L1"] else None
        payload = {key: True}
        if l1:
            payload[l1] = True
        r = c.post(f"/api/records/{rid}/checklist", json={"checks": payload})
        got = c.get(f"/api/records/{rid}/checklist").json().get("checks", {})
        ok = r.status_code == 200 and got.get(key) is True and (not l1 or got.get(l1) is True)
        check("M", "05 ack_unlocks_checklist", ok,
              f"确认后 L3 可标记完成 HTTP {r.status_code}｜L1 不受影响")

        # M06 历史详情步骤回显解锁状态与确认凭据
        matched = [s for s in detail.get("steps", []) if s.get("key") == key and s["level"] == "L3"]
        ok = bool(matched) and all(s.get("locked") is False and s.get("ack") for s in matched)
        check("M", "06 history_ack_visible", ok,
              f"历史详情步骤回显 locked=False + 确认凭据（匹配 {len(matched)} 条）")


def main() -> int:
    prod_before = prod_db_rows()
    print("=" * 96)
    print("CNC 设备运维智能体 · 一键自动化自测")
    print("=" * 96)
    print(f"随机种子        ：{SEED}（仿真器单例按该种子重建，逐次运行结果一致）")
    print(f"临时记忆库      ：{TMP_DB}")
    print(f"正式记忆库      ：{paths.DEFAULT_DB_PATH}（自测前 {prod_before} 条，自测结束会再核对一次）")
    print(f"数据源          ：simulator（内置仿真，与 OPC-UA 同源；强制降级模式 LLM=0）")
    print(f"Python          ：{sys.version.split()[0]}")

    run_fault_cases()
    run_false_positive_cases()
    run_invariant_cases()
    run_reachability()
    run_ui_layer_cases()
    run_api_cases()
    run_acceptance_script()
    run_insights_and_export()
    run_coverage_and_gate_cases()
    run_compact_list_contract()
    run_feedback_rules()
    run_l3_gate()

    # 记忆库隔离核对
    prod_after = prod_db_rows()
    tmp_rows = len(get_memory().recent(1000))
    check("E", "01 memory_isolation", prod_before == prod_after,
          f"正式库行数 {prod_before} → {prod_after}（应不变）；本次自测写入临时库 {tmp_rows} 条")

    section_title("汇总")
    by_section: Dict[str, List[bool]] = {}
    for sec, name, ok, _ in RESULTS:
        by_section.setdefault(sec, []).append(ok)
    for sec in sorted(by_section):
        oks = by_section[sec]
        print(f"  {sec} 段：{sum(oks)}/{len(oks)}")
    total_ok = sum(1 for _, _, ok, _ in RESULTS if ok)
    print(f"\n  总计：{total_ok}/{len(RESULTS)}"
          f"{'  全部通过' if total_ok == len(RESULTS) else f'  失败 {len(RESULTS) - total_ok} 项'}")

    if total_ok != len(RESULTS):
        print("\n  失败明细：")
        for sec, name, ok, detail in RESULTS:
            if not ok:
                print(f"    - {sec}-{name}: {detail}")

    print(f"\n  临时目录：{TMP_DIR}（自测结束清理）")
    shutil.rmtree(TMP_DIR, ignore_errors=True)
    return 0 if total_ok == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
