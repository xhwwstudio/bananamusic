from __future__ import annotations

import asyncio
import hashlib
import time
from typing import Any
from urllib.parse import urlencode

import httpx

from app.services.storage.errors import StorageError
from app.services.storage.types import StorageEntry, StorageRef


class QuarkTVStorageProvider:
    provider_type = 'quark_tv'
    _user_agent = 'Mozilla/5.0 (Linux; Android 13; QuarkTV/1.5.6) AppleWebKit/537.36 Mobile Safari/537.36'
    _api = 'https://open-api-drive.quark.cn'
    _client_id = 'd3194e61504e493eb6222857bccfed94'
    _sign_key = 'kw2dvtd7p4t3pjl2d9ed9yc8yej8kw2d'
    _app_ver = '1.5.6'
    _channel = 'CP'
    _code_api = 'http://api.extscreen.com/quarkdrive'

    def __init__(
        self,
        provider_id: str,
        *,
        refresh_token: str | None = None,
        device_id: str | None = None,
        root_dir_id: str | None = None,
        root_path: str | None = None,
    ) -> None:
        self.provider_id = provider_id
        self.refresh_token = str(refresh_token or '').strip()
        seed = f'{provider_id}:{self.refresh_token or "quark-tv"}'
        self.device_id = str(device_id or hashlib.md5(seed.encode('utf-8')).hexdigest())
        self.root_dir_id = str(root_dir_id or '0')
        self.root_path = self.normalize_path(root_path or '/')
        self.access_token = ''
        self._last_request_at = 0.0
        self._min_interval_sec = 0.2
        self._path_cache: dict[str, tuple[str, dict[str, Any] | None, str]] = {}
        self._dir_list_cache: dict[str, list[dict[str, Any]]] = {}

    def _raise(self, message: str, *, code: str = 'QUARK_TV_ERROR', cause: Exception | None = None):
        raise StorageError(message, code=code, provider_id=self.provider_id, cause=cause)

    def _parse_json(self, response: httpx.Response, action: str) -> dict[str, Any]:
        """Parse a Quark response even when the HTTP status is 4xx.

        Quark reports normal login states (``errno=11003`` "user has not
        confirmed yet") with HTTP 400, so ``raise_for_status()`` must not run
        before the JSON body is inspected.
        """
        try:
            data = response.json()
        except ValueError:
            self._raise(
                f'QuarkTV {action} failed: HTTP {response.status_code} {response.text[:200]}'.strip(),
                code='REMOTE_ERROR',
            )
        if not isinstance(data, dict):
            self._raise(f'QuarkTV {action} failed: unexpected response body', code='REMOTE_ERROR')
        return data

    def _check_ready(self) -> None:
        if not self.refresh_token:
            self._raise('QuarkTV provider missing refresh_token', code='CONFIG_INVALID')

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
        return self.normalize_path(f'{self.root_path.rstrip("/")}/{normalized.lstrip("/")}')

    def _req_sign(self, method: str, pathname: str) -> tuple[str, str, str]:
        timestamp = str(int(time.time() * 1000))
        req_id = hashlib.md5(f'{self.device_id}{timestamp}'.encode('utf-8')).hexdigest()
        token = hashlib.sha256(f'{method}&{pathname}&{timestamp}&{self._sign_key}'.encode('utf-8')).hexdigest()
        return timestamp, token, req_id

    async def _exchange_token_async(self, *, code: str | None = None, refresh_token: str | None = None) -> dict[str, Any]:
        pathname = '/token'
        _tm, _token, req_id = self._req_sign('POST', pathname)
        body = {
            'req_id': req_id,
            'app_ver': self._app_ver,
            'device_id': self.device_id,
            'device_brand': 'Xiaomi',
            'platform': 'tv',
            'device_name': 'M2004J7AC',
            'device_model': 'M2004J7AC',
            'build_device': 'M2004J7AC',
            'build_product': 'M2004J7AC',
            'device_gpu': 'Adreno (TM) 550',
            'activity_rect': '{}',
            'channel': self._channel,
        }
        if refresh_token:
            body['refresh_token'] = refresh_token
        elif code:
            body['code'] = code
        else:
            self._raise('QuarkTV token exchange missing code/refresh_token', code='CONFIG_INVALID')
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(f'{self._code_api}{pathname}', json=body)
        data = self._parse_json(response, 'token exchange')
        if int(data.get('code') or 0) != 200:
            self._raise(str(data.get('message') or data.get('error_info') or 'QuarkTV token exchange failed'), code='AUTH_FAILED')
        token_data = dict(data.get('data') or {})
        return token_data

    async def _refresh_access_token(self) -> None:
        self._check_ready()
        token_data = await self._exchange_token_async(refresh_token=self.refresh_token)
        self.access_token = str(token_data.get('access_token') or '')
        if not self.access_token:
            self._raise('QuarkTV access_token missing', code='AUTH_FAILED')
        new_refresh = str(token_data.get('refresh_token') or '')
        if new_refresh:
            self.refresh_token = new_refresh
        self._path_cache.clear()
        self._dir_list_cache.clear()

    async def _request_async(self, pathname: str, *, method: str = 'GET', params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._check_ready()
        if not self.access_token:
            await self._refresh_access_token()
        now = time.time()
        gap = now - self._last_request_at
        if gap < self._min_interval_sec:
            await asyncio.sleep(self._min_interval_sec - gap)
        self._last_request_at = time.time()

        async def do_request() -> tuple[int, dict[str, Any], str]:
            tm, token, req_id = self._req_sign(method, pathname)
            base_params = {
                'req_id': req_id,
                'access_token': self.access_token,
                'app_ver': self._app_ver,
                'device_id': self.device_id,
                'device_brand': 'Xiaomi',
                'platform': 'tv',
                'device_name': 'M2004J7AC',
                'device_model': 'M2004J7AC',
                'build_device': 'M2004J7AC',
                'build_product': 'M2004J7AC',
                'device_gpu': 'Adreno (TM) 550',
                'activity_rect': '{}',
                'channel': self._channel,
            }
            if params:
                base_params.update({k: str(v) for k, v in params.items() if v is not None})
            headers = {
                'Accept': 'application/json, text/plain, */*',
                'User-Agent': self._user_agent,
                'x-pan-tm': tm,
                'x-pan-token': token,
                'x-pan-client-id': self._client_id,
            }
            async with httpx.AsyncClient(timeout=30.0, headers=headers) as client:
                response = await client.get(f'{self._api}{pathname}', params=base_params)
                text = response.text
                try:
                    data = response.json()
                except Exception:
                    data = {}
                return response.status_code, data, text

        status_code, data, raw_text = await do_request()
        token_expired = int(data.get('status') or 0) == -1 and int(data.get('errno') or 0) == 10001
        auth_like_http_error = status_code in {400, 401, 403}
        if token_expired or auth_like_http_error:
            await self._refresh_access_token()
            status_code, data, raw_text = await do_request()

        if status_code >= 400:
            message = str(data.get('message') or data.get('error_info') or raw_text or f'HTTP {status_code}')
            self._raise(message, code='REMOTE_ERROR')
        if int(data.get('status') or 200) >= 400 or int(data.get('errno') or 0) != 0:
            self._raise(str(data.get('error_info') or data.get('message') or 'QuarkTV request failed'), code='REMOTE_ERROR')
        return data

    async def start_login_async(self) -> dict[str, str]:
        pathname = '/oauth/authorize'
        tm, token, req_id = self._req_sign('GET', pathname)
        headers = {
            'Accept': 'application/json, text/plain, */*',
            'User-Agent': self._user_agent,
            'x-pan-tm': tm,
            'x-pan-token': token,
            'x-pan-client-id': self._client_id,
        }
        params = {
            'req_id': req_id,
            'app_ver': self._app_ver,
            'device_id': self.device_id,
            'device_brand': 'Xiaomi',
            'platform': 'tv',
            'device_name': 'M2004J7AC',
            'device_model': 'M2004J7AC',
            'build_device': 'M2004J7AC',
            'build_product': 'M2004J7AC',
            'device_gpu': 'Adreno (TM) 550',
            'activity_rect': '{}',
            'channel': self._channel,
            'auth_type': 'code',
            'client_id': self._client_id,
            'scope': 'netdisk',
            'qrcode': '1',
            'qr_width': '460',
            'qr_height': '460',
        }
        async with httpx.AsyncClient(timeout=20.0, headers=headers) as client:
            response = await client.get(f'{self._api}{pathname}', params=params)
        data = self._parse_json(response, 'get login code')
        status_value = int(data.get('status') or 200)
        if response.status_code >= 400 or status_value >= 400 or status_value < 0:
            self._raise(
                str(data.get('error_info') or data.get('message') or f'QuarkTV get login code failed: HTTP {response.status_code}'),
                code='REMOTE_ERROR',
            )
        return {
            'qr_data': str(data.get('qr_data') or ''),
            'query_token': str(data.get('query_token') or ''),
            'device_id': self.device_id,
        }

    async def poll_login_async(self, query_token: str) -> dict[str, Any]:
        query_token = str(query_token or '').strip()
        if not query_token:
            self._raise('QuarkTV query_token missing', code='CONFIG_INVALID')
        pathname = '/oauth/code'
        tm, token, req_id = self._req_sign('GET', pathname)
        headers = {
            'Accept': 'application/json, text/plain, */*',
            'User-Agent': self._user_agent,
            'x-pan-tm': tm,
            'x-pan-token': token,
            'x-pan-client-id': self._client_id,
        }
        params = {
            'req_id': req_id,
            'app_ver': self._app_ver,
            'device_id': self.device_id,
            'device_brand': 'Xiaomi',
            'platform': 'tv',
            'device_name': 'M2004J7AC',
            'device_model': 'M2004J7AC',
            'build_device': 'M2004J7AC',
            'build_product': 'M2004J7AC',
            'device_gpu': 'Adreno (TM) 550',
            'activity_rect': '{}',
            'channel': self._channel,
            'client_id': self._client_id,
            'scope': 'netdisk',
            'query_token': query_token,
        }
        async with httpx.AsyncClient(timeout=20.0, headers=headers) as client:
            response = await client.get(f'{self._api}{pathname}', params=params)
        data = self._parse_json(response, 'poll login')
        code = str(data.get('code') or '')
        if code:
            token_data = await self._exchange_token_async(code=code)
            refresh_token = str(token_data.get('refresh_token') or '')
            access_token = str(token_data.get('access_token') or '')
            if not refresh_token:
                self._raise('QuarkTV refresh_token missing after login', code='AUTH_FAILED')
            self.refresh_token = refresh_token
            self.access_token = access_token
            return {
                'ok': True,
                'authorized': True,
                'refresh_token': refresh_token,
                'access_token': access_token,
                'device_id': self.device_id,
            }

        errno = int(data.get('errno') or 0)
        error_info = str(data.get('error_info') or data.get('message') or '').strip()
        # 11003: the QR code has not been confirmed in the Quark app yet.
        if errno == 11003 or '未确认' in error_info:
            return {
                'ok': True,
                'authorized': False,
                'pending': True,
                'message': error_info or '尚未在夸克 App 中确认授权',
                'device_id': self.device_id,
            }
        # 11002: the QR code expired; the user must request a new one.
        if errno == 11002 or '过期' in error_info:
            self._raise('登录二维码已过期，请重新获取登录码', code='LOGIN_EXPIRED')
        status_value = int(data.get('status') or 0)
        if response.status_code >= 400 or status_value < 0:
            self._raise(error_info or f'QuarkTV poll failed: HTTP {response.status_code}', code='REMOTE_ERROR')
        return {
            'ok': True,
            'authorized': False,
            'pending': True,
            'device_id': self.device_id,
        }

    def _entry_from_item(self, item: dict[str, Any], *, full_path: str) -> StorageEntry:
        name = str(item.get('filename') or full_path.rsplit('/', 1)[-1] or '/')
        return StorageEntry(
            provider_id=self.provider_id,
            path=full_path,
            name=name,
            is_dir=int(item.get('isdir') or 0) == 1,
            size=int(item.get('size') or 0),
            modified_at=str(item.get('updated_at') or item.get('created_at') or ''),
            object_id=str(item.get('fid') or ''),
            raw=dict(item),
        )

    async def _list_dir_by_id_async(self, dir_id: str, *, refresh: bool = False) -> list[dict[str, Any]]:
        cache_key = str(dir_id or '')
        if not refresh and cache_key in self._dir_list_cache:
            return list(self._dir_list_cache[cache_key])
        rows: list[dict[str, Any]] = []
        page_index = 0
        page_size = 100
        while True:
            data = await self._request_async('/file', params={
                'method': 'list',
                'parent_fid': dir_id,
                'order_by': '3',
                'desc': '1',
                'category': '',
                'source': '',
                'ex_source': '',
                'list_all': '0',
                'page_size': page_size,
                'page_index': page_index,
            })
            payload = dict(data.get('data') or {})
            chunk = list(payload.get('files') or [])
            rows.extend(chunk)
            total = int(payload.get('total_count') or len(chunk))
            if page_index * page_size >= total or not chunk:
                break
            page_index += 1
        self._dir_list_cache[cache_key] = list(rows)
        return rows

    async def _resolve_path_async(self, path: str) -> tuple[str, dict[str, Any] | None, str]:
        full_path = self._join_rooted_path(path)
        cached = self._path_cache.get(full_path)
        if cached:
            return cached
        if full_path == self.root_path:
            root_name = self.root_path.rsplit('/', 1)[-1] or '/'
            result = (self.root_dir_id, {
                'fid': self.root_dir_id,
                'filename': root_name,
                'isdir': 1,
                'size': 0,
                'updated_at': 0,
            }, full_path)
            self._path_cache[full_path] = result
            return result
        rel = full_path[len(self.root_path):].strip('/') if full_path.startswith(self.root_path) else full_path.strip('/')
        parts = [p for p in rel.split('/') if p]
        current_id = self.root_dir_id
        current_item: dict[str, Any] | None = None
        current_path = self.root_path
        for part in parts:
            next_path = self.normalize_path(f'{current_path.rstrip("/")}/{part}' if current_path != '/' else f'/{part}')
            cached_child = self._path_cache.get(next_path)
            if cached_child:
                current_id, current_item, current_path = cached_child
                continue
            found = None
            for item in await self._list_dir_by_id_async(current_id):
                if str(item.get('filename') or '') == part:
                    found = item
                    break
            if not found:
                result = ('', None, full_path)
                self._path_cache[full_path] = result
                return result
            current_item = found
            current_id = str(found.get('fid') or '')
            current_path = next_path
            self._path_cache[current_path] = (current_id, current_item, current_path)
        result = (current_id, current_item, full_path)
        self._path_cache[full_path] = result
        return result

    def list_dir(self, ref: StorageRef, *, refresh: bool = False) -> list[dict[str, Any]]:
        return asyncio.run(self.list_dir_async(ref, refresh=refresh))

    async def list_dir_async(self, ref: StorageRef, *, refresh: bool = False) -> list[dict[str, Any]]:
        if refresh:
            target_path = self._join_rooted_path(ref.path)
            self._path_cache.pop(target_path, None)
        dir_id, item, full_path = await self._resolve_path_async(ref.path)
        if not dir_id or (item is not None and int(item.get('isdir') or 0) != 1):
            return []
        rows = await self._list_dir_by_id_async(dir_id, refresh=refresh)
        result = []
        for row in rows:
            name = str(row.get('filename') or '')
            path = self.normalize_path(f'{full_path.rstrip("/")}/{name}' if full_path != '/' else f'/{name}')
            entry = self._entry_from_item(row, full_path=path)
            result.append({
                'name': entry.name,
                'path': entry.path,
                'is_dir': entry.is_dir,
                'size': entry.size,
                'modified': entry.modified_at,
                'provider_id': self.provider_id,
                'provider_type': self.provider_type,
                'object_id': entry.object_id,
                'raw': entry.raw,
            })
        return result

    def stat(self, ref: StorageRef) -> StorageEntry | None:
        return asyncio.run(self.stat_async(ref))

    async def stat_async(self, ref: StorageRef) -> StorageEntry | None:
        _id, item, full_path = await self._resolve_path_async(ref.path)
        if not item:
            return None
        return self._entry_from_item(item, full_path=full_path)

    def exists(self, ref: StorageRef) -> bool:
        return asyncio.run(self.exists_async(ref))

    async def exists_async(self, ref: StorageRef) -> bool:
        return await self.stat_async(ref) is not None

    def mkdir(self, ref: StorageRef) -> bool:
        self._raise('QuarkTV mkdir not supported', code='NOT_IMPLEMENTED')

    async def mkdir_async(self, ref: StorageRef) -> bool:
        self._raise('QuarkTV mkdir not supported', code='NOT_IMPLEMENTED')

    def get_download_url(self, ref: StorageRef) -> str:
        return asyncio.run(self.get_download_url_async(ref))

    async def get_download_url_async(self, ref: StorageRef) -> str:
        item = await self.stat_async(ref)
        if not item:
            self._raise(f'QuarkTV file not found: {ref.path}', code='NOT_FOUND')
        if item.is_dir:
            self._raise(f'QuarkTV path is a directory: {ref.path}', code='IS_DIR')
        data = await self._request_async('/file', params={
            'method': 'download',
            'group_by': 'source',
            'fid': item.object_id,
            'resolution': 'low,normal,high,super,2k,4k',
            'support': 'dolby_vision',
        })
        payload = dict(data.get('data') or {})
        url = str(payload.get('download_url') or '')
        if not url:
            self._raise(f'QuarkTV download url missing: {ref.path}', code='DOWNLOAD_URL_MISSING')
        return url

    def read_range(self, ref: StorageRef, start: int = 0, end: int = 262143) -> bytes:
        return asyncio.run(self.read_range_async(ref, start=start, end=end))

    async def read_range_async(self, ref: StorageRef, start: int = 0, end: int = 262143) -> bytes:
        url = await self.get_download_url_async(ref)
        headers = {'Range': f'bytes={start}-{end}', 'User-Agent': self._user_agent}
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            return response.content

    async def read_text_async(self, ref: StorageRef) -> str:
        url = await self.get_download_url_async(ref)
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            response = await client.get(url, headers={'User-Agent': self._user_agent})
            response.raise_for_status()
            return response.text

    async def write_bytes_async(self, ref: StorageRef, content: bytes) -> bool:
        self._raise('QuarkTV upload not supported', code='NOT_IMPLEMENTED')

    def search(self, keyword: str, *, provider_id: str | None = None, roots: list[str] | None = None, limit: int = 20) -> list[dict[str, Any]]:
        return []
