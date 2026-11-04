"""运维记录导出：CSV / Excel（xlsx）两种格式，供工厂老板看停机与故障统计。

设计：
- CSV 用 utf-8-sig（带 BOM）：Excel/WPS 双击打开中文不乱码，无需用户在导入向导里选编码；
- Excel 用自研的零依赖 xlsx 写入器（cncops.xlsx），两张工作表：
    ①「运维记录」：逐条明细（与历史页同源，附工厂/操作人/停机时长等老板视角字段）；
    ②「故障与停机统计」：按"设备 × 故障"汇总次数/解决率/平均处理时长/累计停机 + 合计行。
- 过滤条件（工厂/设备/结果/关键字/时间窗）与历史页保持一致，保证"看到的就能导出"。
"""

from __future__ import annotations

import csv
import io
from typing import Any, Dict, List, Optional

from .memory import EXPORT_HEADERS, MemoryStore


def record_to_row(row: Dict[str, Any]) -> List[Any]:
    """一条运维记录 → 导出行（列序与 EXPORT_HEADERS 一致）。"""
    codes = row.get("alarm_codes") or []
    return [
        row.get("id"),
        row.get("created_at", ""),
        row.get("factory_name") or row.get("factory_id", ""),
        row.get("device_id", ""),
        row.get("device_name", ""),
        row.get("fault_id", ""),          # 知识库编号（如 KB-CNC-001），与界面角标一致
        row.get("fault_title", ""),
        "、".join(str(c) for c in codes),
        row.get("max_level", ""),
        row.get("result", ""),
        row.get("handling_min"),
        row.get("downtime_min"),
        row.get("operator", ""),
        row.get("fb_operator", ""),
        row.get("mode", ""),
        row.get("query", ""),
        row.get("conclusion", ""),
        row.get("comment", ""),
    ]


def build_csv(rows: List[Dict[str, Any]]) -> bytes:
    """明细 CSV（utf-8-sig，Excel 直接打开不乱码）。"""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(EXPORT_HEADERS)
    for row in rows:
        writer.writerow(["" if v is None else v for v in record_to_row(row)])
    return buf.getvalue().encode("utf-8-sig")


def _stat_rows(stats: List[Dict[str, Any]]) -> List[List[Any]]:
    """统计表行 + 合计行（老板视角：哪个设备、哪个故障、坏几次、停多久）。"""
    head = ["设备编号", "设备名称", "工厂", "故障编号", "故障名称", "故障次数",
            "已解决", "未解决", "未反馈", "平均处理时长(分钟)", "累计停机(分钟)", "最近发生"]
    body = [[
        s.get("device_id", ""), s.get("device_name", ""), s.get("factory_id", ""),
        s.get("fault_id", ""), s.get("fault_title", ""), s.get("count", 0),
        s.get("resolved", 0), s.get("unresolved", 0), s.get("no_feedback", 0),
        s.get("avg_handling_min") if s.get("avg_handling_min") is not None else "",
        s.get("downtime", 0), s.get("last_at", ""),
    ] for s in stats]
    total_count = sum(s.get("count", 0) for s in stats)
    total_down = sum(s.get("downtime", 0) for s in stats)
    total_res = sum(s.get("resolved", 0) for s in stats)
    total_unres = sum(s.get("unresolved", 0) for s in stats)
    total_nofb = sum(s.get("no_feedback", 0) for s in stats)
    total_row = ["合计", "", "", "", "", total_count, total_res, total_unres, total_nofb, "", total_down, ""]
    return [head] + body + [total_row]


def build_xlsx(rows: List[Dict[str, Any]], stats: List[Dict[str, Any]],
               title: str = "运维记录与故障停机统计") -> bytes:
    """明细 + 统计两张工作表的 xlsx。"""
    from .xlsx import write_xlsx
    detail = [EXPORT_HEADERS] + [record_to_row(r) for r in rows]
    # 列宽（字符数，约）：让老板打开就能看，不用手动拉列
    detail_widths = [8, 20, 12, 10, 16, 12, 22, 14, 9, 9, 13, 13, 10, 10, 9, 34, 40, 30]
    stat_widths = [10, 16, 10, 12, 22, 9, 8, 8, 8, 16, 15, 20]
    return write_xlsx([
        ("运维记录", detail, detail_widths),
        ("故障与停机统计", _stat_rows(stats), stat_widths),
    ], title=title)


def export_filename(fmt: str) -> str:
    """带日期的下载文件名（attachment 用）。"""
    from datetime import datetime
    stamp = datetime.now().strftime("%Y%m%d")
    return f"运维记录_{stamp}.csv" if fmt == "csv" else f"运维记录与统计_{stamp}.xlsx"


def collect(mem: MemoryStore, *, device_id: str = "", factory_id: str = "",
            result: str = "", keyword: str = "", days: Optional[int] = None
            ) -> Dict[str, Any]:
    """按过滤条件取数（明细 + 统计同源），供 API 层直接组装响应。"""
    rows = mem.export_rows(device_id=device_id or None, factory_id=factory_id or None,
                           result=result, keyword=keyword, days=days)
    stats = mem.fault_summary(device_id=device_id or None, factory_id=factory_id or None,
                              days=days)
    return {"rows": rows, "stats": stats}
