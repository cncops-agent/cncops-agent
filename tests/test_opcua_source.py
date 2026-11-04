"""OPC-UA 数据源端到端测试（未安装 asyncua 时自动跳过）。"""

import time

import pytest

pytest.importorskip("asyncua", reason="未安装 asyncua，跳过 OPC-UA 端到端测试")


def test_opcua_roundtrip(monkeypatch):
    """数据必须真的经过 OPC-UA 协议读取（source=opcua），且故障注入后可被检出。"""
    monkeypatch.setenv("CNCOPS_DATA_SOURCE", "opcua")
    from cncops import config, datasource
    config.settings.cache_clear()

    try:
        datasource.inject_fault("AIR-01", "air_overheat")
        snap, ok = None, False
        for _ in range(40):
            snap = datasource.read_snapshot("AIR-01")
            if snap and "ALM-201" in snap["alarm_codes"]:
                ok = True
                break
            time.sleep(0.3)
        assert snap is not None, "未取到 OPC-UA 快照"
        assert snap["source"] == "opcua", f"数据未走 OPC-UA 通道：{snap.get('degraded')}"
        assert ok, f"OPC-UA 通道未检出排气高温：{snap['values'].get('discharge_temp')}"
        assert "ALM-201" in snap["alarm_codes"]
    finally:
        datasource.clear_faults()
        datasource.stop_server()
        config.settings.cache_clear()
