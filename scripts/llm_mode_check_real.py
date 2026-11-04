"""真实大模型增强验证（DeepSeek / 任意 OpenAI 兼容服务）。

设计铁律：
  1. 真实 Key / Base / Model 全部来自**调用进程的环境变量**，本脚本不写、不读、不回显
     任何密钥；运行结束后项目默认配置不残留任何 Key（仍为无 Key 可跑的降级模式）；
  2. 复用 llm_mode_check.py 的 17 项链路断言（其第 7 项"【模拟增强】"标记是 mock 专用，
     对真实模型改为"增强结论≠降级结论且非空"，意图不变：证明模型真改写了结论；
     第 8 项"原因带大模型补充依据"对真实模型仍有效，不过就如实记不过）；
  3. 额外加第 18 项：用坏端点实例验证"调用失败 / 超时仍自动降级出完整 7 块报告"；
  4. 用 11 类可注入故障（CNC 6 + 空压机 5）逐个在增强开／关两模式下跑，记录实际差异；
  5. 不许改阈值、不许删用例过关：有一项不过就如实写不过。

运行（Key 由环境变量提供，不落盘）：
  CNCOPS_LLM_API_BASE=https://api.deepseek.com/v1 \
  CNCOPS_LLM_API_KEY=<真实Key> \
  CNCOPS_LLM_MODEL=deepseek-chat \
  python scripts/llm_mode_check_real.py

退出码 0 = 链路断言全部通过；逐条结果 + 11 类对比原始数据写入
  docs/真实模型增强验证_raw.txt（该文件不含任何密钥）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Tuple

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

REAL_ENH_PORT = 8151           # 增强实例（指向真实大模型，环境变量继承）
DEG_PORT = 8152                # 降级实例（无 LLM 配置）
BROKEN_PORT = 8153             # 坏端点实例（验证失败自动降级）

# 11 类可注入故障：fault_id -> (设备, 口语问诊)
FAULTS: List[Tuple[str, str, str]] = [
    ("spindle_overheat", "CNC-01", "一号机主轴发烫，温度高得厉害，冷却液好像也不太够"),
    ("spindle_vibration", "CNC-01", "一号机主轴振动得厉害，声音听着不对"),
    ("spindle_bearing_wear", "CNC-01", "一号机主轴又响又烫，振动和温度都上来了"),
    ("spindle_overload", "CNC-01", "一号机主轴负载特别高，像闷车了，进给也掉速"),
    ("coolant_low", "CNC-01", "一号机冷却液好像不够了，主轴温度慢慢往上走"),
    ("cnc_air_low", "CNC-01", "一号机进气压力不足，换刀夹紧都没力气"),
    ("air_pressure_low", "AIR-01", "空压机出口压力上不去，才零点四几，加载率和电流都高"),
    ("air_overheat", "AIR-01", "空压机排气温度太高了，油温也跟着高"),
    ("air_oil_overheat", "AIR-01", "空压机润滑油温度单独飙到报警区，排气还没超"),
    ("air_motor_overload", "AIR-01", "空压机电机电流高、满载运行，担心跳闸"),
    ("air_maintenance_due", "AIR-01", "空压机运行小时到保养周期了，该换油换滤芯了吧"),
]

RESULTS: List[Tuple[str, bool, str]] = []
OUT_LINES: List[str] = []


def say(line: str = "") -> None:
    print(line)
    OUT_LINES.append(line)


def expect(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    say(f"  [{'PASS' if ok else 'FAIL'}] {name}{('｜' + detail) if detail else ''}")


# ---------------------------------------------------------------- 实例管理
def start_server(port: int, db_path: Path, llm_mode: str) -> subprocess.Popen:
    """llm_mode: 'real' 继承真实 LLM 环境变量；'none' 清空 LLM 变量；'broken' 指向不可达端点。"""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC)
    env["CNCOPS_DEVICES_YAML"] = str(ROOT / "config" / "devices.yaml")
    env["CNCOPS_DB_PATH"] = str(db_path)
    env["CNCOPS_DEMO_ALLOW"] = "1"
    env["CNCOPS_LOG_LEVEL"] = "WARNING"
    for k in ("CNCOPS_LLM_API_BASE", "CNCOPS_LLM_API_KEY", "CNCOPS_LLM_MODEL",
              "CNCOPS_LLM_ENABLED", "CNCOPS_LLM_TIMEOUT_SEC"):
        env.pop(k, None)
    if llm_mode == "real":
        # 直接继承调用进程提供的真实环境变量（Key 不在此处出现，也不落盘）
        for k in ("CNCOPS_LLM_API_BASE", "CNCOPS_LLM_API_KEY", "CNCOPS_LLM_MODEL",
                  "CNCOPS_LLM_ENABLED", "CNCOPS_LLM_TIMEOUT_SEC"):
            if os.environ.get(k):
                env[k] = os.environ[k]
        env.setdefault("CNCOPS_LLM_MODEL", "deepseek-chat")
        env.setdefault("CNCOPS_LLM_TIMEOUT_SEC", "12")
    elif llm_mode == "broken":
        env["CNCOPS_LLM_API_BASE"] = "http://127.0.0.1:1/v1"   # 不可达：连接被拒，快速失败
        env["CNCOPS_LLM_API_KEY"] = "dummy-broken-token"
        env["CNCOPS_LLM_MODEL"] = "deepseek-chat"
        env["CNCOPS_LLM_TIMEOUT_SEC"] = "4"
    log = db_path.with_suffix(".server.log")
    f = open(log, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "cncops.api:app",
         "--host", "127.0.0.1", "--port", str(port)],
        stdout=f, stderr=subprocess.STDOUT, env=env, cwd=str(ROOT))
    proc._logfile = f  # type: ignore[attr-defined]
    return proc


def wait_health(port: int, timeout: float = 40.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as r:
                if r.status == 200:
                    return
        except Exception:
            pass
        time.sleep(0.4)
    raise RuntimeError(f"服务 {port} 端口 {timeout}s 内未就绪")


def stop_server(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        proc.kill()
    try:
        proc._logfile.close()  # type: ignore[attr-defined]
    except Exception:
        pass


def api_post(port: int, path: str, body: dict) -> dict:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.loads(r.read().decode("utf-8"))


def api_get(port: int, path: str) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def inject_and_clear(port: int, device: str, fault: str) -> float:
    """注入故障，轮询到报警，返回耗时；调用方负责 clear。"""
    t0 = time.time()
    api_post(port, "/api/demo/inject", {"device_id": device, "fault_id": fault})
    while time.time() - t0 < 25:
        time.sleep(1.0)
        line = api_get(port, "/api/line")
        dev = next((d for d in line.get("devices", []) if d.get("id") == device), {})
        if dev.get("status") == "ALARM":
            return time.time() - t0
    return -1.0


def clear_fault(port: int, device: str) -> None:
    try:
        api_post(port, "/api/demo/clear", {"device_id": device})
    except Exception:
        pass


def diagnose_timed(port: int, query: str) -> Tuple[dict, float]:
    t0 = time.time()
    rep = api_post(port, "/api/diagnose", {"query": query})
    return rep, time.time() - t0


# ---------------------------------------------------------------- 主流程
def main() -> int:
    say("=" * 70)
    say("真实大模型增强验证（DeepSeek / OpenAI 兼容）")
    say("=" * 70)

    base = os.environ.get("CNCOPS_LLM_API_BASE")
    key = os.environ.get("CNCOPS_LLM_API_KEY")
    model = os.environ.get("CNCOPS_LLM_MODEL", "deepseek-chat")
    if not (base and key):
        say("✗ 未检测到真实 LLM 环境变量（CNCOPS_LLM_API_BASE / CNCOPS_LLM_API_KEY）。")
        say("  请先通过环境变量提供真实 Key 后重试；本脚本不读取任何文件中的密钥。")
        return 2
    # 仅在日志中记录非敏感的端点与模型（绝不记录 Key）
    say(f"  目标模型接口：{base} ｜ 模型：{model}")
    say(f"  （密钥由环境变量传入，全程不打印、不落盘、不进报告）")

    tmp = Path(tempfile.mkdtemp(prefix="cncops_real_llm_"))
    enh = start_server(REAL_ENH_PORT, tmp / "enh.db", "real")
    deg = start_server(DEG_PORT, tmp / "deg.db", "none")
    broken = start_server(BROKEN_PORT, tmp / "broken.db", "broken")
    try:
        wait_health(REAL_ENH_PORT)
        wait_health(DEG_PORT)
        wait_health(BROKEN_PORT)
        say("\n── 三实例就绪：增强(real) / 降级(none) / 坏端点(broken) ──")

        query = "一号机主轴发烫，温度高得厉害"
        say(f"\n── 同一口语问诊（链路检查）：{query} ──")

        say("\n── 增强实例：注入 CNC-01 主轴过热 ──")
        t_alarm = inject_and_clear(REAL_ENH_PORT, "CNC-01", "spindle_overheat")
        expect("增强实例：注入后检出报警", t_alarm > 0, f"耗时 {t_alarm:.1f}s")

        say("\n── 降级实例：注入同一故障 ──")
        t_alarm2 = inject_and_clear(DEG_PORT, "CNC-01", "spindle_overheat")
        expect("降级实例：注入后检出报警", t_alarm2 > 0, f"耗时 {t_alarm2:.1f}s")

        rep_enh, t_enh = diagnose_timed(REAL_ENH_PORT, query)
        rep_deg, t_deg = diagnose_timed(DEG_PORT, query)

        h_enh = rep_enh.get("headline") or {}
        h_deg = rep_deg.get("headline") or {}

        say("\n── 增强实例断言（mode=llm）──")
        expect("增强实例：mode=llm（大模型增强生效）", rep_enh.get("mode") == "llm",
               f"mode={rep_enh.get('mode')}")
        expect("增强实例：degraded=False", rep_enh.get("degraded") is False,
               f"degraded={rep_enh.get('degraded')}｜reasons={rep_enh.get('degraded_reasons')}")
        expect("增强实例：llm_used=True", rep_enh.get("llm_used") is True,
               f"llm_used={rep_enh.get('llm_used')}")
        hist_enh = api_get(REAL_ENH_PORT, f"/api/history/{rep_enh.get('record_id')}")
        expect("增强实例：历史详情接口 llm_used=True（记忆库落库值）",
               hist_enh.get("llm_used") is True and hist_enh.get("mode") == "llm",
               f"mode={hist_enh.get('mode')}｜llm_used={hist_enh.get('llm_used')}")
        enh_concl = str((rep_enh.get("headline") or {}).get("conclusion", ""))
        deg_concl = str((rep_deg.get("headline") or {}).get("conclusion", ""))
        expect("增强实例：结论为大模型改写文本（非空且≠降级结论）",
               bool(enh_concl.strip()) and enh_concl != deg_concl,
               f"增强结论={enh_concl[:50]}｜降级结论={deg_concl[:50]}")
        causes = rep_enh.get("causes") or []
        expect("增强实例：原因排序带「大模型补充」依据",
               any("大模型补充" in str(c.get("basis", "")) for c in causes),
               f"causes={len(causes)}｜首条依据含大模型补充="
               f"{any('大模型补充' in str(c.get('basis','')) for c in causes)}")
        expect("增强实例：narrative 非空（语言组织层输出）",
               bool(str(rep_enh.get("narrative") or "").strip()),
               str(rep_enh.get("narrative") or "")[:50])
        expect("增强实例：排查步骤仍取自知识库（步骤数>0 且带 kb_id）",
               len(rep_enh.get("steps") or []) > 0
               and all(s.get("kb_id") for s in rep_enh.get("steps") or []),
               f"steps={len(rep_enh.get('steps') or [])}")

        say("\n── 降级实例断言（mode=rule）──")
        expect("降级实例：mode=rule（规则版）", rep_deg.get("mode") == "rule",
               f"mode={rep_deg.get('mode')}")
        expect("降级实例：degraded=True 且原因=未配置", rep_deg.get("degraded") is True
               and any("未配置" in str(r) for r in rep_deg.get("degraded_reasons") or []),
               f"reasons={rep_deg.get('degraded_reasons')}")
        expect("降级实例：报告格式完整（七块字段齐全）",
               bool(rep_deg.get("headline")) and len(rep_deg.get("evidence") or []) >= 1
               and len(rep_deg.get("steps") or []) > 0,
               f"证据={len(rep_deg.get('evidence') or [])}｜步骤={len(rep_deg.get('steps') or [])}")
        expect("降级实例：llm_used=False（两处接口一致）",
               rep_deg.get("llm_used") is False
               and api_get(DEG_PORT, f"/api/history/{rep_deg.get('record_id')}").get("llm_used") is False,
               f"diagnose={rep_deg.get('llm_used')}")

        say("\n── 两模式对比（同题同设备同故障）──")
        expect("两模式识别同一设备同一 Top-1 故障",
               (rep_enh.get("device") or {}).get("id") == (rep_deg.get("device") or {}).get("id") == "CNC-01"
               and h_enh.get("kb_id") == h_deg.get("kb_id"),
               f"enh={h_enh.get('kb_id')}｜deg={h_deg.get('kb_id')}")
        expect("两模式排查步骤完全相同（步骤恒取自知识库原文）",
               [s.get("text") for s in rep_enh.get("steps") or []]
               == [s.get("text") for s in rep_deg.get("steps") or []],
               f"enh 步骤={len(rep_enh.get('steps') or [])}｜deg 步骤={len(rep_deg.get('steps') or [])}")
        say(f"  耗时对比：增强 {t_enh:.2f}s ｜ 降级 {t_deg:.2f}s（真实模型，仅链路参考）")
        say(f"  报警检出：增强实例 {t_alarm:.1f}s ｜ 降级实例 {t_alarm2:.1f}s")
        clear_fault(REAL_ENH_PORT, "CNC-01")
        clear_fault(DEG_PORT, "CNC-01")

        say("\n── 安全边界复核 ──")
        forbidden = [k for k in ("command", "write", "setpoint", "control") if k in rep_enh]
        expect("增强实例响应无控制类字段", not forbidden, f"forbidden={forbidden}")

        say("\n── 第 18 项：坏端点实例自动降级（超时/失败不崩）──")
        t_alarm_b = inject_and_clear(BROKEN_PORT, "CNC-01", "spindle_overheat")
        rb, _ = diagnose_timed(BROKEN_PORT, query)
        expect("坏端点实例：注入后仍能检出报警（数据源独立）", t_alarm_b > 0, f"耗时 {t_alarm_b:.1f}s")
        expect("坏端点实例：自动降级 degraded=True 且含失败原因",
               rb.get("degraded") is True
               and any(("失败" in str(r) or "超时" in str(r) or "未通过" in str(r))
                       for r in rb.get("degraded_reasons") or []),
               f"reasons={rb.get('degraded_reasons')}")
        expect("坏端点实例：降级后仍输出完整报告（七块齐全）",
               bool(rb.get("headline")) and len(rb.get("evidence") or []) >= 1
               and len(rb.get("steps") or []) > 0,
               f"mode={rb.get('mode')}｜证据={len(rb.get('evidence') or [])}｜步骤={len(rb.get('steps') or [])}")
        clear_fault(BROKEN_PORT, "CNC-01")

        # ---------------------------------------------------------------- 11 类故障增强/降级对比
        say("\n" + "=" * 70)
        say("11 类可注入故障：增强开 / 关 逐类对比")
        say("=" * 70)
        say(f"{'故障':<22}{'设备':<9}{'Top1':<10}{'增强结论(截)':<34}{'降级结论(截)':<34}{'差异观察'}")
        say("-" * 150)
        for fault_id, device, q in FAULTS:
            te = inject_and_clear(REAL_ENH_PORT, device, fault_id)
            td = inject_and_clear(DEG_PORT, device, fault_id)
            renh, _ = diagnose_timed(REAL_ENH_PORT, q)
            rdeg, _ = diagnose_timed(DEG_PORT, q)
            ce = str((renh.get("headline") or {}).get("conclusion", ""))
            cd = str((rdeg.get("headline") or {}).get("conclusion", ""))
            enh_causes = renh.get("causes") or []
            enh_top = enh_causes[0].get("kb_id") if enh_causes else ""
            deg_top = (rdeg.get("headline") or {}).get("kb_id", "")
            same_top = (enh_top == deg_top)
            narrative = bool(str(renh.get("narrative") or "").strip())
            steps_from_kb = all(s.get("kb_id") for s in (renh.get("steps") or []))
            concl_changed = (ce.strip() != cd.strip()) and bool(ce.strip())
            note = []
            if not same_top:
                note.append("Top1不一致")
            if concl_changed:
                note.append("结论被改写")
            if narrative:
                note.append("有narrative")
            if not steps_from_kb:
                note.append("步骤缺kb_id(异常)")
            say(f"{fault_id:<22}{device:<9}{(enh_top or '-'):<10}{ce[:32]:<34}{cd[:32]:<34}{'；'.join(note) or '无差异'}")
            clear_fault(REAL_ENH_PORT, device)
            clear_fault(DEG_PORT, device)

        say("\n" + "=" * 70)
        failed = [n for n, ok, _ in RESULTS if not ok]
        say(f"链路断言：{len(RESULTS) - len(failed)}/{len(RESULTS)} 通过"
            + ("" if not failed else f"｜失败 {len(failed)} 项：{failed}"))
    finally:
        stop_server(enh)
        stop_server(deg)
        stop_server(broken)

    (ROOT / "docs" / "真实模型增强验证_raw.txt").write_text("\n".join(OUT_LINES) + "\n", encoding="utf-8")
    say(f"\n原始结果已写入 docs/真实模型增强验证_raw.txt（不含任何密钥）")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
