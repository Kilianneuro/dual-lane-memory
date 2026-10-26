# Migrations

> 新安装直接使用 `schema.sql`（v2）。
> `001_multiplatform.sql` 用于把旧的 v1 数据库升级到 v2。
> 下面的步骤保留作迁移记录/重放参考。

当前目录只放**已评审、已执行**的数据库迁移脚本。

## v1 -> v2：多平台来源

文件：

```text
001_multiplatform.sql
```

### 执行前

1. 停止 `memory_core` HTTP 服务；
2. 备份数据库（只复制，不删除原文件）：

```powershell
Copy-Item `
  "memory_core/memory.db" `
  "memory_core/memory.db.bak-v1"
```

3. 确认版本：

```sql
SELECT value FROM schema_meta WHERE key = 'schema_version';
-- 期望 1
```

### 执行

如果系统有 `sqlite3` 命令行：

```powershell
sqlite3 "memory_core/memory.db" `
  < "memory_core/migrations\001_multiplatform.sql"
```

也可以用 DB Browser for SQLite 打开 `memory.db`，执行 `001_multiplatform.sql` 全文。

### 迁移做了什么

- `memories` 新增 `context_scope`：
  - `visibility=global` → `share_all`
  - `visibility=origin` → `share_same_platform`
  - `visibility=private` → `share_owner_only`
- `memory_approvals` 新增 `requested_context_scope` / `decided_context_scope`
- 新增 `memory_category_defaults`，写入各分类默认半衰期
- 重建 `memory_sources`：
  - 旧 `source_kind=group/private` → `platform=qq`
  - `context_type=source_kind`
  - `context_id=source_id`
- 重建 `memory_mentions`、`memory_audit`，来源字段同样映射为 `qq` + 旧 kind/id
- `memories`、`memory_tags`、`memory_relations`、FTS 索引**不删除、不重建**，内容保持不变
- 最后把 `schema_meta.schema_version` 设为 `2`

### 执行后验证

```sql
SELECT value FROM schema_meta WHERE key = 'schema_version';  -- 2

SELECT COUNT(*) FROM memories;
SELECT COUNT(*) FROM memory_sources;
SELECT COUNT(*) FROM memory_mentions;
SELECT COUNT(*) FROM memory_audit;

SELECT id, visibility, context_scope
  FROM memories
 ORDER BY id
 LIMIT 20;

SELECT memory_id, platform, context_type, context_id
  FROM memory_sources
 ORDER BY memory_id
 LIMIT 20;

PRAGMA integrity_check;  -- ok
```

### 失败时

迁移脚本使用单个事务：

- 迁移中途失败：SQLite 会回滚，原表保持不变；
- 如果已经部分成功但校验异常：停止服务，用 `memory.db.bak-v1` 复制回去，再排查。

### 迁移完成之后

- `schema.sql` 已替换为 v2；
- `store.py` / `server.py` / CLI 已同步支持 `platform` / `context_type` / `context_id` / `context_scope`；
- 下一阶段才接入桥接，本阶段不动 `src/`。
