from __future__ import annotations

from collections import deque
from typing import Any
from urllib.parse import quote
import html
import logging

import httpx

from app.core.config import settings
from app.services.app_settings import get_runtime_settings, update_settings

logger = logging.getLogger(__name__)


def _provider_alist_config(provider_id: str | None) -> dict[str, Any]:
    pid = str(provider_id or '').strip()
    if not pid:
        return {}
    saved = get_runtime_settings()
    raw = saved.get('storage_providers')
    try:
        loaded = __import__('json').loads(raw) if isinstance(raw, str) and raw.strip() else []
    except Exception:
        loaded = []
    if not isinstance(loaded, list):
        return {}
    for item in loaded:
        if not isinstance(item, dict):
            continue
        if str(item.get('id') or '').strip() == pid and str(item.get('type') or '').strip() == 'alist':
            return dict(item.get('config') or {})
    return {}


def _normalize_alist_item(item: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(item)
    name = normalized.get('name')
    if isinstance(name, str) and name:
        normalized['_raw_name'] = name
        normalized['name'] = html.unescape(name)
    return normalized


def _looks_like_auth_failure(data: dict[str, Any] | None, status_code: int | None = None) -> bool:
    payload = data or {}
    code = payload.get('code')
    message = str(payload.get('message') or '').lower()
    if status_code in (401, 403):
        return True
    if code in (401, 403):
        return True
    auth_hints = (
        'token is expired', 'token expired', 'token is invalidated',
        'unauthorized', 'forbidden', 'invalid token', 'login expired'
    )
    return any(hint in message for hint in auth_hints)


class AListClient:
    def __init__(self, base_url: str | None = None, token: str | None = None, *, username: str | None = None, password: str | None = None, provider_id: str | None = None) -> None:
        provider_cfg = _provider_alist_config(provider_id)
        saved = get_runtime_settings()
        fallback_base_url = settings.alist_url if not provider_id else ''
        fallback_token = settings.alist_token if not provider_id else ''
        saved_base_url = saved.get('alist_url') if not provider_id else ''
        saved_token = saved.get('alist_token') if not provider_id else ''
        saved_username = saved.get('alist_username') if not provider_id else ''
        saved_password = saved.get('alist_password') if not provider_id else ''
        self.base_url = (base_url or provider_cfg.get('base_url') or saved_base_url or fallback_base_url).rstrip('/')
        self.token = token if token is not None else (provider_cfg.get('token') or saved_token or fallback_token)
        self.username = str(username if username is not None else (provider_cfg.get('username') or provider_cfg.get('refresh_username') or saved_username or '')).strip()
        self.password = str(password if password is not None else (provider_cfg.get('password') or provider_cfg.get('refresh_password') or saved_password or ''))
        self.provider_id = str(provider_id or '').strip()

    def _headers(self) -> dict[str, str]:
        headers = {
            'Content-Type': 'application/json',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
        }
        token = str(self.token or '').strip()
        if token and token not in {'0', '1', 'null', 'None', 'undefined'}:
            headers['Authorization'] = token
        return headers

    def _refresh_token(self) -> bool:
        if not self.username or not self.password:
            return False
        with httpx.Client(timeout=20.0) as client:
            response = client.post(
                f'{self.base_url}/api/auth/login',
                headers={
                    'Content-Type': 'application/json',
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
                },
                json={'username': self.username, 'password': self.password},
            )
            response.raise_for_status()
            data = response.json()
        if data.get('code') != 200:
            return False
        token = str((data.get('data') or {}).get('token') or '')
        if not token:
            return False
        self.token = token
        payload = {'alist_token': token}
        if self.provider_id:
            import json
            current_raw = get_runtime_settings().get('storage_providers')
            changed = False
            updated = []
            try:
                loaded = json.loads(current_raw) if isinstance(current_raw, str) and current_raw.strip() else []
            except Exception:
                loaded = []
            if isinstance(loaded, list):
                for item in loaded:
                    if not isinstance(item, dict):
                        continue
                    cloned = dict(item)
                    cfg = dict(cloned.get('config') or {})
                    if str(cloned.get('id') or '').strip() == self.provider_id and str(cloned.get('type') or '').strip() == 'alist':
                        cfg['token'] = token
                        cloned['config'] = cfg
                        changed = True
                    updated.append(cloned)
            if changed:
                payload['storage_providers'] = updated
        update_settings(payload, include_secrets=True, public_view=False)
        return True

    def _post(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        with httpx.Client(timeout=20.0) as client:
            response = client.post(
                f'{self.base_url}{endpoint}',
                headers=self._headers(),
                json=payload,
            )
            data = response.json()
            if response.status_code >= 400:
                if _looks_like_auth_failure(data, response.status_code) and self._refresh_token():
                    response = client.post(
                        f'{self.base_url}{endpoint}',
                        headers=self._headers(),
                        json=payload,
                    )
                    data = response.json()
                response.raise_for_status()
        if data.get('code') != 200:
            if _looks_like_auth_failure(data) and self._refresh_token():
                with httpx.Client(timeout=20.0) as retry_client:
                    retry_response = retry_client.post(
                        f'{self.base_url}{endpoint}',
                        headers=self._headers(),
                        json=payload,
                    )
                    retry_response.raise_for_status()
                    data = retry_response.json()
            if data.get('code') != 200:
                raise RuntimeError(data.get('message') or f'AList call failed: {endpoint}')
        return data

    def list_path(self, path: str, password: str = '') -> list[dict[str, Any]]:
        payload = self._post('/api/fs/list', {'path': path, 'password': password, 'page': 1, 'per_page': 1000, 'refresh': False})
        data = payload.get('data') or {}
        content = data.get('content') or []
        if not isinstance(content, list):
            raise RuntimeError('AList content shape invalid')
        return [_normalize_alist_item(item) for item in content]

    def get_raw_url(self, path: str) -> str:
        payload = self._post('/api/fs/get', {'path': path, 'password': ''})
        data = payload.get('data') or {}
        raw_url = data.get('raw_url') or data.get('url')
        if not raw_url:
            encoded = quote(path)
            raw_url = f'{self.base_url}/d{encoded}'
        return raw_url

    def read_range(self, raw_url: str, start: int = 0, end: int = 262143) -> bytes:
        headers = {
            'Range': f'bytes={start}-{end}',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
        }
        if self.token:
            headers['Authorization'] = self.token
        with httpx.Client(timeout=20.0, follow_redirects=True) as client:
            response = client.get(raw_url, headers=headers)
            response.raise_for_status()
            return response.content

    def find_by_keyword(self, keyword: str, roots: list[str] | None = None, limit: int = 20, max_dirs: int = 120) -> list[dict[str, Any]]:
        roots = roots or ['/quark/music', '/quark']
        queue = deque(roots)
        seen = set()
        keyword = keyword.strip()
        found: list[dict[str, Any]] = []
        while queue and len(seen) < max_dirs and len(found) < limit:
            path = queue.popleft()
            if path in seen:
                continue
            seen.add(path)
            try:
                items = self.list_path(path)
            except Exception:
                continue
            for item in items:
                name = str(item.get('name') or '')
                child = path.rstrip('/') + '/' + name if path != '/' else '/' + name
                is_dir = bool(item.get('is_dir')) or item.get('type') == 1
                if keyword and keyword in name:
                    found.append({'path': child, 'name': name, 'is_dir': is_dir})
                    if len(found) >= limit:
                        break
                if is_dir:
                    queue.append(child)
        return found


class AsyncAListClient:
    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        *,
        username: str | None = None,
        password: str | None = None,
        provider_id: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        provider_cfg = _provider_alist_config(provider_id)
        saved = get_runtime_settings()
        fallback_base_url = settings.alist_url if not provider_id else ''
        fallback_token = settings.alist_token if not provider_id else ''
        saved_base_url = saved.get('alist_url') if not provider_id else ''
        saved_token = saved.get('alist_token') if not provider_id else ''
        saved_username = saved.get('alist_username') if not provider_id else ''
        saved_password = saved.get('alist_password') if not provider_id else ''
        self.base_url = (base_url or provider_cfg.get('base_url') or saved_base_url or fallback_base_url).rstrip('/')
        self.token = token if token is not None else (provider_cfg.get('token') or saved_token or fallback_token)
        self.username = str(username if username is not None else (provider_cfg.get('username') or provider_cfg.get('refresh_username') or saved_username or '')).strip()
        self.password = str(password if password is not None else (provider_cfg.get('password') or provider_cfg.get('refresh_password') or saved_password or ''))
        self.provider_id = str(provider_id or '').strip()
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> AsyncAListClient:
        if self._client is None:
            # 增大超时时间并增加连接池大小，防止并发请求时网盘接口响应过慢导致连接超时（特别是在扫描几千首时）
            limits = httpx.Limits(max_connections=100, max_keepalive_connections=20)
            self._client = httpx.AsyncClient(timeout=60.0, follow_redirects=True, limits=limits)
            self._owns_client = True
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
        self._client = None

    def _headers(self) -> dict[str, str]:
        headers = {
            'Content-Type': 'application/json',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
        }
        token = str(self.token or '').strip()
        if token and token not in {'0', '1', 'null', 'None', 'undefined'}:
            headers['Authorization'] = token
        return headers

    async def _refresh_token(self) -> bool:
        if self._client is None:
            raise RuntimeError('AsyncAListClient not initialized, use "async with" or pass client=')
        if not self.username or not self.password:
            return False
        response = await self._client.post(
            f'{self.base_url}/api/auth/login',
            headers={
                'Content-Type': 'application/json',
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
            },
            json={'username': self.username, 'password': self.password},
        )
        response.raise_for_status()
        data = response.json()
        if data.get('code') != 200:
            return False
        token = str((data.get('data') or {}).get('token') or '')
        if not token:
            return False
        self.token = token
        payload = {'alist_token': token}
        if self.provider_id:
            import json
            current_raw = get_runtime_settings().get('storage_providers')
            changed = False
            updated = []
            try:
                loaded = json.loads(current_raw) if isinstance(current_raw, str) and current_raw.strip() else []
            except Exception:
                loaded = []
            if isinstance(loaded, list):
                for item in loaded:
                    if not isinstance(item, dict):
                        continue
                    cloned = dict(item)
                    cfg = dict(cloned.get('config') or {})
                    if str(cloned.get('id') or '').strip() == self.provider_id and str(cloned.get('type') or '').strip() == 'alist':
                        cfg['token'] = token
                        cloned['config'] = cfg
                        changed = True
                    updated.append(cloned)
            if changed:
                payload['storage_providers'] = updated
        update_settings(payload, include_secrets=True, public_view=False)
        return True

    async def _post(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        if self._client is None:
            raise RuntimeError('AsyncAListClient not initialized, use "async with" or pass client=')
        response = await self._client.post(
            f'{self.base_url}{endpoint}',
            headers=self._headers(),
            json=payload,
        )
        data = response.json()
        if response.status_code >= 400:
            if _looks_like_auth_failure(data, response.status_code) and await self._refresh_token():
                response = await self._client.post(
                    f'{self.base_url}{endpoint}',
                    headers=self._headers(),
                    json=payload,
                )
                data = response.json()
            response.raise_for_status()
        if data.get('code') != 200 and _looks_like_auth_failure(data):
            if await self._refresh_token():
                response = await self._client.post(
                    f'{self.base_url}{endpoint}',
                    headers=self._headers(),
                    json=payload,
                )
                data = response.json()
        return data

    async def list_path(self, path: str, password: str = '', refresh: bool = False) -> list[dict[str, Any]]:
        page = 1
        all_content = []
        while True:
            payload = await self._post('/api/fs/list', {'path': path, 'password': password, 'page': page, 'per_page': 1000, 'refresh': refresh})
            if payload.get('code') != 200:
                raise RuntimeError(payload.get('message') or f'AList list_path failed: {payload}')
            data = payload.get('data') or {}
            content = data.get('content') or []
            if not isinstance(content, list):
                raise RuntimeError('AList content shape invalid')
            all_content.extend(_normalize_alist_item(item) for item in content)
            if len(content) < 1000:
                break
            page += 1
            # 分页拉取时也适当增加一点延迟防 CC
            if page > 1:
                import asyncio
                await asyncio.sleep(0.5)
        return all_content

    async def _resolve_existing_path(self, path: str) -> str:
        raw = str(path or '').strip()
        if not raw or raw == '/':
            return '/'
        parts = [part for part in raw.split('/') if part]
        current = ''
        for idx, part in enumerate(parts):
            candidate = '/' + '/'.join(parts[:idx + 1])
            try:
                payload = await self._post('/api/fs/get', {'path': candidate, 'password': ''})
                if payload.get('code') == 200:
                    current = candidate
                    continue
            except Exception:
                pass
            parent = current or '/'
            items = await self.list_path(parent, refresh=True)
            matched = next((item for item in items if str(item.get('name') or '') == part), None)
            if matched is None:
                matched = next((item for item in items if html.unescape(str(item.get('_raw_name') or '')) == part), None)
            if matched is None:
                raise RuntimeError(f'failed resolve path segment: {part} under {parent}')
            actual_name = str(matched.get('_raw_name') or matched.get('name') or part)
            current = (parent.rstrip('/') + '/' + actual_name) if parent != '/' else '/' + actual_name
        return current or raw

    async def get_path_info(self, path: str) -> dict[str, Any] | None:
        try:
            payload = await self._post('/api/fs/get', {'path': path, 'password': ''})
            if payload.get('code') != 200:
                resolved = await self._resolve_existing_path(path)
                if resolved != path:
                    payload = await self._post('/api/fs/get', {'path': resolved, 'password': ''})
            if payload.get('code') != 200:
                return None
            return payload.get('data') or {}
        except Exception:
            try:
                resolved = await self._resolve_existing_path(path)
                payload = await self._post('/api/fs/get', {'path': resolved, 'password': ''})
                if payload.get('code') != 200:
                    return None
                return payload.get('data') or {}
            except Exception:
                return None

    async def get_raw_url(self, path: str) -> str:
        # 增加获取直链的重试机制，防止 AList 偶发超时导致整个歌曲元数据无法获取
        import asyncio
        resolved_path = path
        max_retries = 3
        for attempt in range(max_retries):
            try:
                payload = await self._post('/api/fs/get', {'path': resolved_path, 'password': ''})
                if payload.get('code') != 200:
                    resolved_path = await self._resolve_existing_path(path)
                    payload = await self._post('/api/fs/get', {'path': resolved_path, 'password': ''})
                if payload.get('code') != 200:
                    raise RuntimeError(payload.get('message') or f'AList get_raw_url failed: {payload}')
                data = payload.get('data') or {}
                raw_url = data.get('raw_url') or data.get('url')
                if not raw_url:
                    encoded = quote(resolved_path)
                    raw_url = f'{self.base_url}/d{encoded}'
                return raw_url
            except Exception as e:
                if attempt == max_retries - 1:
                    raise
                await asyncio.sleep(2 ** (attempt + 1))

    async def read_text_file(self, path: str) -> str:
        if self._client is None:
            raise RuntimeError('AsyncAListClient not initialized, use "async with" or pass client=')
        raw_url = await self.get_raw_url(path)
        response = await self._client.get(raw_url, headers={'User-Agent': 'Mozilla/5.0'})
        response.raise_for_status()
        return response.text

    async def write_bytes(self, path: str, content: bytes) -> bool:
        if self._client is None:
            raise RuntimeError('AsyncAListClient not initialized, use "async with" or pass client=')

        import os
        as_task = 'true' if str(os.getenv('MUSICON_ALIST_PUT_AS_TASK', 'false')).strip().lower() in {'1', 'true', 'yes', 'on'} else 'false'
        put_timeout = httpx.Timeout(connect=20.0, read=300.0, write=300.0, pool=60.0)

        async def _put_once() -> httpx.Response:
            return await self._client.put(
                f'{self.base_url}/api/fs/put',
                headers={
                    'Authorization': self.token,
                    'File-Path': quote(path),
                    'As-Task': as_task,
                    'Content-Length': str(len(content)),
                },
                content=content,
                timeout=put_timeout,
            )

        response = await _put_once()
        retried = False
        body: dict[str, Any] | None = None
        text = response.text or ''
        try:
            body = response.json()
        except Exception:
            body = None

        needs_refresh = response.status_code in (401, 403) or _looks_like_auth_failure(body, response.status_code)
        if needs_refresh and await self._refresh_token():
            retried = True
            response = await _put_once()
            text = response.text or ''
            try:
                body = response.json()
            except Exception:
                body = None

        if response.status_code != 200:
            logger.warning(
                'AList PUT failed provider=%s path=%s status=%s retried=%s as_task=%s body=%r',
                self.provider_id or '<default>',
                path,
                response.status_code,
                retried,
                as_task,
                text[:500],
            )
            return False

        if isinstance(body, dict):
            ok = body.get('code') == 200
            if not ok:
                logger.warning(
                    'AList PUT rejected provider=%s path=%s code=%s message=%r retried=%s as_task=%s body=%r',
                    self.provider_id or '<default>',
                    path,
                    body.get('code'),
                    body.get('message'),
                    retried,
                    as_task,
                    body,
                )
            return ok

        ok = 'unexpected error' in text.lower()
        if not ok:
            logger.warning(
                'AList PUT non-json failure provider=%s path=%s retried=%s as_task=%s body=%r',
                self.provider_id or '<default>',
                path,
                retried,
                as_task,
                text[:500],
            )
        return ok

    async def read_range(self, raw_url: str, start: int = 0, end: int = 262143) -> bytes:
        if self._client is None:
            raise RuntimeError('AsyncAListClient not initialized, use "async with" or pass client=')
        headers = {
            'Range': f'bytes={start}-{end}',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
        }
        if self.token:
            headers['Authorization'] = self.token
        
        # 针对大规模并发扫描的网盘下载限流重试机制
        import asyncio
        max_retries = 3
        for attempt in range(max_retries):
            try:
                response = await self._client.get(raw_url, headers=headers)
                response.raise_for_status()
                return response.content
            except Exception as e:
                if attempt == max_retries - 1:
                    raise
                # 指数退避重试: 2s, 4s
                await asyncio.sleep(2 ** (attempt + 1))
