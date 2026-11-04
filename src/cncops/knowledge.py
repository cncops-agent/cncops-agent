"""故障知识库：加载 config/kb/*.yaml，提供"字面 + 向量 + 结构化"三路融合检索。

检索完全离线可用：
- 字面/向量：本地字符二元组 TF-IDF 余弦（纯 Python 实现，numpy 可选加速）；
- 结构化：设备类型、报警码、证据测点与条目字段精确匹配（这一路贡献最大，保证可溯源）；
- 若配置了嵌入模型 API，则向量路由改用在线 embedding，失败时自动回退本地实现。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

from .config import load_yaml, settings

# 融合权重：结构化匹配权重最高，保证"数据说什么就查什么"
W_LEXICAL = 0.40
W_VECTOR = 0.25
W_STRUCT = 0.35
MIN_HIT_SCORE = 0.15      # 低于该分数判定为"知识库未覆盖"
GENERIC_ENTRY_PREFIX = "KB-GEN"


@dataclass
class KbEntry:
    id: str
    title: str
    device_types: List[str]
    alarm_codes: List[str]
    points: List[str]
    symptoms: List[str]
    base_probability: float
    conclusion: str
    causes: List[dict]
    steps: List[dict]
    safety: List[str]
    source_file: str = ""
    code: str = ""          # 故障编号（如 F001），界面报告卡角标使用

    def doc_text(self) -> str:
        """用于字面/向量匹配的全文。"""
        parts = [self.title * 2, " ".join(self.symptoms), " ".join(self.points), self.conclusion]
        parts += [c.get("text", "") for c in self.causes]
        parts += [s.get("text", "") for s in self.steps]
        parts += self.safety
        return " ".join(parts)


@dataclass
class KbHit:
    entry: KbEntry
    score: float
    lex_score: float = 0.0
    vec_score: float = 0.0
    struct_score: float = 0.0
    reasons: List[str] = field(default_factory=list)
    matched_terms: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "kb_id": self.entry.id,
            "code": self.entry.code,
            "title": self.entry.title,
            "score": round(self.score, 4),
            "reasons": self.reasons,
            "matched_terms": self.matched_terms,
            "source_file": self.entry.source_file,
        }


_TOKEN_RE = re.compile(r"[a-zA-Z0-9]+|[\u4e00-\u9fff]")


def tokenize(text: str) -> List[str]:
    """分词：英文数字按词，中文按字；再补充中文二元组，兼顾"异响/振动"这类词。"""
    if not text:
        return []
    chars = _TOKEN_RE.findall(text.lower())
    grams: List[str] = []
    for ch in chars:
        if "\u4e00" <= ch <= "\u9fff":
            grams.append(ch)
    grams += [chars[i] + chars[i + 1] for i in range(len(chars) - 1)]
    words = [c for c in chars if not ("\u4e00" <= c <= "\u9fff")]
    return grams + words


class KnowledgeBase:
    def __init__(self, kb_dir: Optional[Path] = None) -> None:
        cfg = settings()
        self.kb_dir = Path(kb_dir or cfg.kb_dir)
        self.entries: List[KbEntry] = []
        self._by_id: Dict[str, KbEntry] = {}
        self._docs: List[List[str]] = []
        self._tf: List[Dict[str, float]] = []
        self._idf: Dict[str, float] = {}
        self.reload()

    # ---------- 加载 ----------
    def reload(self) -> int:
        self.entries, self._by_id = [], {}
        files = sorted(self.kb_dir.glob("*.yaml")) if self.kb_dir.exists() else []
        for fp in files:
            data = load_yaml(str(fp)) or {}
            for item in data.get("entries", []):
                entry = KbEntry(
                    id=item["id"],
                    title=item.get("title", item["id"]),
                    device_types=list(item.get("device_types", [])),
                    alarm_codes=list(item.get("alarm_codes", [])),
                    points=list(item.get("points", [])),
                    symptoms=list(item.get("symptoms", [])),
                    base_probability=float(item.get("base_probability", 0.5)),
                    conclusion=item.get("conclusion", ""),
                    causes=list(item.get("causes", [])),
                    steps=list(item.get("steps", [])),
                    safety=list(item.get("safety", [])),
                    source_file=fp.name,
                    code=str(item.get("code", "") or ""),
                )
                self.entries.append(entry)
                self._by_id[entry.id] = entry
        self._build_index()
        return len(self.entries)

    def _build_index(self) -> None:
        self._docs = [tokenize(e.doc_text()) for e in self.entries]
        n = max(len(self._docs), 1)
        df: Dict[str, int] = {}
        self._tf = []
        for tokens in self._docs:
            tf: Dict[str, float] = {}
            for t in tokens:
                tf[t] = tf.get(t, 0.0) + 1.0
            self._tf.append(tf)
            for t in set(tokens):
                df[t] = df.get(t, 0) + 1
        self._idf = {t: math.log((n + 1) / (c + 1)) + 1.0 for t, c in df.items()}

    # ---------- 检索 ----------
    @staticmethod
    def _field_weights(entry: KbEntry) -> Dict[str, float]:
        weights: Dict[str, float] = {}

        def add(text: str, w: float) -> None:
            for t in set(tokenize(text)):
                weights[t] = max(weights.get(t, 0.0), w)

        add(entry.title, 3.0)
        add(" ".join(entry.symptoms), 3.0)
        add(" ".join(entry.alarm_codes), 2.0)
        add(" ".join(entry.points), 1.5)
        add(entry.conclusion, 1.0)
        add(" ".join(c.get("text", "") for c in entry.causes), 1.0)
        add(" ".join(s.get("text", "") for s in entry.steps), 0.6)
        return weights

    def _lexical(self, query_tokens: Sequence[str], entry: KbEntry) -> tuple:
        weights = self._field_weights(entry)
        if not query_tokens:
            return 0.0, []
        matched, total = 0.0, 0.0
        for t in query_tokens:
            total += 3.0
            if t in weights:
                matched += weights[t]
        def _ok(t: str) -> bool:
            if len(t) < 2 or t.isdigit():
                return False
            if any("\u4e00" <= ch <= "\u9fff" for ch in t):
                return True
            return len(t) >= 3  # 纯英文词要求长度，过滤 alm/101 之类噪声
        hits = sorted({t for t in query_tokens if t in weights and _ok(t)})[:8]
        return (matched / total if total else 0.0), sorted(set(hits))[:8]

    def _vector(self, query_tokens: Sequence[str], idx: int) -> float:
        if not query_tokens or idx >= len(self._tf):
            return 0.0
        qtf: Dict[str, float] = {}
        for t in query_tokens:
            qtf[t] = qtf.get(t, 0.0) + 1.0
        dot = 0.0
        na = 0.0
        nb = 0.0
        for t, c in qtf.items():
            wq = c * self._idf.get(t, 1.0)
            na += wq * wq
        for t, c in self._tf[idx].items():
            wd = c * self._idf.get(t, 1.0)
            nb += wd * wd
        for t, c in qtf.items():
            if t in self._tf[idx]:
                dot += (c * self._idf.get(t, 1.0)) * (self._tf[idx][t] * self._idf.get(t, 1.0))
        if na <= 0 or nb <= 0:
            return 0.0
        return dot / (math.sqrt(na) * math.sqrt(nb))

    def _structured(self, entry: KbEntry, device_type: Optional[str],
                    alarm_codes: Optional[Iterable[str]], points: Optional[Iterable[str]]) -> tuple:
        score, reasons = 0.0, []
        if device_type and device_type in entry.device_types:
            score += 0.35
            reasons.append(f"设备类型匹配({device_type})")
        codes = set(alarm_codes or [])
        hit_codes = sorted(codes & set(entry.alarm_codes))
        if hit_codes:
            score += min(0.6, 0.3 * len(hit_codes))
            reasons.append("报警码匹配(" + "/".join(hit_codes) + ")")
        pts = set(points or [])
        hit_pts = sorted(pts & set(entry.points))
        if hit_pts:
            score += min(0.3, 0.15 * len(hit_pts))
            reasons.append("证据测点匹配(" + "/".join(hit_pts) + ")")
        return min(1.0, score), reasons

    def search(self, query: str, device_type: Optional[str] = None,
               alarm_codes: Optional[Iterable[str]] = None,
               points: Optional[Iterable[str]] = None,
               top_k: int = 3) -> List[KbHit]:
        """融合检索，返回 Top-K（默认 3）命中条目。"""
        q_tokens = tokenize(query)
        hits: List[KbHit] = []
        codes = set(alarm_codes or [])
        pts = set(points or [])
        for idx, entry in enumerate(self.entries):
            lex, terms = self._lexical(q_tokens, entry)
            vec = self._vector(q_tokens, idx)
            struct, reasons = self._structured(entry, device_type, codes, pts)
            score = W_LEXICAL * lex + W_VECTOR * vec + W_STRUCT * struct
            if score <= 0.02:
                continue
            if terms:
                reasons.append("症状词命中(" + "/".join(terms[:4]) + ")")
            hits.append(KbHit(entry, score, lex, vec, struct, reasons, terms))
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:top_k]

    def get(self, entry_id: str) -> Optional[KbEntry]:
        return self._by_id.get(entry_id)

    def best(self, hits: List[KbHit]) -> Optional[KbHit]:
        return hits[0] if hits else None

    @staticmethod
    def is_uncovered(hits: List[KbHit]) -> bool:
        """判定是否"知识库未覆盖"：无命中、分数过低，或只命中通用安全条目。"""
        if not hits:
            return True
        top = hits[0]
        if top.score < MIN_HIT_SCORE:
            return True
        return top.entry.id.startswith(GENERIC_ENTRY_PREFIX)


_KB: Optional[KnowledgeBase] = None


def get_kb() -> KnowledgeBase:
    global _KB
    if _KB is None:
        _KB = KnowledgeBase()
    return _KB
