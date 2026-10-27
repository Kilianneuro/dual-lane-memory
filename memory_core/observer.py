"""Pluggable group-conversation observer.

Phase A ships a deterministic mock. It never calls a model and never stores raw
messages; it only produces an episode summary from a few keywords. A future
LLMObserver can implement the same protocol and return richer impressions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol

_TOKEN_RE = re.compile(r"[\u4e00-\u9fff]{2,8}")
_STOP = {
    "我们", "你们", "他们", "就是", "这个", "那个", "还是", "真的", "可以",
    "什么", "怎么", "现在", "今天", "今晚", "哈哈", "不是", "然后", "已经",
}


@dataclass
class Observation:
    episode: str = ""
    topics: tuple[str, ...] = field(default_factory=tuple)
    impressions: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {
            "episode": self.episode,
            "topics": list(self.topics),
            "impressions": list(self.impressions),
        }


class Observer(Protocol):
    def observe(self, messages: list[dict[str, Any]],
                *, context: dict[str, Any] | None = None) -> Observation:
        ...


class MockObserver:
    """Offline fallback used until an LLM observer is configured."""

    def __init__(self, max_topics: int = 5) -> None:
        self.max_topics = max(1, int(max_topics))

    def observe(self, messages: list[dict[str, Any]],
                *, context: dict[str, Any] | None = None) -> Observation:
        counts: dict[str, int] = {}
        for message in messages[:100]:
            text = str(message.get("text") or "")
            for token in _TOKEN_RE.findall(text):
                if token in _STOP:
                    continue
                counts[token] = counts.get(token, 0) + 1
        topics = tuple(
            word for word, _ in sorted(
                counts.items(), key=lambda item: (-item[1], -len(item[0]), item[0])
            )[:self.max_topics]
        )
        if topics:
            episode = "群里最近聊到：" + "、".join(topics) + "。"
        else:
            episode = "群里最近有一些零散聊天。"
        return Observation(episode=episode[:300], topics=topics)
