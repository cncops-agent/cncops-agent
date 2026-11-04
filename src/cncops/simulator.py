"""内置内存仿真数据源：离线可跑，行为与 OPC-UA 数据源保持一致（同一只读接口）。

故障注入只修改"仿真器自身"的数值，不等同于向真实设备下发控制指令。
"""

from __future__ import annotations

import random
import threading
from datetime import datetime
from typing import Dict, List, Optional

from .catalog import Device, devices, get_device
from .config import load_yaml
from . import paths


class Simulator:
    """按 config/simulation.yaml 产出实时数据；支持故障注入与恢复。"""

    def __init__(self, seed: int = 20261018) -> None:
        cfg = load_yaml(str(paths.SIMULATION_YAML)) or {}
        self.baselines: Dict[str, Dict[str, float]] = cfg.get("baselines", {})
        self.faults: Dict[str, dict] = {f["id"]: f for f in cfg.get("faults", [])}
        self.ramp = float(cfg.get("ramp_factor", 0.45))
        self.noise_ratio = float(cfg.get("noise_ratio", 0.02))
        self._rng = random.Random(seed)
        self._lock = threading.Lock()
        self._state: Dict[str, Dict[str, float]] = {}
        self._targets: Dict[str, Dict[str, float]] = {}     # 注入中的故障目标值
        self._active: Dict[str, str] = {}                   # device_id -> fault_id
        for dev in devices():
            base = self.baselines.get(dev.type, {})
            self._state[dev.id] = {k: v for k, v in base.items() if isinstance(v, (int, float))}

    # ---------- 内部工具 ----------
    def _noise(self, base: float) -> float:
        return self._rng.gauss(0.0, abs(base) * self.noise_ratio)

    def _advance(self, dev: Device) -> Dict[str, float]:
        """推进一步：向目标值/基线值爬坡，并叠加噪声。"""
        base = self.baselines.get(dev.type, {})
        cur = self._state.setdefault(dev.id, {})
        target = self._targets.get(dev.id, {})
        new_vals: Dict[str, float] = {}
        for key, baseline in base.items():
            if not isinstance(baseline, (int, float)):
                continue
            goal = target.get(key, baseline)
            cur_val = cur.get(key, baseline)
            new_val = cur_val + (goal - cur_val) * self.ramp
            new_val += self._noise(baseline)
            new_vals[key] = round(new_val, 3)
        return new_vals

    # ---------- 对外接口（只读） ----------
    def snapshot(self, device_id: str) -> Optional[dict]:
        """读取某台设备的实时快照（模拟 OPC-UA 读取）。"""
        dev = get_device(device_id)
        if dev is None:
            return None
        with self._lock:
            values = self._advance(dev)
            self._state[dev.id] = values
        return {
            "device_id": dev.id,
            "device_name": dev.name,
            "device_type": dev.type,
            "ts": datetime.now().isoformat(timespec="seconds"),
            "values": dict(values),
            "source": "simulator",
            "active_fault": self._active.get(dev.id),
        }

    def snapshot_all(self) -> List[dict]:
        return [s for s in (self.snapshot(d.id) for d in devices()) if s]

    # ---------- 故障注入（仅作用于仿真器自身） ----------
    def inject(self, device_id: str, fault_id: str) -> str:
        dev = get_device(device_id)
        if dev is None:
            raise ValueError(f"未知设备: {device_id}")
        fault = self.faults.get(fault_id)
        if fault is None:
            raise ValueError(f"未知故障剧本: {fault_id}")
        if dev.type not in fault.get("device_types", []):
            raise ValueError(f"故障 {fault_id} 不适用于设备类型 {dev.type}")
        with self._lock:
            self._targets[device_id] = dict(fault.get("target", {}))
            self._active[device_id] = fault_id
        return fault_id

    def clear(self, device_id: Optional[str] = None) -> None:
        """清除故障（device_id 为空表示全部恢复）。"""
        with self._lock:
            if device_id is None:
                self._targets.clear()
                self._active.clear()
            else:
                self._targets.pop(device_id, None)
                self._active.pop(device_id, None)

    def active_faults(self) -> Dict[str, str]:
        return dict(self._active)

    def list_faults(self) -> List[dict]:
        return list(self.faults.values())


_SIM: Optional[Simulator] = None


def get_simulator() -> Simulator:
    """进程内共享的仿真器实例。"""
    global _SIM
    if _SIM is None:
        _SIM = Simulator()
    return _SIM
