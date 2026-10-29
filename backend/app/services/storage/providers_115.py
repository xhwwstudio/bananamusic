from __future__ import annotations

import asyncio
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from cryptography.hazmat.primitives import hashes

try:
    from oss2 import Bucket, SizedFileAdapter, StsAuth, determine_part_size
    from oss2.exceptions import ServerError
    from oss2.models import PartInfo
    from oss2.utils import b64encode_as_string
except Exception:  # pragma: no cover - optional runtime dependency
    Bucket = SizedFileAdapter = StsAuth = determine_part_size = PartInfo = b64encode_as_string = None  # type: ignore[assignment]
    ServerError = Exception  # type: ignore[assignment]

try:
    from p115client import P115Client
    from p115client.tool.attr import get_attr, get_id_to_path, normalize_attr
    try:
        from p115client.tool.fs_files import iter_fs_files
    except ImportError:
        # p115client >= 0.0.9 renamed iter_fs_files -> fs_files_iter (same
        # parameters used here: page_size / cooldown / app / request kwargs).
        from p115client.tool.fs_files import fs_files_iter as iter_fs_files
except Exception:  # pragma: no cover - optional runtime dependency
    P115Client = None  # type: ignore[assignment]
    get_attr = get_id_to_path = normalize_attr = iter_fs_files = None  # type: ignore[assignment]

from app.services.storage.errors import StorageError
from app.services.storage.types import StorageEntry, StorageRef


def _build_client(cookies: str, **kwargs):
    """Create a P115Client across p115client versions.

    ``check_for_relogin`` existed in 0.0.8 but was removed from the public
    constructor in 0.0.9; drop it when the installed version rejects it.
    """
    try:
        return P115Client(cookies=cookies, **kwargs)
    except TypeError:
        kwargs.pop('check_for_relogin', None)
        return P115Client(cookies=cookies, **kwargs)


class OneOneFiveStorageProvider:
    provider_type = '115'
    _user_agent = 'Mozilla/5.0 115Browser/27.0.5.7'
    _referer = 'https://115.com/'
    _cookie_domain = '.115.com'

    _api_login_check = 'https://passportapi.115.com/app/1.0/web/1.0/check/sso'
    _api_status_check = 'https://my.115.com/?ct=guide&ac=status'
    _api_user_info = 'https://my.115.com/?ct=ajax&ac=nav'
    _api_dir_add = 'https://webapi.115.com/files/add'

    def __init__(
        self,
        provider_id: str,
        *,
        cookie: str | None = None,
        qrcode_token: str | None = None,
        qrcode_source: str | None = None,
        root_dir_id: str | None = None,
        root_path: str | None = None,
        page_size: int | None = None,
    ) -> None:
        self.provider_id = provider_id
        self.cookie = str(cookie or '')
        self.qrcode_token = str(qrcode_token or '')
        self.qrcode_source = str(qrcode_source or 'android') or 'android'
        self.root_dir_id = str(root_dir_id or '0') or '0'
        self.root_path = self.normalize_path(root_path or '/')
        self.page_size = max(1, min(int(page_size or 200), 1150))
        self._user_id = 0
        self._last_request_at = 0.0
        self._min_interval_sec = 0.25
        self._failure_count = 0
        self._failure_threshold = 3
        self._cooldown_sec = 45.0
        self._cooldown_until = 0.0
        self._upload_client: Any | None = None
        self._browse_client: Any | None = None
        self._download_client: Any | None = None
        self._path_id_cache: dict[str, str] = {'/': self.root_dir_id}
        self._item_cache: dict[str, dict[str, Any]] = {}

    def _raise(self, message: str, *, code: str = '115_ERROR', cause: Exception | None = None) -> None:
        raise StorageError(message, code=code, provider_id=self.provider_id, cause=cause)

    def normalize_path(self, path: str) -> str:
        raw = str(path or '/').strip() or '/'
        if not raw.startswith('/'):
            raw = '/' + raw
        while '//' in raw:
            raw = raw.replace('//', '/')
        return raw or '/'

    def _join_rooted_path(self, path: str) -> str:
        normalized = self.normalize_path(path)
        if self.root_path == '/':
            return normalized
        if normalized == '/':
            return self.root_path
        return self.normalize_path(f"{self.root_path.rstrip('/')}/{normalized.lstrip('/')}")

    def _cookie_map(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for part in self.cookie.split(';'):
            item = part.strip()
            if not item or '=' not in item:
                continue
            k, v = item.split('=', 1)
            result[k.strip()] = v.strip()
        return result

    def _headers(self, *, with_cookie: bool = True) -> dict[str, str]:
        headers = {
            'Accept': 'application/json, text/plain, */*',
            'User-Agent': self._user_agent,
            'Referer': self._referer,
        }
        if with_cookie and self.cookie.strip():
            headers['Cookie'] = self.cookie
        return headers

    def _check_ready(self) -> None:
        if not self.cookie.strip() and not self.qrcode_token.strip():
            self._raise('115 provider missing cookie or qrcode token', code='CONFIG_INVALID')

    def _ensure_browse_deps(self) -> None:
        if P115Client is None or get_attr is None or get_id_to_path is None or normalize_attr is None or iter_fs_files is None:
            self._raise('115 browse dependencies missing: please install p115client', code='DEPENDENCY_MISSING')

    def _get_browse_client(self) -> Any:
        self._ensure_browse_deps()
        if self._browse_client is None:
            self._check_ready()
            if not self.cookie.strip():
                self._raise('115 browse currently requires cookie login', code='CONFIG_INVALID')
            self._browse_client = P115Client(cookies=self.cookie)
        return self._browse_client

    def _before_request(self) -> None:
        now = time.time()
        if self._cooldown_until > now:
            remain = round(self._cooldown_until - now, 1)
            self._raise(f'115 provider cooling down, retry in {remain}s', code='RATE_LIMITED')
        gap = now - self._last_request_at
        if gap < self._min_interval_sec:
            time.sleep(self._min_interval_sec - gap)
        self._last_request_at = time.time()

    async def _before_request_async(self) -> None:
        now = time.time()
        if self._cooldown_until > now:
            remain = round(self._cooldown_until - now, 1)
            self._raise(f'115 provider cooling down, retry in {remain}s', code='RATE_LIMITED')
        gap = now - self._last_request_at
        if gap < self._min_interval_sec:
            await asyncio.sleep(self._min_interval_sec - gap)
        self._last_request_at = time.time()

    def _record_success(self) -> None:
        self._failure_count = 0
        self._cooldown_until = 0.0

    def _record_failure(self) -> None:
        self._failure_count += 1
        if self._failure_count >= self._failure_threshold:
            self._cooldown_until = time.time() + self._cooldown_sec

    def _request(self, method: str, url: str, *, params: dict[str, Any] | None = None, data: dict[str, Any] | None = None) -> dict[str, Any]:
        self._check_ready()
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                self._before_request()
                with httpx.Client(timeout=30.0, headers=self._headers()) as client:
                    response = client.request(method, url, params=params, data=data)
                    response.raise_for_status()
                    payload = response.json()
                self._record_success()
                return payload if isinstance(payload, dict) else {}
            except Exception as exc:
                last_exc = exc
                status_code = getattr(getattr(exc, 'response', None), 'status_code', None)
                if status_code == 405:
                    self._cooldown_until = time.time() + max(self._cooldown_sec, 300.0)
                    self._raise('115 request blocked by WAF (HTTP 405), enter cooldown', code='RATE_LIMITED', cause=exc)
                self._record_failure()
                if attempt < 2:
                    time.sleep(1.0 * (attempt + 1))
                    continue
                break
        self._raise(f'115 request failed: {url}', code='REMOTE_ERROR', cause=last_exc)

    async def _request_async(self, method: str, url: str, *, params: dict[str, Any] | None = None, data: dict[str, Any] | None = None) -> dict[str, Any]:
        self._check_ready()
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                await self._before_request_async()
                async with httpx.AsyncClient(timeout=30.0, headers=self._headers()) as client:
                    response = await client.request(method, url, params=params, data=data)
                    response.raise_for_status()
                    payload = response.json()
                self._record_success()
                return payload if isinstance(payload, dict) else {}
            except Exception as exc:
                last_exc = exc
                status_code = getattr(getattr(exc, 'response', None), 'status_code', None)
                if status_code == 405:
                    self._cooldown_until = time.time() + max(self._cooldown_sec, 300.0)
                    self._raise('115 request blocked by WAF (HTTP 405), enter cooldown', code='RATE_LIMITED', cause=exc)
                self._record_failure()
                if attempt < 2:
                    await asyncio.sleep(1.0 * (attempt + 1))
                    continue
                break
        self._raise(f'115 request failed: {url}', code='REMOTE_ERROR', cause=last_exc)

    def _is_login_ok(self, data: dict[str, Any]) -> bool:
        code = int(data.get('code') or 0)
        state = data.get('state')
        if code == 0 and state in (0, False):
            return True
        if isinstance(data.get('data'), dict) and int((data.get('data') or {}).get('user_id') or 0) > 0:
            return True
        return False

    def _ensure_login(self) -> None:
        data = self._request('GET', self._api_login_check, params={'_': str(int(time.time() * 1000))})
        if not self._is_login_ok(data):
            data2 = self._request('GET', self._api_status_check, params={'_': str(int(time.time() * 1000))})
            if not bool(data2.get('state')):
                self._raise('115 cookie invalid or expired', code='UNAUTHORIZED')
        try:
            self._user_id = int((data.get('data') or {}).get('user_id') or self._user_id or 0)
        except Exception:
            pass

    async def _ensure_login_async(self) -> None:
        data = await self._request_async('GET', self._api_login_check, params={'_': str(int(time.time() * 1000))})
        if not self._is_login_ok(data):
            data2 = await self._request_async('GET', self._api_status_check, params={'_': str(int(time.time() * 1000))})
            if not bool(data2.get('state')):
                self._raise('115 cookie invalid or expired', code='UNAUTHORIZED')
        try:
            self._user_id = int((data.get('data') or {}).get('user_id') or self._user_id or 0)
        except Exception:
            pass

    def _entry_from_item(self, item: dict[str, Any], *, full_path: str) -> StorageEntry:
        if 'is_dir' in item:
            is_dir = bool(item.get('is_dir'))
        elif item.get('type') == 1:
            is_dir = True
        elif item.get('fid'):
            is_dir = False
        else:
            is_dir = bool(item.get('cid'))
        file_id = str(item.get('id') or item.get('fid') or item.get('cid') or '')
        name = str(item.get('name') or item.get('n') or item.get('file_name') or full_path.rsplit('/', 1)[-1] or '/')
        size = int(item.get('size') or item.get('s') or 0)
        modified_raw = str(item.get('mtime') or item.get('ctime') or item.get('t') or '')
        modified_at = modified_raw
        if modified_raw.isdigit():
            modified_at = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(int(modified_raw)))
        return StorageEntry(
            provider_id=self.provider_id,
            path=self.normalize_path(full_path),
            name=name,
            is_dir=is_dir,
            size=size,
            modified_at=modified_at,
            object_id=file_id or None,
            raw=dict(item),
        )

    def _item_dict(self, item: dict[str, Any], *, parent_path: str) -> dict[str, Any]:
        name = str(item.get('name') or item.get('n') or item.get('file_name') or '')
        path = self.normalize_path(f"{parent_path.rstrip('/')}/{name}" if parent_path != '/' else f"/{name}")
        entry = self._entry_from_item(item, full_path=path)
        return {
            **dict(item),
            'name': entry.name,
            'is_dir': entry.is_dir,
            'path': entry.path,
            'provider_id': self.provider_id,
            'provider_type': self.provider_type,
            'object_id': entry.object_id,
            'modified': entry.modified_at or '',
            'pickcode': item.get('pickcode') or item.get('pc') or '',
        }

    def _browse_request_kwargs(self, *, app: str = 'web') -> dict[str, Any]:
        return {'app': app, 'headers': {'User-Agent': self._user_agent}}

    def _cache_item(self, path: str, item: dict[str, Any]) -> None:
        normalized = self.normalize_path(path)
        payload = dict(item)
        payload['path'] = normalized
        self._item_cache[normalized] = payload
        item_id = str(payload.get('id') or payload.get('cid') or payload.get('fid') or '').strip()
        if item_id:
            self._path_id_cache[normalized] = item_id

    def _cache_dir_listing(self, parent_path: str, items: list[dict[str, Any]]) -> None:
        normalized_parent = self.normalize_path(parent_path)
        for item in items:
            name = str(item.get('name') or item.get('n') or item.get('file_name') or '').strip()
            if not name or name in {'.', '..'}:
                continue
            full_path = self.normalize_path(f"{normalized_parent.rstrip('/')}/{name}" if normalized_parent != '/' else f"/{name}")
            self._cache_item(full_path, item)

    def _resolve_dir_id(self, path: str) -> str:
        normalized = self.normalize_path(path)
        if normalized == '/':
            return self.root_dir_id
        cached = str(self._path_id_cache.get(normalized) or '').strip()
        if cached:
            return cached
        full_path = self._join_rooted_path(normalized)
        client = self._get_browse_client()
        try:
            file_id = get_id_to_path(client=client, path=full_path, **self._browse_request_kwargs(app='web'))
        except KeyError:
            file_id = get_id_to_path(client=client, path=full_path, refresh=True, **self._browse_request_kwargs(app='web'))
        resolved = str(file_id)
        if resolved:
            self._path_id_cache[normalized] = resolved
        return resolved

    async def _resolve_dir_id_async(self, path: str) -> str:
        return await asyncio.to_thread(self._resolve_dir_id, path)

    async def resolve_scan_root_id_async(self, path: str) -> str:
        return await self._resolve_dir_id_async(path)

    def _list_dir_items(self, path: str, *, refresh: bool = False, object_id: str | None = None) -> list[dict[str, Any]]:
        client = self._get_browse_client()
        cid = str(object_id or '').strip() or self._resolve_dir_id(path)
        if cid:
            self._path_id_cache[self.normalize_path(path)] = cid
        items: list[dict[str, Any]] = []
        if refresh:
            self._item_cache.pop(self.normalize_path(path), None)
        for data in iter_fs_files(client, int(cid), page_size=self.page_size, cooldown=1.5, **self._browse_request_kwargs(app='web')):
            for raw_item in data.get('data', []):
                item = normalize_attr(raw_item)
                items.append(dict(item))
        self._cache_dir_listing(path, items)
        return items

    async def _list_dir_items_async(self, path: str, *, refresh: bool = False, object_id: str | None = None) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._list_dir_items, path, refresh=refresh, object_id=object_id)

    def _find_item_by_path(self, path: str) -> dict[str, Any] | None:
        normalized = self.normalize_path(path)
        if normalized == '/':
            return {'id': int(self.root_dir_id or '0'), 'cid': int(self.root_dir_id or '0'), 'name': self.root_path.rsplit('/', 1)[-1] or '/', 'is_dir': True, 'size': 0, 'pickcode': ''}
        cached_item = self._item_cache.get(normalized)
        if cached_item is not None:
            return dict(cached_item)
        full_path = self._join_rooted_path(normalized)
        client = self._get_browse_client()
        cached_id = str(self._path_id_cache.get(normalized) or '').strip()
        if cached_id:
            file_id = cached_id
        else:
            try:
                file_id = get_id_to_path(client=client, path=full_path, **self._browse_request_kwargs(app='web'))
            except KeyError:
                file_id = get_id_to_path(client=client, path=full_path, refresh=True, **self._browse_request_kwargs(app='web'))
            self._path_id_cache[normalized] = str(file_id)
        item = get_attr(client=client, id=file_id, **self._browse_request_kwargs(app='web'))
        result = dict(item) if item else None
        if result:
            self._cache_item(normalized, result)
        return result

    async def _find_item_by_path_async(self, path: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._find_item_by_path, path)

    def _get_download_client(self) -> Any:
        self._ensure_browse_deps()
        if self._download_client is None:
            self._check_ready()
            if not self.cookie.strip():
                self._raise('115 download currently requires cookie login', code='CONFIG_INVALID')
            # 改成更接近 AList/115driver 的模型：
            # 直接基于当前 cookie 导入同一份凭据，不再额外做 login_another_app app bootstrap。
            self._download_client = _build_client(self.cookie, check_for_relogin=True)
        return self._download_client

    def _download_info(self, pickcode: str, *, user_agent: str | None = None) -> dict[str, Any]:
        if not pickcode:
            self._raise('115 pickcode is empty', code='NOT_FOUND')
        client = self._get_download_client()
        result = client.download_url(
            pickcode,
            user_agent=str(user_agent or self._user_agent),
        )
        url = result.geturl() if hasattr(result, 'geturl') else str(result)
        if not url:
            self._raise('115 download URL empty', code='REMOTE_ERROR')
        return {
            'url': result,
            'file_name': '',
            'pick_code': str(pickcode),
            'headers': dict(getattr(result, 'headers', None) or {}),
        }

    async def _download_info_async(self, pickcode: str, *, user_agent: str | None = None) -> dict[str, Any]:
        return await asyncio.to_thread(self._download_info, pickcode, user_agent=user_agent)

    def list_dir(self, ref: StorageRef, *, refresh: bool = False) -> list[dict[str, Any]]:
        self._ensure_login()
        return [self._item_dict(item, parent_path=self.normalize_path(ref.path)) for item in self._list_dir_items(ref.path, refresh=refresh, object_id=ref.object_id)]

    async def list_dir_async(self, ref: StorageRef, *, refresh: bool = False) -> list[dict[str, Any]]:
        await self._ensure_login_async()
        return [self._item_dict(item, parent_path=self.normalize_path(ref.path)) for item in await self._list_dir_items_async(ref.path, refresh=refresh, object_id=ref.object_id)]

    def stat(self, ref: StorageRef) -> StorageEntry | None:
        self._ensure_login()
        normalized = self.normalize_path(ref.path)
        if normalized == '/':
            return StorageEntry(provider_id=self.provider_id, path='/', name=self.root_path.rsplit('/', 1)[-1] or '/', is_dir=True, size=0, modified_at='')
        item = self._find_item_by_path(normalized)
        return self._entry_from_item(item, full_path=normalized) if item else None

    async def stat_async(self, ref: StorageRef) -> StorageEntry | None:
        await self._ensure_login_async()
        normalized = self.normalize_path(ref.path)
        if normalized == '/':
            return StorageEntry(provider_id=self.provider_id, path='/', name=self.root_path.rsplit('/', 1)[-1] or '/', is_dir=True, size=0, modified_at='')
        item = await self._find_item_by_path_async(normalized)
        return self._entry_from_item(item, full_path=normalized) if item else None

    def exists(self, ref: StorageRef) -> bool:
        return self.stat(ref) is not None

    async def exists_async(self, ref: StorageRef) -> bool:
        return await self.stat_async(ref) is not None

    def mkdir(self, ref: StorageRef) -> bool:
        self._ensure_login()
        normalized = self.normalize_path(ref.path)
        parent = normalized.rsplit('/', 1)[0] or '/'
        name = normalized.rsplit('/', 1)[-1]
        parent_id = self._resolve_dir_id(parent)
        data = self._request('POST', self._api_dir_add, data={'pid': parent_id, 'cname': name})
        return bool(data.get('state'))

    async def mkdir_async(self, ref: StorageRef) -> bool:
        await self._ensure_login_async()
        normalized = self.normalize_path(ref.path)
        parent = normalized.rsplit('/', 1)[0] or '/'
        name = normalized.rsplit('/', 1)[-1]
        parent_id = await self._resolve_dir_id_async(parent)
        data = await self._request_async('POST', self._api_dir_add, data={'pid': parent_id, 'cname': name})
        return bool(data.get('state'))

    def get_download_url(self, ref: StorageRef) -> str:
        self._ensure_login()
        item = self._find_item_by_path(ref.path)
        if not item:
            self._raise(f'115 file not found: {ref.path}', code='NOT_FOUND')
        pickcode = str(item.get('pickcode') or item.get('pc') or '')
        client = self._get_download_client()
        resp = client.download_url_web(pickcode, user_agent=self._user_agent)
        url = str(resp.get('file_url') or '')
        if not url:
            self._raise('115 web download URL empty', code='REMOTE_ERROR')
        return url

    def get_stream_redirect_url(self, ref: StorageRef, *, user_agent: str | None = None) -> str:
        self._ensure_login()
        item = self._find_item_by_path(ref.path)
        if not item:
            self._raise(f'115 file not found: {ref.path}', code='NOT_FOUND')
        pickcode = str(item.get('pickcode') or item.get('pc') or '')
        client = self._get_download_client()
        ua = str(user_agent or self._user_agent or '').strip() or self._user_agent
        resp = client.download_url_app(pickcode, user_agent=ua, app='android')
        url = str((resp.get('data') or {}).get('url') or resp.get('url') or '')
        if not url:
            self._raise('115 android stream redirect URL empty', code='REMOTE_ERROR')
        return url

    async def get_download_url_async(self, ref: StorageRef) -> str:
        await self._ensure_login_async()
        item = await self._find_item_by_path_async(ref.path)
        if not item:
            self._raise(f'115 file not found: {ref.path}', code='NOT_FOUND')
        return await asyncio.to_thread(self.get_download_url, ref)

    async def get_stream_redirect_url_async(self, ref: StorageRef, *, user_agent: str | None = None) -> str:
        await self._ensure_login_async()
        item = await self._find_item_by_path_async(ref.path)
        if not item:
            self._raise(f'115 file not found: {ref.path}', code='NOT_FOUND')
        return await asyncio.to_thread(self.get_stream_redirect_url, ref, user_agent=user_agent)

    @staticmethod
    def _extract_cookie_pairs(set_cookie_headers: list[str]) -> str:
        pairs: list[str] = []
        for raw in set_cookie_headers:
            part = str(raw or '').split(';', 1)[0].strip()
            if part and '=' in part:
                pairs.append(part)
        return '; '.join(pairs)

    def _read_range_via_webapi_session(self, pickcode: str, start: int, end: int) -> bytes:
        base_headers = {
            'User-Agent': self._user_agent,
            'Referer': self._referer,
            'Cookie': self.cookie,
        }
        with httpx.Client(timeout=60.0, follow_redirects=False, headers=base_headers) as client:
            download_resp = client.get('https://webapi.115.com/files/download', params={'pickcode': pickcode})
            download_resp.raise_for_status()
            payload = download_resp.json()
            file_url = str(payload.get('file_url') or '')
            if not file_url:
                self._raise('115 web download URL empty', code='REMOTE_ERROR')
            download_cookie = self._extract_cookie_pairs(download_resp.headers.get_list('set-cookie'))
            cookie_parts = [self.cookie.strip(), download_cookie]
            cookie_header = '; '.join(part for part in cookie_parts if part)
            range_headers = {
                'User-Agent': self._user_agent,
                'Referer': self._referer,
                'Range': f'bytes={start}-{end}',
            }
            if cookie_header:
                range_headers['Cookie'] = cookie_header
            try:
                response = client.get(file_url, headers=range_headers)
                response.raise_for_status()
                return response.content
            except Exception as exc:
                self._raise('115 web range request failed', code='REMOTE_ERROR', cause=exc)

    async def _read_range_via_webapi_session_async(self, pickcode: str, start: int, end: int) -> bytes:
        return await asyncio.to_thread(self._read_range_via_webapi_session, pickcode, start, end)

    def read_range(self, ref: StorageRef, start: int = 0, end: int = 262143) -> bytes:
        self._ensure_login()
        item = self._find_item_by_path(ref.path)
        if not item:
            self._raise(f'115 file not found: {ref.path}', code='NOT_FOUND')
        pickcode = str(item.get('pickcode') or item.get('pc') or '')
        return self._read_range_via_webapi_session(pickcode, start, end)

    async def read_range_async(self, ref: StorageRef, start: int = 0, end: int = 262143) -> bytes:
        await self._ensure_login_async()
        item = await self._find_item_by_path_async(ref.path)
        if not item:
            self._raise(f'115 file not found: {ref.path}', code='NOT_FOUND')
        pickcode = str(item.get('pickcode') or item.get('pc') or '')
        return await self._read_range_via_webapi_session_async(pickcode, start, end)

    async def read_text_async(self, ref: StorageRef) -> str:
        data = await self.read_range_async(ref, 0, 1024 * 1024 - 1)
        return data.decode('utf-8', 'ignore')

    @staticmethod
    def _calc_sha1(filepath: Path, size: int | None = None) -> str:
        sha1 = hashes.Hash(hashes.SHA1())
        with filepath.open('rb') as handle:
            if size:
                sha1.update(handle.read(size))
            else:
                while True:
                    chunk = handle.read(1024 * 1024)
                    if not chunk:
                        break
                    sha1.update(chunk)
        return sha1.finalize().hex()

    def _ensure_upload_deps(self) -> None:
        if P115Client is None or Bucket is None or StsAuth is None or determine_part_size is None or PartInfo is None or SizedFileAdapter is None or b64encode_as_string is None:
            self._raise('115 upload dependencies missing: please install p115client and oss2', code='DEPENDENCY_MISSING')

    def _get_upload_client(self) -> Any:
        self._ensure_upload_deps()
        if self._upload_client is None:
            self._check_ready()
            if not self.cookie.strip():
                self._raise('115 upload currently requires cookie login', code='CONFIG_INVALID')
            base_client = _build_client(self.cookie, check_for_relogin=True)
            upload_apps = [
                'android',
                '115android',
                'qandroid',
                'ios',
                '115ios',
                'tv',
            ]
            last_exc: Exception | None = None
            for app in upload_apps:
                try:
                    upload_client = base_client.login_another_app(app, replace=False, check_for_relogin=True)
                    upload_client.upload_key(app='android')
                    self._upload_client = upload_client
                    break
                except Exception as exc:
                    last_exc = exc
                    continue
            if self._upload_client is None:
                self._raise('115 upload app login bootstrap failed', code='UNAUTHORIZED', cause=last_exc)
        return self._upload_client

    def _upload_gettoken(self) -> tuple[str, str, str, str, datetime]:
        client = self._get_upload_client()
        token_resp = client.upload_gettoken()
        endpoint = 'http://oss-cn-shenzhen.aliyuncs.com'
        access_key_id = str(token_resp.get('AccessKeyId') or '')
        access_key_secret = str(token_resp.get('AccessKeySecret') or '')
        security_token = str(token_resp.get('SecurityToken') or '')
        expiration_str = str(token_resp.get('Expiration') or '')
        if not access_key_id or not access_key_secret or not security_token or not expiration_str:
            self._raise('115 upload token invalid', code='REMOTE_ERROR')
        expiration_time = datetime.fromisoformat(expiration_str.replace('Z', '+00:00'))
        return endpoint, access_key_id, access_key_secret, security_token, expiration_time

    @staticmethod
    def _is_token_expiring(expiration_time: datetime, threshold_minutes: int = 5) -> bool:
        now = datetime.now(timezone.utc)
        return (expiration_time - now).total_seconds() < threshold_minutes * 60

    def _read_range_hash(self, filepath: Path, range_str: str) -> str:
        start, end = map(int, range_str.split('-'))
        with filepath.open('rb') as handle:
            handle.seek(start)
            chunk = handle.read(end - start + 1)
        sha1 = hashes.Hash(hashes.SHA1())
        sha1.update(chunk)
        return sha1.finalize().hex().upper()

    def _ensure_parent_dir(self, ref: StorageRef) -> str:
        normalized = self.normalize_path(ref.path)
        parent = normalized.rsplit('/', 1)[0] or '/'
        if parent == '/':
            return self.root_dir_id
        parts = [part for part in parent.split('/') if part]
        current_path = '/'
        current_cid = self.root_dir_id
        for part in parts:
            next_path = self.normalize_path(f"{current_path.rstrip('/')}/{part}" if current_path != '/' else f"/{part}")
            try:
                item = self._find_item_by_path(next_path)
            except FileNotFoundError:
                item = None
            except Exception:
                item = None
            if item and not bool(item.get('fid')):
                current_path = next_path
                current_cid = str(item.get('cid') or item.get('id') or current_cid)
                continue
            data = self._request('POST', self._api_dir_add, data={'pid': current_cid, 'cname': part})
            if not bool(data.get('state')):
                self._raise(f'115 mkdir failed: {next_path}', code='REMOTE_ERROR')
            current_path = next_path
            current_cid = str(data.get('cid') or data.get('file_id') or current_cid)
        return current_cid

    def _upload_via_client(self, ref: StorageRef, local_path: Path) -> bool:
        client = self._get_upload_client()
        normalized = self.normalize_path(ref.path)
        filename = normalized.rsplit('/', 1)[-1]
        target_pid = self._ensure_parent_dir(ref)
        file_size = local_path.stat().st_size
        file_sha1 = self._calc_sha1(local_path)

        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                resp = client.upload_file(
                    str(local_path),
                    pid=target_pid,
                    filename=filename,
                    filesha1=file_sha1,
                    filesize=file_size,
                    partsize=-1,
                )
                if isinstance(resp, dict) and bool(resp.get('state')):
                    return True
                message = 'unknown'
                if isinstance(resp, dict):
                    message = str(resp.get('error') or resp.get('statusmsg') or resp.get('message') or message)
                self._raise(f'115 upload failed: {message}', code='REMOTE_ERROR')
            except Exception as exc:
                last_exc = exc
                if attempt < 2:
                    try:
                        self._upload_client = None
                        self._get_upload_client()
                    except Exception:
                        pass
                    time.sleep(1.5 * (attempt + 1))
                    continue
                break
        self._raise('115 upload failed after retries', code='REMOTE_ERROR', cause=last_exc)
        return False

    async def write_bytes_async(self, ref: StorageRef, content: bytes) -> bool:
        await self._ensure_login_async()
        suffix = Path(ref.path).suffix or '.bin'
        with tempfile.NamedTemporaryFile(prefix='musicon-115-', suffix=suffix, delete=False) as tmp:
            tmp.write(content)
            temp_path = Path(tmp.name)
        try:
            return await asyncio.to_thread(self._upload_via_client, ref, temp_path)
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except Exception:
                pass

    def search(self, keyword: str, *, roots: list[str] | None = None, limit: int = 20) -> list[dict[str, Any]]:
        matched: list[dict[str, Any]] = []
        for root in (roots or ['/']):
            try:
                items = self.list_dir(StorageRef(provider_id=self.provider_id, path=root))
            except Exception:
                continue
            for item in items:
                if keyword.strip().lower() in str(item.get('name') or '').lower():
                    matched.append(item)
                    if len(matched) >= limit:
                        return matched
        return matched
