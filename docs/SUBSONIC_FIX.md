# Symfonium 同步失败：根因分析与修复说明

> 适用版本：MusicOn 1.8.9 镜像（`geelonn/musicon:latest`，后端自报 1.9.0）
> 修复版本：1.9.1（本仓库）
> 对应上游 issue：<https://github.com/gefl24/music-on/issues/4>

## 1. 故障现象

- Docker 部署 MusicOn，配置网盘并完成扫描，Web 控制台显示曲库正常。
- Symfonium 添加 Subsonic 服务器时可以保存账号，`ping` 也能通过，但**媒体库同步始终失败**，客户端拿不到歌曲。
- Symfonium 日志中的第一条硬错误：

```
Expected BEGIN_ARRAY but was BEGIN_OBJECT at path $.subsonic-response.openSubsonicExtensions
```

- 在 Nginx 层伪造 `openSubsonicExtensions: []` 后，客户端仍会在后续步骤失败（作者在 issue 中确认“都试过了，不行”）。

## 2. 复现方法

仓库自带一个可重复的复现脚本（无需 Docker、无需真实网盘）：

```bash
cd backend
python ../tools/repro_subsonic.py        # 使用临时 SQLite + 合成曲库
```

脚本会构造包含 `AC/DC`、`Tyson Yoshi、王嘉尔`、`I'm Just Your Type (命中偏爱)`、
`夜夜夜夜（R&B版）` 等“难缠”名称的曲库，并模拟 Symfonium 的调用顺序。
在修复前的代码上可以稳定复现：

- `openSubsonicExtensions` 是 JSON 对象而不是数组；
- `getArtists` 返回的 artist id 在 `getArtist` 中查不到（`status: failed`）；
- `getAlbumList2` 返回的 album id 在 `getAlbum` 中查不到（`Album not found`）；
- `search3.view`、`scrobble.view` 返回 404（被前端兜底路由接住时甚至是 200 的非 Subsonic 响应）。

## 3. 根因清单

### 3.1 `getOpenSubsonicExtensions` 返回了对象（首因）

OpenSubsonic 规范要求：

```json
"openSubsonicExtensions": [
  { "name": "songLyrics", "versions": [1] }
]
```

即 **数组**。原实现返回：

```json
"openSubsonicExtensions": {
  "openSubsonicExtension": [
    { "name": "openSubsonic", "versions": "1" },
    { "name": "transcodeOffset", "versions": "1" }
  ]
}
```

Symfonium 使用 Moshi 严格按 `List<Extension>` 反序列化，于是直接抛
`JsonDataException` 并**中止整个同步流程**。这是同步失败的第一块多米诺骨牌。

> 另外，原实现声称支持 `transcodeOffset`，但 `/rest/stream` 根本没有实现 `offset` 参数，
> 属于“虚假广告”，修复后只声明真正实现的能力（当前无可声明扩展，返回空数组）。

相关代码：`backend/app/api/subsonic.py :: get_open_subsonic_extensions`

### 3.2 `getIndexes` 的 `shortcut` 是对象

```python
payload['shortcut'] = {}
```

Subsonic 的 `indexes.shortcut` 是数组。客户端解析 `getIndexes` 时会再次遇到
`Expected BEGIN_ARRAY but was BEGIN_OBJECT`（路径变为 `$.subsonic-response.indexes.shortcut`）。
修复为 `[]`。

相关代码：`backend/app/api/subsonic.py :: get_indexes`

### 3.3 列表接口与详情接口的 ID 生成规则不一致（最致命的逻辑 Bug）

数据库中两个函数各自实现了“把艺术家/专辑名变成 ID”：

| 位置 | 规则 | `AC/DC` | `Tyson Yoshi、王嘉尔` | `I'm Just Your Type (命中偏爱)` |
| --- | --- | --- | --- | --- |
| `db_data.build_indexes()` / `build_album_list()` | `lower().replace(' ', '-')` | `ac/dc` | `tyson-yoshi、王嘉尔` | `i'm-just-your-type-(命中偏爱)` |
| `subsonic._slugify()`（详情接口反查时使用） | 先把非 `[0-9a-z\u4e00-\u9fff._()-]` 字符替换为 `-` | `ac-dc` | `tyson-yoshi-王嘉尔` | `i-m-just-your-type-(命中偏爱)` |

于是客户端拿到列表里的 id 去请求详情时：

```
getArtist?id=ac/dc                  -> failed: Artist not found
getAlbum?id=夜夜夜夜（r&b版）        -> failed: Album not found
```

Symfonium 的同步策略是“先拉列表，再逐个拉详情”，任何一个 404 都会让该轮同步报错。
曲库里只要存在一个带 `/`、`、`、`（`、`'` 的艺术家或专辑，同步就会失败——这解释了
为什么“能添加账号、能拉到 1311 位艺术家，但就是同步不完”。

**修复**：新增 `backend/app/core/slugs.py` 作为唯一 ID 生成实现，`db_data` 与
`subsonic` 全部改为调用 `artist_id()` / `album_id()`，保证“列表返回的 id 一定能被详情接口解析”。

相关代码：`backend/app/core/slugs.py`、`db_data.build_indexes()`、`db_data.build_album_list()`、`db_data.list_starred_items()`

### 3.4 歌曲的 `starred` 字段是布尔值

Subsonic/OpenSubsonic 中 `Child.starred` 是 **ISO-8601 日期字符串**（未收藏时应省略）。
原实现无条件输出：

```json
"starred": false
```

严格反序列化会报 `Expected a string but was BOOLEAN`，导致整首歌甚至整个专辑解析失败。
修复为：已收藏时输出真实收藏时间（RFC3339），未收藏时不输出该字段。

相关代码：`subsonic.to_subsonic_song()`

### 3.5 `search2 / search3 / scrobble` 缺少 `.view` 路由

Symfonium 等客户端统一请求 `/rest/xxx.view`。原代码中这三个接口只注册了不带后缀的路径：

```python
@router.get('/search3')
@router.get('/scrobble')
```

因此 `/rest/search3.view` 直接 404（若前端静态文件缺失，还会被 SPA 兜底路由接住，
返回 HTTP 200 的 `{"name":"MusicOn","status":"bootstrapped"}`，客户端解析时再次报错）。
修复：补齐 `.view` 别名并同时接受 GET/POST。

相关代码：`subsonic.search2 / search3 / scrobble`

### 3.6 `setRating` 的 SQL 缺少 f-string，必然 500

```python
text('''
    INSERT INTO ratings ...
    DO UPDATE SET rating = EXCLUDED.rating, updated_at = {db_now_expr()}
''')
```

少了 `f` 前缀，`{db_now_expr()}` 被原样发给 SQLite：

```
sqlite3.OperationalError: unrecognized token: "{"
```

Symfonium 在同步/播放时会回传用户评分（`setRating`），一次 500 就足以让“同步完成但有错误”。
修复：补上 `f` 前缀。

相关代码：`db_data.set_song_rating()`

### 3.7 时间戳不是 RFC3339

SQLite 的 `CURRENT_TIMESTAMP` 形如 `2026-10-29 02:53:00`（空格分隔、无时区），
而 Subsonic 的 `created` / `starred` / `changed` 要求 ISO-8601。
部分客户端用 `Instant.parse()` 解析会直接抛异常。
修复：`dt_to_iso()` 统一输出 `2026-10-29T02:53:00Z`。

相关代码：`app/db/compat.py :: dt_to_iso`

### 3.8 `getArtist` 只返回前 20 首歌

```python
'song': [to_subsonic_song(song, auth) for song in artist_songs[:20]]
```

对“单艺术家精选集/合辑”类客户端会造成曲目缺失。已改为返回该艺术家的全部歌曲。

### 3.9 `startScan` 永远返回 `scanning: true`

原实现无论是否真的有扫描任务都返回 `scanning: true`，某些客户端会因此轮询等待、
表现为“同步卡住”。修复为返回真实的 `getScanStatus` 状态（扫描由 Web 控制台/定时任务驱动）。

### 3.10 认证只接受 `t` + `s`

原 `verify_subsonic_auth` 把 `t`、`s` 声明为必填；使用 `p=`（明文/十六进制密码）的客户端
会收到 FastAPI 的 422。修复为兼容 `p=`，同时保留 token 认证。

## 4. 额外修复（同一条同步链路上的隐患）

| 问题 | 影响 | 修复 |
| --- | --- | --- |
| `list_recent_plays` 对 SQLite 的字符串时间戳调用 `.strftime()` | `getNowPlaying` 500 | 增加 `_play_timestamp_text()` 兼容 str/datetime |
| `createPlaylist` 忽略 `playlistId` | 客户端“更新歌单”会重复创建 | 支持按 `playlistId` 替换曲目 |
| `getAlbumList`(v1) 忽略 `size/offset` | 老客户端分页可能死循环/重复 | 支持分页与排序 |
| `getAlbumList2` 忽略 `genre`、`newest` 不排序 | 榜单/流派页异常 | 支持 genre 过滤、按 `created` 排序 |
| 缺少 `getAlbumInfo2` / `getSimilarSongs2` / `getLyricsBySongId` | 新客户端 404 | 增加兼容实现 |
| `indexes` 的字母分组直接用汉字/小写字符 | 客户端索引错乱 | 仅 A-Z 与 `#` |
| `lastModified` 恒为 0 | 客户端无法做增量同步 | 取曲库最大 `updated_at`（毫秒） |
| `requirements.txt` 缺 `cryptography` | 干净环境构建后 115 存储导入失败 | 显式声明依赖 |
| 源码目录运行时找不到前端 | 本地跑只显示 JSON | `main.py` 回退到 `frontend/dist` |

## 5. 修改文件一览

| 文件 | 变更 |
| --- | --- |
| `backend/app/core/slugs.py` | 新增：统一 ID 生成 |
| `backend/app/api/subsonic.py` | 协议修复主体（+223 / -65） |
| `backend/app/services/db_data.py` | ID 统一、starred 时间、评分 SQL、时间戳、最近播放 |
| `backend/app/services/cover_cache.py` | 数据目录可用 `MUSICON_DATA_DIR` 覆盖（源码运行不再写 `/app/data`） |
| `backend/app/db/compat.py` | `dt_to_iso` 输出 RFC3339 |
| `backend/app/main.py` | 版本号 1.9.1、前端目录回退 |
| `backend/requirements.txt` | 增加 `cryptography` |
| `docs/patches/subsonic-sync-fix.patch` | 相对于原始镜像源码的 unified diff |

## 6. 验证

`tools/test_subsonic_compliance.py` 会构造 10 张专辑 / 30 首歌的曲库（含各种特殊字符），
模拟 Symfonium 的完整调用链，并对 JSON/XML 结构做 216 项断言：

```bash
cd backend
python ../tools/test_subsonic_compliance.py
# checks: 216, failures: 0
# ALL COMPLIANCE CHECKS PASSED
```

覆盖内容：

1. `ping`、`getOpenSubsonicExtensions`（数组类型）、`getIndexes`（`shortcut` 数组、索引字母）
2. `getArtists` 返回的每个 artist id → `getArtist` / `getMusicDirectory` / `getArtistInfo2` 全部可解析
3. `getAlbumList2` 分页遍历全部专辑 → 每个 album id → `getAlbum`，并校验 `song.parent`、`songCount`
4. `getMusicDirectory` 逐层浏览
5. `getGenres` / `search3` / `getRandomSongs` / `getSimilarSongs2` / `getTopSongs`
6. `star` / `unstar` / `getStarred2`（`starred` 为字符串）/ `setRating` / `saveBookmark` / `scrobble` / `getNowPlaying` / 歌词
7. 歌单创建、读取、按 `playlistId` 替换、删除
8. `getScanStatus` 不再假扫描；`p=` 明文/十六进制认证；缺失凭证返回 401
9. XML 形态（`openSubsonicExtensions` 包装元素、`artists/index/artist`、`album`、`albumList2`、`starred2`）

此外用真实 `uvicorn` 启动做过冒烟测试：`/health` 200、`/` 返回前端、未知 `/rest/*` 404、
`ping.view` 与 `getOpenSubsonicExtensions.view`（JSON/XML）均正常。

## 7. 升级与客户端操作

1. 用本仓库重新构建镜像并替换 `geelonn/musicon`：

   ```bash
   docker compose build
   docker compose up -d
   ```

   或仅对现有部署应用补丁：把 `docs/patches/subsonic-sync-fix.patch` 在 `backend/` 目录下
   `git apply -p1` / `patch -p1`，再重建镜像（直接 `docker cp` 进容器在重启后会丢失）。

2. 数据库**无需迁移**，原有歌曲、歌单、收藏、评分全部保留。

3. 建议在 Symfonium 中对该服务器执行一次“清除缓存/重新同步”。
   由于特殊字符艺术家/专辑的 ID 规则已修正，旧 ID 会失效，完整重同步可以避免残留条目。

4. 若仍失败，请抓取 MusicOn 容器日志中的 `[subsonic]` 行（已自动脱敏 `t/p/s`），
   以及 Symfonium 的 `Verbose/Subsonic` 日志，对照具体接口定位。
