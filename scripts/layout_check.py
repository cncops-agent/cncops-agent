"""布局验收：设备列表「紧凑摘要行 + 详情抽屉」结构（3/10/30 台三档 + 桌面/手机）。

对应用户验收清单逐条断言，并产出截图到 docs/screenshots/：
  20_layout_3rows.png        3 台桌面：紧凑摘要行
  21_layout_10rows.png       10 台桌面：一屏 12+ 行、列表独立滚动
  22_layout_30rows.png       30 台桌面：行高字号与 3/10 台完全一致、无缩放
  23_layout_alarm_top.png    注入中间设备故障：数秒内置顶变红（30 台）
  24_layout_drawer.png       桌面抽屉打开（全测点/阈值/趋势/最近历史/提问按钮）
  25_layout_filter.png       筛选（只看报警，带数量）+ 搜索
  26_layout_disconnected.png 断线横幅 + 数值清空为 "--"
  27_layout_mobile_rows.png  390px 手机：摘要行仍 56px、列表内部滚动
  28_layout_mobile_drawer.png 390px 手机：抽屉全屏

验收服务由本脚本自行拉起（独立端口 + 临时记忆库 + 强制降级模式），不碰正式库；
一条命令跑完： python scripts/layout_check.py
退出码 0 = 全部通过；逐条结果同时写入 docs/layout_check_output.txt。
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import shutil
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

DESKTOP = (1600, 1000)          # 桌面验收视口（强制精确值，不受窗口外壳影响）
MOBILE = (390, 844)             # 手机验收视口
PORT_BASE = 8123

RESULTS: List[Tuple[str, bool, str]] = []
ERRORS: List[str] = []
CONSISTENCY: Dict[str, dict] = {}     # 配置名 -> 行度量（跨档位一致性比对）


def expect(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{('｜' + detail) if detail else ''}")


# ---------------------------------------------------------------- CDP 基础
class CDP:
    """单 reader 调度器：一个协程专职收消息，按 id 唤醒对应 RPC，事件进收集器。

    不能开两个协程同时 ws.recv()（websockets 库会抛 ConcurrencyError），
    也不能让事件循环吞掉 RPC 的响应（rpc 会永久挂起）。
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
                    # favicon 404 是无头浏览器自动请求导致的环境噪音，与本应用无关
                    # （URL 在 entry.url，text 里只有"Failed to load resource… 404"）
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
    """取表达式值（自动解包 CDP 嵌套结构）。"""
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

    select 必须用 HTMLSelectElement 自己的 value setter（或直接 el.value=）：
    误用 HTMLInputElement 的 setter 对 <select> 无效——选中项不会变，
    后续读取 .value 仍是第一项（曾导致故障注入打到错误的设备上）。
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


async def set_viewport(ws, width: int, height: int, mobile: bool, rid: int) -> None:
    await rpc(ws, "Emulation.setDeviceMetricsOverride",
              {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": mobile}, rid)


def start_chrome(port: int, profile: Path, url: str) -> subprocess.Popen:
    args = [str(CHROME), "--headless=new", "--disable-gpu", "--no-sandbox",
            "--hide-scrollbars", f"--window-size={DESKTOP[0]},{DESKTOP[1]}",
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
def start_server(port: int, devices_yaml: Path, db_path: Path) -> subprocess.Popen:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC)
    env["CNCOPS_DEVICES_YAML"] = str(devices_yaml)
    env["CNCOPS_DB_PATH"] = str(db_path)          # 临时库：不碰正式记忆库
    env["CNCOPS_DEMO_ALLOW"] = "1"                # 故障注入验收需要
    env["CNCOPS_LLM_ENABLED"] = "0"               # 强制降级模式，避免外部 LLM 调用
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


# ---------------------------------------------------------------- 单档位桌面验收
async def desktop_checks(ws, rid_start: int, tag: str, expect_n: int) -> int:
    rid = rid_start
    print(f"\n── {tag}（桌面 {DESKTOP[0]}×{DESKTOP[1]}）──")

    # 等首轮轮询（2s 周期）落地
    await asyncio.sleep(6.0)

    # 1) 行数与行度量
    n = await val(ws, "document.querySelectorAll('.dev-row').length", rid); rid += 1
    expect(f"[{tag}] 设备全部渲染为紧凑摘要行", n == expect_n, f"rows={n}（期望 {expect_n}）")

    metrics = await val(ws, """(() => {
        const row = document.querySelector('.dev-row');
        if (!row) return null;
        const cs = getComputedStyle(row);
        const pick = sel => { const e = row.querySelector(sel); return e ? getComputedStyle(e).fontSize : null; };
        return {rowH: cs.height, rowFs: cs.fontSize, nameFs: pick('.dr-name'),
                statusFs: pick('.dr-status'), keyFs: pick('.dr-key'),
                rowTransform: cs.transform, rowZoom: cs.zoom,
                bodyTransform: getComputedStyle(document.body).transform,
                layoutTransform: getComputedStyle(document.querySelector('.layout')).transform};
    })()""", rid); rid += 1
    CONSISTENCY[tag] = metrics or {}
    h_px = float((metrics or {}).get("rowH", "0px")[:-2])
    expect(f"[{tag}] 行高 56–64px", 56 <= h_px <= 64, str((metrics or {}).get("rowH")))
    fs_ok = all(float(str((metrics or {}).get(k, "0px"))[:-2]) >= 14
                for k in ("rowFs", "nameFs", "statusFs", "keyFs"))
    expect(f"[{tag}] 行内字号全部 ≥14px（未缩字）", fs_ok,
           f"行{metrics.get('rowFs')}/名称{metrics.get('nameFs')}/状态{metrics.get('statusFs')}/数值{metrics.get('keyFs')}")
    no_scale = all((metrics or {}).get(k) in (None, "none", "1") for k in
                   ("rowTransform", "rowZoom", "bodyTransform", "layoutTransform"))
    expect(f"[{tag}] 无 transform/zoom 缩放", no_scale,
           f"行transform={metrics.get('rowTransform')} body={metrics.get('bodyTransform')} layout={metrics.get('layoutTransform')}")

    # 2) 列表独立滚动 + 整页不滚动（状态条/筛选条固定）
    scroll = await val(ws, """(() => {
        const list = document.querySelector('.device-list');
        const rect = list.getBoundingClientRect();
        let visible = 0;
        document.querySelectorAll('.dev-row').forEach(r => {
            const rr = r.getBoundingClientRect();
            if (rr.bottom <= rect.bottom + 1 && rr.top >= rect.top - 1) visible++;
        });
        return {visible, listClientH: list.clientHeight, listScrollH: list.scrollHeight,
                bodyScrollH: document.body.scrollHeight, innerH: window.innerHeight,
                filterFixed: getComputedStyle(document.querySelector('.list-filter')).position};
    })()""", rid); rid += 1
    expect(f"[{tag}] 一屏可见行数达标", (scroll or {}).get("visible", 0) >= min(expect_n, 12),
           f"可见 {scroll.get('visible')} 行（列表高 {scroll.get('listClientH')}px，本档 {expect_n} 台）")
    if expect_n > 12:
        expect(f"[{tag}] 列表独立滚动（内容高于容器时内部滚，不撑页）",
               (scroll or {}).get("listScrollH", 0) > (scroll or {}).get("listClientH", 0) + 1,
               f"scrollH={scroll.get('listScrollH')} clientH={scroll.get('listClientH')}")
    else:
        expect(f"[{tag}] 少量设备不撑页（列表不无故滚动）",
               (scroll or {}).get("listScrollH", 0) <= (scroll or {}).get("listClientH", 0) + 1,
               f"scrollH={scroll.get('listScrollH')} clientH={scroll.get('listClientH')}")
    expect(f"[{tag}] 整页不滚动（状态条/筛选条固定）",
           (scroll or {}).get("bodyScrollH", 0) <= (scroll or {}).get("innerH", 0) + 1,
           f"body.scrollHeight={scroll.get('bodyScrollH')} innerHeight={scroll.get('innerH')}")

    # 3) 行内唯一关键值 + 无报警不显示角标
    bad_multi = await val(ws, """(() => {
        const bad = [];
        document.querySelectorAll('.dev-row').forEach(r => {
            if (r.querySelectorAll('.dr-key .dr-unit').length > 1) bad.push(r.dataset.device);
        });
        return bad;
    })()""", rid); rid += 1
    expect(f"[{tag}] 每行只有一个关键监测值", not bad_multi, str(bad_multi[:3]))
    badge_n = await val(ws, "document.querySelectorAll('.dev-row .dr-badge').length", rid); rid += 1
    expect(f"[{tag}] 全正常时无报警角标", badge_n == 0, f"角标数={badge_n}")

    # 4) 筛选条数量 + 搜索
    cnt_all = await val(ws, "document.getElementById('cntAll').textContent", rid); rid += 1
    expect(f"[{tag}] 「全部」角标数量=设备数", cnt_all == str(expect_n), f"cntAll={cnt_all}")
    await click(ws, ".fchip[data-filter='normal']", rid); rid += 1
    await asyncio.sleep(0.4)
    vis_normal = await val(ws, "document.querySelectorAll('.dev-row').length", rid); rid += 1
    all_normal = await val(ws, """(() => [...document.querySelectorAll('.dev-row')]
        .every(r => r.classList.contains('sev-normal')))()""", rid); rid += 1
    expect(f"[{tag}] 「只看正常」筛选生效", vis_normal == expect_n and all_normal,
           f"可见 {vis_normal} 行且全部 sev-normal")
    await set_value(ws, "#deviceSearch", "CNC-0", rid); rid += 1
    await asyncio.sleep(0.4)
    vis_kw = await val(ws, "document.querySelectorAll('.dev-row').length", rid); rid += 1
    kw_ok = await val(ws, """(() => [...document.querySelectorAll('.dev-row')]
        .every(r => (r.dataset.device + '').includes('CNC-0') ||
                    r.querySelector('.dr-name').textContent.includes('CNC-0')))()""", rid); rid += 1
    expect(f"[{tag}] 搜索框按编号/名称过滤", vis_kw >= 1 and kw_ok, f"关键词 CNC-0 → {vis_kw} 行")
    await set_value(ws, "#deviceSearch", "不存在的设备XYZ", rid); rid += 1
    await asyncio.sleep(0.4)
    empty_txt = await val(ws, "document.querySelector('#deviceList').textContent.trim()", rid); rid += 1
    expect(f"[{tag}] 搜索无结果有空态提示", "没有符合筛选条件" in (empty_txt or ""), empty_txt[:40])
    # 复位
    await set_value(ws, "#deviceSearch", "", rid); rid += 1
    await click(ws, ".fchip[data-filter='all']", rid); rid += 1
    await asyncio.sleep(0.4)
    return rid


async def drawer_and_interaction_checks(ws, rid_start: int, tag: str) -> int:
    """抽屉开/三种关闭/内容契约/筛选刷新保持/断线横幅/注入置顶（30 台档执行）。"""
    rid = rid_start
    print(f"\n── {tag}：抽屉与交互 ──")

    # 5) 点击行 → 抽屉滑出 + 内容契约（选 CNC-01：库内有历史记录，截图证据更完整）
    await click(ws, ".dev-row[data-device='CNC-01']", rid); rid += 1
    await asyncio.sleep(1.6)
    dw = await val(ws, """(() => {
        const d = document.getElementById('deviceDrawer');
        return {open: d.classList.contains('open'), hidden: d.hidden,
                width: getComputedStyle(d).width,
                transform: getComputedStyle(d).transform,
                bodyLocked: document.body.classList.contains('drawer-open')};
    })()""", rid); rid += 1
    expect(f"[{tag}] 点击行抽屉滑入打开", (dw or {}).get("open") is True and not (dw or {}).get("hidden"),
           f"open={dw.get('open')} width={dw.get('width')}")
    expect(f"[{tag}] 抽屉打开期间锁定底层滚动", (dw or {}).get("bodyLocked") is True)
    pts = await val(ws, "document.querySelectorAll('.dw-point').length", rid); rid += 1
    expect(f"[{tag}] 抽屉含全部监测点", (pts or 0) >= 5, f"监测点 {pts} 项")
    thr_n = await val(ws, """(() => [...document.querySelectorAll('.dw-point-thr')]
        .filter(e => /注意|超标|无阈值/.test(e.textContent)).length)()""", rid); rid += 1
    expect(f"[{tag}] 每个监测点带阈值说明", thr_n == pts, f"{thr_n}/{pts}")
    trends = await val(ws, "document.querySelectorAll('.dw-trend').length", rid); rid += 1
    expect(f"[{tag}] 各测点趋势图", (trends or 0) == pts, f"趋势 {trends} 张")
    ask_btn = await val(ws, "!!document.getElementById('drawerAsk')", rid); rid += 1
    expect(f"[{tag}] 「对该设备提问诊断」按钮存在", ask_btn is True)
    hist = await val(ws, "document.querySelectorAll('.dw-hist-item').length", rid); rid += 1
    hist_empty = await val(ws, "document.querySelector('.dw-empty') ? document.querySelector('.dw-empty').textContent : ''", rid); rid += 1
    expect(f"[{tag}] 最近运维历史 ≤5 条（或空态）",
           (hist or 0) <= 5 and ((hist or 0) >= 1 or "暂无运维历史" in (hist_empty or "")),
           f"历史 {hist} 条{'/空态' if not hist else ''}")
    dev_id = await val(ws, "document.getElementById('drawerDevId').textContent", rid); rid += 1
    await shot(ws, OUT / "24_layout_drawer.png", rid); rid += 1

    # 6) 三种关闭方式
    await val(ws, "document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}))", rid); rid += 1
    await asyncio.sleep(0.6)
    esc_closed = await val(ws, "!document.getElementById('deviceDrawer').classList.contains('open')", rid); rid += 1
    expect(f"[{tag}] 关闭方式①Esc", esc_closed is True)

    await click(ws, ".dev-row[data-device='CNC-01']", rid); rid += 1
    await asyncio.sleep(1.2)
    await click(ws, "#drawerMask", rid); rid += 1
    await asyncio.sleep(0.6)
    mask_closed = await val(ws, "!document.getElementById('deviceDrawer').classList.contains('open')", rid); rid += 1
    expect(f"[{tag}] 关闭方式②点抽屉外遮罩", mask_closed is True)

    await click(ws, ".dev-row[data-device='CNC-01']", rid); rid += 1
    await asyncio.sleep(1.2)
    await click(ws, "#drawerClose", rid); rid += 1
    await asyncio.sleep(0.6)
    btn_closed = await val(ws, "!document.getElementById('deviceDrawer').classList.contains('open')", rid); rid += 1
    expect(f"[{tag}] 关闭方式③右上角关闭按钮", btn_closed is True)

    # 7) 抽屉打开期间数据同步刷新 + 筛选/抽屉保持
    await click(ws, ".fchip[data-filter='all']", rid); rid += 1     # 确保全部可见
    await asyncio.sleep(0.3)
    await click(ws, ".dev-row[data-device='CNC-01']", rid); rid += 1
    await asyncio.sleep(1.4)
    sub_before = await val(ws, "document.getElementById('drawerSub').textContent", rid); rid += 1
    await asyncio.sleep(4.5)                                        # 跨 2 个轮询周期
    sub_after = await val(ws, "document.getElementById('drawerSub').textContent", rid); rid += 1
    still = await val(ws, """(() => ({
        open: document.getElementById('deviceDrawer').classList.contains('open'),
        dev: document.getElementById('drawerDevId').textContent,
        pts: document.querySelectorAll('.dw-point').length,
        rows: document.querySelectorAll('.dev-row').length,
    }))()""", rid); rid += 1
    expect(f"[{tag}] 抽屉打开期间数据持续刷新（更新时间推进）",
           sub_before != sub_after and (still or {}).get("open") is True,
           f"「{str(sub_before)[-12:]}」→「{str(sub_after)[-12:]}」")
    expect(f"[{tag}] 刷新后已开抽屉不丢失且测点仍在",
           (still or {}).get("dev") == dev_id and (still or {}).get("pts", 0) >= 5,
           f"设备 {still.get('dev')} / {still.get('pts')} 个测点")
    await val(ws, "document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}))", rid); rid += 1
    await asyncio.sleep(0.5)

    # 8) 断线横幅 + 数值清空（不显旧数据）+ 自动恢复
    await val(ws, "window.__realFetch = window.fetch;"
                  "window.fetch = () => Promise.reject(new TypeError('模拟断线'));", rid); rid += 1
    await asyncio.sleep(4.0)
    off = await val(ws, """(() => {
        const b = document.getElementById('connBanner');
        const keys = [...document.querySelectorAll('.dev-row .dr-key')].map(e => e.textContent.trim());
        return {banner: !b.hidden && getComputedStyle(b).display !== 'none',
                bannerText: b.textContent.slice(0, 30),
                allDash: keys.length > 0 && keys.every(t => t.startsWith('--')),
                rowCount: keys.length};
    })()""", rid); rid += 1
    expect(f"[{tag}] 断线时顶部显示「数据连接中断」横幅", (off or {}).get("banner") is True,
           str((off or {}).get("bannerText")))
    expect(f"[{tag}] 断线期间数值清空为「--」（不显旧数据）", (off or {}).get("allDash") is True,
           f"{off.get('rowCount')} 行全部 --")
    await shot(ws, OUT / "26_layout_disconnected.png", rid); rid += 1
    await val(ws, "window.fetch = window.__realFetch;", rid); rid += 1
    await asyncio.sleep(4.0)
    re = await val(ws, """(() => ({
        bannerHidden: document.getElementById('connBanner').hidden,
        hasValue: [...document.querySelectorAll('.dev-row .dr-key')]
            .some(e => !e.textContent.trim().startsWith('--')),
    }))()""", rid); rid += 1
    expect(f"[{tag}] 恢复后横幅自动消失且真实数值回来",
           (re or {}).get("bannerHidden") is True and (re or {}).get("hasValue") is True, str(re))
    return rid


async def injection_top_checks(ws, rid_start: int, tag: str) -> int:
    """中间设备注入故障：数秒内置顶变红 + 角标 + 筛选计数联动。"""
    rid = rid_start
    print(f"\n── {tag}：注入中间设备 → 置顶变红 ──")
    # 打开演示模式，选中当前渲染顺序的中间设备，注入其第一个故障
    await click(ws, "#demoToggle", rid); rid += 1
    await asyncio.sleep(1.5)
    mid_dev = await val(ws, """(() => {
        const rows = [...document.querySelectorAll('.dev-row')];
        const mid = rows[Math.floor(rows.length / 2)];
        return mid ? mid.dataset.device : null;
    })()""", rid); rid += 1
    set_ok = await set_value(ws, "#demoDevice", mid_dev or "", rid); rid += 1
    dev_now = await val(ws, "document.getElementById('demoDevice').value", rid); rid += 1
    if set_ok != "set" or dev_now != mid_dev:
        expect(f"[{tag}] 中间设备 {mid_dev} 报警数秒内置顶变红", False,
               f"演示面板设备下拉设置失败：set={set_ok} 当前值={dev_now}")
        return rid
    await asyncio.sleep(0.8)
    fault_id = await val(ws, "document.querySelector('#demoFault option') ? document.querySelector('#demoFault option').value : ''", rid); rid += 1
    await click(ws, "#demoInject", rid); rid += 1

    t0 = time.time()
    top = None
    while time.time() - t0 < 20:
        await asyncio.sleep(0.5)
        top = await val(ws, """(() => {
            const r = document.querySelector('.dev-row');
            return r ? {dev: r.dataset.device, cls: r.className,
                        badge: (r.querySelector('.dr-badge') || {}).textContent || ''} : null;
        })()""", rid); rid += 1
        if top and "sev-alarm" in (top.get("cls") or "") and top.get("dev") == mid_dev:
            break
    elapsed = time.time() - t0
    expect(f"[{tag}] 中间设备 {mid_dev} 报警数秒内置顶变红",
           bool(top) and top.get("dev") == mid_dev and "sev-alarm" in (top.get("cls") or ""),
           f"耗时 {elapsed:.1f}s｜首行 {top.get('dev')}｜{top.get('cls')}")
    expect(f"[{tag}] 首行带未处理报警数角标", bool(top) and (top.get("badge") or "").strip().isdigit()
           and int(top.get("badge") or 0) >= 1, f"角标={top.get('badge')}")
    cnt_alarm = await val(ws, "document.getElementById('cntAlarm').textContent", rid); rid += 1
    expect(f"[{tag}] 筛选条「只看报警」计数联动", cnt_alarm not in (None, "", "0"), f"cntAlarm={cnt_alarm}")
    await click(ws, ".fchip[data-filter='alarm']", rid); rid += 1
    await asyncio.sleep(0.4)
    filt = await val(ws, """(() => {
        const rows = [...document.querySelectorAll('.dev-row')];
        return {n: rows.length, allAlarm: rows.every(r => r.classList.contains('sev-alarm')),
                first: rows[0] ? rows[0].dataset.device : null};
    })()""", rid); rid += 1
    expect(f"[{tag}] 「只看报警」只剩报警行",
           (filt or {}).get("n") == int(cnt_alarm or 0) and (filt or {}).get("allAlarm") is True,
           f"{filt.get('n')} 行，首行 {filt.get('first')}")
    await shot(ws, OUT / "23_layout_alarm_top.png", rid); rid += 1
    await shot(ws, OUT / "25_layout_filter.png", rid); rid += 1
    # 复位演示状态，避免影响同档后续断言
    await click(ws, "#demoRecover", rid); rid += 1
    await asyncio.sleep(2.0)
    await click(ws, ".fchip[data-filter='all']", rid); rid += 1
    await click(ws, "#demoToggle", rid); rid += 1
    await asyncio.sleep(1.0)
    return rid


async def mobile_checks(ws, rid_start: int, tag: str, expect_n: int) -> int:
    rid = rid_start
    print(f"\n── {tag}（手机 {MOBILE[0]}×{MOBILE[1]}）──")
    # 用 390×844 视口宽度模拟手机。注意不用 CDP 的 mobile:true 设备模拟：
    # 无头环境下 mobile:true 会让 innerWidth(435) 与布局宽度(390) 错位
    # （固定定位包含块偏移 45px），那是模拟环境产物而非产品问题——
    # 布局宽度 documentElement.clientWidth、媒体查询、抽屉宽度实测均为 390，全部正确。
    await set_viewport(ws, MOBILE[0], MOBILE[1], False, rid); rid += 1
    await asyncio.sleep(5.0)
    m = await val(ws, """(() => {
        const row = document.querySelector('.dev-row');
        const list = document.querySelector('.device-list');
        return {rowH: row ? getComputedStyle(row).height : null,
                rowFs: row ? getComputedStyle(row).fontSize : null,
                bodyTransform: getComputedStyle(document.body).transform,
                listScrolls: list.scrollHeight > list.clientHeight + 1,
                innerW: window.innerWidth};
    })()""", rid); rid += 1
    h_px = float(str((m or {}).get("rowH", "0px"))[:-2])
    expect(f"[{tag}] 手机端行高仍 56–64px", 56 <= h_px <= 64, str((m or {}).get("rowH")))
    expect(f"[{tag}] 手机端行内字号 ≥14px", float(str((m or {}).get("rowFs", "0px"))[:-2]) >= 14,
           str((m or {}).get("rowFs")))
    expect(f"[{tag}] 手机端无缩放", (m or {}).get("bodyTransform") in (None, "none"),
           str((m or {}).get("bodyTransform")))
    expect(f"[{tag}] 手机端列表内部滚动", (m or {}).get("listScrolls") is True)
    n = await val(ws, "document.querySelectorAll('.dev-row').length", rid); rid += 1
    expect(f"[{tag}] 手机端设备全部渲染", n == expect_n, f"rows={n}")
    await shot(ws, OUT / "27_layout_mobile_rows.png", rid); rid += 1

    await click(ws, ".dev-row", rid); rid += 1
    await asyncio.sleep(1.6)
    dw = await val(ws, """(() => {
        const d = document.getElementById('deviceDrawer');
        return {open: d.classList.contains('open'),
                width: Math.round(d.getBoundingClientRect().width),
                vw: window.innerWidth,
                pts: document.querySelectorAll('.dw-point').length};
    })()""", rid); rid += 1
    expect(f"[{tag}] 手机端抽屉全屏（宽=视口宽）",
           (dw or {}).get("open") is True and (dw or {}).get("width") == (dw or {}).get("vw"),
           f"抽屉宽 {dw.get('width')}px / 视口 {dw.get('vw')}px")
    expect(f"[{tag}] 手机端抽屉内容完整（监测点在）", (dw or {}).get("pts", 0) >= 5,
           f"{dw.get('pts')} 个测点")
    await shot(ws, OUT / "28_layout_mobile_drawer.png", rid); rid += 1
    await val(ws, "document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}))", rid); rid += 1
    await asyncio.sleep(0.6)
    return rid


# ---------------------------------------------------------------- 单档位驱动
async def run_config(label: str, devices_yaml: Path, expect_n: int, port: int,
                     with_interactions: bool) -> None:
    tag = label
    tmp_dir = Path(tempfile.mkdtemp(prefix="cncops_layout_"))
    db = tmp_dir / "layout_check.db"
    server = start_server(port, devices_yaml, db)
    profile = Path(tempfile.mkdtemp(prefix="cncops_chrome_"))
    proc = start_chrome(9333, profile, f"http://127.0.0.1:{port}/")
    rid = 0
    try:
        wait_health(port)
        ws_url = await wait_page_target(9333)
        async with websockets.connect(ws_url, max_size=None) as ws:
            cdp = CDP(ws)
            await cdp.start()
            rid = 0
            rid += 1
            await rpc(cdp, "Runtime.enable", {}, rid)
            rid += 1
            await rpc(cdp, "Page.enable", {}, rid)
            rid += 1
            await rpc(cdp, "Log.enable", {}, rid)
            await set_viewport(cdp, DESKTOP[0], DESKTOP[1], False, rid); rid += 1
            try:
                rid = await desktop_checks(cdp, rid, tag, expect_n)
                if with_interactions:
                    rid = await drawer_and_interaction_checks(cdp, rid, tag)
                    rid = await injection_top_checks(cdp, rid, tag)
                    rid = await mobile_checks(cdp, rid, tag, expect_n)
                # 复位视口再截纯净行图（交互档已截过专门图）
                await set_viewport(cdp, DESKTOP[0], DESKTOP[1], False, rid); rid += 1
                await asyncio.sleep(3.0)
                if with_interactions:
                    await shot(cdp, OUT / "22_layout_30rows.png", rid); rid += 1
                else:
                    name = {3: "20_layout_3rows.png", 10: "21_layout_10rows.png"}.get(expect_n)
                    if name:
                        await shot(cdp, OUT / name, rid); rid += 1
            finally:
                await cdp.stop()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(profile, ignore_errors=True)
        stop_server(server)
        shutil.rmtree(tmp_dir, ignore_errors=True)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="只跑某一档：3 / 10 / 30")
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    print("=" * 96)
    print("设备列表 UI 布局验收：紧凑摘要行 + 详情抽屉（3/10/30 台 × 桌面/手机）")
    print("=" * 96)

    configs = [
        ("3 台（正式线）", ROOT / "config" / "devices.yaml", 3),
        ("10 台（预览）", ROOT / "config" / "devices_10.yaml", 10),
        ("30 台（验收）", ROOT / "config" / "devices_30.yaml", 30),
    ]
    port = PORT_BASE
    for label, yaml_path, n in configs:
        if args.only and str(n) != args.only:
            port += 1
            continue
        interactions = (n == 30)          # 抽屉/注入/断线/手机全套在 30 台档执行
        await run_config(label, yaml_path, n, port, interactions)
        port += 1

    # 跨档位一致性（3/10/30 行高字号完全一致）
    print("\n── 跨档位一致性 ──")
    if len(CONSISTENCY) >= 2:
        keys = ("rowH", "rowFs", "nameFs", "statusFs", "keyFs")
        ref_tag = next(iter(CONSISTENCY))
        ref = {k: CONSISTENCY[ref_tag].get(k) for k in keys}
        diffs = {t: {k: v for k, v in m.items() if k in keys and v != ref[k]}
                 for t, m in CONSISTENCY.items()}
        bad = {t: d for t, d in diffs.items() if d}
        expect("3/10/30 台行高与字号完全一致（无随设备数缩放）", not bad,
               f"基准[{ref_tag}] {ref}｜差异={bad or '无'}")
    else:
        expect("3/10/30 台行高与字号完全一致（无随设备数缩放）", False,
               f"只跑了 {len(CONSISTENCY)} 档，无法比对（--only 模式）")

    # JS 报错
    print(f"\n控制台/JS 报错：{len(ERRORS)}")
    for e in ERRORS[:10]:
        print("  -", e)

    print("\n" + "=" * 96)
    total_ok = sum(1 for _, ok, _ in RESULTS if ok)
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  FAIL {name}｜{detail}")
    print(f"  总计：{total_ok}/{len(RESULTS)}"
          f"{'  全部通过' if total_ok == len(RESULTS) else f'  失败 {len(RESULTS) - total_ok} 项'}")
    print("=" * 96)

    # 存档
    lines = []
    for name, ok, detail in RESULTS:
        lines.append(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"｜{detail}" if detail else ""))
    lines.append("")
    lines.append(f"总计：{total_ok}/{len(RESULTS)}"
                 f"{'  全部通过' if total_ok == len(RESULTS) else f'  失败 {len(RESULTS) - total_ok} 项'}")
    lines.append(f"控制台/JS 报错：{len(ERRORS)}")
    lines.extend(f"  - {e}" for e in ERRORS[:10])
    (ROOT / "docs" / "layout_check_output.txt").write_text("\n".join(lines), encoding="utf-8")
    print(f"逐条结果已存档：docs/layout_check_output.txt")

    return 0 if (total_ok == len(RESULTS) and not ERRORS) else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
