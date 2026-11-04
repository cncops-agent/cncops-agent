"""诊断报告卡七块全览 + 二次同类故障相似历史引用 截图脚本（无头 Chrome，CDP 协议）。

对应用户返工清单「二-1」要求固定化的两张关键截图：
  08_diagnosis_report.png   诊断报告卡七块全览（结论/证据/原因排序/L1-L3 步骤/安全警告/知识库来源/相似历史）
  09_history_reference.png  二次同类故障：报告主动引用上一次处理记录（相似历史区块）

流程：自起验收服务（独立端口 + 临时记忆库 + 强制降级，不碰正式库）→ 无头 Chrome →
  预热：注入 CNC-01 主轴过热 → 口语诊断（临时库留下第一条同类记录，记录号从报告卡读取）→ 全部恢复；
  第一轮：再次注入同类故障 → 口语诊断 → 断言报告卡七块齐全 + 相似历史引用预热记录 → 全页截图 08；
  第二轮：恢复后第三次注入同类故障 → 口语诊断 → 断言相似历史引用第一轮记录 → 滚动到
  「相似历史」区块截图 09。
  全部走真实 UI 路径（不在接口层重复诊断，避免一条问诊产生两条记录）。

一条命令跑完： python scripts/shot_diagnosis.py
退出码 0 = 全部断言通过；逐条结果写入 docs/shot_diagnosis_output.txt。
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import websockets

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
OUT = ROOT / "docs" / "screenshots"
CHROME = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")

VIEWPORT = (1600, 1000)
PORT = 8125
CDP_PORT = PORT + 100

RESULTS: List[Tuple[str, bool, str]] = []
ERRORS: List[str] = []
OUT_LINES: List[str] = []


def say(line: str = "") -> None:
    print(line)
    OUT_LINES.append(line)


def expect(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    say(f"  [{'PASS' if ok else 'FAIL'}] {name}{('｜' + detail) if detail else ''}")


# ---------------------------------------------------------------- CDP 基础
class CDP:
    """单 reader 调度器：一个协程专职收消息，按 id 唤醒对应 RPC，事件进收集器。"""

    def __init__(self, ws):
        self.ws = ws
        self.rid = 0
        self.pending: Dict[int, asyncio.Future] = {}
        self.reader: Optional[asyncio.Task] = None

    async def start(self) -> None:
        self.reader = asyncio.create_task(self._read_loop())

    async def stop(self) -> None:
        if self.reader:
            self.reader.cancel()

    async def _read_loop(self) -> None:
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                if "id" in msg and msg["id"] in self.pending:
                    fut = self.pending.pop(msg["id"])
                    if not fut.done():
                        fut.set_result(msg)
                elif msg.get("method") == "Runtime.exceptionThrown":
                    d = msg["params"]["exceptionDetails"]
                    ERRORS.append(f"JS异常: {d.get('text')} {d.get('exception', {}).get('description', '')[:160]}")
                elif msg.get("method") == "Log.entryAdded" and msg["params"]["entry"].get("level") == "error":
                    entry = msg["params"]["entry"]
                    text = entry.get("text", "")[:160]
                    if "favicon" not in (entry.get("url") or "") and "favicon" not in text:
                        ERRORS.append("console error: " + text)
        except Exception:
            return

    async def call(self, method: str, params: dict):
        self.rid += 1
        rid = self.rid
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self.pending[rid] = fut
        await self.ws.send(json.dumps({"id": rid, "method": method, "params": params or {}}))
        return await fut


async def rpc(ws, method: str, params: dict, rid: int):
    return await ws.call(method, params)


async def evaluate(ws, expr: str, rid: int):
    return await rpc(ws, "Runtime.evaluate",
                     {"expression": expr, "returnByValue": True, "awaitPromise": True}, rid)


async def val(ws, expr: str, rid: int):
    r = await evaluate(ws, expr, rid)
    return r.get("result", {}).get("result", {}).get("value")


async def shot(ws, path: Path, rid: int, full_page: bool = False) -> None:
    params = {"format": "png"}
    if full_page:
        params["captureBeyondViewport"] = True
    resp = await rpc(ws, "Page.captureScreenshot", params, rid)
    if "result" not in resp:
        raise RuntimeError(f"截图失败：{json.dumps(resp, ensure_ascii=False)[:300]}")
    data = base64.b64decode(resp["result"]["data"])
    path.write_bytes(data)
    say(f"  截图 {path.name}（{len(data) // 1024} KB）")


async def click(ws, selector: str, rid: int) -> str:
    return await val(ws, f"""(() => {{
        const el = document.querySelector({json.dumps(selector)});
        if (!el) return 'not-found';
        el.click();
        return 'clicked';
    }})()""", rid)


async def set_value(ws, selector: str, value: str, rid: int) -> str:
    return await val(ws, f"""(() => {{
        const el = document.querySelector({json.dumps(selector)});
        if (!el) return 'not-found';
        const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement : HTMLInputElement;
        Object.getOwnPropertyDescriptor(proto.prototype, 'value').set.call(el, {json.dumps(value)});
        el.dispatchEvent(new Event('input', {{bubbles: true}}));
        el.dispatchEvent(new Event('change', {{bubbles: true}}));
        return el.value === {json.dumps(value)} ? 'set' : 'value-mismatch:' + el.value;
    }})()""", rid)


async def set_viewport(ws, width: int, height: int, rid: int) -> None:
    await rpc(ws, "Emulation.setDeviceMetricsOverride",
              {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": False}, rid)


def start_chrome(port: int, profile: Path, url: str) -> subprocess.Popen:
    args = [str(CHROME), "--headless=new", "--disable-gpu", "--no-sandbox",
            "--hide-scrollbars", f"--window-size={VIEWPORT[0]},{VIEWPORT[1]}",
            f"--remote-debugging-port={port}", f"--user-data-dir={profile}", url]
    return subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


async def wait_page_target(port: int, timeout: int = 25) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=2) as r:
                tabs = json.loads(r.read().decode())
            for tab in tabs:
                if tab.get("type") == "page" and tab.get("webSocketDebuggerUrl"):
                    return tab["webSocketDebuggerUrl"]
        except Exception:
            pass
        await asyncio.sleep(0.5)
    raise RuntimeError("未找到可用的页面 target")


# ---------------------------------------------------------------- 验收服务
def start_server(port: int, db_path: Path) -> subprocess.Popen:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC)
    env["CNCOPS_DEVICES_YAML"] = str(ROOT / "config" / "devices.yaml")
    env["CNCOPS_DB_PATH"] = str(db_path)          # 临时库：不碰正式记忆库
    env["CNCOPS_DEMO_ALLOW"] = "1"                # 故障注入验收需要
    env["CNCOPS_LLM_ENABLED"] = "0"               # 强制降级模式（与产品自测一致）
    env["CNCOPS_LOG_LEVEL"] = "WARNING"
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
    raise RuntimeError(f"验收服务 {port} 端口 {timeout}s 内未就绪")


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
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


# ---------------------------------------------------------------- DOM 断言片段
# 报告卡七块：①结论(.report-head .conclusion) ②证据(.evidence-row) ③原因排序(.cause-row)
# ④分级步骤(.step-group.l1/l2/l3) ⑤安全警告(.warn-list li) ⑥知识库角标(#kbBadge) ⑦相似历史(.history-ref)
SEVEN_BLOCK_JS = """(() => {
  const q = (s) => document.querySelectorAll(s).length;
  return {
    head: !!document.querySelector('.report-head .conclusion'),
    evidence: q('.evidence-row'),
    causes: q('.cause-row'),
    steps_l1: q('.step-group.l1'), steps_l2: q('.step-group.l2'), steps_l3: q('.step-group.l3'),
    warnings: q('.warn-list li'),
    kb_badge: !!document.getElementById('kbBadge'),
    history_ref: q('.history-ref'),
  };
})()"""

RECORD_ID_JS = """(() => {
  const sub = document.querySelector('.report-sub');
  if (!sub) return null;
  const m = sub.textContent.match(/记录号\\s*#(\\d+)/);
  return m ? parseInt(m[1], 10) : null;
})()"""


async def wait_alarm(ws, rid: int, timeout: float = 25.0) -> bool:
    t0 = time.time()
    n = 0
    while time.time() - t0 < timeout:
        await asyncio.sleep(1.0)
        rid += 1
        n = await val(ws, "document.querySelectorAll('.dev-row.sev-alarm').length", rid)
        if n and n >= 1:
            return True
    return False


async def wait_clear(ws, rid: int, timeout: float = 20.0):
    t0 = time.time()
    n = 0
    while time.time() - t0 < timeout:
        await asyncio.sleep(1.0)
        rid += 1
        n = await val(ws, "document.querySelectorAll('.dev-row.sev-alarm').length", rid)
        if not n:
            break
    return n


async def wait_report(ws, rid: int, timeout: float = 20.0) -> bool:
    t0 = time.time()
    ok = False
    while time.time() - t0 < timeout:
        await asyncio.sleep(0.8)
        rid += 1
        ok = await val(ws, "!!document.querySelector('.report-head .conclusion')", rid)
        if ok:
            return True
    return False


# ---------------------------------------------------------------- 主流程
async def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="cncops_shot_diag_"))
    db = tmp / "check.db"
    profile = tmp / "chrome-profile"
    server = start_server(PORT, db)
    chrome = None
    rid = 1
    try:
        wait_health(PORT)
        chrome = start_chrome(CDP_PORT, profile, f"http://127.0.0.1:{PORT}/")
        ws_url = await wait_page_target(CDP_PORT)
        async with websockets.connect(ws_url, max_size=32 * 1024 * 1024) as sock:
            cdp = CDP(sock)
            await cdp.start()
            await set_viewport(cdp, VIEWPORT[0], VIEWPORT[1], rid); rid += 1
            say("\n── 步骤 0：页面加载 ──")
            await asyncio.sleep(6.0)               # 等首轮轮询落地

            say("\n── 步骤 1（预热）：注入 CNC-01 主轴过热 → 口语诊断 → 全部恢复 ──")
            api_post(PORT, "/api/demo/inject",
                     {"device_id": "CNC-01", "fault_id": "spindle_overheat"})
            expect("预热：报警摘要行置顶变红", await wait_alarm(cdp, rid))
            rid += 1
            await set_value(cdp, "#queryInput", "一号机主轴发烫，温度高得厉害", rid); rid += 1
            await click(cdp, "#askBtn", rid); rid += 1
            expect("预热：报告卡渲染（结论先行）", await wait_report(cdp, rid))
            await asyncio.sleep(1.5)
            rid += 1
            warm_id = await val(cdp, RECORD_ID_JS, rid)
            expect("预热：从报告卡读到记录号", isinstance(warm_id, int), f"记录号 #{warm_id}")
            api_post(PORT, "/api/demo/clear", {})
            n = await wait_clear(cdp, rid)
            expect("预热后全部恢复（无报警行）", n in (0, None), f"sev-alarm 行数={n}")

            say("\n── 步骤 2（第一轮）：再次注入同类故障 → 口语诊断 → 截图 08（报告卡七块全览） ──")
            api_post(PORT, "/api/demo/inject",
                     {"device_id": "CNC-01", "fault_id": "spindle_overheat"})
            expect("第一轮：报警摘要行置顶变红", await wait_alarm(cdp, rid))
            rid += 1
            await set_value(cdp, "#queryInput", "一号机主轴发烫，温度高得厉害", rid); rid += 1
            await click(cdp, "#askBtn", rid); rid += 1
            expect("第一轮：报告卡渲染（结论先行）", await wait_report(cdp, rid))
            await asyncio.sleep(2.0)               # 等证据火花线/趋势就绪
            rid += 1
            r1_id = await val(cdp, RECORD_ID_JS, rid)
            expect("第一轮：从报告卡读到记录号", isinstance(r1_id, int), f"记录号 #{r1_id}")
            rid += 1
            blocks = await val(cdp, SEVEN_BLOCK_JS, rid) or {}
            expect("第一轮：报告卡七块齐全（结论/证据/原因/L1/L2/L3 步骤/安全警告/知识库角标/相似历史）",
                   bool(blocks.get("head")) and int(blocks.get("evidence") or 0) >= 1
                   and int(blocks.get("causes") or 0) >= 1
                   and int(blocks.get("steps_l1") or 0) >= 1
                   and int(blocks.get("steps_l3") or 0) >= 1
                   and int(blocks.get("warnings") or 0) >= 1
                   and bool(blocks.get("kb_badge")) and int(blocks.get("history_ref") or 0) >= 1,
                   json.dumps(blocks, ensure_ascii=False))
            rid += 1
            cited = await val(cdp, "(() => { const h = document.querySelector('.history-ref');"
                                   " return h ? h.getAttribute('data-history') : null; })()", rid)
            expect(f"第一轮：相似历史引用预热记录 #{warm_id}", cited == str(warm_id),
                   f"data-history={cited}（期望 {warm_id}）")
            rid += 1
            await val(cdp, "window.scrollTo(0, 0)", rid)
            await asyncio.sleep(0.6)
            await shot(cdp, OUT / "08_diagnosis_report.png", rid, full_page=True)

            say("\n── 步骤 3（第二轮）：恢复后第三次注入同类故障 → 口语诊断 → 截图 09（相似历史引用） ──")
            api_post(PORT, "/api/demo/clear", {})
            n = await wait_clear(cdp, rid)
            expect("第二轮前：全部恢复（无报警行）", n in (0, None), f"sev-alarm 行数={n}")
            api_post(PORT, "/api/demo/inject",
                     {"device_id": "CNC-01", "fault_id": "spindle_overheat"})
            expect("第二轮：报警摘要行置顶变红", await wait_alarm(cdp, rid))
            rid += 1
            await set_value(cdp, "#queryInput", "一号机主轴又发烫了", rid); rid += 1
            await click(cdp, "#askBtn", rid); rid += 1
            expect("第二轮：报告卡渲染（结论先行）", await wait_report(cdp, rid))
            await asyncio.sleep(2.0)
            rid += 1
            r2_id = await val(cdp, RECORD_ID_JS, rid)
            expect("第二轮：从报告卡读到记录号", isinstance(r2_id, int), f"记录号 #{r2_id}")
            rid += 1
            cited2 = await val(cdp, "(() => { const h = document.querySelector('.history-ref');"
                                    " return h ? h.getAttribute('data-history') : null; })()", rid)
            expect(f"第二轮：相似历史引用第一轮记录 #{r1_id}", cited2 == str(r1_id),
                   f"data-history={cited2}（期望 {r1_id}）")
            rid += 1
            ht = await val(cdp, "document.querySelector('.history-ref').textContent", rid)
            expect("第二轮：相似历史区块含故障名与处理结果",
                   bool(ht) and "主轴温度过高" in (ht or "") and "处理结果" in (ht or ""),
                   str(ht or "")[:80].replace("\n", " "))
            rid += 1
            await val(cdp, "document.querySelector('.history-ref')"
                           ".scrollIntoView({block:'center'})", rid)
            await asyncio.sleep(1.0)
            await shot(cdp, OUT / "09_history_reference.png", rid)

            await cdp.stop()
    finally:
        if chrome:
            chrome.terminate()
        stop_server(server)

    say("\n" + "=" * 64)
    failed = [n for n, ok, _ in RESULTS if not ok]
    say(f"总计：{len(RESULTS) - len(failed)}/{len(RESULTS)}  全部通过" if not failed
        else f"总计：{len(RESULTS) - len(failed)}/{len(RESULTS)}  失败 {len(failed)} 项：{failed}")
    say(f"控制台/JS 报错：{len(ERRORS)}")
    for e in ERRORS[:10]:
        say("  - " + e)
    (ROOT / "docs" / "shot_diagnosis_output.txt").write_text("\n".join(OUT_LINES) + "\n",
                                                             encoding="utf-8")
    return 0 if (not failed and not ERRORS) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
