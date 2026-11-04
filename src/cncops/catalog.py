"""设备与点位目录：从 config/devices.yaml 加载，提供别名解析。

设计约束：空压机 AIR-01 拥有完全独立的点位命名空间，绝不复用 CNC 点位
（测试 test_catalog.py::test_point_namespace_no_overlap 会断言这一点）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from typing import Dict, List, Optional

from .config import load_yaml
from . import paths


@dataclass(frozen=True)
class Point:
    key: str
    label: str
    unit: str = ""


# 默认工厂：devices.yaml 未声明 factories 时使用（兼容早期单线配置）
DEFAULT_FACTORY_ID = "F001"
DEFAULT_FACTORY_NAME = "默认工厂"


@dataclass(frozen=True)
class Factory:
    """工厂（多工厂预留）：一台设备归属一个工厂；tenant_id 为后续多租户隔离预留。"""

    id: str
    name: str
    tenant_id: str = "default"


@dataclass(frozen=True)
class Device:
    id: str
    name: str
    type: str
    location: str = ""
    aliases: tuple = ()
    points: Dict[str, Point] = field(default_factory=dict, compare=False)
    key_points: tuple = ()     # 设备卡上优先展示的关键读数（2~4 个）
    factory_id: str = DEFAULT_FACTORY_ID   # 所属工厂（多工厂隔离维度）

    @property
    def point_keys(self) -> List[str]:
        return list(self.points.keys())

    @property
    def key_point_keys(self) -> List[str]:
        """关键读数列表：未配置时自动取前 4 个非状态点位，保证卡片永远有数可显示。"""
        if self.key_points:
            return [k for k in self.key_points if k in self.points] or \
                   [k for k in self.points if k not in ("status", "alarm_code")][:4]
        return [k for k in self.points if k not in ("status", "alarm_code")][:4]

    def has_point(self, key: str) -> bool:
        return key in self.points

    def unit_of(self, key: str) -> str:
        p = self.points.get(key)
        return p.unit if p else ""


@lru_cache(maxsize=1)
def devices() -> List[Device]:
    """加载全部设备（缓存，避免重复读盘）。"""
    raw = load_yaml(str(paths.DEVICES_YAML)) or {}
    default_factory = (factories()[0].id if factories() else DEFAULT_FACTORY_ID)
    result: List[Device] = []
    for item in raw.get("devices", []):
        pts = {p["key"]: Point(p["key"], p.get("label", p["key"]), p.get("unit", ""))
               for p in item.get("points", [])}
        result.append(Device(
            id=item["id"],
            name=item.get("name", item["id"]),
            type=item.get("type", ""),
            location=item.get("location", ""),
            aliases=tuple(a.lower() for a in item.get("aliases", [])),
            points=pts,
            key_points=tuple(item.get("key_points", []) or ()),
            factory_id=str(item.get("factory_id") or default_factory),
        ))
    return result


@lru_cache(maxsize=1)
def factories() -> List[Factory]:
    """加载工厂目录（缓存）。devices.yaml 未声明时退回单工厂，保证旧配置可用。"""
    raw = load_yaml(str(paths.DEVICES_YAML)) or {}
    out: List[Factory] = []
    for item in raw.get("factories", []) or []:
        out.append(Factory(
            id=str(item.get("id") or DEFAULT_FACTORY_ID),
            name=str(item.get("name") or item.get("id") or DEFAULT_FACTORY_NAME),
            tenant_id=str(item.get("tenant_id") or "default"),
        ))
    return out or [Factory(DEFAULT_FACTORY_ID, DEFAULT_FACTORY_NAME)]


@lru_cache(maxsize=1)
def factory_index() -> Dict[str, Factory]:
    return {f.id: f for f in factories()}


def factory_of(device_id: str) -> str:
    """设备所属工厂 id（设备不存在时归入默认工厂，避免记录丢失工厂维度）。"""
    dev = get_device(device_id)
    return dev.factory_id if dev and dev.factory_id else DEFAULT_FACTORY_ID


def factory_name(factory_id: str) -> str:
    fac = factory_index().get(factory_id)
    return fac.name if fac else factory_id or DEFAULT_FACTORY_NAME


@lru_cache(maxsize=1)
def device_by_id() -> Dict[str, Device]:
    return {d.id: d for d in devices()}


def get_device(device_id: str) -> Optional[Device]:
    return device_by_id().get(device_id)


def resolve_device(text: str) -> Optional[Device]:
    """从口语文本中解析设备：按最长别名优先匹配，避免"一号/一号加工中心"歧义。

    返回 None 表示未识别到设备，由上层按"异常最显著设备"推断。
    """
    if not text:
        return None
    hay = text.lower().replace(" ", "")
    best: Optional[Device] = None
    best_len = 0
    for dev in devices():
        for alias in dev.aliases:
            if alias and alias in hay and len(alias) > best_len:
                best, best_len = dev, len(alias)
    return best


def point_namespace_overlap() -> Dict[str, List[str]]:
    """统计**不同设备类型之间**共用的点位 key。

    同类设备（两台 CNC）共用点位属正常；但 AIR-01 绝不允许借用 CNC 的点位，
    因此这里只按设备类型聚合后比较。
    """
    by_key: Dict[str, set] = {}
    for dev in devices():
        for key in dev.point_keys:
            by_key.setdefault(key, set()).add(dev.type)
    return {k: sorted(v) for k, v in by_key.items() if len(v) > 1}
