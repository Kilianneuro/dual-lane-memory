# v0.1.0-alpha

First public alpha of **Dual-Lane Memory Core**, a local-first memory core for chat agents.

## Highlights

- Two lifecycles:
  - slow lane: identity / preference / relationship / promise / experience, 14–365 day half-lives;
  - fast lane: `media_impression`, 7-day half-life, with compression and cleanup.
- Explicit and automatic promotion from the fast lane to long-term memory.
- Scope-aware retrieval: `global` / `origin` / `private` plus `share_all` / `share_same_platform` / `share_owner_only`.
- Owner-private read-all option (`owner_read_all`), without changing group visibility.
- Sensitive/secret memories withheld until owner approval.
- SQLite + FTS5, no vector database.
- Precision-first retrieval with an OR fallback for natural-language questions.
- Group observation (`POST /v1/memory/observe`) for `episode` / `opinion` / `emotion`.
- Bounded self-model proposals with owner approval; base role cards stay read-only.
- Fail-open bridge hook.

## Verification

- 43 automated tests.
- External regression covered: install, init, serve, health, extraction, sensitive approval, LIKE escaping, 413 handling, and client-disconnect logs.
- No database, tokens, chat logs, QQ IDs, or machine paths are included in the package.

## Known limitations

- Default compression is a mock (first N characters).
- Default extraction is regex-based; an LLM extractor can implement the `Extractor` protocol.
- Only the QQ/SnowLuma adapter is included.
- No dashboard or graph relations yet.
- Single-owner local deployment, not a hosted multi-tenant service.

## Quick start

```powershell
python -m unittest discover -s memory_core/tests
python -m memory_core init
# edit memory_core/config.json and set owner_id
python -m memory_core serve
```

## Prior art

Related work includes Mem0, Letta/MemGPT, MemoryOS, MemoryBank, SCM, dual-mem, and SurfCore. This release claims an engineering combination, not the invention of decay, forgetting, or tiered memory.
