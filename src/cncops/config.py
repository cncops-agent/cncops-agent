"""配置中心：所有开关、地址、超时、阈值全部来自环境变量 + config/*.yaml。

约定：
1. 任何密钥只允许通过环境变量进入进程，代码中不得出现厂商名或密钥字面量；
2. 外部调用（OPC-UA / LLM / 向量库）的超时与降级开关统一在这里管理；
3. 阈值不散落在业务代码里，统一读取 config/thresholds.yaml。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Optional

from . import paths

try:  # PyYAML 缺失时给出明确报错，而不是静默失败
    import yaml
except ImportError as exc:  # pragma: no cover
    raise ImportError("缺少依赖 PyYAML，请先执行 pip install -r requirements.txt") from exc


def _load_dotenv(path: Optional[Path] = None) -> None:
    """极简 .env 加载器：只做 KEY=VALUE，不覆盖已存在的环境变量。"""
    env_file = path or paths.ROOT / ".env"
    if not env_file.exists():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _env_float(key: str, default: float) -> float:
    try:
        return float(_env(key) or default)
    except ValueError:
        return default


@dataclass
class Settings:
    """运行时配置（只读快照）。"""

    data_source: str = "simulator"          # simulator | opcua
    opcua_endpoint: str = "opc.tcp://127.0.0.1:4840/cncops/"
    opcua_timeout_sec: float = 3.0          # OPC-UA 读取超时，超时后降级到内存仿真
    sample_interval_sec: float = 2.0

    llm_api_base: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    llm_timeout_sec: float = 8.0            # LLM 超时即降级
    llm_enabled: str = "auto"               # auto | 0 | 1

    embedding_api_base: str = ""
    embedding_api_key: str = ""
    embedding_model: str = ""
    embedding_timeout_sec: float = 5.0

    db_path: Path = field(default_factory=lambda: paths.DEFAULT_DB_PATH)
    kb_dir: Path = field(default_factory=lambda: paths.KB_DIR)
    log_level: str = "INFO"

    # ---- 多租户 / 多工厂预留（本阶段为单租户单值，SaaS 化时按租户注入）----
    # tenant_id：租户标识。当前所有数据同属 "default" 一个租户；上云后由部署环境
    # （环境变量 / 网关头）注入，memory 层全部查询都会带上该条件做行级隔离。
    tenant_id: str = "default"
    # default_operator：未显式填写操作人时的落库值（演示/联调用），留空表示"匿名"。
    default_operator: str = ""

    # 演示模式总开关：关闭时后端拒绝一切故障注入请求（界面也会隐藏入口）。
    # 默认 1：本项目数据源本身是仿真器，不是真实设备；接真实设备时必须置 0。
    demo_allow: str = "1"
    # HTTP 服务端口（web 前端与 API 同源，由 uvicorn 读取）
    api_host: str = "127.0.0.1"
    api_port: int = 8000

    # ---- 派生属性 ----
    @property
    def demo_allowed(self) -> bool:
        return self.demo_allow not in ("0", "false", "False", "no")
    @property
    def llm_configured(self) -> bool:
        """是否配置了可用的大模型（有地址 + 有 Key + 有模型名即视为可用）。"""
        return bool(self.llm_api_base and self.llm_api_key and self.llm_model)

    @property
    def llm_enabled_effective(self) -> bool:
        """结合强制开关后的最终取值：auto 取决于是否配置，0/1 为人工强制。"""
        if self.llm_enabled == "0":
            return False
        if self.llm_enabled == "1":
            return self.llm_configured
        return self.llm_configured

    @property
    def embedding_enabled(self) -> bool:
        return bool(self.embedding_api_base and self.embedding_api_key and self.embedding_model)


@lru_cache(maxsize=1)
def settings() -> Settings:
    """加载一次配置并缓存（进程内共享）。"""
    _load_dotenv()
    return Settings(
        data_source=_env("CNCOPS_DATA_SOURCE", "simulator").lower(),
        opcua_endpoint=_env("CNCOPS_OPCUA_ENDPOINT", "opc.tcp://127.0.0.1:4840/cncops/"),
        opcua_timeout_sec=_env_float("CNCOPS_OPCUA_TIMEOUT_SEC", 3.0),
        sample_interval_sec=_env_float("CNCOPS_SAMPLE_INTERVAL_SEC", 2.0),
        llm_api_base=_env("CNCOPS_LLM_API_BASE"),
        llm_api_key=_env("CNCOPS_LLM_API_KEY"),
        llm_model=_env("CNCOPS_LLM_MODEL"),
        llm_timeout_sec=_env_float("CNCOPS_LLM_TIMEOUT_SEC", 8.0),
        llm_enabled=_env("CNCOPS_LLM_ENABLED", "auto").lower(),
        embedding_api_base=_env("CNCOPS_EMBEDDING_API_BASE"),
        embedding_api_key=_env("CNCOPS_EMBEDDING_API_KEY"),
        embedding_model=_env("CNCOPS_EMBEDDING_MODEL"),
        embedding_timeout_sec=_env_float("CNCOPS_EMBEDDING_TIMEOUT_SEC", 5.0),
        db_path=Path(_env("CNCOPS_DB_PATH", str(paths.DEFAULT_DB_PATH))),
        kb_dir=Path(_env("CNCOPS_KB_DIR", str(paths.KB_DIR))),
        log_level=_env("CNCOPS_LOG_LEVEL", "INFO").upper(),
        demo_allow=_env("CNCOPS_DEMO_ALLOW", "1"),
        api_host=_env("CNCOPS_API_HOST", "127.0.0.1"),
        api_port=int(_env_float("CNCOPS_API_PORT", 8000)),
        tenant_id=_env("CNCOPS_TENANT_ID", "default"),
        default_operator=_env("CNCOPS_DEFAULT_OPERATOR", ""),
    )


@lru_cache(maxsize=8)
def load_yaml(file_path: str) -> Any:
    """读取并缓存 YAML 配置文件。"""
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {path}")
    with path.open("r", encoding="utf-8") as fp:
        return yaml.safe_load(fp)


def thresholds_for(device_type: str) -> Dict[str, Dict[str, Any]]:
    """取某类设备的阈值表（上层业务禁止自己写死阈值）。"""
    data = load_yaml(str(paths.THRESHOLDS_YAML)) or {}
    return data.get(device_type, {}) or {}
