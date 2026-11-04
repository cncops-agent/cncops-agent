"""演示脚本：跑通 6 个口语用例，覆盖模糊描述、知识库未覆盖、同类复发引用与降级模式。

运行：python scripts/demo_cases.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cncops import datasource  # noqa: E402
from cncops.agent import diagnose, feedback  # noqa: E402
from cncops.config import settings  # noqa: E402
from cncops.memory import MemoryStore  # noqa: E402
from cncops.render import render_markdown  # noqa: E402


def wait_detect(device_id: str, timeout: float = 30.0) -> tuple:
    """注入后轮询，直到检出异常，返回 (耗时秒, 快照)。"""
    start = time.time()
    while time.time() - start < timeout:
        snap = datasource.read_snapshot(device_id)
        if snap and snap.get("alarm_codes"):
            return round(time.time() - start, 2), snap
        time.sleep(0.5)
    return round(time.time() - start, 2), datasource.read_snapshot(device_id)


def wait_normal(device_id: str, timeout: float = 30.0) -> float:
    """清除故障后等待数据回归正常，返回耗时。"""
    start = time.time()
    while time.time() - start < timeout:
        snap = datasource.read_snapshot(device_id)
        if snap and not snap.get("alarm_codes") and not [
                e for e in snap.get("evidences", []) if e.get("severity") != "normal"]:
            return round(time.time() - start, 2)
        time.sleep(0.5)
    return round(time.time() - start, 2)


def banner(title: str) -> None:
    print("\n" + "=" * 96)
    print(f"用例 {title}")
    print("=" * 96)


def main() -> int:
    cfg = settings()
    print(f"数据源：{cfg.data_source} ｜ 大模型可用：{cfg.llm_enabled_effective}（{cfg.llm_api_base or '未配置'}）"
          f" ｜ 记忆库：{cfg.db_path}")

    # 干净起跑：清空记忆库
    mem = MemoryStore(cfg.db_path)
    mem.reset()

    # ---- 用例 1：口语描述（振动 + 发热 → 组合故障）----
    banner("1：口语描述「二号机主轴又响又烫，是不是轴承坏了」")
    datasource.clear_faults()
    datasource.inject_fault("CNC-02", "spindle_bearing_wear")
    elapsed, snap = wait_detect("CNC-02")
    print(f"注入故障后检出耗时：{elapsed} 秒（要求 < 30 秒）｜ 派生报警码：{snap['alarm_codes']}")
    r1 = diagnose("二号机主轴又响又烫，是不是轴承坏了")
    print(render_markdown(r1))

    # ---- 用例 2：空压机压力不足 ----
    banner("2：口语描述「空压机没劲儿，压力表上不去」")
    datasource.inject_fault("AIR-01", "air_pressure_low")
    elapsed, snap = wait_detect("AIR-01")
    print(f"注入故障后检出耗时：{elapsed} 秒 ｜ 派生报警码：{snap['alarm_codes']}")
    r2 = diagnose("空压机没劲儿，压力表上不去")
    print(render_markdown(r2))

    # ---- 用例 3：模糊描述，数据驱动 ----
    banner("3：模糊描述「机器不太对劲」（未点名设备、未说症状）")
    datasource.clear_faults()
    datasource.inject_fault("CNC-01", "spindle_bearing_wear")
    elapsed, snap = wait_detect("CNC-01")
    print(f"注入故障后检出耗时：{elapsed} 秒 ｜ 派生报警码：{snap['alarm_codes']}")
    r3 = diagnose("机器不太对劲")
    print(render_markdown(r3))

    # 给第一次诊断回填反馈：没解决（用于演示排序影响与复发引用）
    if r3.record_id:
        feedback(r3.record_id, resolved=False, comment="按建议换了刀具仍然响")

    # ---- 用例 4：知识库未覆盖 ----
    banner("4：知识库未覆盖「一号机主轴有股焦糊味还冒烟」")
    datasource.clear_faults()
    print(f"清除故障后数据回归正常耗时：{wait_normal('CNC-01')} 秒")
    r4 = diagnose("一号机主轴有股焦糊味还冒烟")
    print(render_markdown(r4))

    # ---- 用例 5：同类故障复发引用历史 ----
    banner("5：同类故障复发「一号机又开始了，跟上次一样」")
    datasource.inject_fault("CNC-01", "spindle_bearing_wear")
    elapsed, snap = wait_detect("CNC-01")
    print(f"注入故障后检出耗时：{elapsed} 秒 ｜ 派生报警码：{snap['alarm_codes']}")
    r5 = diagnose("一号机又开始了，跟上次一样")
    print(render_markdown(r5))

    # ---- 用例 6：无异常时的正常结论 ----
    banner("6：正常状态「帮我看看二号机有没有问题」")
    datasource.clear_faults()
    print(f"清除故障后数据回归正常耗时：{wait_normal('CNC-02')} 秒")
    r6 = diagnose("帮我看看二号机有没有问题")
    print(render_markdown(r6))

    print("\n" + "=" * 96)
    print("演示结束：以上 6 个用例均为真实运行结果（模式为降级模式：无 LLM Key 时自动生效）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
