"""反馈规则浏览器验收：操作人必填 + 停机时长自动计时（无头 Chrome，CDP 协议）。

对应用户需求：
  1. 反馈区「操作人」必填——空姓名点击「已解决/没解决」被拦截并提示，不提交；
  2. 「停机时长」从设备报警开始自动计时，提交反馈（确认解决）时停表并计入历史记录。

流程：自起验收服务（临时库/演示允许/强制降级）→ 无头 Chrome 打开页面 →
  演示模式注入 CNC-01 主轴过热 → 口语诊断 → 断言停机计时读数出现 →
  空操作人提交被拦截（截图 29）→ 补填操作人提交成功、停表回显（截图 30）→
  历史详情页核对「实际处理人/停机时长」已入账（截图 31）。

一条命令跑完： python scripts/feedback_check.py
退出码 0 = 全部通过；结果同时写入 docs/feedback_check_output.txt。
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
PORT = 8124

RESULTS: List[Tuple[str, bool, str]] = []
ERRORS: List[str] = []


def expect(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{('｜' + detail) if detail else ''}")


# ---------------------------------------------------------------- CDP 基础
class CDP:
    """单 reader 调度器：一个协程专职收消息，按 id 唤醒对应 RPC，事件进收集器。

    不能开两个协程同时 ws.recv()（websockets 库会抛 ConcurrencyError）。
    """

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
                    # favicon 404 是无头浏览器自动请求的环境噪音，与本应用无关
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


async def shot(ws, path: Path, rid: int) -> None:
    resp = await rpc(ws, "Page.captureScreenshot", {"format": "png"}, rid)
    if "result" not in resp:
        raise RuntimeError(f"截图失败：{json.dumps(resp, ensure_ascii=False)[:300]}")
    data = base64.b64decode(resp["result"]["data"])
    path.write_bytes(data)
    print(f"  截图 {path.name}（{len(data) // 1024} KB）")


async def click(ws, selector: str, rid: int) -> str:
    return await val(ws, f"""(() => {{
        const el = document.querySelector({json.dumps(selector)});
        if (!el) return 'not-found';
        el.click();
        return 'clicked';
    }})()""", rid)


async def set_value(ws, selector: str, value: str, rid: int) -> str:
    """给 input/select 赋值并触发原生事件。

    select 必须用 HTMLSelectElement 自己的 value setter（el.value=）：
    误用 HTMLInputElement 的 setter 对 <select> 无效（曾致注入打错设备）。
    """
    return await val(ws, f"""(() => {{
        const el = document.querySelector({json.dumps(selector)});
        if (!el) return 'not-found';
        if (el.tagName === 'SELECT') {{
            el.value = {json.dumps(value)};
        }} else {{
            const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement : HTMLInputElement;
            Object.getOwnPropertyDescriptor(proto.prototype, 'value').set.call(el, {json.dumps(value)});
        }}
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
    env["CNCOPS_LLM_ENABLED"] = "0"               # 强制降级模式
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


def api_get(port: int, path: str) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
        return json.loads(r.read().decode())


# ---------------------------------------------------------------- 主流程
async def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="cncops_feedback_check_"))
    db = tmp / "check.db"
    profile = tmp / "chrome-profile"
    server = start_server(PORT, db)
    chrome = None
    rid = 1
    try:
        wait_health(PORT)
        chrome = start_chrome(PORT + 100, profile, f"http://127.0.0.1:{PORT}/")
        ws_url = await wait_page_target(PORT + 100)
        async with websockets.connect(ws_url, max_size=32 * 1024 * 1024) as sock:
            cdp = CDP(sock)
            await cdp.start()
            await set_viewport(cdp, VIEWPORT[0], VIEWPORT[1], rid); rid += 1

            print("\n── 步骤 1：页面加载 + 演示模式注入 CNC-01 主轴过热 ──")
            await asyncio.sleep(6.0)               # 等首轮轮询落地

            # 打开演示模式并注入（走真实 UI 路径：开关 → 选设备 → 选故障 → 注入）
            r = await click(cdp, "#demoToggle", rid); rid += 1
            expect("演示模式开关可打开", r == "clicked", r)
            await asyncio.sleep(0.8)
            r = await set_value(cdp, "#demoDevice", "CNC-01", rid); rid += 1
            expect("注入面板设备选中 CNC-01", r == "set", r)
            await asyncio.sleep(0.5)
            r = await set_value(cdp, "#demoFault", "spindle_overheat", rid); rid += 1
            expect("注入面板故障选中主轴过热", r == "set", r)
            r = await click(cdp, "#demoInject", rid); rid += 1
            expect("点击注入故障", r == "clicked", r)

            # 等报警行出现（2 秒轮询 + 去抖）
            t0 = time.time()
            alarmed = False
            while time.time() - t0 < 25:
                await asyncio.sleep(1.0)
                n = await val(cdp,
                              "document.querySelectorAll('.dev-row.sev-alarm').length", rid); rid += 1
                if n and n >= 1:
                    alarmed = True
                    break
            expect("报警摘要行置顶变红", alarmed, f"sev-alarm 行数={n}")

            print("\n── 步骤 2：口语诊断 → 停机自动计时读数 ──")
            await set_value(cdp, "#queryInput", "一号机主轴发烫，温度高得厉害", rid); rid += 1
            await click(cdp, "#askBtn", rid); rid += 1
            # 等报告渲染（反馈区随报告出现）
            t0 = time.time()
            has_report = False
            while time.time() - t0 < 20:
                await asyncio.sleep(0.8)
                ok = await val(cdp, "!!document.querySelector('#fbDowntimeAuto')"
                                      " && !!document.querySelector('.report-head')", rid); rid += 1
                if ok:
                    has_report = True
                    break
            expect("诊断报告渲染（反馈区出现）", has_report)

            await asyncio.sleep(1.5)               # 等一轮轮询把 alarm_since 带下来
            readout = await val(cdp, "document.getElementById('fbDowntimeAuto').textContent", rid); rid += 1
            expect("停机自动计时读数出现", bool(readout) and "计时" in (readout or ""),
                   f"「{readout}」")

            print("\n── 步骤 3：空操作人提交 → 必填拦截 ──")
            # 清空操作人（全新 profile 本应为空，显式清空保证确定性）
            await set_value(cdp, "#fbOperator", "", rid); rid += 1
            await click(cdp, "#fbOk", rid); rid += 1
            await asyncio.sleep(1.2)
            toast = await val(cdp, "(() => { const t = document.getElementById('toast');"
                                    " return t.hidden ? '' : t.textContent; })()", rid); rid += 1
            fb_done_hidden = await val(cdp, "document.getElementById('fbDone').hidden", rid); rid += 1
            expect("空操作人点击「已解决」被拦截", bool(toast) and "操作人" in (toast or ""),
                   f"toast=「{toast}」")
            expect("拦截后未写入反馈（fbDone 仍隐藏）", fb_done_hidden is True,
                   f"fbDone.hidden={fb_done_hidden}")
            await shot(cdp, OUT / "29_feedback_required.png", rid); rid += 1

            print("\n── 步骤 4：补填操作人 → 提交成功、停表并计入记录 ──")
            # 让计时走一会儿，使停机分钟数为正（报警起点 → 现在）
            await asyncio.sleep(42.0)
            await set_value(cdp, "#fbOperator", "张三", rid); rid += 1
            await click(cdp, "#fbOk", rid); rid += 1
            t0 = time.time()
            done_text = ""
            while time.time() - t0 < 15:
                await asyncio.sleep(0.8)
                done_text = await val(cdp, "document.getElementById('fbDone').textContent", rid); rid += 1
                if done_text:
                    break
            expect("补填操作人后提交成功", bool(done_text) and "已记录" in (done_text or ""),
                   f"「{done_text}」")
            m = re.search(r"停机\s*(\d+)\s*分钟", done_text or "")
            expect("回显停机时长（分钟）", bool(m), f"done=「{done_text}」")
            stopped = await val(cdp, "document.getElementById('fbDowntimeAuto').textContent", rid); rid += 1
            expect("提交后计时停止（读数切换为已停止）",
                   bool(stopped) and "已停止" in (stopped or ""), f"「{stopped}」")
            await shot(cdp, OUT / "30_feedback_downtime_auto.png", rid); rid += 1

            print("\n── 步骤 5：历史页核对「实际处理人 / 停机时长」入账 ──")
            # 接口层核对：该记录 fb_operator=张三 且 downtime_min 为数值
            hist = api_get(PORT, "/api/history?device=CNC-01&limit=20")
            rec = next((i for i in hist.get("items", [])
                        if i.get("fb_operator") == "张三"), None)
            expect("历史列表接口：实际处理人=张三 且停机时长已入账",
                   bool(rec) and isinstance(rec.get("downtime_min"), int),
                   f"记录 #{rec.get('id') if rec else '无'}｜fb_operator="
                   f"{rec.get('fb_operator') if rec else '无'}｜downtime_min="
                   f"{rec.get('downtime_min') if rec else '无'}")

            # 界面层核对：历史详情展开后两行都在
            await click(cdp, "#navHistory", rid); rid += 1
            await asyncio.sleep(2.0)
            await click(cdp, ".hist-summary", rid); rid += 1
            await asyncio.sleep(1.5)
            detail = await val(cdp, "document.querySelector('.hist-detail').textContent", rid); rid += 1
            expect("历史详情展示「实际处理人/停机时长」",
                   bool(detail) and "实际处理人" in (detail or "") and "停机时长" in (detail or "")
                   and "张三" in (detail or ""),
                   f"详情含张三={'张三' in (detail or '')}，含停机时长={'停机时长' in (detail or '')}")
            await shot(cdp, OUT / "31_feedback_history.png", rid); rid += 1

            await cdp.stop()
    finally:
        if chrome:
            chrome.terminate()
        stop_server(server)

    print("\n" + "=" * 64)
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"总计：{len(RESULTS) - len(failed)}/{len(RESULTS)}  全部通过" if not failed
          else f"总计：{len(RESULTS) - len(failed)}/{len(RESULTS)}  失败 {len(failed)} 项：{failed}")
    print(f"控制台/JS 报错：{len(ERRORS)}")
    for e in ERRORS[:10]:
        print("  -", e)
    return 0 if (not failed and not ERRORS) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
