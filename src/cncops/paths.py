"""统一路径管理：所有文件位置集中在此，避免散落硬编码。"""

import os
import sys
from pathlib import Path

# src/cncops/paths.py -> 项目根目录（开发/源码模式）
_ROOT_DEV = Path(__file__).resolve().parents[2]


def _frozen_base() -> Path:
    """PyInstaller 冻结（单目录）时，资源根 = sys._MEIPASS（即可执行文件所在目录）。"""
    return Path(getattr(sys, "_MEIPASS", str(_ROOT_DEV)))


def _writable_base() -> Path:
    """可写目录（数据/日志）：优先放可执行文件同目录；若该目录不可写（如安装到
    Program Files），回退到 %LOCALAPPDATA%/cncops-demo，保证 SQLite 与日志能创建。"""
    exe_dir = Path(sys.executable).resolve().parent
    try:
        exe_dir.mkdir(parents=True, exist_ok=True)
        probe = exe_dir / ".cncops_writetest"
        probe.write_text("")
        probe.unlink()
        return exe_dir
    except Exception:
        return Path(os.environ.get(
            "LOCALAPPDATA",
            str(Path.home() / "AppData" / "Local"),
        )) / "cncops-demo"


if getattr(sys, "frozen", False):
    ROOT = _frozen_base()
    _WB = _writable_base()
    DATA_DIR = _WB / "data"
    LOG_DIR = _WB / "logs"
else:
    ROOT = _ROOT_DEV
    DATA_DIR = ROOT / "data"
    LOG_DIR = ROOT / "logs"

CONFIG_DIR = ROOT / "config"
KB_DIR = CONFIG_DIR / "kb"

DEFAULT_DB_PATH = DATA_DIR / "cncops.db"


def _resolve(rel_or_abs: str) -> Path:
    p = Path(rel_or_abs)
    return p if p.is_absolute() else ROOT / rel_or_abs


# 设备目录默认用 config/devices.yaml；可用环境变量切换到其他配置（如 10 台设备演示）
DEVICES_YAML = (_resolve(os.environ["CNCOPS_DEVICES_YAML"])
                if os.environ.get("CNCOPS_DEVICES_YAML") else CONFIG_DIR / "devices.yaml")
THRESHOLDS_YAML = CONFIG_DIR / "thresholds.yaml"
ALARMS_YAML = CONFIG_DIR / "alarms.yaml"
SIMULATION_YAML = CONFIG_DIR / "simulation.yaml"
LEXICON_YAML = CONFIG_DIR / "lexicon.yaml"


def ensure_dirs() -> None:
    """确保运行时目录存在（数据/日志）。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
