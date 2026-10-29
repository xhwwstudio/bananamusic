# Quark TV 登录码 / 授权轮询失效修复（1.9.2）

## 症状

在后台「存储源」里选择 **夸克 TV（`quark_tv`）**，点「获取登录码」时前端弹出错误，
或扫码后点「我已扫码，取 Token」失败。

从服务器日志可以看到两种表现：

```
POST /api/settings/quark-tv/login/start HTTP/1.1" 401 Unauthorized
POST /api/settings/quark-tv/login/start HTTP/1.1" 500 Internal Server Error
POST /api/settings/quark-tv/login/poll  HTTP/1.1" 500 Internal Server Error
```

## 根因

### 1. 登录态过期后，前端只显示“Unauthorized”（401）

`main.py` 的中间件对所有 `/api/*` 请求做会话校验，失败时返回
`{"detail": "Unauthorized"}`。而前端设置页在渲染存储源时就会请求
`/api/settings`，一旦 Web 会话过期（例如改过密码、或 cookie 里是旧密码派生的
token），整页的保存/测试/取码请求全部 401，看起来像“获取登录码出错”。

修复：返回可读中文提示 `登录已失效，请重新登录后再操作`，并且 401 只影响
单个请求，不再让页面误判为存储源本身的问题。

### 2. 夸克把“未确认授权”当成 HTTP 400 返回（本轮主因）

`providers_quark_tv.py` 原来在解析响应体之前就调用
`response.raise_for_status()`：

```python
response = await client.get(f"{self._api}{pathname}", params=params)
response.raise_for_status()      # ← 400 直接抛 HTTPStatusError
data = response.json()
```

但夸克开放平台的语义是：

| 场景 | HTTP | JSON |
|---|---|---|
| 二维码未确认 | **400** | `{"status": -1, "errno": 11003, "error_info": "用户未确认授权"}` |
| 二维码过期 | **400** | `{"status": -1, "errno": 11002, "error_info": "授权码Code二维码过期"}` |
| 还未扫码 | 200 | `{"status": 200, ...}` |

于是“用户还没扫码/还没确认”这个**正常中间态**被当成异常抛出，接口 500，
前端只能显示“轮询登录失败”。

修复：

- 新增 `_parse_json()`，先解析 JSON 再判断状态码；
- `errno=11003` 映射为 `authorized=false, pending=true`（前端继续提示“还没授权完成”）；
- `errno=11002` 映射为 `StorageError(code="LOGIN_EXPIRED")`，前端提示重新获取二维码；
- 真正的 HTTP/业务错误才抛错，并带上夸克的 `error_info` 原文。

### 3. `start` / `poll` 接口缺少兜底异常处理

原来只有 `except StorageError`，任何其他异常（网络超时、DNS、JSON 解析失败、
provider 不存在）都会变成裸 500，前端拿不到可读信息。

修复：两个接口都补 `except Exception` 兜底 + `logger.exception`，并统一返回
`{message, code, provider_id, hint}` 结构，前端可展示 `hint`。

## 改动文件

| 文件 | 说明 |
|---|---|
| `backend/app/services/storage/providers_quark_tv.py` | 响应解析重构、11002/11003 语义化 |
| `backend/app/api/app_data.py` | `login/start`、`login/poll` 兜底与错误结构 |
| `backend/app/main.py` | 401 提示改为中文可读文案 |

## 验证

```bash
# 1. 获取登录码
POST /api/settings/quark-tv/login/start
→ 200 {"ok": true, "qr_data": "iVBORw0...", "query_token": "sta...", "device_id": "..."}

# 2. 未扫码时轮询（以前 500，现在 200 pending）
POST /api/settings/quark-tv/login/poll   {"query_token": "<上一步的>"}
→ 200 {"ok": true, "authorized": false, "pending": true, "message": "用户未确认授权"}

# 3. 过期/无效二维码（以前 500，现在 400 可读）
POST /api/settings/quark-tv/login/poll   {"query_token": "bad-token"}
→ 400 {"detail": {"message": "登录二维码已过期，请重新获取登录码", "code": "LOGIN_EXPIRED", ...}}
```

扫码并在夸克 App 确认后，同一接口返回：

```json
{"ok": true, "authorized": true, "refresh_token": "eyJhbGci...", "access_token": "...", "device_id": "..."}
```

页面会自动把 `refresh_token` 填进存储源配置，点「保存存储源」即可。

## 使用注意

- 二维码有效期约 **2 分钟**，过期就点「获取登录码」重新生成，不要反复点「我已扫码」。
- 扫码要用 **夸克 App**（不是浏览器），并在手机上点“确认登录”。
- 获取到 `refresh_token` 后**必须点「保存存储源」**，否则刷新页面就丢了。
- 如果点「获取登录码」直接提示“登录已失效”，先退出后台重新登录，再回存储源页操作。
- 服务器需要能访问 `https://open-api-drive.quark.cn`（VPS 实测可达）。
