"""OPC-UA 只读客户端（可选）：只提供读取，不提供任何写入方法。

只读是硬性约束：本模块刻意不实现 write/set 相关方法，
任何"下发控制指令"的需求在这里无路可走。
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Dict, Optional

from .catalog import get_device
from .config import settings


class OpcUaDataSource:
    """通过 OPC-UA 读取设备实时数据（失败由上层降级）。"""

    def __init__(self, endpoint: Optional[str] = None, timeout: Optional[float] = None) -> None:
        cfg = settings()
        self.endpoint = endpoint or cfg.opcua_endpoint
        self.timeout = timeout or cfg.opcua_timeout_sec

    async def _read_device(self, client, ns_index: int, device_id: str) -> Optional[dict]:
        dev = get_device(device_id)
        if dev is None:
            return None
        root = await client.nodes.objects.get_child([f"{ns_index}:cncops"])
        obj = await root.get_child([f"{ns_index}:{device_id}"])
        values: Dict[str, float] = {}
        for child in await obj.get_children():
            bname = await child.read_browse_name()
            key = bname.Name
            if key in ("status", "alarm_code"):
                continue
            try:
                values[key] = round(float(await child.read_value()), 3)
            except (TypeError, ValueError):
                continue
        return {
            "device_id": dev.id,
            "device_name": dev.name,
            "device_type": dev.type,
            "ts": datetime.now().isoformat(timespec="seconds"),
            "values": values,
            "source": "opcua",
        }

    async def _read(self, device_id: str) -> Optional[dict]:
        from asyncua import Client  # 延迟导入，未安装时由上层降级

        async with Client(url=self.endpoint, timeout=self.timeout) as client:
            ns_index = await client.get_namespace_index("urn:cncops:line01")
            return await self._read_device(client, ns_index, device_id)

    def read_snapshot(self, device_id: str) -> Optional[dict]:
        """同步封装：带超时保护，超时抛 asyncio.TimeoutError 由上层降级处理。"""
        return asyncio.run(asyncio.wait_for(self._read(device_id), timeout=self.timeout + 2.0))
