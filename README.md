# Dual-Lane Memory Core

![test](https://github.com/Kilianneuro/dual-lane-memory/actions/workflows/test.yml/badge.svg)

A local-first memory core for chat agents, built around two lifecycles rather than one flat vector store.

**Status: early alpha.** The core and the QQ/SnowLuma hook are usable, but this is not a production-hardened multi-tenant service.

## Why two lanes

| Lane | Used for | Default behavior |
|---|---|---|
| Slow lane | identity, preference, relationship, promise, experience | category-specific half-lives (14–365 days), compression and edge cleanup |
| Fast lane | `media_impression` (short-lived media reactions) | 7-day half-life; compress after 1 and 7 days; purge after 14 days |

Fast-lane memories are not just deleted. They can be consolidated:

- explicit `upgrade_memory()` to `experience` / `preference` / `game_progress`;
- automatic promotion when a `media_impression` has been mentioned at least 3 times;
- automatic promotion when it carries the `owner-shared` tag.

All lifecycle actions are written to `memory_audit`.

## Other properties

- **Scope-aware retrieval**: `share_all`, `share_same_platform`, `share_owner_only`; visibility is `global`, `origin`, or `private`.
- **Sensitive approval**: sensitive/secret memories are withheld from retrieval until the owner approves them.
- **Budgeted injection**: at most 8 results and ~1200 characters per search.
- **Precision + relaxed recall**: FTS5 bigram AND first; if empty, OR fallback with BM25 ranking.
- **Fail-open hook**: if the memory service is down, chat continues without memory.
- **SQLite + FTS5 only**: no vector database, no external service required.

## Quick start

Requires Python 3.10+.

```powershell
cd dual-lane-memory
python -m memory_core init
```

Edit `memory_core/config.json` and set `owner_id` to your own stable owner ID. Then:

```powershell
python -m memory_core serve
```

The service listens on `127.0.0.1:9881` by default. Tokens are generated in `memory_core/api-token` and `memory_core/admin-token`; they are ignored by git.

```powershell
python -m memory_core stats
python -m memory_core pending
python -m memory_core extract "我现在在玩只狼，卡在苇名弦一郎"
```

## Run tests

```powershell
python -m unittest discover -s memory_core/tests
```

## Extension points

The two places where a model would be plugged in are isolated behind protocols:

- `memory_core/extract.py` — `Extractor` protocol; `RegexExtractor` is the dependency-free default. A future `LLMExtractor` only needs to return `ExtractedFact` objects.
- `memory_core/compress.py` — `Compressor` protocol; `MockCompressor` keeps the first N characters. `lifecycle.compress_memory()` already accepts a custom compressor.

```python
from memory_core.extract import Extractor, ExtractedFact
from memory_core.compress import Compressor

class MyExtractor(Extractor):
    def extract(self, text, *, source=None):
        return [ExtractedFact(content="...", category="experience")]

class MyCompressor(Compressor):
    def compress(self, text, *, max_chars=50, level=1):
        return "..."
```

The hook calls `POST /v1/memory/extract` before saving; if that call fails, it falls back to the local JS regex. No API key or network dependency is included in this repository.

## Hook integration

`src/memory-hook.js` is a non-invasive adapter. It expects a running memory service and a token file:

```js
import { createMemoryHook } from './src/memory-hook.js';

const hook = createMemoryHook({
  root: process.cwd(),
  baseUrl: 'http://127.0.0.1:9881',
  tokenFile: './memory_core/api-token',
  ownerId: 'YOUR_OWNER_ID',
});
```

`prefetch()` searches, `attach()` prepends a labelled memory block, and `onTurnEnd()` saves extracted facts. All failures are fail-open.

## Limitations

- The default compressor is a **mock** (first N characters); an LLM compressor can implement the `Compressor` protocol.
- The default extractor is regex-based; an LLM extractor can implement the `Extractor` protocol. The JS hook calls `POST /v1/memory/extract` first and falls back to its local regex if the service is unavailable.
- The only adapter included is QQ/SnowLuma. The schema is platform-ready, but there is no Bilibili/Discord adapter yet.
- No dashboard, graph relations, or conflict resolution yet.
- Built for a single owner on a local machine, not as a hosted service.

## Prior art

This project does not claim invention of decay, forgetting, or tiered memory. Related work includes:

- [Mem0](https://github.com/mem0ai/mem0) — memory layer for agents
- [Letta / MemGPT](https://github.com/letta-ai/letta) — core/archival/recall memory
- [MemoryOS](https://github.com/BAI-LAB/MemoryOS) — hierarchical memory OS
- [MemoryBank implementation](https://github.com/karthiknadar1204/memory-bank-paper-implementation) — Ebbinghaus-style forgetting
- [SCM: Lifecycle Memory for Language Agents](https://zenodo.org/records/21323877) — consolidation/forgetting
- [dual-mem](https://pypi.org/project/dual-mem/) — dual-system layered memory

The specific combination here is: dual-lane lifecycle + explicit/automatic promotion + scope-aware retrieval + owner approval, implemented in SQLite/FTS5 with a fail-open chat hook.

## License

MIT. See `LICENSE` and `NOTICE.md`.
