from __future__ import annotations

from typing import Any

from app.services.alist_client import AListClient, AsyncAListClient
from app.services.storage.types import StorageEntry, StorageRef


class AlistStorageProvider:
    provider_type = 'alist'

    def __init__(self, provider_id: str, *, base_url: str | None = None, token: str | None = None, username: str | None = None, password: str | None = None) -> None:
        self.provider_id = provider_id
        self._base_url = base_url
        self._token = token
        self._username = username
        self._password = password

    def _client(self) -> AListClient:
        return AListClient(base_url=self._base_url, token=self._token, username=self._username, password=self._password, provider_id=self.provider_id)

    def _async_client(self) -> AsyncAListClient:
        return AsyncAListClient(base_url=self._base_url, token=self._token, username=self._username, password=self._password, provider_id=self.provider_id)

    def normalize_path(self, path: str) -> str:
        raw = str(path or '').strip()
        if not raw:
            return '/'
        normalized = '/' + '/'.join(part for part in raw.split('/') if part)
        return normalized or '/'

    def _entry_from_item(self, ref: StorageRef, item: dict[str, Any], *, path: str | None = None) -> StorageEntry:
        name = str(item.get('name') or '')
        target_path = path or self.normalize_path(f"{ref.path.rstrip('/')}/{name}" if ref.path != '/' else f"/{name}")
        return StorageEntry(
            provider_id=self.provider_id,
            path=target_path,
            name=name,
            is_dir=bool(item.get('is_dir')) or item.get('type') == 1,
            size=int(item.get('size') or 0),
            modified_at=str(item.get('modified') or item.get('updated_at') or item.get('created') or ''),
            raw=dict(item),
        )

    def list_dir(self, ref: StorageRef, *, refresh: bool = False) -> list[dict[str, Any]]:
        _ = refresh
        return self._client().list_path(self.normalize_path(ref.path))

    async def list_dir_async(self, ref: StorageRef, *, refresh: bool = False) -> list[dict[str, Any]]:
        async with self._async_client() as client:
            return await client.list_path(self.normalize_path(ref.path), refresh=refresh)

    def stat(self, ref: StorageRef) -> StorageEntry | None:
        path = self.normalize_path(ref.path)
        if path == '/':
            return StorageEntry(provider_id=self.provider_id, path='/', name='/', is_dir=True, size=0, modified_at='')
        parent = path.rsplit('/', 1)[0] or '/'
        name = path.rsplit('/', 1)[-1]
        for item in self._client().list_path(parent):
            if str(item.get('name') or '') == name:
                return self._entry_from_item(StorageRef(provider_id=ref.provider_id, root=ref.root, path=parent), item, path=path)
        return None

    async def stat_async(self, ref: StorageRef) -> StorageEntry | None:
        path = self.normalize_path(ref.path)
        if path == '/':
            return StorageEntry(provider_id=self.provider_id, path='/', name='/', is_dir=True, size=0, modified_at='')
        async with self._async_client() as client:
            info = await client.get_path_info(path)
        if not info:
            return None
        name = path.rsplit('/', 1)[-1] or '/'
        return StorageEntry(
            provider_id=self.provider_id,
            path=path,
            name=name,
            is_dir=bool(info.get('is_dir')) or info.get('type') == 1,
            size=int(info.get('size') or 0),
            modified_at=str(info.get('modified') or info.get('updated_at') or info.get('created') or ''),
            raw=dict(info),
        )

    def exists(self, ref: StorageRef) -> bool:
        return self.stat(ref) is not None

    async def exists_async(self, ref: StorageRef) -> bool:
        return await self.stat_async(ref) is not None

    def mkdir(self, ref: StorageRef) -> bool:
        path = self.normalize_path(ref.path)
        payload = self._client()._post('/api/fs/mkdir', {'path': path})
        return payload.get('code') == 200

    async def mkdir_async(self, ref: StorageRef) -> bool:
        path = self.normalize_path(ref.path)
        async with self._async_client() as client:
            payload = await client._post('/api/fs/mkdir', {'path': path})
        return payload.get('code') == 200

    def get_download_url(self, ref: StorageRef) -> str:
        return self._client().get_raw_url(self.normalize_path(ref.path))

    async def get_download_url_async(self, ref: StorageRef) -> str:
        async with self._async_client() as client:
            return await client.get_raw_url(self.normalize_path(ref.path))

    def read_range(self, ref: StorageRef, start: int = 0, end: int = 262143) -> bytes:
        client = self._client()
        return client.read_range(client.get_raw_url(self.normalize_path(ref.path)), start=start, end=end)

    async def read_range_async(self, ref: StorageRef, start: int = 0, end: int = 262143) -> bytes:
        async with self._async_client() as client:
            raw_url = await client.get_raw_url(self.normalize_path(ref.path))
            return await client.read_range(raw_url, start=start, end=end)

    async def read_text_async(self, ref: StorageRef) -> str:
        async with self._async_client() as client:
            return await client.read_text_file(self.normalize_path(ref.path))

    async def write_bytes_async(self, ref: StorageRef, content: bytes) -> bool:
        async with self._async_client() as client:
            return await client.write_bytes(self.normalize_path(ref.path), content)

    def search(self, keyword: str, *, roots: list[str] | None = None, limit: int = 20) -> list[dict[str, Any]]:
        items = self._client().find_by_keyword(keyword, roots=roots, limit=limit)
        return [
            {
                **item,
                'provider_id': self.provider_id,
                'provider_type': self.provider_type,
            }
            for item in items
        ]
