"""Pluggable fact extraction.

The default implementation is deliberately dependency-free. A future LLM
extractor only needs to implement the same `Extractor` protocol; the rest of
the memory core does not need to change.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol

SENSITIVE_RE = re.compile(
    r"(?:住址|地址|小区|门牌|电话|手机号|身份证|护照|密码|银行卡|支付宝|微信支付|生日|医院|诊断|病历|吃药|工资|存款)"
)


@dataclass(frozen=True)
class ExtractedFact:
    content: str
    category: str = "other"
    sensitivity: str = "normal"
    confidence: float = 0.8
    tags: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {
            "content": self.content,
            "category": self.category,
            "sensitivity": self.sensitivity,
            "confidence": self.confidence,
            "tags": list(self.tags),
        }


class Extractor(Protocol):
    def extract(self, text: str, *, source: dict[str, Any] | None = None) -> list[ExtractedFact]:
        ...


RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("game_progress", re.compile(r"我(?:现在)?(?:在玩|玩|卡在|打到|通关了|买了|抽到|练了)([^。！？!?\n]{2,80})")),
    ("preference", re.compile(r"我(?:喜欢|爱|讨厌|不喜欢|害怕)([^。！？!?\n]{2,80})")),
    ("identity", re.compile(r"我(?:叫|是|住在|来自|养了)([^。！？!?\n]{2,80})")),
    ("promise", re.compile(r"我(?:答应|保证|决定)([^。！？!?\n]{2,80})")),
    ("opinion", re.compile(r"我(?:觉得|认为|感觉)([^。！？!?\n]{2,80})")),
)


class RegexExtractor:
    """Small, deterministic extractor used as the default/fallback."""

    def __init__(self, max_facts: int = 2) -> None:
        self.max_facts = max(1, int(max_facts))

    def extract(self, text: str, *, source: dict[str, Any] | None = None) -> list[ExtractedFact]:
        value = " ".join(str(text or "").split())
        if len(value) < 4:
            return []
        facts: list[ExtractedFact] = []
        seen: set[str] = set()
        for category, pattern in RULES:
            match = pattern.search(value)
            if not match:
                continue
            content = match.group(0).strip()
            if content.startswith("我"):
                content = "用户" + content[1:]
            content = content[:300]
            if len(content) < 4 or content in seen:
                continue
            seen.add(content)
            facts.append(ExtractedFact(
                content=content,
                category=category,
                sensitivity="sensitive" if SENSITIVE_RE.search(content) else "normal",
            ))
            if len(facts) >= self.max_facts:
                break
        return facts


def extract_facts(text: str, extractor: Extractor | None = None,
                  *, source: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Convenience wrapper returning JSON-friendly dicts."""
    chosen = extractor or RegexExtractor()
    return [fact.as_dict() for fact in chosen.extract(text, source=source)]
