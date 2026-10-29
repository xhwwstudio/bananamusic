# MusicOn Backend

当前后端已经不是最早那坨“只立骨架”的状态了，至少数据库这块已经双模式跑起来。

## 当前能力

- FastAPI app
- 配置管理
- SQLite / PostgreSQL 双模式数据库访问
- 基础健康检查
- 扫描、播放历史、歌单、Subsonic 用户、榜单历史等核心表结构
- PG -> SQLite 核心业务数据迁移脚本

## PG -> SQLite 迁移

新增脚本：`backend/scripts/migrate_pg_to_sqlite.py`

用途：把 PostgreSQL 里的核心业务数据搬到 SQLite，当前覆盖：

- `songs`
- `play_history`
- `playlists`
- `playlist_tracks`
- `scan_tasks`
- `scan_state`
- `file_index`
- `dirty_dirs`
- `subsonic_users`
- `starred_items`
- `ratings`
- `bookmarks`
- `app_settings`
- `curation_update_history`
- `autofill_failed_uploads`

### 运行方式

```bash
cd backend
. .venv/bin/activate
python scripts/migrate_pg_to_sqlite.py \
  --pg-url 'postgresql+psycopg://musicon:change_me@127.0.0.1:5432/musicon' \
  --sqlite-url 'sqlite:////absolute/path/to/musicon.db'
```

默认行为：

- 会先按当前后端逻辑初始化 SQLite 目标库结构
- 目标表默认先清空，再把 PG 里的数据整批写进去
- JSONB 字段会转成 SQLite 下的 JSON 文本
- 自增主键表会保留原始 ID

如果你想保留目标库已有数据，用 upsert 合并：

```bash
python scripts/migrate_pg_to_sqlite.py \
  --pg-url 'postgresql+psycopg://musicon:change_me@127.0.0.1:5432/musicon' \
  --sqlite-url 'sqlite:////absolute/path/to/musicon.db' \
  --keep-target
```

### 自测脚本

新增：`backend/scripts/test_pg_to_sqlite_migration.py`

它会自动做这几件事：

1. 起一个临时 PostgreSQL Docker 容器
2. 构造覆盖上述 15 张核心表的测试数据
3. 调用迁移脚本导入 SQLite
4. 用 SQLite 直接校验：
   - 各表记录数
   - 播放历史聚合查询
   - 歌单曲目聚合查询
   - JSON 字段可读性（`scan_state` / `curation_update_history`）
   - 上传失败记录关键字段

运行：

```bash
cd backend
. .venv/bin/activate
python scripts/test_pg_to_sqlite_migration.py
```

要求本机可用 Docker，因为它会临时拉起 `postgres:16-alpine` 做自测。
