from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from app.services.app_settings import get_runtime_settings
from app.services.storage.provider_registry import StorageProviderRegistry
from app.services.storage.errors import StorageError
from app.services.storage.types import StorageEntry, StorageProviderConfig, StorageRef


class StorageService:
    def __init__(self, registry: StorageProviderRegistry, *, default_provider_id: str) -> None:
        self.registry = registry
        self.default_provider_id = default_provider_id

    def make_ref(self, path: str, *, provider_id: str | None = None, root: str = 'default', object_id: str | None = None) -> StorageRef:
        return StorageRef(provider_id=provider_id or self.default_provider_id, root=root, path=path, object_id=object_id)

    def get_provider(self, provider_id: str | None = None):
        return self.registry.get(provider_id or self.default_provider_id)

    def normalize_path(self, path: str, *, provider_id: str | None = None) -> str:
        return self.get_provider(provider_id).normalize_path(path)

    def list_dir(self, ref: StorageRef, *, refresh: bool = False) -> list[dict[str, Any]]:
        return self.get_provider(ref.provider_id).list_dir(ref, refresh=refresh)

    async def list_dir_async(self, ref: StorageRef, *, refresh: bool = False) -> list[dict[str, Any]]:
        return await self.get_provider(ref.provider_id).list_dir_async(ref, refresh=refresh)

    def stat(self, ref: StorageRef) -> StorageEntry | None:
        return self.get_provider(ref.provider_id).stat(ref)

    async def stat_async(self, ref: StorageRef) -> StorageEntry | None:
        return await self.get_provider(ref.provider_id).stat_async(ref)

    def exists(self, ref: StorageRef) -> bool:
        return self.get_provider(ref.provider_id).exists(ref)

    async def exists_async(self, ref: StorageRef) -> bool:
        return await self.get_provider(ref.provider_id).exists_async(ref)

    def mkdir(self, ref: StorageRef) -> bool:
        return self.get_provider(ref.provider_id).mkdir(ref)

    async def mkdir_async(self, ref: StorageRef) -> bool:
        return await self.get_provider(ref.provider_id).mkdir_async(ref)

    def get_download_url(self, ref: StorageRef) -> str:
        return self.get_provider(ref.provider_id).get_download_url(ref)

    async def get_download_url_async(self, ref: StorageRef) -> str:
        return await self.get_provider(ref.provider_id).get_download_url_async(ref)

    def read_range(self, ref: StorageRef, start: int = 0, end: int = 262143) -> bytes:
        return self.get_provider(ref.provider_id).read_range(ref, start=start, end=end)

    async def read_range_async(self, ref: StorageRef, start: int = 0, end: int = 262143) -> bytes:
        return await self.get_provider(ref.provider_id).read_range_async(ref, start=start, end=end)

    async def read_text_async(self, ref: StorageRef) -> str:
        return await self.get_provider(ref.provider_id).read_text_async(ref)

    async def write_bytes_async(self, ref: StorageRef, content: bytes) -> bool:
        return await self.get_provider(ref.provider_id).write_bytes_async(ref, content)

    def search(self, keyword: str, *, provider_id: str | None = None, roots: list[str] | None = None, limit: int = 20) -> list[dict[str, Any]]:
        return self.get_provider(provider_id).search(keyword, roots=roots, limit=limit)

    def resolve_scan_ref(self) -> StorageRef:
        runtime = get_runtime_settings()
        provider_id = str(runtime.get('last_scan_provider_id') or runtime.get('storage_default_provider_id') or self.default_provider_id).strip() or self.default_provider_id
        path = str(runtime.get('last_scan_path') or '/').strip() or '/'
        root = str(runtime.get('last_scan_root') or 'default').strip() or 'default'
        return self.make_ref(path, provider_id=provider_id, root=root)

    def resolve_autofill_ref(self) -> StorageRef:
        runtime = get_runtime_settings()
        provider_id = str(runtime.get('autofill_provider_id') or runtime.get('storage_default_provider_id') or self.default_provider_id).strip() or self.default_provider_id
        path = str(runtime.get('autofill_path') or '/MusicOn_AutoDownload/').strip() or '/MusicOn_AutoDownload/'
        root = str(runtime.get('autofill_root') or 'default').strip() or 'default'
        return self.make_ref(path, provider_id=provider_id, root=root)


def parse_storage_provider_rows(raw: Any) -> list[StorageProviderConfig]:
    provider_rows: list[StorageProviderConfig] = []
    if isinstance(raw, str) and raw.strip():
        try:
            decoded = json.loads(raw)
            if isinstance(decoded, list):
                for item in decoded:
                    if isinstance(item, dict):
                        provider_rows.append(StorageProviderConfig(
                            id=str(item.get('id') or '').strip(),
                            type=str(item.get('type') or '').strip(),
                            name=str(item.get('name') or item.get('id') or '').strip(),
                            config=dict(item.get('config') or {}),
                        ))
        except Exception:
            provider_rows = []
    return [row for row in provider_rows if row.id and row.type]


def build_storage_service(*, provider_rows: list[StorageProviderConfig], default_provider_id: str) -> StorageService:
    if not provider_rows:
        raise StorageError('No storage providers configured', code='PROVIDER_NOT_FOUND')
    registry = StorageProviderRegistry()
    for row in provider_rows:
        if row.type == 'alist':
            from app.services.storage.providers_alist import AlistStorageProvider
            registry.register(AlistStorageProvider(
                row.id,
                base_url=str(row.config.get('base_url') or '') or None,
                token=str(row.config.get('token') or '') or None,
                username=str(row.config.get('username') or row.config.get('refresh_username') or '') or None,
                password=str(row.config.get('password') or row.config.get('refresh_password') or '') or None,
            ))
        elif row.type == 'quark':
            from app.services.storage.providers_quark import QuarkStorageProvider
            registry.register(QuarkStorageProvider(
                row.id,
                cookie=str(row.config.get('cookie') or '') or None,
                root_dir_id=str(row.config.get('root_dir_id') or '0') or '0',
                root_path=str(row.config.get('root_path') or '/') or '/',
                drive_base_url=str(row.config.get('drive_base_url') or 'https://drive.quark.cn') or 'https://drive.quark.cn',
                open_base_url=str(row.config.get('open_base_url') or '') or None,
                provider_type=str(row.type or 'quark'),
            ))
        elif row.type == '115':
            from app.services.storage.providers_115 import OneOneFiveStorageProvider
            registry.register(OneOneFiveStorageProvider(
                row.id,
                cookie=str(row.config.get('cookie') or '') or None,
                qrcode_token=str(row.config.get('qrcode_token') or '') or None,
                qrcode_source=str(row.config.get('qrcode_source') or 'android') or 'android',
                root_dir_id=str(row.config.get('root_dir_id') or '0') or '0',
                root_path=str(row.config.get('root_path') or '/') or '/',
                page_size=int(row.config.get('page_size') or 200),
            ))
        elif row.type == 'quark_tv':
            from app.services.storage.providers_quark_tv import QuarkTVStorageProvider
            registry.register(QuarkTVStorageProvider(
                row.id,
                refresh_token=str(row.config.get('refresh_token') or '') or None,
                device_id=str(row.config.get('device_id') or '') or None,
                root_dir_id=str(row.config.get('root_dir_id') or '0') or '0',
                root_path=str(row.config.get('root_path') or '/') or '/',
            ))
    return StorageService(registry, default_provider_id=default_provider_id)


@lru_cache(maxsize=1)
def get_storage_service() -> StorageService:
    runtime = get_runtime_settings()
    provider_rows = parse_storage_provider_rows(runtime.get('storage_providers'))

    default_provider_id = str(runtime.get('storage_default_provider_id') or (provider_rows[0].id if provider_rows else '')).strip()
    if not default_provider_id and provider_rows:
        default_provider_id = provider_rows[0].id
    return build_storage_service(provider_rows=provider_rows, default_provider_id=default_provider_id)
