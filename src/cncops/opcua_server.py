"""OPC-UA 仿真服务器（可选）：把内置仿真器的数据以标准 OPC-UA 节点暴露出来。

地址空间：Objects / cncops / {CNC-01, CNC-02, AIR-01} / {测点变量}
只暴露数值测点；设备状态与报警码由 rules 从数值派生，不作为独立节点，
从根上避免"报警码与数据不一致"的误报问题。
"""

from __future__ import annotations

import asyncio
import threading
from typing import Dict, Optional

from .catalog import devices
from .simulator import Simulator

NAMESPACE_URI = "urn:cncops:line01"
_ROOT_NAME = "cncops"


class OpcUaServerThread:
    """在后台线程中运行 asyncua 服务器，供同一进程内的只读客户端连接。"""

    def __init__(self, endpoint: str, simulator: Simulator, interval: float = 1.0) -> None:
        self.endpoint = endpoint
        self.sim = simulator
        self.interval = interval
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.ready = threading.Event()
        self.error: Optional[str] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="opcua-server", daemon=True)
        self._thread.start()
        if not self.ready.wait(timeout=15):
            raise RuntimeError(f"OPC-UA 服务器启动超时：{self.error or '未知原因'}")

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

    # ---------- 内部 ----------
    def _run(self) -> None:
        try:
            asyncio.run(self._main())
        except Exception as exc:  # pragma: no cover
            self.error = str(exc)
            self.ready.set()

    async def _main(self) -> None:
        from asyncua import Server  # 延迟导入：未安装 asyncua 时不影响其它功能

        server = Server()
        await server.init()
        server.set_endpoint(self.endpoint)
        server.set_server_name("cncops OPC-UA 仿真服务器")
        idx = await server.register_namespace(NAMESPACE_URI)

        root = await server.nodes.objects.add_object(idx, _ROOT_NAME)
        nodes: Dict[str, Dict[str, object]] = {}
        for dev in devices():
            obj = await root.add_object(idx, dev.id)
            nodes[dev.id] = {}
            for key in dev.point_keys:
                if key in ("status", "alarm_code"):
                    continue  # 状态与报警码由数据派生，不设独立节点
                init_val = float(self.sim.baselines.get(dev.type, {}).get(key, 0.0))
                var = await obj.add_variable(idx, key, init_val)
                await var.set_writable(False)   # 对外只读
                nodes[dev.id][key] = var

        async with server:
            self.ready.set()
            while not self._stop.is_set():
                for snap in self.sim.snapshot_all():
                    dev_nodes = nodes.get(snap["device_id"])
                    if not dev_nodes:
                        continue
                    for key, var in dev_nodes.items():
                        if key in snap["values"]:
                            await var.write_value(float(snap["values"][key]))
                await asyncio.sleep(self.interval)
