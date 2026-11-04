"""增强模式 vs 降级模式 对比验收（本地模拟 OpenAI 兼容服务；无需真实 Key，也不写任何密钥）。

背景：本机没有真实大模型 Key（铁律：密钥只走本地 `.env`，不写进代码/文档/报告）。
本脚本用**本地模拟服务**验证「配了可用大模型」时的完整增强链路，并与「无 Key 降级」同题对比：

  1. 模拟服务（Python 标准库 http.server）：OpenAI 兼容 `POST /v1/chat/completions`，
     返回合法增强载荷（conclusion / narrative / understood / causes；causes 的 kb_id
     取请求里检索候选的首位，保证必然通过 `agent._validate_llm` 校验）；
  2. 增强实例：cncops 服务（临时库）指向模拟服务——API Base / Model 走环境变量，
     Key 为**运行时随机生成的本地测试令牌**（非真实密钥，不落盘、不入库、不进报告）；
  3. 降级实例：不配任何 LLM 变量的 cncops 服务（另一个临时库）；
  4. 同一口语问诊（CNC-01 主轴过热）各跑一次，逐字段对比 + 记录耗时。

一条命令跑完： python scripts/llm_mode_check.py
退出码 0 = 增强链路断言全部通过；逐条结果写入 docs/llm_mode_check_output.txt。

边界说明：本脚本验证的是**增强模式代码链路**（调用→校验→采纳→步骤仍取自知识库）；
真实大模型与规则版的效果差异，需在 `.env` 配置真实 Key 后另行验证（README 2.5）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import List, Tuple

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

MOCK_PORT = 8099
ENH_PORT = 8131          # 增强实例（指向模拟服务）
DEG_PORT = 8132          # 降级实例（无 LLM 配置）

RESULTS: List[Tuple[str, bool, str]] = []
OUT_LINES: List[str] = []


def say(line: str = "") -> None:
    print(line)
    OUT_LINES.append(line)


def expect(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    say(f"  [{'PASS' if ok else 'FAIL'}] {name}{('｜' + detail) if detail else ''}")


# ---------------------------------------------------------------- 模拟大模型服务
class _MockLLM(BaseHTTPRequestHandler):
    """OpenAI 兼容模拟服务：从请求里取检索候选，拼一个必然通过校验的增强载荷。"""

    def log_message(self, *args):  # 静音
        pass

    def do_GET(self) -> None:
        if self.path.endswith("/health"):
            self._json(200, {"status": "ok"})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8")
        try:
            req = json.loads(raw)
        except Exception:
            self._json(400, {"error": "bad json"})
            return
        if not self.path.endswith("/chat/completions"):
            self._json(404, {"error": "not found"})
            return
        # 取出 user 消息里的诊断载荷，取检索候选首位作为增强排序的 Top-1
        user = ""
        for m in req.get("messages", []):
            if m.get("role") == "user":
                user = m.get("content") or ""
        try:
            payload = json.loads(user)
            candidates = payload.get("kb_candidates") or []
            top = candidates[0]["kb_id"] if candidates else ""
        except Exception:
            top = ""
        content = json.dumps({
            "conclusion": "【模拟增强】结合实时数据与知识库检索，判定当前异常并给出处置顺序。",
            "narrative": "【模拟增强】口语已理解；证据与检索候选一致，建议按分级步骤处置并跟踪趋势。",
            "understood": {"symptoms": ["发烫", "温度高", "过热"]},
            "causes": [{"kb_id": top, "rank": 1, "why": "模拟服务补充：该候选同时解释报警码与超标测点"}],
        }, ensure_ascii=False)
        self._json(200, {
            "id": "chatcmpl-mock", "object": "chat.completion", "created": 0,
            "model": req.get("model", "mock-model"),
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                         "finish_reason": "stop"}],
        })

    def _json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def start_mock() -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer(("127.0.0.1", MOCK_PORT), _MockLLM)
    import threading
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# ---------------------------------------------------------------- cncops 实例
def start_server(port: int, db_path: Path, with_llm: bool, token: str) -> subprocess.Popen:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC)
    env["CNCOPS_DEVICES_YAML"] = str(ROOT / "config" / "devices.yaml")
    env["CNCOPS_DB_PATH"] = str(db_path)
    env["CNCOPS_DEMO_ALLOW"] = "1"
    env["CNCOPS_LOG_LEVEL"] = "WARNING"
    for k in ("CNCOPS_LLM_API_BASE", "CNCOPS_LLM_API_KEY", "CNCOPS_LLM_MODEL",
              "CNCOPS_LLM_ENABLED", "CNCOPS_LLM_TIMEOUT_SEC"):
        env.pop(k, None)
    if with_llm:
        env["CNCOPS_LLM_API_BASE"] = f"http://127.0.0.1:{MOCK_PORT}/v1"
        env["CNCOPS_LLM_API_KEY"] = token        # 运行时随机令牌（本地测试夹具，非真实密钥）
        env["CNCOPS_LLM_MODEL"] = "mock-model"
        env["CNCOPS_LLM_TIMEOUT_SEC"] = "8"
    log = db_path.with_suffix(".server.log")
    f = open(log, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "cncops.api:app",
         "--host", "127.0.0.1", "--port", str(port)],
        stdout=f, stderr=subprocess.STDOUT, env=env, cwd=str(ROOT))
    proc._logfile = f                              # type: ignore[attr-defined]
    return proc


def wait_health(port: int, timeout: float = 30.0) -> None:
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
        proc._logfile.close()                      # type: ignore[attr-defined]
    except Exception:
        pass


def api_post(port: int, path: str, body: dict) -> dict:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def api_get(port: int, path: str) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def inject_and_wait_alarm(port: int) -> float:
    """注入 CNC-01 主轴过热，轮询到报警为止，返回耗时秒数。"""
    t0 = time.time()
    api_post(port, "/api/demo/inject",
             {"device_id": "CNC-01", "fault_id": "spindle_overheat"})
    while time.time() - t0 < 25:
        time.sleep(1.0)
        line = api_get(port, "/api/line")
        dev = next((d for d in line.get("devices", []) if d.get("id") == "CNC-01"), {})
        if dev.get("status") == "ALARM":
            return time.time() - t0
    return -1.0


def diagnose_timed(port: int, query: str) -> Tuple[dict, float]:
    t0 = time.time()
    rep = api_post(port, "/api/diagnose", {"query": query})
    return rep, time.time() - t0


# ---------------------------------------------------------------- 主流程
def main() -> int:
    say("增强模式 vs 降级模式 对比验收（本地模拟 OpenAI 兼容服务）")
    mock = start_mock()
    token = "mock-local-" + uuid.uuid4().hex      # 运行时随机令牌，非真实密钥
    tmp = Path(tempfile.mkdtemp(prefix="cncops_llm_mode_"))
    enh = start_server(ENH_PORT, tmp / "enh.db", with_llm=True, token=token)
    deg = start_server(DEG_PORT, tmp / "deg.db", with_llm=False, token="")
    try:
        wait_health(MOCK_PORT if False else ENH_PORT)
        wait_health(DEG_PORT)
        say(f"  模拟大模型服务就绪：http://127.0.0.1:{MOCK_PORT}/v1（Key 为运行时随机令牌）")

        say("\n── 增强实例：注入 CNC-01 主轴过热 ──")
        t_alarm = inject_and_wait_alarm(ENH_PORT)
        expect("增强实例：注入后检出报警", t_alarm > 0, f"耗时 {t_alarm:.1f}s")

        say("\n── 降级实例：注入同一故障 ──")
        t_alarm2 = inject_and_wait_alarm(DEG_PORT)
        expect("降级实例：注入后检出报警", t_alarm2 > 0, f"耗时 {t_alarm2:.1f}s")

        query = "一号机主轴发烫，温度高得厉害"
        say(f"\n── 同一口语问诊：{query} ──")
        rep_enh, t_enh = diagnose_timed(ENH_PORT, query)
        rep_deg, t_deg = diagnose_timed(DEG_PORT, query)

        say("\n── 增强实例断言（mode=llm） ──")
        expect("增强实例：mode=llm（大模型增强生效）", rep_enh.get("mode") == "llm",
               f"mode={rep_enh.get('mode')}")
        expect("增强实例：degraded=False", rep_enh.get("degraded") is False,
               f"degraded={rep_enh.get('degraded')}｜reasons={rep_enh.get('degraded_reasons')}")
        expect("增强实例：llm_used=True", rep_enh.get("llm_used") is True,
               f"llm_used={rep_enh.get('llm_used')}")
        hist_enh = api_get(ENH_PORT, f"/api/history/{rep_enh.get('record_id')}")
        expect("增强实例：历史详情接口 llm_used=True（记忆库落库值）",
               hist_enh.get("llm_used") is True and hist_enh.get("mode") == "llm",
               f"mode={hist_enh.get('mode')}｜llm_used={hist_enh.get('llm_used')}")
        expect("增强实例：结论为大模型组织后的文本",
               "【模拟增强】" in str((rep_enh.get("headline") or {}).get("conclusion", "")),
               str((rep_enh.get("headline") or {}).get("conclusion", ""))[:60])
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

        say("\n── 降级实例断言（mode=rule） ──")
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

        say("\n── 两模式对比（同题同设备同故障） ──")
        h_enh = rep_enh.get("headline") or {}
        h_deg = rep_deg.get("headline") or {}
        expect("两模式识别同一设备同一 Top-1 故障",
               (rep_enh.get("device") or {}).get("id") == (rep_deg.get("device") or {}).get("id") == "CNC-01"
               and h_enh.get("kb_id") == h_deg.get("kb_id"),
               f"enh={h_enh.get('kb_id')}｜deg={h_deg.get('kb_id')}")
        expect("两模式排查步骤完全相同（步骤恒取自知识库原文）",
               [s.get("text") for s in rep_enh.get("steps") or []]
               == [s.get("text") for s in rep_deg.get("steps") or []],
               f"enh 步骤={len(rep_enh.get('steps') or [])}｜deg 步骤={len(rep_deg.get('steps') or [])}")
        say(f"  耗时对比：增强 {t_enh:.2f}s ｜ 降级 {t_deg:.2f}s（本地模拟服务，仅链路参考）")
        say(f"  报警检出：增强实例 {t_alarm:.1f}s ｜ 降级实例 {t_alarm2:.1f}s")
        say(f"  记录落库：增强 #{rep_enh.get('record_id')} ｜ 降级 #{rep_deg.get('record_id')}")

        say("\n── 安全边界复核：增强模式也没有产生任何控制类字段 ──")
        forbidden = [k for k in ("command", "write", "setpoint", "control") if k in rep_enh]
        expect("增强实例响应无控制类字段", not forbidden, f"forbidden={forbidden}")
    finally:
        stop_server(enh)
        stop_server(deg)
        mock.shutdown()

    say("\n" + "=" * 64)
    failed = [n for n, ok, _ in RESULTS if not ok]
    say(f"总计：{len(RESULTS) - len(failed)}/{len(RESULTS)}  全部通过" if not failed
        else f"总计：{len(RESULTS) - len(failed)}/{len(RESULTS)}  失败 {len(failed)} 项：{failed}")
    (ROOT / "docs" / "llm_mode_check_output.txt").write_text("\n".join(OUT_LINES) + "\n",
                                                             encoding="utf-8")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
