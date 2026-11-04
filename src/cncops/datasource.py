"""数据源门面：统一"只读取实时数据"的入口，并负责超时降级。

- simulator：内置内存仿真（离线可跑，默认）
- opcua：连接 OPC-UA 服务器；连接失败/超时自动降级到 simulator 并在快照中标注
"""

from __future__ import annotations

import asyncio
from typing import Dict, List, Optional

from .config import settings
from . import rules
from .simulator import Simulator, get_simulator

_server_thread = None
_opcua_client = None


def simulator() -> Simulator:
    return get_simulator()


def ensure_opcua_server():
    """当配置为 opcua 时，在同一进程内拉起仿真服务器线程（供只读客户端连接）。"""
    global _server_thread
    cfg = settings()
    if cfg.data_source != "opcua":
        return None
    if _server_thread is not None:
        return _server_thread
    from .opcua_server import OpcUaServerThread  # 延迟导入，未安装 asyncua 时不影响主流程
    _server_thread = OpcUaServerThread(cfg.opcua_endpoint, get_simulator(), interval=1.0)
    _server_thread.start()
    return _server_thread


def _opcua():
    global _opcua_client
    if _opcua_client is None:
        from .opcua_client import OpcUaDataSource
        _opcua_client = OpcUaDataSource()
    return _opcua_client


def read_raw(device_id: str) -> Optional[dict]:
    """读取原始快照（未派生状态与报警码）。"""
    cfg = settings()
    if cfg.data_source == "opcua":
        try:
            ensure_opcua_server()
            snap = _opcua().read_snapshot(device_id)
            if snap:
                return snap
        except (asyncio.TimeoutError, OSError, ImportError, Exception) as exc:  # 任意异常都降级
            snap = get_simulator().snapshot(device_id)
            if snap:
                snap["degraded"] = f"OPC-UA 不可用（{type(exc).__name__}），已降级为内置仿真数据源"
                return snap
            return None
    return get_simulator().snapshot(device_id)


def read_snapshot(device_id: str) -> Optional[dict]:
    """读取并补齐派生字段（状态、报警码、证据）的完整快照。"""
    snap = read_raw(device_id)
    if snap is None:
        return None
    return rules.attach_derived(snap)


def read_all() -> List[dict]:
    from .catalog import devices
    return [s for s in (read_snapshot(d.id) for d in devices()) if s]


def inject_fault(device_id: str, fault_id: str) -> str:
    """注入故障（只作用于仿真器自身，非控制指令）。"""
    return get_simulator().inject(device_id, fault_id)


def clear_faults(device_id: Optional[str] = None) -> None:
    get_simulator().clear(device_id)


def active_faults() -> Dict[str, str]:
    return get_simulator().active_faults()


def stop_server() -> None:
    global _server_thread
    if _server_thread is not None:
        _server_thread.stop()
        _server_thread = None
