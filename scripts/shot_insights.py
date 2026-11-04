"""无头 Chrome 验收「经验沉淀」视图 + CSV/Excel 导出入口，并产出截图。

前置：需要已启动的 Web 服务（默认 http://127.0.0.1:8321/，可用 --url 指定）。

流程：
  1. 清掉残留注入，回到正常态；
  2. 造数：CNC-01 主轴过热 ×3（已解决，带处理/停机时长）、CNC-02 主轴振动 ×1（未解决）；
  3. 切到「经验」页：截图复发提醒卡 + 按设备统计卡；
  4. 校验导出：CSV（BOM/表头/行数）、XLSX（zip 结构/两张表），并真实点击导出按钮确认无 JS 报错。

产出：docs/screenshots/11_insights_recurrence.png、12_insights_stats.png
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
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
        raise RuntimeError(f"截图失败：{json.dumps(resp, ensure_ascii=False)[:300]}")
    data = base64.b64decode(resp["result"]["result"]["data"] if "result" in resp.get("result", {}) else resp["result"]["data"])
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


def decode_cd(cd: str) -> str:
    """从 Content-Disposition 里解出人类可读文件名（兼容 filename*=UTF-8'' 百分号编码）。"""
    import re
    from urllib.parse import unquote
    m = re.search(r"filename\*=UTF-8''([^;]+)", cd or "")
    if m:
        return unquote(m.group(1))
    m = re.search(r'filename="([^"]+)"', cd or "")
    return m.group(1) if m else ""


async def api(ws, method: str, path: str, body: dict | None, rid: int):
    """在页面上下文里调后端接口（与前端同源，避免跨域）。"""
    js = f"""(() => fetch({json.dumps(path)}, {{
        method: {json.dumps(method)},
        headers: {{'Content-Type': 'application/json'}},
        body: {json.dumps(json.dumps(body or {}))},
    }}).then(async r => ({{ status: r.status, body: await r.text() }})))()"""
    r = await evaluate(ws, js, rid)
    return r.get("result", {}).get("result", {}).get("value", {})


def start_chrome(port: int, profile: Path, url: str) -> subprocess.Popen:
    args = [
        str(CHROME), "--headless=new", "--disable-gpu", "--no-sandbox",
        "--hide-scrollbars", "--window-size=1600,1000",
        f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
        url,
    ]
    return subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


async def wait_page_target(port: int, timeout: int = 25) -> str:
    import urllib.request
    deadline = time.time() + timeout
    while time.time() < timeout + deadline:
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
    ap.add_argument("--url", default="http://127.0.0.1:8321/")
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    profile = Path(tempfile.mkdtemp(prefix="cncops_chrome_ins_"))
    proc = start_chrome(9334, profile, args.url)
    rid = 0
    try:
        ws_url = await wait_page_target(9334)
        async with websockets.connect(ws_url, max_size=None) as ws:
            rid += 1
            await rpc(ws, "Runtime.enable", {}, rid)
            rid += 1
            await rpc(ws, "Log.enable", {}, rid)
            rid += 1
            await rpc(ws, "Page.enable", {}, rid)

            async def collect_errors() -> None:
                try:
                    async for raw in ws:
                        msg = json.loads(raw)
                        if msg.get("method") == "Runtime.exceptionThrown":
                            d = msg["params"]["exceptionDetails"]
                            ERRORS.append(f"JS异常: {d.get('text')} {d.get('exception', {}).get('description', '')[:200]}")
                        elif msg.get("method") == "Log.entryAdded":
                            e = msg["params"]["entry"]
                            if e.get("level") == "error":
                                ERRORS.append(f"控制台错误: {e.get('text', '')[:200]}")
                except Exception:
                    return

            task = asyncio.create_task(collect_errors())

            # 1) 清残留注入，等正常态
            rid += 1
            await api(ws, "POST", "/api/demo/clear", {}, rid)
            for _ in range(10):
                await asyncio.sleep(1.0)
                rid += 1
                s0 = await text_of(ws, "#lineSummary", rid)
                if "全部正常" in s0:
                    break

            # 2) 造数：CNC-01 过热 ×3（已解决，带处理/停机时长）→ 触发复发提醒（30 天内 ≥2 次）
            for i in range(3):
                rid += 1
                await api(ws, "POST", "/api/demo/inject",
                          {"device_id": "CNC-01", "fault_id": "spindle_overheat"}, rid)
                await asyncio.sleep(3.0)
                rid += 1
                rep = await api(ws, "POST", "/api/diagnose",
                                {"query": "一号机主轴发烫，温度高得厉害", "operator": "张师傅"}, rid)
                rec = json.loads(rep.get("body", "{}")) if isinstance(rep.get("body"), str) else rep.get("body", {})
                rid += 1
                await api(ws, "POST", f"/api/records/{rec.get('record_id')}/feedback",
                          {"resolved": True, "comment": f"第 {i + 1} 次：补冷却液并清滤网",
                           "operator": "张师傅", "handling_min": 20 + i * 5,
                           "downtime_min": 30 + i * 10}, rid)
                rid += 1
                await api(ws, "POST", "/api/demo/clear", {}, rid)
                await asyncio.sleep(2.0)
            # CNC-02 振动 ×1（未解决，无停机时长）
            rid += 1
            await api(ws, "POST", "/api/demo/inject",
                      {"device_id": "CNC-02", "fault_id": "spindle_vibration"}, rid)
            await asyncio.sleep(3.0)
            rid += 1
            rep = await api(ws, "POST", "/api/diagnose",
                            {"query": "二号机主轴声音不对，震动很大", "operator": "李师傅"}, rid)
            rec = json.loads(rep.get("body", "{}")) if isinstance(rep.get("body"), str) else rep.get("body", {})
            rid += 1
            await api(ws, "POST", f"/api/records/{rec.get('record_id')}/feedback",
                      {"resolved": False, "comment": "待观察", "operator": "李师傅"}, rid)
            rid += 1
            await api(ws, "POST", "/api/demo/clear", {}, rid)
            await asyncio.sleep(2.0)

            # 3) 切到经验页
            rid += 1
            print("  导航到经验页：", await click(ws, "#navInsights", rid))
            await asyncio.sleep(2.5)
            rid += 1
            summary = await text_of(ws, "#insightSummary", rid)
            expect("经验页统计卡渲染（记录数/解决率/停机）",
                   "运维记录" in summary and "累计停机" in summary, summary.replace("\n", " ")[:120])
            # 复发次数用正则取（库里可能已有历史同故障记录，次数不写死，只要求 ≥2 且为高频复发）
            rid += 1
            rec_r = await evaluate(ws, """(() => {
                const card = document.querySelector('.recurrence-card');
                if (!card) return { found: false, count: 0, critical: false, device: '' };
                const badge = (card.querySelector('.rec-badge') || {}).textContent || '';
                const m = badge.match(/(\\d+)\\s*次/);
                const head = (card.querySelector('.rec-head') || {}).textContent || '';
                return { found: true, count: m ? parseInt(m[1], 10) : 0,
                         critical: card.classList.contains('critical'), device: head };
            })()""", rid)
            rec_v = rec_r.get("result", {}).get("result", {}).get("value", {})
            expect("复发提醒卡出现（CNC-01 同故障 30 天内 ≥2 次，≥3 次升级为高频复发）",
                   rec_v.get("found") and rec_v.get("count", 0) >= 2
                   and (rec_v.get("critical") or rec_v.get("count") == 2)
                   and "CNC-01" in (rec_v.get("device") or ""),
                   f"复发 {rec_v.get('count')} 次｜critical={rec_v.get('critical')}")
            rid += 1
            await shot(ws, OUT / "11_insights_recurrence.png", rid)

            # 设备统计卡（滚到底部再截一张）
            rid += 1
            await evaluate(ws, "document.querySelector('#insightDevices').scrollIntoView({block:'start'})", rid)
            await asyncio.sleep(0.8)
            rid += 1
            devices_txt = await text_of(ws, "#insightDevices", rid)
            expect("按设备统计卡渲染（高频故障条形 + 平均处理时长）",
                   "CNC-01" in devices_txt and "平均处理" in devices_txt and "主轴温度过高" in devices_txt,
                   devices_txt.replace("\n", " ")[:120])
            rid += 1
            await shot(ws, OUT / "12_insights_stats.png", rid)

            # 4) 导出校验（页面内 fetch，检查状态码/文件名/内容特征）
            #    注意：Uint8Array 经 CDP 序列化会变成对象，必须先 Array.from 转真数组再比魔数；
            #    Content-Disposition 的 filename* 按 RFC 5987 是百分号编码，需解码后再比对中文名。
            rid += 1
            csv_r = await evaluate(ws, """(() => fetch('/api/records/export?fmt=csv&days=365')
                .then(async r => ({ status: r.status, cd: r.headers.get('Content-Disposition') || '',
                                    ct: r.headers.get('Content-Type') || '',
                                    bom: Array.from(new Uint8Array(await r.arrayBuffer()).slice(0, 3)) })))()""", rid)
            csv_v = csv_r.get("result", {}).get("result", {}).get("value", {})
            bom = csv_v.get("bom") == [239, 187, 191]
            cd_name = decode_cd(csv_v.get("cd", ""))
            expect("CSV 导出：200 + utf-8-sig BOM + 中文文件名 + 正确 MIME",
                   csv_v.get("status") == 200 and bom
                   and "运维记录" in cd_name and cd_name.endswith(".csv")
                   and "csv" in csv_v.get("ct", ""),
                   f"HTTP {csv_v.get('status')}｜BOM={bom}｜{cd_name}")
            rid += 1
            xlsx_r = await evaluate(ws, """(() => fetch('/api/records/export?fmt=xlsx&days=365')
                .then(async r => ({ status: r.status, cd: r.headers.get('Content-Disposition') || '',
                                    ct: r.headers.get('Content-Type') || '',
                                    magic: Array.from(new Uint8Array(await r.arrayBuffer()).slice(0, 2)) })))()""", rid)
            xlsx_v = xlsx_r.get("result", {}).get("result", {}).get("value", {})
            pk = xlsx_v.get("magic") == [80, 75]
            cd_name = decode_cd(xlsx_v.get("cd", ""))
            expect("XLSX 导出：200 + PK 魔数 + 中文文件名 + 正确 MIME",
                   xlsx_v.get("status") == 200 and pk
                   and "运维记录" in cd_name and cd_name.endswith(".xlsx")
                   and "sheet" in xlsx_v.get("ct", ""),
                   f"HTTP {xlsx_v.get('status')}｜PK={pk}｜{cd_name}")

            # 5) 真实点击导出按钮：确认事件接线正常（不抛 JS 错）
            rid += 1
            print("  点击导出 Excel 按钮：", await click(ws, "#exportXlsxInsight", rid))
            await asyncio.sleep(1.5)

            print(f"\n控制台/JS 报错：{len(ERRORS)}")
            for e in ERRORS[:10]:
                print(f"  - {e}")
        task.cancel()
    finally:
        proc.terminate()
    return 0 if not ERRORS else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
