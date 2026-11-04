"""命令行入口：演示与自测用（ diagnose / inject / history / feedback / devices ... ）。"""

from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional

from . import datasource
from .agent import diagnose, feedback
from .catalog import devices
from .render import render_markdown
from .simulator import Simulator


def cmd_devices(_: argparse.Namespace) -> int:
    for dev in devices():
        print(f"{dev.id} {dev.name} [{dev.type}] {dev.location}")
        print("   别名：" + "、".join(dev.aliases))
        print("   点位：" + "、".join(f"{p.label}({p.key})" for p in dev.points.values()))
    return 0


def cmd_snapshot(args: argparse.Namespace) -> int:
    snaps = [datasource.read_snapshot(args.device)] if args.device else datasource.read_all()
    for snap in snaps:
        if not snap:
            continue
        print(json.dumps(snap, ensure_ascii=False, indent=2))
    return 0


def cmd_faults(_: argparse.Namespace) -> int:
    sim = Simulator()
    for f in sim.list_faults():
        print(f"{f['id']:<22} {f['name']:<18} 适用：{'/'.join(f['device_types'])} ｜ {f.get('desc', '')}")
    return 0


def cmd_inject(args: argparse.Namespace) -> int:
    datasource.inject_fault(args.device, args.fault)
    print(f"已注入故障：{args.device} <- {args.fault}（数据将在 2~3 个采样周期内爬坡到位）")
    return 0


def cmd_clear(args: argparse.Namespace) -> int:
    datasource.clear_faults(args.device)
    print("已清除故障：" + (args.device or "全部设备"))
    return 0


def cmd_diagnose(args: argparse.Namespace) -> int:
    report = diagnose(args.text, device_id=args.device)
    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(render_markdown(report))
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    from .memory import get_memory
    rows = get_memory().device_history(args.device, limit=args.limit)
    if not rows:
        print("暂无历史记录")
        return 0
    for r in rows:
        state = {1: "已解决", 0: "未解决", None: "未反馈"}.get(r.get("resolved"), "未反馈")
        print(f"#{r['id']} {r['created_at']} {r['device_id']} 【{r.get('fault_id')} {r.get('fault_title','')}】"
              f" {r.get('conclusion')} ｜ {state}")
    return 0


def cmd_feedback(args: argparse.Namespace) -> int:
    fid = feedback(args.record_id, args.resolved, args.comment or "")
    state = "解决了" if args.resolved else "没解决"
    print(f"已记录反馈 #{fid}：记录 #{args.record_id} -> {state}（将影响下次同类诊断的原因排序）")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cncops", description="设备运维 AI 智能体（只读诊断）")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("devices", help="列出设备与点位").set_defaults(func=cmd_devices)

    sp = sub.add_parser("snapshot", help="读取实时数据快照")
    sp.add_argument("--device", help="设备号，如 CNC-02")
    sp.set_defaults(func=cmd_snapshot)

    sub.add_parser("faults", help="列出可用故障剧本").set_defaults(func=cmd_faults)

    ij = sub.add_parser("inject", help="注入故障（仅作用于仿真器）")
    ij.add_argument("--device", required=True)
    ij.add_argument("--fault", required=True)
    ij.set_defaults(func=cmd_inject)

    cl = sub.add_parser("clear", help="清除故障")
    cl.add_argument("--device")
    cl.set_defaults(func=cmd_clear)

    dg = sub.add_parser("diagnose", help="执行一次诊断")
    dg.add_argument("text", help="口语描述，如：二号机声音不对还发烫")
    dg.add_argument("--device", help="指定设备号")
    dg.add_argument("--json", action="store_true", help="输出 JSON")
    dg.set_defaults(func=cmd_diagnose)

    hs = sub.add_parser("history", help="查看设备历史诊断")
    hs.add_argument("--device", required=True)
    hs.add_argument("--limit", type=int, default=10)
    hs.set_defaults(func=cmd_history)

    fb = sub.add_parser("feedback", help="回填处理反馈")
    fb.add_argument("record_id", type=int)
    fb.add_argument("--resolved", action="store_true", help="标记为解决")
    fb.add_argument("--comment", default="")
    fb.set_defaults(func=cmd_feedback)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
