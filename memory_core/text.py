"""Chinese-friendly tokenization for SQLite FTS5.

No third-party dependency: CJK text is indexed as unigrams + bigrams.
A query of two or more CJK characters is turned into overlapping bigrams
combined with AND, which gives useful substring-like matching without jieba.

Examples:
    make_search_text("用户喜欢深夜聊天")
      -> "用 用户 户 户喜 喜 喜欢 欢 欢深 深 深夜 夜 夜聊 聊 聊天 天 ..."
    make_fts_query("深夜聊天")
      -> '"深夜" AND "夜聊" AND "聊天"'
"""

from __future__ import annotations

import re

_CJK_RUN = re.compile(r"[\u3400-\u9fff]+")
_TOKEN_RE = re.compile(r"[\u3400-\u9fff]+|[A-Za-z0-9_]+")


def tokenize(text: str) -> list[str]:
    """Return FTS tokens: CJK unigrams + bigrams, ASCII words lower-cased."""
    tokens: list[str] = []
    for match in _TOKEN_RE.finditer(str(text or "")):
        part = match.group(0)
        if _CJK_RUN.fullmatch(part):
            tokens.extend(part)  # unigrams
            tokens.extend(part[i:i + 2] for i in range(len(part) - 1))  # bigrams
        else:
            tokens.append(part.lower())
    return tokens


def make_search_text(*parts: object) -> str:
    """Build the space-separated, de-duplicated text stored in memories.search_text."""
    seen: set[str] = set()
    tokens: list[str] = []
    for part in parts:
        for token in tokenize("" if part is None else str(part)):
            if token not in seen:
                seen.add(token)
                tokens.append(token)
    return " ".join(tokens)


def make_fts_query(text: str, operator: str = "AND") -> str:
    """Build a safe FTS5 MATCH expression from user text.

    CJK runs of length >= 2 use bigrams only (the index also has unigrams).
    A 1-character CJK query uses that unigram. ASCII words are AND-ed by
    default; callers can pass operator="OR" for a relaxed fallback query.
    """
    op = " OR " if str(operator).upper() == "OR" else " AND "
    terms: list[str] = []
    for match in _TOKEN_RE.finditer(str(text or "")):
        part = match.group(0)
        if _CJK_RUN.fullmatch(part):
            if len(part) == 1:
                terms.append(part)
            else:
                terms.extend(part[i:i + 2] for i in range(len(part) - 1))
        else:
            terms.append(part.lower())

    seen: set[str] = set()
    unique: list[str] = []
    for term in terms:
        if term and term not in seen:
            seen.add(term)
            unique.append(term)
    if not unique:
        return ""
    return op.join('"' + term.replace('"', '""') + '"' for term in unique)


def normalize_key(value: object) -> str:
    """Normalize an entity/predicate/object lookup key."""
    return re.sub(r"\s+", "", str(value or "").strip().casefold())


def normalize_tag(value: object) -> str:
    return re.sub(r"\s+", "", str(value or "").strip().casefold())[:40]
