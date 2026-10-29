# Storage Provider 抽象改造说明

## 目标
把 AList 从系统唯一入口，降级为一个独立的存储 Provider 模块；业务层统一依赖 Storage Service，后续可继续扩展 Quark / 115 / 阿里云盘等实现。

## 本次已完成

### 1. 新增统一存储抽象层
新增目录：`app/services/storage/`

包含：
- `types.py`：`StorageRef` / `StorageEntry` / `StorageProviderConfig`
- `base.py`：StorageProvider 协议定义
- `errors.py`：统一存储错误类型
- `provider_registry.py`：Provider 注册中心
- `providers_alist.py`：AList Provider 实现
- `service.py`：统一 `StorageService` 门面 + 默认 Provider 装配

### 2. AList 已模块化
AList 不再被视为系统本体，而是：
- provider_id: `alist-main`
- provider_type: `alist`

业务侧通过 `get_storage_service()` 获取统一服务，再通过：
- `make_ref(path)`
- `normalize_path(path)`
- `list_dir_async()`
- `get_download_url()`
- `read_range()`
等能力访问存储。

### 3. 扫描链路已切到 Storage Service
改造文件：
- `app/services/scan_service.py`
- `app/api/app_data.py`
- `app/services/scheduler.py`

效果：
- 扫描入口先走 provider-aware 的路径规范化
- 扫描内部不再直接依赖 AsyncAListClient
- 目录列举、直链读取、Range 读取都经由 Storage Service
- 扫描结果日志中的 source 改为 provider_id

### 4. 播放 / 封面 / Subsonic 直链读取已脱离 AList 直连调用
改造文件：
- `app/api/app_data.py`
- `app/api/subsonic.py`

效果：
- 歌曲播放直链改为从 Storage Service 获取
- 嵌入歌词读取改为从 Storage Service 读取 Range
- 封面探测依然复用现有逻辑，但下载入口已从 provider 抽象拿链接

### 5. 设置层已具备 Provider 扩展位
改造文件：
- `app/services/app_settings.py`

新增运行时设置字段：
- `storage_default_provider_id`
- `storage_providers`
- `last_scan_provider_id`
- `last_scan_root`
- `autofill_provider_id`
- `autofill_root`

兼容策略：
- 现有老配置仍可继续工作
- 如果没有显式配置 provider 列表，系统自动回退为一个默认的 `alist-main`

### 6. 自动补全 / 定时任务路径已纳入统一路径规范
改造文件：
- `app/services/auto_fill_service.py`
- `app/services/scheduler.py`

效果：
- 上传目录与扫描目录会先做统一 normalize
- 后续扩展 provider 时，这些入口不需要再重写一遍业务逻辑

## 7. 自动补全上传链路已切到 Provider 接口
本次继续完成：
- 文件存在校验：改走 `storage.exists_async()`
- 目录创建：改走 `storage.mkdir_async()`
- 上传成功校验：改走 `storage.stat_async()` + `storage.list_dir_async()`
- 文件写入：改走 `storage.write_bytes_async()`

这意味着自动补全上传已经不再直接把 `/api/fs/get`、`/api/fs/mkdir`、`/api/fs/put` 这些 AList HTTP 端点写死在主业务流程里。
主流程现在依赖的是 **Storage Provider 能力**，不是 AList 私有接口语义。

## 当前默认 Provider 结构
默认由运行时设置自动组装：

```json
{
  "storage": {
    "default_provider_id": "alist-main",
    "providers": [
      {
        "id": "alist-main",
        "type": "alist",
        "name": "AList Main"
      }
    ]
  }
}
```

## 已验证
- `python3 -m compileall app` 通过
- `get_storage_service()` 可成功实例化
- 默认 `alist-main` Provider 可正确构造 `StorageRef`
- 路径 normalize 结果正常

## 后续扩展方式
新增其他网盘时，只需要：
1. 在 `app/services/storage/` 下增加新的 provider 实现
2. 在 `service.py` 的装配阶段注册新 provider
3. 在运行时设置里新增对应 provider 配置
4. 逐步把自动补全上传链路也从 AList HTTP 特有实现继续上提到统一 Provider API

## 第二个 Provider 骨架（已落位）
本轮已新增：
- `app/services/storage/providers_quark.py`

当前状态：
- `provider_type = 'quark'`
- 已支持被 `storage_providers` 配置装配与注册
- 已预留配置字段：`cookie` / `root_dir_id` / `root_path` / `drive_base_url` / `open_base_url`
- 已补最小读取链路：`list_dir` / `stat` / `exists` / `mkdir` / `get_download_url` / `read_range` / `read_text_async`
- 已补上传骨架：`write_bytes_async`（按 quark 分片上传协议接线，待真实 cookie/root 实测）
- 已补朴素搜索：`search`（当前为递归遍历式实现，后续可替换为更高效策略）
- 已新增联调脚本：`backend/scripts/quark_provider_smoke.py`

意义：
- 这已经不是“第二 provider 空壳”了
- 当前抽象已经能承载 **第二个非 AList Provider** 的真实读取/上传接口面，不需要再改主业务层路由结构
- 下一步重点不再是继续堆接口声明，而是拿真实夸克 cookie + root_dir_id 做第一轮联调验收

## 还保留的现实约束
本次目标是“把 AList 模块化”，不是“一次性抹平所有网盘差异”。
因此以下内容暂未完全抽象：
- provider 间 move / rename 等更完整协议还未全部落齐
- source/target 双 provider 任务模型尚未完全进入数据库实体
- 搜索路由名仍叫 `alist_search`，但内部实现已改为调用通用 provider search 能力；仅接口命名尚未统一重构

但核心结构已经搭好了：
**AList 现在已经可以被当作一个独立模块使用，而不是系统唯一入口。**
