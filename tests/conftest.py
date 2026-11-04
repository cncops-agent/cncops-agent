"""pytest 公共配置：把源码加入路径，并为测试准备独立的临时数据库。"""

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cncops import config, datasource, knowledge, memory  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """每个用例使用独立的 SQLite 文件，避免测试之间互相污染。"""
    monkeypatch.setenv("CNCOPS_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("CNCOPS_LLM_ENABLED", "0")   # 默认关闭大模型，保证离线可跑
    config.settings.cache_clear()
    memory._STORE = None
    knowledge._KB = None
    datasource.clear_faults()
    yield
    datasource.clear_faults()
    config.settings.cache_clear()
    memory._STORE = None
