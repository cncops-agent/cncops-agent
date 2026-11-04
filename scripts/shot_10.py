"""10 台设备预览截图：用无头 Chrome（CDP）抓两张图到 docs/screenshots/。

  - 10_normal.png   正常态：10 行紧凑摘要全部可见，顶部状态条
  - 10_alarm.png     注入 2 个故障后：报警行置顶变红、状态条变红

用法：python scripts/shot_10.py  [base_url]
"""
from __future__ import annotations

import asyncio
import base64
import json
import sys
import urllib.request
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "screenshots"
CHROME = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")


async def rpc(ws, method, params, rid):
    await ws.send(json.dumps({"id": rid, "method": method, "params": params or {}}))
    while True:
        msg = json.loads(await ws.recv())
        if msg.get("id") == rid:
            return msg


async def evaluate(ws, expr, rid):
    return await rpc(ws, "Runtime.evaluate",
                    {"expression": expr, "returnByValue": True, "awaitPromise": True}, rid)


async def shot(ws, path, rid):
    resp = await rpc(ws, "Page.captureScreenshot", {"format": "png"}, rid)
    data = base64.b64decode(resp["result"]["data"])
    path.write_bytes(data)
    print(f"  截图 {path.name}（{len(data)//1024} KB）")


async def text_of(ws, selector, rid):
    r = await evaluate(ws, f"(() => {{const el=document.querySelector({json.dumps(selector)});"
                          f"return el?el.textContent.trim():'(无)';}})()", rid)
    return r.get("result", {}).get("result", {}).get("value", "?")


def post(url, payload):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode())
    except Exception as e:
        return getattr(e, "code", 0), str(e)


def start_chrome(port, profile, url):
    return subprocess.Popen(
        [str(CHROME), "--headless=new", "--disable-gpu", "--no-sandbox",
         "--hide-scrollbars", f"--window-size=1680,1760",
         f"--remote-debugging-port={port}", f"--user-data-dir={profile}", url],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


async def wait_target(port, timeout=25):
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=2) as r:
                tabs = json.loads(r.read().decode())
            for t in tabs:
                if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                    return t["webSocketDebuggerUrl"]
        except Exception:
            pass
        await asyncio.sleep(0.5)
    raise RuntimeError("未找到页面 target")


import subprocess


async def main() -> int:
    import tempfile
    base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8020"
    OUT.mkdir(parents=True, exist_ok=True)
    profile = Path(tempfile.mkdtemp(prefix="cdp_10_"))
    proc = start_chrome(9344, profile, base)
    rid = 0
    try:
        ws_url = await wait_target(9344)
        async with websockets.connect(ws_url) as ws:
            await asyncio.sleep(3.5)
            rid += 1
            await shot(ws, OUT / "10_normal.png", rid)
            print("正常态状态条：", await text_of(ws, "#lineSummary", rid))

            # 注入两个故障，演示报警卡置顶 + 状态条变红
            rid += 1
            s1, j1 = post(f"{base}/api/demo/inject", {"device_id": "CNC-03", "fault_id": "spindle_overheat"})
            s2, j2 = post(f"{base}/api/demo/inject", {"device_id": "AIR-02", "fault_id": "air_pressure_low"})
            print(f"  注入 CNC-03: HTTP {s1} {j1.get('message','')}")
            print(f"  注入 AIR-02: HTTP {s2} {j2.get('message','')}")
            for _ in range(14):
                await asyncio.sleep(1)
                rid += 1
                summ = await text_of(ws, "#lineSummary", rid)
                if "报警" in summ:
                    break
            rid += 1
            await shot(ws, OUT / "10_alarm.png", rid)
            print("报警态状态条：", await text_of(ws, "#lineSummary", rid))
            rid += 1
            top_ids = await evaluate(
                ws, "(() => [...document.querySelectorAll('.dev-row')]"
                    ".slice(0,3).map(r => (r.querySelector('.dr-name') || {}).textContent))()", rid)
            print("置顶摘要行：", top_ids.get("result", {}).get("result", {}).get("value"))
        return 0
    finally:
        proc.terminate()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
