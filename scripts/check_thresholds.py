"""阈值可达性自检：断言每个故障剧本的"必越限测点"在注入后必定越限，且裕度大于 3σ 噪声。

这是规避旧项目"阈值不可达"问题的守门脚本：仿真量程与阈值同源于 config/，
任何一边被改动导致越不过阈值，本脚本就会失败。

运行：python scripts/check_thresholds.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cncops import rules  # noqa: E402
from cncops.config import load_yaml, thresholds_for  # noqa: E402
from cncops.paths import SIMULATION_YAML  # noqa: E402


def normalized_margin(value: float, rule: dict, baseline: float) -> tuple:
    """计算归一化裕度：避免了不同量纲（°C / MPa / 比率）无法直接比较的问题。"""
    direction = rule.get("direction", "above")
    if direction == "deviation":
        nominal = float(rule.get("nominal") or 1.0)
        dev = abs(value - nominal) / abs(nominal)
        thr = float(rule.get("alarm") if dev >= float(rule.get("alarm", 1)) else rule.get("warn", 1))
        return abs(dev - thr), "比率"
    hit = rule.get("alarm") if rules.evaluate(value, rule) == "alarm" else rule.get("warn")
    if hit is None or not baseline:
        return 0.0, "绝对值"
    return abs(value - float(hit)) / abs(baseline), "相对"


def main() -> int:
    cfg = load_yaml(str(SIMULATION_YAML)) or {}
    noise_ratio = float(cfg.get("noise_ratio", 0.01))
    baselines = cfg.get("baselines", {})
    faults = cfg.get("faults", [])
    failures = 0

    print(f"噪声设定：{noise_ratio * 100:.1f}%（3σ = {noise_ratio * 300:.1f}% 归一化裕度门槛）\n")
    print(f"{'故障剧本':<20}{'测点':<22}{'目标值':>10}{'触发阈值':>10}{'判定':>8}{'归一化裕度':>12}  结果")
    print("-" * 96)
    for fault in faults:
        must = set(fault.get("must_trigger", []))
        for dev_type in fault.get("device_types", []):
            table = thresholds_for(dev_type)
            base = baselines.get(dev_type, {})
            for point, target in fault.get("target", {}).items():
                rule = table.get(point)
                if rule is None:
                    print(f"{fault['id']:<20}{point:<22}{'缺少阈值规则':>26}  FAIL")
                    failures += 1
                    continue
                severity = rules.evaluate(float(target), rule)
                hit = rule.get("alarm") if severity == "alarm" else rule.get("warn")
                margin, _ = normalized_margin(float(target), rule, float(base.get(point, target)))
                is_must = point in must
                if is_must:
                    ok = severity != "normal" and margin > noise_ratio * 3
                    if not ok:
                        failures += 1
                    mark = "PASS" if ok else "FAIL"
                else:
                    mark = "INFO(伴随)"
                print(f"{fault['id']:<20}{point:<22}{float(target):>10.2f}"
                      f"{(hit if hit is not None else 0):>10.2f}{severity:>8}{margin:>12.3f}  {mark}")

    print("-" * 96)
    if failures == 0:
        print("结论：全部故障剧本的必越限测点均可稳定越限（阈值可达），自检通过")
    else:
        print(f"结论：存在 {failures} 项阈值不可达，需调整 config/simulation.yaml 或 config/thresholds.yaml")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
