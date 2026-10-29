# BananaMusic（MusicOn 可构建源码 + Subsonic 同步修复）

> 本项目基于 [gefl24/music-on](https://github.com/gefl24/music-on)（Docker 镜像 `geelonn/musicon`）反编译整理出的**可构建源码**，并修复了 **Symfonium / PlaySub 等 Subsonic 客户端无法同步媒体库** 的问题。

- 上游仓库只提供 README 与截图，不提供源码；本仓库的 `backend/` 来自官方镜像内的 Python 源码（未编译），`frontend/dist/` 为官方构建产物。
- 本次修复版本：**1.9.1**（上游镜像自报 1.9.0 / 标签 v1.8.9）。
- 同步问题根因与逐条修复说明见 **[docs/SUBSONIC_FIX.md](docs/SUBSONIC_FIX.md)**，夸克 TV 登录问题见 **[docs/QUARK_TV_LOGIN_FIX.md](docs/QUARK_TV_LOGIN_FIX.md)**，完整项目分析见 **[docs/ANALYSIS.md](docs/ANALYSIS.md)**。

![首页](docs/images/home.png)
![歌单页](docs/images/playlists.png)

## ✨ 功能

- **多云盘**：AList、夸克（Cookie / 开放平台）、115、夸克 TV。
- **异步扫描**：全量/增量、目录快照 + 文件索引、8~16 并发、Range 读取 + mutagen 解析标签/封面/歌词。
- **Subsonic 1.16.1**：40+ 端点，Symfonium / 音流 / DSub / PlaySub 均可接入；支持 JSON 与 XML。
- **零带宽播放**：普通歌曲 302 跳转网盘直链；夸克由服务端带 Cookie 反向代理（支持 Range）；在线虚拟歌跳转 LX 音源。
- **Web 控制台**：曲库管理、批量删除、歌单、扫描任务实时日志、播放统计、系统设置。
- **榜单与自动补全**（可选）：对接 LX Music Server，每日拉取 5 大榜单生成精选歌单；可把缺失歌曲自动下载、写标签、上传回网盘并触发增量扫描。
- **封面优化**：Pillow 统一转 WebP（≤800×800）+ 缩略图，按 SHA1 去重。

## 🚀 快速开始（Docker Compose）

```bash
git clone https://github.com/xhwwstudio/bananamusic.git
cd bananamusic
docker compose up -d --build
```

访问 `http://localhost:8000`：

1. 默认账号 `admin` / `changeme`，登录后**立即在【系统设置】修改密码**。
2. 在【系统设置】添加存储源（AList 地址 + Token，或夸克 Cookie，或 115 Cookie）。
3. 在【扫描任务】选择存储源、填写音乐目录（如 `/Music`），新建扫描。
4. 客户端（Subsonic）：
   - 服务器：`http://你的IP:8000`
   - 用户名：`admin`（或你在设置里创建的 Subsonic 用户）
   - 密码：系统设置中的 Subsonic 密码

### PostgreSQL（推荐大曲库）

```yaml
services:
  musicon:
    build: .
    environment:
      DATABASE_URL: postgresql+psycopg://musicon:change_me@postgres:5432/musicon
    depends_on:
      postgres:
        condition: service_healthy
    ports: ["8000:8000"]
    volumes: ["./data:/app/data"]
  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: musicon
      POSTGRES_USER: musicon
      POSTGRES_PASSWORD: change_me
    volumes: ["pgdata:/var/lib/postgresql/data"]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U musicon -d musicon"]
      interval: 10s
      retries: 10
volumes:
  pgdata:
```

## 🛠️ 本地开发

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    Linux/macOS: source .venv/bin/activate
pip install -r backend/requirements.txt

# 后端（会自动使用 ../frontend/dist 作为前端）
cd backend
DATABASE_URL="sqlite:///./data/musicon.db" uvicorn app.main:app --reload --port 8000
```

运行 Subsonic 协议测试（无需真实网盘）：

```bash
cd backend
python ../tools/test_subsonic_compliance.py
# checks: 216, failures: 0
```

只复现 Symfonium 的失败场景：

```bash
cd backend
python ../tools/repro_subsonic.py
```

## 🔧 本次修复（1.9.1）

| 问题 | 影响 | 修复 |
| --- | --- | --- |
| `openSubsonicExtensions` 返回对象而非数组 | Symfonium 同步直接中止 | 返回 `[]`，XML 用 `<openSubsonicExtension>` 包装 |
| 列表接口与详情接口 ID 规则不一致 | 含 `/`、`、`、`（`、`'` 的艺术家/专辑 404 | 新增 `app/core/slugs.py`，全链路统一 |
| `indexes.shortcut` 为对象 | JSON 解析失败 | 改为数组 |
| 歌曲 `starred: false` | 严格客户端类型错误 | 未收藏省略，收藏输出 ISO 时间 |
| `search3/scrobble` 缺 `.view` 路由 | 搜索/播放上报 404 | 补齐 `.view` + POST |
| `setRating` SQL 缺 f-string | 评分必然 500 | 修复 SQL |
| SQLite 时间戳非 ISO | 部分客户端解析异常 | 统一 RFC3339 |
| `getArtist` 截断 20 首 | 曲目缺失 | 返回全部 |
| `startScan` 永远 scanning | 客户端轮询卡住 | 返回真实状态 |
| 认证不支持 `p=` | PlaySub 等无法连接 | 兼容明文/hex `p` |
| 夸克 TV 取登录码/轮询报错 | 无法添加夸克存储源 | 夸克把“未确认/已过期”当 HTTP 400 返回，改为先解析 JSON 再判状态（见 [docs/QUARK_TV_LOGIN_FIX.md](docs/QUARK_TV_LOGIN_FIX.md)） |
| `p115client` 版本锁定失效 | 构建失败 | 放宽到 `>=0.0.9`，兼容 `fs_files_iter` 重命名 |

数据库**无需迁移**。升级后建议在客户端清除缓存并完整重同步（特殊字符艺术家/专辑的 ID 已变更）。

## ⚙️ 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | `sqlite:///./data/musicon.db` | SQLite 或 `postgresql+psycopg://...` |
| `PORT` | `8000` | 容器监听端口（`docker/start-app.sh` 固定 8000） |
| `TZ` | `Asia/Shanghai` | 时区 |
| `LX_SERVICE_MODE` | `embedded` | `embedded` / `remote` |
| `LX_REMOTE_URL` | 空 | 远程 LX Server 地址 |
| `LX_EMBEDDED_ENABLED` | `true` | 是否拉起内嵌 LX Server |
| `SUBSONIC_USER` / `SUBSONIC_PASS` | `admin` / `changeme` | 初始 Subsonic 账号（会被 Web 设置覆盖） |

> 注意：上游 README 提到的 `SECRET_KEY` 在当前代码中并未使用；会话 token 由账号密码哈希派生。

## ☁️ 发布到你自己的 GitHub（无需安装 git）

仓库内置了基于 GitHub Git Data API 的发布脚本，一次提交全部文件：

```powershell
# 1. 创建 Fine-grained Token：仅选择 bananamusic 仓库，Contents = Read and write
$env:GITHUB_TOKEN = "github_pat_xxx"

# 2. 预览将上传哪些文件（不需要 token）
python tools/publish_github.py --dry-run

# 3. 发布（默认 xhwwstudio/bananamusic，main 分支）
python tools/publish_github.py
```

脚本只从环境变量读取 Token，不会写入任何文件或打印到日志；发布完成后建议到 GitHub 撤销该 Token。
也可用 `GITHUB_REPO`、`GITHUB_BRANCH`、`COMMIT_MESSAGE` 覆盖默认值。

## 📁 目录

```text
backend/            FastAPI 后端（app/、scripts/、requirements.txt）
frontend/dist/      官方 Vite 构建产物（无 Vue 源码）
docker/             Dockerfile、start-app.sh
docs/               分析报告、修复说明、补丁、截图
tools/              协议一致性测试、复现脚本
```

## ⚠️ 已知限制

- **前端只有构建产物**：上游未发布 Vue 源码，无法二次开发 UI；`index-*.js` 为压缩代码。
- **未收录 LX Server**：内嵌模式需要把官方镜像里的 `/app/vendor/lxserver`（含 `node_modules`）挂载到 `/app/vendor/lxserver`，或设置 `LX_SERVICE_MODE=remote` 指向外部 LX Server。未提供时榜单/补全不可用，其余功能正常。
- **无服务端转码**：`stream` 直接 302/代理原始文件。
- 年份字段未入库，`fromYear/toYear` 仅接受不生效。
- 建议单 worker 运行（缓存与调度器为进程内单例）。

## 🙏 致谢与声明

- 原始项目：**MusicOn** by [gefl24](https://github.com/gefl24)（镜像 [geelonn/musicon](https://hub.docker.com/r/geelonn/musicon)）。
- 本仓库仅用于个人学习、备份与自己部署的缺陷修复；源码版权归原作者所有。若上游开源，请以官方仓库为准。
- 第三方组件：LX Music Server、mutagen、Pillow、FastAPI 等遵循各自许可证。

## 📄 License

MIT（沿用上游 README 声明）。见 [LICENSE](LICENSE)。
