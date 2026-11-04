"""大模型接入层（OpenAI 兼容协议）。

约定：
1. 厂商地址 / 模型名 / Key 全部来自环境变量，代码里不出现任何厂商名或密钥；
2. 任何异常（未配置、超时、网络错误、返回不可解析）都只返回 None，由上层降级；
3. 大模型只用于口语理解、原因排序与语言组织，不得凭空产出排查步骤（步骤恒取自知识库）。
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from .config import Settings, settings

try:  # httpx 可选，缺失时退回标准库 urllib
    import httpx  # type: ignore
    _HAS_HTTPX = True
except ImportError:  # pragma: no cover
    _HAS_HTTPX = False
    import urllib.error
    import urllib.request


_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.S)


class LLMClient:
    def __init__(self, cfg: Optional[Settings] = None) -> None:
        self.cfg = cfg or settings()

    # ---------- 可用性 ----------
    @property
    def available(self) -> bool:
        return self.cfg.llm_enabled_effective

    @property
    def reason(self) -> str:
        if not self.cfg.llm_configured:
            return "未配置 LLM_API_BASE / LLM_API_KEY / LLM_MODEL"
        if not self.cfg.llm_enabled_effective:
            return "CNCOPS_LLM_ENABLED=0，已强制关闭大模型"
        return "available"

    # ---------- 底层 HTTP ----------
    def _post(self, url: str, payload: Dict[str, Any], timeout: float) -> Optional[Dict[str, Any]]:
        headers = {"Content-Type": "application/json"}
        if self.cfg.llm_api_key:
            headers["Authorization"] = f"Bearer {self.cfg.llm_api_key}"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if _HAS_HTTPX:
            with httpx.Client(timeout=timeout, trust_env=False) as client:
                resp = client.post(url, content=body, headers=headers)
                resp.raise_for_status()
                return resp.json()
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")  # type: ignore
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # type: ignore
            return json.loads(resp.read().decode("utf-8"))

    # ---------- 对话 ----------
    def chat(self, system: str, user: str, temperature: float = 0.2) -> Optional[str]:
        """返回模型文本；任何失败返回 None（上层据此降级）。"""
        if not self.available:
            return None
        base = self.cfg.llm_api_base.rstrip("/")
        url = f"{base}/chat/completions"
        payload = {
            "model": self.cfg.llm_model,
            "temperature": temperature,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
        }
        try:
            data = self._post(url, payload, self.cfg.llm_timeout_sec)
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            return None

    def chat_json(self, system: str, user: str) -> Optional[Dict[str, Any]]:
        """调用模型并解析为 JSON；解析失败返回 None。"""
        raw = self.chat(system, user)
        if not raw:
            return None
        return parse_json_loose(raw)

    def embed(self, texts: List[str]) -> Optional[List[List[float]]]:
        """可选：在线向量嵌入；未配置或失败返回 None（上层回退本地 TF-IDF）。"""
        if not self.cfg.embedding_enabled:
            return None
        base = self.cfg.embedding_api_base.rstrip("/")
        payload = {"model": self.cfg.embedding_model, "input": texts}
        try:
            data = self._post(f"{base}/embeddings", payload, self.cfg.embedding_timeout_sec)
        except Exception:
            return None
        if not isinstance(data, dict) or "data" not in data:
            return None
        try:
            return [item["embedding"] for item in sorted(data["data"], key=lambda x: x.get("index", 0))]
        except (KeyError, TypeError):
            return None


def parse_json_loose(raw: str) -> Optional[Dict[str, Any]]:
    """宽松解析：允许模型输出带 ```json 代码块或前后废话。"""
    text = raw.strip()
    m = _JSON_FENCE.search(text)
    if m:
        text = m.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
