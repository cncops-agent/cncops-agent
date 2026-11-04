"""口语解析器（确定性实现，不依赖大模型）：设备别名 + 症状同义词扩展。

之所以不用大模型做这步：口语解析是诊断的入口，必须可测试、可复现；
大模型只被用来做"理解补充、原因排序、语言组织"，不参与事实抽取。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .catalog import resolve_device
from .config import load_yaml
from . import paths


@dataclass
class ParsedQuery:
    raw: str
    device_id: Optional[str] = None
    device_name: str = ""
    device_alias_hit: str = ""
    symptom_points: List[str] = field(default_factory=list)
    expanded_terms: List[str] = field(default_factory=list)
    vague: bool = False
    danger_terms: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "raw": self.raw,
            "device_id": self.device_id,
            "device_name": self.device_name,
            "device_alias_hit": self.device_alias_hit,
            "symptom_points": self.symptom_points,
            "expanded_terms": self.expanded_terms,
            "vague": self.vague,
            "danger_terms": self.danger_terms,
            "notes": self.notes,
        }


def _lexicon() -> dict:
    return load_yaml(str(paths.LEXICON_YAML)) or {}


def parse(text: str) -> ParsedQuery:
    """解析一句口语：识别设备、扩展症状同义词、检测模糊与危险描述。"""
    lex = _lexicon()
    hay = (text or "").lower().replace(" ", "")
    result = ParsedQuery(raw=text or "")

    dev = resolve_device(text or "")
    if dev:
        result.device_id = dev.id
        result.device_name = dev.name
        # 记录命中的别名，便于报告里说明"系统是怎么认出设备的"
        for alias in dev.aliases:
            if alias and alias in hay:
                result.device_alias_hit = alias
                break
    else:
        result.notes.append("未在描述中识别到设备，将按实时数据异常最显著的设备推断")

    synonyms: Dict[str, List[str]] = lex.get("synonyms", {}) or {}
    for point, words in synonyms.items():
        for w in words:
            if w in hay:
                if point not in result.symptom_points:
                    result.symptom_points.append(point)
                    # 同义词扩展：把该测点的全部说法加入检索词，提高知识库召回
                    result.expanded_terms.extend(words[:4])
                break

    result.expanded_terms = sorted(set(result.expanded_terms))

    vague_terms = lex.get("vague_terms", []) or []
    if any(v in hay for v in vague_terms):
        result.vague = True
        result.notes.append("描述模糊，未命中具体症状词，改用实时数据超标项驱动检索")

    danger_terms = lex.get("danger_terms", []) or []
    result.danger_terms = [d for d in danger_terms if d in hay]
    if result.danger_terms:
        result.notes.append("检测到危险现象描述（" + "、".join(result.danger_terms) + "），安全等级强制提升")

    return result
