"""用无头 Chrome（CDP 协议）真实走一遍界面验收脚本，并截图。

产出 8 张截图到 docs/screenshots/：
  01_normal.png        正常运行：三台设备全绿、顶部状态条
  02_injected.png      演示模式注入故障后：报警摘要行置顶变红
  03_report.png        输入口语描述后的诊断报告卡（结论/证据/分级步骤）
  04_stepl1.png        排查模式：单步展示
  05_stepl3_confirm.png L3 安全确认弹层（未勾选 LOTO / 未填确认人时确认不可点）
  06_stepl3.png        勾选确认后的 L3 单步
  07_demo.png          演示模式注入面板
  08_history.png       历史页：时间线 + 展开的完整报告

前置：需要已启动的 Web 服务（默认 http://127.0.0.1:8000/，可用 --url 指定；
建议指向临时库实例，避免把验收记录写进正式记忆库，见 README 5.5）。

只用标准库 + websockets，不引入浏览器框架依赖。
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "screenshots"
ERRORS: list = []
CHROME = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")


async def rpc(ws, method: str, params: dict, rid: int):
    await ws.send(json.dumps({"id": rid, "method": method, "params": params or {}}))
    while True:
        msg = json.loads(await ws.recv())
        if msg.get("id") == rid:
            return msg


async def evaluate(ws, expr: str, rid: int):
    return await rpc(ws, "Runtime.evaluate",
                     {"expression": expr, "returnByValue": True, "awaitPromise": True}, rid)


async def shot(ws, path: Path, rid: int) -> None:
    resp = await rpc(ws, "Page.captureScreenshot", {"format": "png"}, rid)
    if "result" not in resp:
        raise RuntimeError(f"截图失败（原始响应）：{json.dumps(resp, ensure_ascii=False)[:400]}")
    data = base64.b64decode(resp["result"]["data"])
    path.write_bytes(data)
    print(f"  截图 {path.name}（{len(data) // 1024} KB）")


async def click(ws, selector: str, rid: int) -> str:
    js = f"""(() => {{
        const el = document.querySelector({json.dumps(selector)});
        if (!el) return 'not-found';
        el.click();
        return 'clicked';
    }})()"""
    r = await evaluate(ws, js, rid)
    return r.get("result", {}).get("result", {}).get("value", "?")


async def text_of(ws, selector: str, rid: int) -> str:
    js = f"""(() => {{
        const el = document.querySelector({json.dumps(selector)});
        return el ? el.textContent.trim() : '(元素不存在)';
    }})()"""
    r = await evaluate(ws, js, rid)
    return r.get("result", {}).get("result", {}).get("value", "?")


def expect(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{('｜' + detail) if detail else ''}")
    if not ok:
        ERRORS.append(f"断言失败: {name} {detail}")


async def set_value(ws, selector: str, value: str, rid: int) -> str:
    """给 input/textarea 赋值并触发 React/Vue 之外的原生事件（这里是原生 JS，够用）。"""
    js = f"""(() => {{
        const el = document.querySelector({json.dumps(selector)});
        if (!el) return 'not-found';
        const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement : HTMLInputElement;
        Object.getOwnPropertyDescriptor(proto.prototype, 'value').set.call(el, {json.dumps(value)});
        el.dispatchEvent(new Event('input', {{bubbles: true}}));
        el.dispatchEvent(new Event('change', {{bubbles: true}}));
        return 'set';
    }})()"""
    r = await evaluate(ws, js, rid)
    return r.get("result", {}).get("result", {}).get("value", "?")


def start_chrome(port: int, profile: Path, url: str) -> subprocess.Popen:
    args = [
        str(CHROME), "--headless=new", "--disable-gpu", "--no-sandbox",
        "--hide-scrollbars", "--window-size=1600,1000",
        f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
        url,                                  # 直接打开目标页面（后续按抓取到的 page target 连接）
    ]
    return subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


async def wait_page_target(port: int, timeout: int = 25) -> str:
    """轮询 /json/list，直到拿到目标页面的 webSocketDebuggerUrl。

    注意：必须连页面级 target 而不是浏览器级 target，否则没有 Page 域（截图会报 wasn't found）。
    """
    import urllib.request
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


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8000/")
    ap.add_argument("--wait", type=float, default=2.0)
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    profile = Path(tempfile.mkdtemp(prefix="cncops_chrome_"))
    proc = start_chrome(9333, profile, args.url)
    rid = 0
    errors: list[str] = []
    try:
        ws_url = await wait_page_target(9333)
        async with websockets.connect(ws_url, max_size=None) as ws:
            rid += 1
            await rpc(ws, "Runtime.enable", {}, rid)
            rid += 1
            await rpc(ws, "Log.enable", {}, rid)
            rid += 1
            await rpc(ws, "Page.enable", {}, rid)
            # 先清掉可能残留的注入故障，等产线回到正常再开始（保证"正常状态"截图真实）
            rid += 1
            await evaluate(ws, "fetch('/api/demo/clear',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}).then(r=>r.text())", rid)
            for _ in range(12):
                await asyncio.sleep(1.0)
                rid += 1
                s0 = await text_of(ws, "#lineSummary", rid)
                if "全部正常" in s0:
                    break
            await asyncio.sleep(1.0)

            # 只收集“本应用”的报错：忽略无头浏览器/环境自身向外部域名（非 localhost）的 telemetry 请求
            _EXTERNAL = ("tencentbuddy.com", "wb.tencent", "clientservices", "google", ".cn/ext", "microsoft")

            async def _is_external(text: str) -> bool:
                t = (text or "").lower()
                return any(h in t for h in _EXTERNAL)

            async def collect_errors() -> None:
                try:
                    async for raw in ws:
                        msg = json.loads(raw)
                        if msg.get("method") == "Runtime.exceptionThrown":
                            d = msg["params"]["exceptionDetails"]
                            txt = f"JS异常: {d.get('text')} {d.get('exception', {}).get('description', '')[:200]}"
                            if not await _is_external(txt):
                                ERRORS.append(txt)
                        elif msg.get("method") == "Log.entryAdded":
                            e = msg["params"]["entry"]
                            if e.get("level") in ("error", "warning"):
                                txt = f"控制台{e['level']}: {e.get('text', '')[:200]}"
                                if not await _is_external(txt):
                                    ERRORS.append(txt)
                except Exception:
                    return

            task = asyncio.create_task(collect_errors())
            try:
                # 1) 正常状态
                rid += 1
                await shot(ws, OUT / "01_normal.png", rid)
                s = await text_of(ws, "#lineSummary", rid)
                expect("10 秒内能看出产线状态（顶部一句话总览）", "全部正常" in s or "台正常" in s, s)
                print("步骤 1 完成：正常状态")

                # 2) 打开演示模式 + 注入故障
                rid += 1
                print("  演示模式开关：", await click(ws, "#demoToggle", rid))
                await asyncio.sleep(1.5)
                opt = await text_of(ws, "#demoDevice option", rid)
                expect("演示面板设备下拉框已填充", "undefined" not in opt, opt)
                rid += 1
                print("  注入按钮：", await click(ws, "#demoInject", rid))
                await asyncio.sleep(8)          # 等数据爬坡到报警（要求"数秒内变红"）
                summary = await text_of(ws, "#lineSummary", rid)
                if "报警" not in summary:
                    state = await text_of(ws, "#demoState", rid)
                    toast = await text_of(ws, "#toast", rid)
                    expect("注入后顶部状态条出现报警", False,
                           f"{summary}｜注入状态：{state}｜提示：{toast}")
                else:
                    expect("注入后顶部状态条出现报警", True, summary)
                first_cls = await evaluate(
                    ws, "(() => (document.querySelector('.dev-row') || {}).className || '')()", rid)
                first_cls = first_cls.get("result", {}).get("result", {}).get("value", "")
                expect("报警设备摘要行排到最前且红色高亮", "sev-alarm" in first_cls, first_cls)
                rid += 1
                await shot(ws, OUT / "07_demo.png", rid)
                rid += 1
                await shot(ws, OUT / "02_injected.png", rid)
                print("步骤 2 完成：注入后报警摘要行置顶变红")

                # 3-pre) 故意预置旧版脏缓存，复现「换演示库后 id 重号串数据」的用户场景
                rid += 1
                await evaluate(ws,
                    "(() => { localStorage.setItem('cncops:operator','11'); "
                    "localStorage.setItem('cncops:checks:1', JSON.stringify("
                    "{'__legacy_all_done__':true})); return 'seeded'; })()", rid)

                # 3) 口语提问 → 诊断报告
                rid += 1
                await set_value(ws, "#queryInput", "一号机主轴发烫，温度高得厉害", rid)
                rid += 1
                print("  诊断按钮：", await click(ws, "#askBtn", rid))
                await asyncio.sleep(4)
                concl = await text_of(ws, ".conclusion", rid)
                expect("报告第一行是结论且带故障编号", ("主轴" in concl and "F00" in concl), concl[:60])
                kb_detail_clickable = await evaluate(
                    ws, "(() => !!document.getElementById('kbBadge'))()", rid)
                expect("故障编号角标可点击（知识库详情）",
                       kb_detail_clickable.get("result", {}).get("result", {}).get("value") is True)
                spark_n = await evaluate(
                    ws, "(() => document.querySelectorAll('.evidence-row .spark').length)()", rid)
                expect("每条证据带迷你趋势图",
                       (spark_n.get("result", {}).get("result", {}).get("value") or 0) >= 1)
                # 回归：浏览器里即便残留旧版本地缓存，新一轮报告也必须零勾选、操作人留空
                clean = await evaluate(ws,
                    "(() => ({ checked: [...document.querySelectorAll('[data-step]')]"
                    ".filter(x => x.checked).length, "
                    "total: document.querySelectorAll('[data-step]').length, "
                    "op: (document.getElementById('fbOperator') || {}).value || '', "
                    "runtime: Object.keys(App.checks || {}).length }))()", rid)
                clean = clean.get("result", {}).get("result", {}).get("value", {}) or {}
                expect("新一轮诊断步骤全部未勾选（旧版本地缓存不串数据）",
                       clean.get("total", 0) > 0 and clean.get("checked") == 0,
                       f"共 {clean.get('total')} 步，已勾选 {clean.get('checked')} 步，运行时键数 {clean.get('runtime')}")
                expect("操作人框默认留空，必须由本人填写",
                       clean.get("op") == "", f"实际值：{clean.get('op')!r}")
                rid += 1
                await evaluate(ws, "window.scrollTo(0, 0)", rid)
                rid += 1
                await shot(ws, OUT / "03_report.png", rid)
                print("步骤 3 完成：诊断报告")

                # 4) 排查模式
                rid += 1
                print("  开始排查：", await click(ws, "#startStepMode", rid))
                await asyncio.sleep(1.5)
                prog = await text_of(ws, "#stepProgress", rid)
                expect("排查模式显示进度", "第 1 步" in prog, prog)
                rid += 1
                await shot(ws, OUT / "04_stepl1.png", rid)
                # 连点若干次"完成下一步"直到出现 L3 安全确认
                for _ in range(12):
                    rid += 1
                    await click(ws, "#stepNext", rid)
                    await asyncio.sleep(0.9)
                    rid += 1
                    visible = await evaluate(
                        ws, "(() => !document.getElementById('safeModal').hidden)()", rid)
                    if visible.get("result", {}).get("result", {}).get("value"):
                        break
                rid += 1
                await shot(ws, OUT / "05_stepl3_confirm.png", rid)
                modal_visible = await evaluate(
                    ws, "(() => { const m = document.getElementById('safeModal');"
                        " return !m.hidden && getComputedStyle(m).display !== 'none'; })()", rid)
                expect("L3 安全确认弹层真实可见（不被排查模式遮挡）",
                       modal_visible.get("result", {}).get("result", {}).get("value") is True)
                confirm_disabled = await evaluate(
                    ws, "(() => document.getElementById('safeConfirm').disabled)()", rid)
                expect("L3 安全确认：未勾选前不能进入",
                       confirm_disabled.get("result", {}).get("result", {}).get("value") is True)
                # 勾选安全声明 + 填写确认人 → 服务端闸门确认留痕 → 进入 L3 步骤
                rid += 1
                await click(ws, "#safeAck", rid)
                await asyncio.sleep(0.4)
                only_checked = await evaluate(
                    ws, "(() => document.getElementById('safeConfirm').disabled)()", rid)
                expect("L3 确认：只勾选未署名仍不能进入",
                       only_checked.get("result", {}).get("result", {}).get("value") is True)
                rid += 1
                await set_value(ws, "#safeAckBy", "持证工程师-验收", rid)
                await asyncio.sleep(0.4)
                rid += 1
                await click(ws, "#safeConfirm", rid)
                await asyncio.sleep(1.5)
                l3text = await text_of(ws, ".step-mode-text", rid)
                expect("确认后进入 L3 单步（超大字步骤正文）", len(l3text) > 10, l3text[:40])
                rid += 1
                await shot(ws, OUT / "06_stepl3.png", rid)
                print("步骤 4 完成：排查模式 + L3 安全确认")

                # 5) 退出排查 → 反馈 → 历史页
                # 操作人为产品必填项：空姓名会被前端拦截（后端同样 400），先填再提交
                rid += 1
                await click(ws, "#stepExit", rid)
                await asyncio.sleep(1.0)
                rid += 1
                await set_value(ws, "#fbOperator", "验收机器人", rid)
                await asyncio.sleep(0.4)
                rid += 1
                await click(ws, "#fbOk", rid)
                await asyncio.sleep(1.5)
                fb = await text_of(ws, "#fbDone", rid)
                expect("点「已解决」出现成功提示", "已记录" in fb, fb)
                rid += 1
                await click(ws, "#navHistory", rid)
                await asyncio.sleep(2.5)
                rid += 1
                await click(ws, "[data-toggle]", rid)
                await asyncio.sleep(2.0)
                hist = await text_of(ws, ".hist-item", rid)
                expect("反馈后历史页立即可查到该记录", "已解决" in hist, hist[:80])
                rid += 1
                await evaluate(ws, "window.scrollTo(0, 0)", rid)
                rid += 1
                await shot(ws, OUT / "08_history.png", rid)
                print("步骤 5 完成：反馈写入 + 历史页")

                # 6) 断线验收：接口异常时必须明示「数据连接中断」，恢复后自动回到正常
                rid += 1
                await evaluate(
                    ws, "window.__realFetch = window.fetch;"
                        "window.fetch = () => Promise.reject(new TypeError('模拟断线'));", rid)
                await asyncio.sleep(4.0)          # 等下一次轮询（2s 周期）撞上断线
                banner = await evaluate(
                    ws, "(() => { const b = document.getElementById('connBanner');"
                        " return !b.hidden && getComputedStyle(b).display !== 'none'; })()", rid)
                expect("断开后端后顶部显示「数据连接中断」且不再假装正常",
                       banner.get("result", {}).get("result", {}).get("value") is True)
                summary_off = await text_of(ws, "#lineSummary", rid)
                expect("断线期间状态条不显示旧数值冒充正常",
                       "数据连接中断" in summary_off, summary_off)
                rid += 1
                await evaluate(ws, "window.fetch = window.__realFetch;", rid)
                await asyncio.sleep(4.0)
                banner2 = await evaluate(
                    ws, "(() => document.getElementById('connBanner').hidden)()", rid)
                expect("后端恢复后连接中断提示自动消失",
                       banner2.get("result", {}).get("result", {}).get("value") is True)
                summary_re = await text_of(ws, "#lineSummary", rid)
                expect("恢复后状态条重新出现实时总览（真实数据，不再是连接中断文案）",
                       "产线状态：" in summary_re and "数据连接中断" not in summary_re, summary_re)
                print("步骤 6 完成：断线提示与自动恢复")
            finally:
                task.cancel()

        print("\n控制台/JS 报错：", len(ERRORS))
        for e in ERRORS[:12]:
            print("  -", e)
        return 0 if not ERRORS else 2
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(profile, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
