"""HTTP API 层（FastAPI）：为工业 HMI 前端提供**全部**动态数据。

设计要点：
1. 所有数值都来自数据源（内置仿真 / OPC-UA），接口层不做任何伪造，也不缓存"旧快照冒充实时"；
   数据源异常时返回 503，由前端显示「数据连接中断」而不是继续显示上一次的值。
2. 演示（故障注入）能力有服务端总开关 demo_allowed，关闭时注入接口返回 403，
   与前端"面板不可见"形成双保险。
3. 趋势数据来自后台采样线程的环形缓冲（monitor.TrendBuffer），用于报告卡里的迷你趋势图；
   缓冲缺失时返回空序列由前端显示"采集中"，不编造曲线。
4. 步骤勾选状态落库（step_checks），刷新页面后仍保留。
5. L3 危险步骤的服务端强制闸门：未经持证人员署名确认，L3 步骤不能标记完成
   （l3_acks 留痕，绕开前端直接调接口同样返回 400）。这是人工确认，不是断电检测。
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Dict, List, Optional

from . import catalog, config, datasource, export as export_mod, monitor, paths, rules
from .agent import diagnose, feedback
from .knowledge import get_kb
from .memory import get_memory

try:
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import JSONResponse, Response
    from fastapi.staticfiles import StaticFiles
    from pydantic import BaseModel
except ImportError as exc:  # pragma: no cover - 依赖缺失时给出明确提示
    raise ImportError("缺少后端依赖，请执行 pip install fastapi uvicorn") from exc


logger = logging.getLogger(__name__)

STATUS_META = {
    "RUN": {"text": "运行正常", "icon": "🟢", "severity": "normal"},
    "WARN": {"text": "有异常倾向", "icon": "🟡", "severity": "warn"},
    "ALARM": {"text": "报警", "icon": "🔴", "severity": "alarm"},
}
SEVERITY_META = {
    "normal": {"text": "正常", "icon": "🟢"},
    "warn": {"text": "偏高/偏低", "icon": "🟡"},
    "alarm": {"text": "超标", "icon": "🔴"},
}
SEVERITY_RANK = {"normal": 0, "warn": 1, "alarm": 2}
LEVEL_META = {
    "L1": {"text": "操作工可执行", "icon": "🟢", "cls": "lv1"},
    "L2": {"text": "维修工执行", "icon": "🟡", "cls": "lv2"},
    "L3": {"text": "必须工程师执行", "icon": "🔴", "cls": "lv3"},
}

# 后台采样缓冲：趋势曲线与迷你趋势图的数据来源
TREND = monitor.TrendBuffer()
# 采样周期（秒）。默认 1 秒：既要让迷你趋势图有足够密度，也要让故障注入后数秒内就有明显变化
TREND_INTERVAL = float(os.environ.get("CNCOPS_TREND_INTERVAL_SEC", "1.0") or 1.0)
_SAMPLER: Optional[monitor.TrendSampler] = None

# ---------------------------------------------------------------- 停机自动计时
# 设备报警事件跟踪（进程内内存态，服务重启后重新开始计时）：
#   - 设备进入报警且当前无事件 → 以当前时间记录事件起点；
#   - 报警持续 → 保留起点不清；
#   - 报警消失 → 事件标记 cleared（起点保留，供反馈时计算停机时长）；
#   - 报警再次出现 → 开启新事件（替换旧事件）；
#   - 反馈提交（确认解决）→ 事件标记 consumed：**即使报警仍在持续也不再起新表**
#     （停机计时已停），直到报警消失后事件关闭；此后再报警才开启新事件。
# 由 /api/line 每次被调用时更新（前端 2 秒轮询 ≈ 实时）。
# 反馈提交时消费该设备的事件：未填报停机时长则按"反馈时间 − 事件起点"自动计算
# （确认解决即停表），已填报则以填报值为准（事件同样消费，计时停止）。
_ALARM_EPISODES: Dict[str, Dict[str, Any]] = {}


def _update_alarm_episodes(cards: List[Dict[str, Any]]) -> None:
    """按本轮 /api/line 的设备状态更新报警事件（计时器的"走时"由轮询驱动）。"""
    now = datetime.now().isoformat(timespec="seconds")
    for card in cards:
        dev_id = card.get("id") or ""
        if not dev_id:
            continue
        ep = _ALARM_EPISODES.get(dev_id)
        if card.get("status") == "ALARM":
            if ep is None:
                _ALARM_EPISODES[dev_id] = {"start": now, "cleared": ""}
            elif ep.get("cleared") and not ep.get("consumed"):
                # 报警消失后再次出现 → 新事件；已确认解决（consumed）的不再起新表
                _ALARM_EPISODES[dev_id] = {"start": now, "cleared": ""}
        elif ep is not None:
            if ep.get("consumed"):
                _ALARM_EPISODES.pop(dev_id, None)      # 已确认解决且报警已消失：事件关闭
            elif not ep.get("cleared"):
                ep["cleared"] = now


def _consume_alarm_episode(device_id: str) -> Optional[Dict[str, Any]]:
    """标记该设备的报警事件为已消费：反馈确认解决 → 停机计时停止。

    事件不立即删除（报警可能仍在持续）：保留起点供同一事件的后续反馈沿用，
    并阻止"报警仍在持续就起新表"；报警消失后由 _update_alarm_episodes 关闭。
    """
    ep = _ALARM_EPISODES.get(device_id or "")
    if ep:
        ep["consumed"] = True
        if not ep.get("cleared"):
            ep["cleared"] = datetime.now().isoformat(timespec="seconds")
    return ep


def _alarm_episode_info(device_id: str) -> Dict[str, Any]:
    """该设备当前报警事件信息：起点时间 + 是否仍在报警（供前端展示与续计时）。"""
    ep = _ALARM_EPISODES.get(device_id or "") or {}
    return {
        "alarm_since": ep.get("start"),
        "alarm_active": bool(ep) and not ep.get("cleared"),
    }


# ---------------------------------------------------------------- 工具函数
def _metric_item(point_key: str, snapshot: dict, dev: catalog.Device) -> Optional[Dict[str, Any]]:
    """把一个测点整理成前端可直接渲染的项（值 + 阈值 + 等级 + 文案）。"""
    evidence = next((e for e in rules.build_evidence(snapshot) if e.point == point_key), None)
    if evidence is None:
        return None
    pt = dev.points.get(point_key)
    sev = SEVERITY_META.get(evidence.severity, SEVERITY_META["normal"])
    return {
        "key": point_key,
        "label": pt.label if pt else evidence.label,
        "value": round(float(evidence.value), 2),
        "unit": evidence.unit,
        "severity": evidence.severity,
        "severity_text": sev["text"],
        "severity_icon": sev["icon"],
        "warn": evidence.warn,
        "alarm": evidence.alarm,
        "direction": evidence.direction,
        "text": evidence.text,
        "hint": evidence.hint,
    }


def _device_card(snapshot: dict, dev: catalog.Device) -> Dict[str, Any]:
    """左侧设备卡：名称 + 状态灯（图标+文字，不单靠颜色）+ 关键读数。"""
    status = snapshot.get("status", "RUN")
    meta = STATUS_META.get(status, STATUS_META["RUN"])
    metrics = [m for k in dev.key_point_keys if (m := _metric_item(k, snapshot, dev))]
    worst = max((m["severity"] for m in metrics), key=lambda s: SEVERITY_RANK.get(s, 0), default="normal")
    return {
        "id": dev.id,
        "name": dev.name,
        "type": dev.type,
        "location": dev.location,
        "factory_id": dev.factory_id,
        "factory_name": catalog.factory_name(dev.factory_id),
        "status": status,
        "status_text": meta["text"],
        "status_icon": meta["icon"],
        "severity": meta["severity"],
        "worst_severity": worst,
        "alarm_codes": snapshot.get("alarm_codes", []),
        "alarm_messages": snapshot.get("alarm_messages", []),
        "update_time": snapshot.get("ts", ""),
        "data_source": snapshot.get("source", ""),
        "degraded": bool(snapshot.get("degraded")),
        "key_metrics": metrics,
        "point_count": len(dev.points),
    }


def _line_summary(cards: List[Dict[str, Any]]) -> Dict[str, Any]:
    """顶部状态条：一句话总览 + 当前时间。"""
    total = len(cards)
    alarms = [c for c in cards if c["status"] == "ALARM"]
    warns = [c for c in cards if c["status"] == "WARN"]
    normals = [c for c in cards if c["status"] == "RUN"]
    if alarms:
        worst, sentence = "alarm", f"{total} 台设备中 {len(alarms)} 台报警"
    elif warns:
        worst, sentence = "warn", f"{total} 台设备中 {len(warns)} 台需要注意"
    else:
        worst, sentence = "normal", f"{total} 台设备全部正常"
    return {
        "total": total,
        "normal": len(normals),
        "warn": len(warns),
        "alarm": len(alarms),
        "worst": worst,
        "sentence": sentence,
        "alarm_devices": [c["id"] for c in alarms],
    }


def _read_snapshot_or_503(device_id: str) -> dict:
    """读快照；失败即 503，让前端明确显示连接中断，而不是返回脏数据。"""
    try:
        snap = datasource.read_snapshot(device_id)
    except Exception as exc:                                   # pragma: no cover - 依赖真实故障
        logger.exception("读取设备快照失败")
        raise HTTPException(status_code=503, detail=f"数据连接中断：{exc}") from exc
    if not snap:
        raise HTTPException(status_code=503, detail="数据连接中断：未取到该设备数据")
    return snap


def _confidence(probability: float) -> Dict[str, str]:
    """把置信度数值映射成"高/中/低"，并给一句人话解释。"""
    if probability >= 0.55:
        return {"level": "high", "text": "高", "hint": "数据与知识库条目高度吻合"}
    if probability >= 0.32:
        return {"level": "mid", "text": "中", "hint": "有多条可能的故障，建议按顺序排查"}
    return {"level": "low", "text": "低", "hint": "证据不充分，需现场复核"}


def _series(device_id: str, point: str, n: int) -> List[Dict[str, Any]]:
    """取某测点最近 n 个采样点（供迷你趋势图/火花线使用）。"""
    items = TREND.series(device_id, point)[-n:]
    return [{"t": t, "v": round(float(v), 3)} for t, v in items]


def _cross_info(device_id: str, point: str, threshold: Optional[float], direction: str, n: int) -> Dict[str, Any]:
    """计算"从什么时候开始超的"：在窗口内找第一次越限的采样点。

    缓冲刚启动、样本不足时找不到越限点：此时若当前值确实越限，就把最后一个采样点
    作为"刚刚开始"的近似，避免前端显示"从未知时间起"这种没有信息量的文案。
    """
    interval = max(1, int(round(TREND_INTERVAL)))
    items = TREND.series(device_id, point)[-n:]
    if not items or threshold is None:
        return {"cross_time": None, "cross_points": 0, "over_seconds": 0}
    first: Optional[int] = None
    for i, (_t, v) in enumerate(items):
        crossed = (float(v) >= float(threshold)) if direction == "above" else (float(v) <= float(threshold))
        if crossed:
            first = i
            break
    if first is None:
        # 采样线程比诊断读取慢半拍：只要这条证据本身已判定异常，就把最后一个采样点
        # 作为起点近似值，并标注 approx，避免前端显示"未知起跳时间"。
        return {"cross_time": items[-1][0], "cross_points": 1,
                "over_seconds": interval, "approx": True}
    return {
        "cross_time": items[first][0],
        "cross_points": len(items) - first,
        "over_seconds": (len(items) - first) * interval,
        "approx": False,
    }


def _serialize_steps(raw_steps: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """把知识库步骤原文整理成前端清单项。

    每项包含：稳定的 key（勾选状态回传与刷新恢复用）、等级、操作内容、
    所需工具、所需物料、预计耗时、安全提示、来源条目编号。
    """
    out: List[Dict[str, Any]] = []
    for idx, st in enumerate(raw_steps):
        level = str(st.get("level", "L2")).upper()
        meta = LEVEL_META.get(level, LEVEL_META["L2"])
        entry = get_kb().get(st.get("kb_id", "")) if st.get("kb_id") else None
        out.append({
            "key": f"{st.get('kb_id', 'STEP')}#{idx}",
            "index": idx,
            "level": level,
            "level_text": meta["text"],
            "level_icon": meta["icon"],
            "level_cls": meta["cls"],
            "text": st.get("text", ""),
            "tools": list(st.get("tools") or []),
            "materials": list(st.get("materials") or []),
            "eta_min": int(st.get("eta_min") or 0),
            "safety": list(st.get("safety") or []),
            "danger_hits": list(st.get("danger_hits") or []),
            "danger_badge": ("⚠ 危险：" + "、".join(st.get("danger_hits") or [])) if st.get("danger_hits") else "",
            "kb_id": st.get("kb_id", ""),
            "kb_code": entry.code if entry else "",
            "kb_title": entry.title if entry else "",
        })
    return out


def _enrich_steps(report) -> List[Dict[str, Any]]:
    """诊断报告里的步骤（来源为智能体产出的步骤列表）。"""
    return _serialize_steps(report.steps)


def _report_payload(report, n_points: int = 40) -> Dict[str, Any]:
    """把诊断报告整理成前端要的完整结构（结论先行 + 证据带趋势 + 步骤带工具耗时）。"""
    steps = _enrich_steps(report)
    # L3 危险步骤的服务端闸门状态：未由持证人员署名确认前一律 locked。
    # 诚实边界：这是「人工确认留痕」，不是「设备断电检测」——只读数据源拿不到
    # 主电源与挂锁状态，物理安全由设备侧硬回路与现场 LOTO 规程承担。
    acks = get_memory().get_l3_acks(report.record_id) if report.record_id else {}
    for s in steps:
        if s["level"] == "L3":
            s["locked"] = s["key"] not in acks
            s["ack"] = acks.get(s["key"])
    top = report.causes[0] if report.causes else None
    top_entry = get_kb().get(top.kb_id) if top else None
    dev = catalog.get_device(report.device_id)

    evidences = []
    for ev in report.abnormal_evidence or []:
        thr_line = ev.get("alarm") if ev.get("severity") == "alarm" else ev.get("warn")
        info = _cross_info(report.device_id, ev["point"], thr_line, ev.get("direction", "above"), n_points)
        evidences.append({
            **ev,
            "spark": _series(report.device_id, ev["point"], n_points),
            "threshold_line": thr_line,
            "cross_time": info["cross_time"],
            "cross_points": info["cross_points"],
            "over_seconds": info["over_seconds"],
            "deviation": _deviation(ev),
        })

    history = []
    if report.history_ref:
        ref = report.history_ref
        resolved = ref.get("resolved")
        history.append({
            "id": ref.get("id"),
            "created_at": ref.get("created_at", ""),
            "device_id": ref.get("device_id", report.device_id),
            "conclusion": ref.get("conclusion", ""),
            "fault_title": ref.get("fault_title", ""),
            "result": "未反馈" if resolved is None else ("已解决" if int(resolved) == 1 else "未解决"),
            "comment": ref.get("fb_comment") or "",
            "similarity": "同一设备、同一故障指纹",
        })

    return {
        "record_id": report.record_id,
        "created_at": report.ts,
        "device": {
            "id": report.device_id,
            "name": report.device_name or (dev.name if dev else report.device_id),
            "type": report.device_type,
            "note": report.device_selection_note,
        },
        "query": report.query,
        "mode": report.mode,
        "llm_used": report.mode == "llm",
        "degraded": report.degraded,
        "degraded_reasons": report.degraded_reasons,
        "uncovered": report.uncovered,
        # ---- 第一行就是结论 ----
        "headline": {
            "device": report.device_name or report.device_id,
            "fault": top.title if top else ("未匹配到已知故障" if report.uncovered else "未见明确异常"),
            "code": top_entry.code if top_entry else (report.kb_hits[0].get("code", "") if report.kb_hits else ""),
            "kb_id": top.kb_id if top else "",
            "confidence": _confidence(top.probability) if top else {"level": "low", "text": "低", "hint": "无匹配条目"},
            "probability": round(top.probability, 3) if top else 0.0,
            "conclusion": report.conclusion,
        },
        "evidence": evidences,
        "all_evidence": report.evidence,
        "alarm_codes": report.alarm_codes,
        "alarm_messages": report.alarm_messages,
        "causes": [dict(c.to_dict(), code=(get_kb().get(c.kb_id).code if get_kb().get(c.kb_id) else ""))
                   for c in report.causes],
        "steps": steps,
        "steps_by_level": {"L1": [s for s in steps if s["level"] == "L1"],
                           "L2": [s for s in steps if s["level"] == "L2"],
                           "L3": [s for s in steps if s["level"] == "L3"]},
        "l3_acks": acks,
        "total_eta_min": sum(s["eta_min"] for s in steps),
        "warnings": report.warnings,
        "kb_hits": report.kb_hits,
        "history": history,
        "device_history": report.device_history[:5],
        "narrative": report.narrative,
    }


def _deviation(ev: Dict[str, Any]) -> str:
    """超标幅度文案：让新人一眼看出超了多少。"""
    thr = ev.get("alarm") if ev.get("severity") == "alarm" else ev.get("warn")
    if thr is None:
        return ""
    diff = float(ev["value"]) - float(thr)
    ratio = (abs(diff) / abs(float(thr))) * 100 if thr else 0
    rel = "高于" if float(ev["value"]) >= float(thr) else "低于"
    return f"{rel}阈值 {abs(diff):.2f}{ev.get('unit', '')}（{ratio:.0f}%）"


def _fault_code(fault: Dict[str, Any]) -> str:
    return str(fault.get("code") or fault.get("id") or "")


# ---------------------------------------------------------------- 请求体
class DiagnoseRequest(BaseModel):
    query: str
    device_id: Optional[str] = None
    operator: str = ""                     # 操作人（报修/提问人），落库到运维记录


class FeedbackRequest(BaseModel):
    resolved: bool
    comment: str = ""
    operator: str = ""                     # 操作人（实际处理人）——必填，空值返回 400
    handling_min: Optional[int] = None     # 处理时长（分钟），缺省按反馈时间差推算
    downtime_min: Optional[int] = None     # 停机时长（分钟）；缺省按报警起点自动计时，确认解决停表


class ChecklistRequest(BaseModel):
    checks: Dict[str, bool] = {}


class L3AckRequest(BaseModel):
    step_key: str
    ack_by: str = ""                       # 确认人（持证人员姓名/工号）——必填
    mode: str = "manual"                   # manual=人工确认 / signal=隔离信号（二期接真机）
    signal_source: str = ""                # signal 模式必须填真实信号来源，禁止空来源伪造


class InjectRequest(BaseModel):
    device_id: str
    fault_id: str


class ClearRequest(BaseModel):
    device_id: Optional[str] = None


# ---------------------------------------------------------------- 应用
@asynccontextmanager
async def lifespan(_app: FastAPI):
    """启动后台采样线程（趋势数据源），退出时停止。"""
    global _SAMPLER
    monitor.pump(TREND, rounds=20)                    # 先补几拍，页面打开即有像样的曲线
    _SAMPLER = monitor.TrendSampler(TREND, interval=TREND_INTERVAL)
    _SAMPLER.start()
    try:
        yield
    finally:
        if _SAMPLER:
            _SAMPLER.stop()


app = FastAPI(title="CNC 设备运维智能体 API", version="1.0.0", lifespan=lifespan)


@app.exception_handler(Exception)
async def unhandled_exception_handler(_request, exc: Exception):     # pragma: no cover
    """未捕获异常统一返回 503 语义的前端可读错误，避免前端拿到半截数据。"""
    logger.exception("接口异常")
    return JSONResponse(status_code=500, content={"detail": f"接口异常：{exc}"})


# ---------- 健康与产线总览 ----------
@app.get("/api/health")
def health() -> Dict[str, Any]:
    return {
        "status": "ok",
        "server_time": datetime.now().isoformat(timespec="seconds"),
        "data_source": config.settings().data_source,
        "trend_points": {d.id: TREND.length(d.id) for d in catalog.devices()},
        "demo_allowed": config.settings().demo_allowed,
    }


@app.get("/api/line")
def line_status() -> Dict[str, Any]:
    """顶部状态条 + 左侧紧凑摘要行（报警设备自动排到最前）。

    每次调用同时驱动停机自动计时器（_update_alarm_episodes）：前端 2 秒轮询一次，
    设备报警事件在此登记起点；设备卡附带 alarm_since/alarm_active 供前端展示。
    """
    cards: List[Dict[str, Any]] = []
    for dev in catalog.devices():
        snap = datasource.read_snapshot(dev.id)
        if snap:
            cards.append(_device_card(snap, dev))
        else:                                          # 单台读不到也要体现出来，不能静默跳过
            cards.append({
                "id": dev.id, "name": dev.name, "type": dev.type, "location": dev.location,
                "status": "OFFLINE", "status_text": "数据连接中断", "status_icon": "⚫",
                "severity": "offline", "worst_severity": "offline", "alarm_codes": [],
                "alarm_messages": ["取不到该设备数据"], "update_time": "", "data_source": "",
                "degraded": True, "key_metrics": [], "point_count": len(dev.points),
            })
    order = {d.id: i for i, d in enumerate(catalog.devices())}
    cards.sort(key=lambda c: ({"alarm": 0, "warn": 1, "offline": 2, "normal": 3}.get(c["severity"], 3),
                              order.get(c["id"], 99)))
    _update_alarm_episodes(cards)
    for card in cards:
        card.update(_alarm_episode_info(card["id"]))
    summary = _line_summary(cards)
    active = datasource.active_faults()
    for card in cards:
        card["active_fault"] = active.get(card["id"])
    return {
        "line": (config.load_yaml(str(paths.DEVICES_YAML)) or {}).get("line", "仿真加工线"),
        "server_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "summary": summary,
        "devices": cards,
        "active_faults": active,
    }


@app.get("/api/devices/{device_id}")
def device_detail(device_id: str, n: int = 40) -> Dict[str, Any]:
    """点击设备卡展开：该设备全部监测点 + 趋势。"""
    dev = catalog.get_device(device_id)
    if dev is None:
        raise HTTPException(status_code=404, detail=f"设备不存在：{device_id}")
    snap = _read_snapshot_or_503(device_id)
    metrics = [m for k in dev.points if (m := _metric_item(k, snap, dev)) and k not in ("status", "alarm_code")]
    return {
        "device": {
            "id": dev.id, "name": dev.name, "type": dev.type, "location": dev.location,
            "status": snap.get("status", ""), "status_text": STATUS_META.get(snap.get("status", ""), {}).get("text", ""),
            "status_icon": STATUS_META.get(snap.get("status", ""), {}).get("icon", ""),
            "update_time": snap.get("ts", ""), "data_source": snap.get("source", ""),
        },
        "points": metrics,
        "trend": {m["key"]: _series(device_id, m["key"], n) for m in metrics},
        "alarm_codes": snap.get("alarm_codes", []),
        "alarm_messages": snap.get("alarm_messages", []),
    }


@app.get("/api/trend/{device_id}")
def trend(device_id: str, n: int = 40) -> Dict[str, Any]:
    dev = catalog.get_device(device_id)
    if dev is None:
        raise HTTPException(status_code=404, detail=f"设备不存在：{device_id}")
    keys = [k for k in dev.points if k not in ("status", "alarm_code")]
    return {"device_id": device_id, "n": n,
            "series": {k: _series(device_id, k, n) for k in keys}}


# ---------- 诊断 ----------
@app.post("/api/diagnose")
def api_diagnose(req: DiagnoseRequest) -> Dict[str, Any]:
    query = (req.query or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="请先描述设备哪里不对劲")
    try:
        report = diagnose(query, device_id=req.device_id, operator=(req.operator or "").strip())
    except Exception as exc:                                        # pragma: no cover
        logger.exception("诊断失败")
        raise HTTPException(status_code=503, detail=f"数据连接中断：{exc}") from exc
    payload = _report_payload(report)
    if report.record_id:
        payload["checks"] = get_memory().get_step_checks(report.record_id)
    else:
        payload["checks"] = {}
    return payload


@app.get("/api/kb/{kb_id}")
def kb_detail(kb_id: str) -> Dict[str, Any]:
    """点击报告卡上的故障编号角标查看的知识库条目详情。"""
    entry = get_kb().get(kb_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"知识库条目不存在：{kb_id}")
    return {
        "kb_id": entry.id,
        "code": entry.code,
        "title": entry.title,
        "device_types": entry.device_types,
        "alarm_codes": entry.alarm_codes,
        "points": entry.points,
        "symptoms": entry.symptoms,
        "conclusion": entry.conclusion,
        "causes": entry.causes,
        "steps": [{"level": s.get("level"), "text": s.get("text", ""),
                   "tools": s.get("tools", []), "materials": s.get("materials", []),
                   "eta_min": s.get("eta_min", 0)} for s in entry.steps],
        "safety": entry.safety,
        "source_file": f"config/kb/{entry.source_file}",
    }


# ---------- L3 安全确认 + 步骤勾选 ----------
def _l3_step_keys(record_id: int) -> set:
    """该次诊断里所有 L3 步骤的 key（服务端从落库步骤原文还原，不信任前端传参）。"""
    row = get_memory().get(record_id)
    if row is None:
        return set()
    return {s["key"] for s in _serialize_steps(row.get("steps") or []) if s["level"] == "L3"}


@app.get("/api/records/{record_id}/l3-acks")
def get_l3_acks(record_id: int) -> Dict[str, Any]:
    if get_memory().get(record_id) is None:
        raise HTTPException(status_code=404, detail=f"诊断记录不存在：{record_id}")
    return {"record_id": record_id, "acks": get_memory().get_l3_acks(record_id)}


@app.post("/api/records/{record_id}/l3-ack")
def api_l3_ack(record_id: int, req: L3AckRequest) -> Dict[str, Any]:
    """L3 危险步骤安全确认：服务端强制闸门 + 确认留痕。

    诚实边界：这是**人工确认**，不是设备断电检测。只读数据源拿不到主电源与挂锁状态，
    确认内容由现场持证人员作出；系统只保证未确认时 L3 步骤不可标记完成，并把确认人
    与时间落库可追溯。物理安全由设备侧硬回路与现场 LOTO 规程承担。
    """
    if get_memory().get(record_id) is None:
        raise HTTPException(status_code=404, detail=f"诊断记录不存在：{record_id}")
    key = (req.step_key or "").strip()
    if key not in _l3_step_keys(record_id):
        raise HTTPException(status_code=400, detail="该步骤不是本次诊断的 L3 危险步骤，无需/无法确认")
    ack_by = (req.ack_by or "").strip()
    if not ack_by:
        raise HTTPException(status_code=400, detail="确认人不能为空：请填写持证人员姓名/工号")
    mode = (req.mode or "manual").strip()
    if mode not in ("manual", "signal"):
        raise HTTPException(status_code=400, detail="确认模式只能是 manual（人工）或 signal（隔离信号）")
    signal_source = (req.signal_source or "").strip()
    if mode == "signal" and not signal_source:
        raise HTTPException(status_code=400, detail="signal 模式必须提供真实隔离信号来源，禁止空来源伪造")
    ack = get_memory().add_l3_ack(record_id, key, ack_by, mode, signal_source)
    return {"record_id": record_id, **ack}


@app.get("/api/records/{record_id}/checklist")
def get_checklist(record_id: int) -> Dict[str, Any]:
    return {"record_id": record_id, "checks": get_memory().get_step_checks(record_id)}


@app.post("/api/records/{record_id}/checklist")
def save_checklist(record_id: int, req: ChecklistRequest) -> Dict[str, Any]:
    """勾选持久化；L3 步骤未经服务端安全确认一律拒绝标记完成（绕前端也拦得住）。"""
    if get_memory().get(record_id) is None:
        raise HTTPException(status_code=404, detail=f"诊断记录不存在：{record_id}")
    checks = req.checks or {}
    l3_keys = _l3_step_keys(record_id)
    acks = get_memory().get_l3_acks(record_id)
    existing = get_memory().get_step_checks(record_id)
    # 只拦「本次新勾选」的 L3 步骤；老库里改造前已存在的勾选不重复拦，避免误伤历史数据
    blocked = [k for k, v in checks.items()
               if v and k in l3_keys and k not in acks and not existing.get(k)]
    if blocked:
        # 先整体校验再逐条写入：拒绝时不留下半条勾选
        raise HTTPException(
            status_code=400,
            detail="L3 危险步骤未完成安全确认：请先由持证人员确认断电挂牌上锁（LOTO）并署名后再标记完成")
    for key, checked in checks.items():
        get_memory().set_step_check(record_id, str(key), bool(checked))
    return {"record_id": record_id, "checks": get_memory().get_step_checks(record_id)}


# ---------- 反馈 ----------
@app.post("/api/records/{record_id}/feedback")
def api_feedback(record_id: int, req: FeedbackRequest) -> Dict[str, Any]:
    """反馈回填：处理结果 + 处理人（必填）+ 处理/停机时长。

    两条产品规则在此强制：
    1. 操作人（姓名/工号）必填——空姓名直接 400，不落库；
    2. 停机时长自动计时——未填报时按"设备报警起点 → 现在"计算分钟数并写入记录，
       同时消费该设备的报警事件（确认解决即停表）；已填报则以填报值为准。
    """
    row = get_memory().get(record_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"诊断记录不存在：{record_id}")
    operator = (req.operator or "").strip()
    if not operator:
        raise HTTPException(status_code=400,
                            detail="操作人不能为空：请填写处理人姓名/工号后再提交反馈")
    handling_min = req.handling_min if (req.handling_min is not None and req.handling_min >= 0) else None
    downtime_min = req.downtime_min if (req.downtime_min is not None and req.downtime_min >= 0) else None
    # 停机自动计时：反馈即"确认解决"，无论是否填报都先消费报警事件（停表）
    downtime_auto = False
    episode = _consume_alarm_episode(row.get("device_id") or "")
    if downtime_min is None and episode:
        try:
            started = datetime.fromisoformat(str(episode.get("start")))
            downtime_min = max(0, int(round((datetime.now() - started).total_seconds() / 60)))
            downtime_auto = True
        except (ValueError, TypeError):
            downtime_min = None
    fb_id = feedback(record_id, resolved=req.resolved, comment=req.comment or "",
                     operator=operator,
                     handling_min=handling_min, downtime_min=downtime_min)
    item = next((r for r in get_memory().recent(500) if r.get("id") == record_id), {})
    return {
        "ok": True,
        "feedback_id": fb_id,
        "record_id": record_id,
        "resolved": bool(req.resolved),
        "comment": req.comment or "",
        "message": ("已记录：本次处理有效，下次会优先推荐。" if req.resolved
                    else "已记录：下次会下调该原因排序并提示升级工程师。"),
        "downtime_min": downtime_min,
        "downtime_auto": downtime_auto,
        "row": item,
    }


# ---------- 历史 ----------
@app.get("/api/history")
def history_list(device: str = "", q: str = "", result: str = "", limit: int = 100) -> Dict[str, Any]:
    mem = get_memory()
    if device:
        rows = mem.device_history(device, limit=limit)
    else:
        rows = mem.recent(limit)
    kw = (q or "").strip().lower()

    def keep(row: Dict[str, Any]) -> bool:
        resolved = row.get("resolved")
        state = "未反馈" if resolved is None else ("已解决" if int(resolved) == 1 else "未解决")
        if result and state != result:
            return False
        if not kw:
            return True
        hay = " ".join([str(row.get("device_id", "")), str(row.get("query", "")),
                        str(row.get("conclusion", "")), str(row.get("fault_title", "")),
                        str(row.get("max_level", ""))]).lower()
        return kw in hay

    items = []
    for r in rows:
        resolved = r.get("resolved")
        entry = get_kb().get(r.get("fault_id", "")) if r.get("fault_id") else None
        items.append({
            "id": r.get("id"),
            "created_at": r.get("created_at", ""),
            "device_id": r.get("device_id", ""),
            "device_name": r.get("device_name", ""),
            "factory_id": r.get("factory_id", ""),
            "factory_name": catalog.factory_name(r.get("factory_id", "")),
            "operator": r.get("operator", ""),
            "fb_operator": r.get("fb_operator", ""),
            "handling_min": get_memory().handling_minutes(r),
            "downtime_min": r.get("downtime_min"),
            "query": r.get("query", ""),
            "conclusion": r.get("conclusion", ""),
            "fault_id": r.get("fault_id", ""),
            "fault_title": r.get("fault_title", ""),
            "fault_code": entry.code if entry else "",
            "max_level": r.get("max_level", ""),
            "mode": r.get("mode", ""),
            "result": "未反馈" if resolved is None else ("已解决" if int(resolved) == 1 else "未解决"),
            "resolved": resolved,
            "comment": r.get("fb_comment") or "",
        })
    items = [i for i in items if keep(i)]
    return {"items": items, "count": len(items), "devices": [d.id for d in catalog.devices()],
            "factories": [{"id": f.id, "name": f.name} for f in catalog.factories()]}


@app.get("/api/history/{record_id}")
def history_detail(record_id: int) -> Dict[str, Any]:
    row = get_memory().get(record_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"诊断记录不存在：{record_id}")
    entry = get_kb().get(row.get("fault_id", "")) if row.get("fault_id") else None
    snapshot = row.get("snapshot") or {}
    steps_src = []
    for kb_id in (row.get("kb_ids") or []):
        ent = get_kb().get(kb_id)
        if not ent:
            continue
        steps_src.extend(ent.steps)
    # 步骤原文已在诊断时落库，历史页用同一套 key 还原当时的勾选状态
    steps = _serialize_steps(row.get("steps") or steps_src)
    acks = get_memory().get_l3_acks(record_id)
    for s in steps:
        if s["level"] == "L3":
            s["locked"] = s["key"] not in acks
            s["ack"] = acks.get(s["key"])
    checks = get_memory().get_step_checks(record_id)
    resolved = row.get("resolved")
    return {
        "id": row.get("id"),
        "created_at": row.get("created_at", ""),
        "device_id": row.get("device_id", ""),
        "device_name": row.get("device_name", ""),
        "factory_id": row.get("factory_id", ""),
        "factory_name": catalog.factory_name(row.get("factory_id", "")),
        "operator": row.get("operator", ""),
        "query": row.get("query", ""),
        "conclusion": row.get("conclusion", ""),
        "fault_id": row.get("fault_id", ""),
        "fault_code": entry.code if entry else "",
        "fault_title": row.get("fault_title", ""),
        "max_level": row.get("max_level", ""),
        "mode": row.get("mode", ""),
        "llm_used": bool(row.get("llm_used")),
        "result": "未反馈" if resolved is None else ("已解决" if int(resolved) == 1 else "未解决"),
        "fb_operator": row.get("fb_operator", ""),
        "handling_min": get_memory().handling_minutes(row),
        "downtime_min": row.get("downtime_min"),
        "comment": row.get("fb_comment") or "",
        "snapshot": snapshot.get("values", {}) if isinstance(snapshot, dict) else {},
        "evidence": row.get("evidence") or [],
        "steps": steps,
        "checks": checks,
        "l3_acks": acks,
        "checked_count": sum(1 for v in checks.values() if v),
        "step_count": len(steps),
    }


# ---------- 多工厂目录 ----------
@app.get("/api/factories")
def factory_list() -> Dict[str, Any]:
    """工厂列表（含设备数与运维记录数）：经验沉淀/历史页的工厂筛选器数据源。

    目录 = 配置声明的工厂 ∪ 记忆库里已登记过的工厂（如演示/迁移进来的历史工厂），
    保证"有记录的工厂"在筛选器里一定可见（自测 I-07 暴露只读配置会漏工厂的问题）。
    """
    mem = get_memory()
    counts = mem.factory_record_counts()
    db_factories = mem.known_factories()
    items = []
    seen = set()
    for fac in list(catalog.factories()) + [
            catalog.Factory(f["id"], f["name"] or f["id"], f.get("tenant_id") or "default")
            for f in db_factories]:
        if fac.id in seen:
            continue
        seen.add(fac.id)
        devices = [d for d in catalog.devices() if d.factory_id == fac.id]
        items.append({
            "id": fac.id, "name": fac.name, "tenant_id": fac.tenant_id,
            "device_count": len(devices), "record_count": counts.get(fac.id, 0),
        })
    return {"items": items, "tenant_id": config.settings().tenant_id}


# ---------- 经验沉淀（运维复盘） ----------
@app.get("/api/insights")
def insights(factory_id: str = "", device_id: str = "", days: int = 90,
             recur_days: int = 30) -> Dict[str, Any]:
    """经验沉淀视图：按设备统计高频故障/平均处理时长 + 复发故障提醒。

    - days：统计窗口（默认近 90 天）；
    - recur_days：复发判定窗口（验收口径：同设备 30 天内同故障 ≥2 次即提醒，≥3 次升级）。
    """
    days = max(1, min(int(days or 90), 3650))
    recur_days = max(1, min(int(recur_days or 30), 365))
    mem = get_memory()
    devices = mem.device_stats(device_id=device_id or None,
                               factory_id=factory_id or None, days=days)
    for dev in devices:
        for f in dev["top_faults"]:
            entry = get_kb().get(f["fault_id"])
            f["fault_code"] = entry.code if entry else ""
    recurrences = mem.recurring_faults(days=recur_days, device_id=device_id or None,
                                       factory_id=factory_id or None)
    for rec in recurrences:
        entry = get_kb().get(rec["fault_id"])
        rec["fault_code"] = entry.code if entry else ""
        rec["device_name"] = rec.get("device_name") or rec["device_id"]
    summary = mem.insights_summary(device_id=device_id or None,
                                   factory_id=factory_id or None, days=days)
    summary["recurrence_count"] = len(recurrences)
    return {
        "window_days": days,
        "recurrence_days": recur_days,
        "summary": summary,
        "devices": devices,
        "recurrences": recurrences,
        "factories": [{"id": f.id, "name": f.name} for f in catalog.factories()],
    }


# ---------- 数据导出（CSV / Excel） ----------
@app.get("/api/records/export")
def records_export(fmt: str = "csv", device: str = "", factory_id: str = "",
                   result: str = "", q: str = "", days: int = 0):
    """导出运维记录：fmt=csv|xlsx。

    CSV 为 utf-8-sig（Excel/WPS 双击直接打开）；xlsx 含「运维记录」明细 +
    「故障与停机统计」汇总两张表（工厂老板视角：哪个设备、哪个故障、坏几次、停多久）。
    """
    fmt = (fmt or "csv").lower()
    if fmt not in ("csv", "xlsx"):
        raise HTTPException(status_code=400, detail="仅支持 csv 或 xlsx 两种格式")
    if result and result not in ("已解决", "未解决", "未反馈"):
        raise HTTPException(status_code=400, detail=f"不支持的处理结果筛选：{result}")
    days_val = int(days or 0)
    if days_val < 0:
        days_val = 0
    data = export_mod.collect(get_memory(), device_id=device, factory_id=factory_id,
                              result=result, keyword=q, days=days_val or None)
    filename = export_mod.export_filename(fmt)
    if fmt == "csv":
        content = export_mod.build_csv(data["rows"])
        media = "text/csv; charset=utf-8-sig"
    else:
        content = export_mod.build_xlsx(data["rows"], data["stats"])
        media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    # RFC 5987：中文文件名同时给 ASCII 兜底与 UTF-8 编码值，避免各浏览器下载乱码
    from urllib.parse import quote
    ascii_name = f"cncops_records_{fmt}"
    disposition = (f"attachment; filename=\"{ascii_name}\"; "
                   f"filename*=UTF-8''{quote(filename)}")
    return Response(content=content, media_type=media,
                    headers={"Content-Disposition": disposition})


# ---------- 演示模式（故障注入），受服务端总开关约束 ----------
def _require_demo() -> None:
    if not config.settings().demo_allowed:
        raise HTTPException(status_code=403, detail="演示模式已在服务端关闭（CNCOPS_DEMO_ALLOW=0）")


@app.get("/api/demo/status")
def demo_status() -> Dict[str, Any]:
    allowed = config.settings().demo_allowed
    return {
        "allowed": allowed,
        "reason": "演示模式可用（数据源为仿真器）" if allowed else "已被 CNCOPS_DEMO_ALLOW=0 关闭",
        "active_faults": datasource.active_faults(),
    }


@app.get("/api/demo/catalog")
def demo_catalog() -> Dict[str, Any]:
    _require_demo()
    faults = datasource.simulator().list_faults()
    items = []
    for dev in catalog.devices():
        items.append({
            "device_id": dev.id,
            "device_name": dev.name,
            "faults": [{"id": f["id"], "name": f.get("name", f["id"]), "desc": f.get("desc", "")}
                       for f in faults if dev.type in f.get("device_types", [])],
        })
    return {"devices": items, "active_faults": datasource.active_faults()}


@app.post("/api/demo/inject")
def demo_inject(req: InjectRequest) -> Dict[str, Any]:
    _require_demo()
    dev = catalog.get_device(req.device_id)
    if dev is None:
        raise HTTPException(status_code=404, detail=f"设备不存在：{req.device_id}")
    known = {f["id"] for f in datasource.simulator().list_faults() if dev.type in f.get("device_types", [])}
    if req.fault_id not in known:
        raise HTTPException(status_code=400, detail=f"该设备不支持的故障类型：{req.fault_id}")
    try:
        datasource.inject_fault(req.device_id, req.fault_id)
    except Exception as exc:                                        # pragma: no cover
        raise HTTPException(status_code=503, detail=f"注入失败：{exc}") from exc
    return {"ok": True, "device_id": req.device_id, "fault_id": req.fault_id,
            "active_faults": datasource.active_faults(),
            "message": f"已向 {req.device_id} 注入「{req.fault_id}」，设备数据将在数秒内变化。"}


@app.post("/api/demo/clear")
def demo_clear(req: ClearRequest) -> Dict[str, Any]:
    _require_demo()
    datasource.clear_faults(req.device_id)
    return {"ok": True, "active_faults": datasource.active_faults(),
            "message": f"{req.device_id or '全部设备'} 已恢复正常"}


# ---------- 静态前端（web/static 存在时才挂载） ----------
WEB_DIR = paths.ROOT / "web" / "static"
if WEB_DIR.exists():                                                # pragma: no cover - 运行环境相关
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
