"""安全分级与只读约束的测试（属于交付铁律，必须可回归）。"""

import inspect
from pathlib import Path

from cncops import paths, safety
from cncops.opcua_client import OpcUaDataSource


def test_dangerous_step_forced_to_l3():
    """涉及拆机/带电/高压/主轴拆装/液压的步骤一律升级为 L3 并附安全警告。"""
    steps = [
        {"level": "L1", "text": "记录报警码与温度读数"},
        {"level": "L2", "text": "停机后拆机检查主轴轴承"},
        {"level": "L2", "text": "在电气柜内测量带电回路电压"},
        {"level": "L2", "text": "液压阀块需要拆下检修"},
    ]
    out = safety.escalate_steps(steps)
    assert out[0]["level"] == "L1"
    for item in out[1:]:
        assert item["level"] == "L3", f"危险步骤未被升级：{item['text']}"
        assert item["danger_hits"] and item["safety"]


def test_warnings_always_present():
    warnings = safety.collect_warnings(["条目自带警告"], [])
    assert any("下发任何控制指令" in w for w in warnings)
    assert "条目自带警告" in warnings


def test_opcua_client_is_readonly():
    """只读约束：客户端不得提供任何写入能力。"""
    forbidden = [name for name, _ in inspect.getmembers(OpcUaDataSource, predicate=inspect.isfunction)
                 if name.startswith(("write", "set_", "call_method"))]
    assert not forbidden, f"只读客户端不应包含写入方法：{forbidden}"

    source = (paths.ROOT / "src" / "cncops" / "opcua_client.py").read_text(encoding="utf-8")
    for token in ("write_value", "set_value", "set_attribute", "call_method"):
        assert token not in source, f"只读客户端源码中不应出现写入调用：{token}"


def test_no_secret_in_source():
    """代码中不得出现任何密钥字面量（只允许从环境变量读取）。"""
    import re

    literal_key = re.compile(r"""(?:api_key|API_KEY|token|TOKEN)\s*=\s*["'][^"'\s]{8,}["']""")
    for py in (paths.ROOT / "src").rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        assert "sk-" not in text and "Bearer sk" not in text, f"{py} 中疑似包含密钥"
        assert not literal_key.search(text), f"{py} 中疑似硬编码了密钥：{literal_key.search(text)}"


def test_env_example_has_no_secret():
    content = (paths.ROOT / ".env.example").read_text(encoding="utf-8")
    assert "CNCOPS_LLM_API_KEY=" in content
    for line in content.splitlines():
        if line.startswith("CNCOPS_LLM_API_KEY="):
            assert line.split("=", 1)[1].strip() == "", ".env.example 不得包含真实密钥"
