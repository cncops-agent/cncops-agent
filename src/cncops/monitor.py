"""监控趋势采样：为前端监控页提供时序数据。

设计要点：
1. 采样在**后台线程**中进行，页面刷新（Streamlit rerun）不会丢失已采数据；
2. 数据只写内存环形缓冲，不落库，避免污染运维记忆库；
3. 采样只读快照（read_snapshot），与诊断链路完全同源，不存在第二套数据；
4. 线程为 daemon，进程退出即结束；提供 stop() 便于演示后收尾。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Dict, List, Optional, Tuple

from . import datasource
from .catalog import devices

DEFAULT_MAXLEN = 180          # 保存最近 180 个采样点（1 秒 1 点 ≈ 3 分钟）
DEFAULT_INTERVAL = 1.0        # 采样周期（秒）


class TrendBuffer:
    """按设备保存测点时序（环形缓冲）。"""

    def __init__(self, maxlen: int = DEFAULT_MAXLEN) -> None:
        self.maxlen = maxlen
        self._data: Dict[str, deque] = {}      # device_id -> deque[(ts, values_dict)]

    def add(self, device_id: str, ts: str, values: Dict[str, float]) -> None:
        buf = self._data.setdefault(device_id, deque(maxlen=self.maxlen))
        buf.append((ts, dict(values)))

    def series(self, device_id: str, point: str) -> List[Tuple[str, float]]:
        """取某个测点的时序，供画图使用（缺失的采样点会被跳过）。"""
        out: List[Tuple[str, float]] = []
        for ts, values in self._data.get(device_id, []):
            if point in values:
                out.append((ts, float(values[point])))
        return out

    def latest(self, device_id: str) -> Optional[Tuple[str, Dict[str, float]]]:
        buf = self._data.get(device_id)
        return buf[-1] if buf else None

    def length(self, device_id: str) -> int:
        return len(self._data.get(device_id, []))

    def clear(self) -> None:
        self._data.clear()


class TrendSampler(threading.Thread):
    """后台周期采样线程：按固定周期读取全部设备快照写入缓冲。"""

    def __init__(self, buffer: TrendBuffer, interval: float = DEFAULT_INTERVAL) -> None:
        super().__init__(daemon=True, name="cncops-trend-sampler")
        self.buffer = buffer
        self.interval = interval
        self._stop = threading.Event()

    def run(self) -> None:  # pragma: no cover - 线程主循环
        while not self._stop.is_set():
            try:
                for snap in datasource.read_all():
                    self.buffer.add(snap["device_id"], snap["ts"], snap["values"])
            except Exception:
                # 采样失败不影响页面：下一拍继续尝试
                pass
            self._stop.wait(self.interval)

    def stop(self) -> None:
        self._stop.set()


def pump(buffer: TrendBuffer, rounds: int = 1, interval: float = 0.0) -> None:
    """手动补采若干拍（用于脚本走查 / 页面刚打开时快速填充曲线）。"""
    for _ in range(max(1, rounds)):
        for snap in datasource.read_all():
            buffer.add(snap["device_id"], snap["ts"], snap["values"])
        if interval:
            time.sleep(interval)
