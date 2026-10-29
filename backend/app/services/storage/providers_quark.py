from __future__ import annotations

import asyncio
import base64
import hashlib
import time
from html import unescape
from typing import Any
from xml.sax.saxutils import escape as xml_escape

import httpx

from app.services.storage.errors import StorageError
from app.services.storage.types import StorageEntry, StorageRef


class QuarkStorageProvider:
    provider_type = 'quark'
    _user_agent = (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) quark-cloud-drive/2.5.20 Chrome/100.0.4896.160 '
        'Electron/18.3.5.4-b478491100 Safari/537.36 Channel/pckk_other_ch'
    )
    _referer = 'https://pan.quark.cn'

    def __init__(
        self,
        provider_id: str,
        *,
        cookie: str | None = None,
        root_dir_id: str | None = None,
        root_path: str | None = None,
        drive_base_url: str | None = None,
        open_base_url: str | None = None,
        provider_type: str | None = None,
    ) -> None:
        self.provider_id = provider_id
        self.provider_type = str(provider_type or 'quark')
        self.cookie = cookie or ''
        self.root_dir_id = str(root_dir_id or '0')
        self.root_path = self.normalize_path(root_path or '/')
        self.drive_base_url = (drive_base_url or 'https://drive.quark.cn').rstrip('/')
        self.open_base_url = (open_base_url or '').rstrip('/')
        self.api_base = f'{self.drive_base_url}/1/clouddrive'
        self._min_interval_sec = 0.35
        self._cooldown_sec = 90.0
        self._failure_threshold = 3
        self._last_request_at = 0.0
        self._failure_count = 0
        self._cooldown_until = 0.0

    def _headers(self) -> dict[str, str]:
        return {
            'Cookie': self.cookie,
            'Accept': 'application/json, text/plain, */*',
            'Referer': self._referer,
            'User-Agent': self._user_agent,
        }

    def _params(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {'pr': 'ucpro', 'fr': 'pc'}
        if extra:
            params.update(extra)
        return params

    def _raise(self, message: str, *, code: str = 'QUARK_ERROR', cause: Exception | None = None):
        raise StorageError(message, code=code, provider_id=self.provider_id, cause=cause)

    def _check_ready(self) -> None:
        if not self.cookie.strip():
            self._raise('Quark provider missing cookie', code='CONFIG_INVALID')

    def _cookie_map(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for part in str(self.cookie or '').split(';'):
            item = part.strip()
            if not item or '=' not in item:
                continue
            k, v = item.split('=', 1)
            result[k.strip()] = v.strip()
        return result

    def _save_cookies(self, response: httpx.Response) -> None:
        cookie_map = self._cookie_map()
        changed = False
        for key in ('__puus', '__pus'):
            try:
                value = response.cookies.get(key)
            except Exception:
                value = None
            if value:
                cookie_map[key] = value
                changed = True
        if changed:
            self.cookie = '; '.join(f'{k}={v}' for k, v in cookie_map.items())

    def _before_request(self) -> None:
        now = time.time()
        if self._cooldown_until > now:
            remain = round(self._cooldown_until - now, 1)
            self._raise(f'Quark provider cooling down after repeated failures, retry in {remain}s', code='RATE_LIMITED')
        gap = now - self._last_request_at
        if gap < self._min_interval_sec:
            time.sleep(self._min_interval_sec - gap)
        self._last_request_at = time.time()

    async def _before_request_async(self) -> None:
        now = time.time()
        if self._cooldown_until > now:
            remain = round(self._cooldown_until - now, 1)
            self._raise(f'Quark provider cooling down after repeated failures, retry in {remain}s', code='RATE_LIMITED')
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

    def _request(self, method: str, pathname: str, *, params: dict[str, Any] | None = None, json_body: dict[str, Any] | None = None) -> dict[str, Any]:
        self._check_ready()
        url = f'{self.api_base}{pathname}'
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                self._before_request()
                with httpx.Client(timeout=60.0, headers=self._headers()) as client:
                    response = client.request(method, url, params=self._params(params), json=json_body)
                    self._save_cookies(response)
                    response.raise_for_status()
                    data = response.json()
                if int(data.get('status') or 200) >= 400 or int(data.get('code') or 0) != 0:
                    message = str(data.get('message') or f'Quark request failed: {pathname}')
                    detail = data.get('data')
                    if detail not in (None, '', [], {}):
                        message = f'{message} | data={detail}'
                    self._record_failure()
                    self._raise(message, code='REMOTE_ERROR')
                self._record_success()
                return data
            except Exception as exc:
                last_exc = exc
                self._record_failure()
                if attempt < 2:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                break
        self._raise(f'Quark request failed: {pathname}', cause=last_exc)

    async def _request_async(self, method: str, pathname: str, *, params: dict[str, Any] | None = None, json_body: dict[str, Any] | None = None) -> dict[str, Any]:
        self._check_ready()
        url = f'{self.api_base}{pathname}'
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                await self._before_request_async()
                async with httpx.AsyncClient(timeout=60.0, headers=self._headers()) as client:
                    response = await client.request(method, url, params=self._params(params), json=json_body)
                    self._save_cookies(response)
                    response.raise_for_status()
                    data = response.json()
                if int(data.get('status') or 200) >= 400 or int(data.get('code') or 0) != 0:
                    message = str(data.get('message') or f'Quark request failed: {pathname}')
                    detail = data.get('data')
                    if detail not in (None, '', [], {}):
                        message = f'{message} | data={detail}'
                    self._record_failure()
                    self._raise(message, code='REMOTE_ERROR')
                self._record_success()
                return data
            except Exception as exc:
                last_exc = exc
                self._record_failure()
                if attempt < 2:
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                break
        self._raise(f'Quark request failed: {pathname}', cause=last_exc)

    def _item_to_entry_dict(self, item: dict[str, Any], *, parent_path: str) -> dict[str, Any]:
        name = unescape(str(item.get('file_name') or item.get('name') or ''))
        is_file = bool(item.get('file'))
        path = self.normalize_path(f'{parent_path.rstrip("/")}/{name}' if parent_path != '/' else f'/{name}')
        return {
            **item,
            'name': name,
            'is_dir': not is_file,
            'path': path,
            'provider_id': self.provider_id,
            'provider_type': self.provider_type,
            'object_id': str(item.get('fid') or ''),
            'modified': str(item.get('updated_at') or item.get('l_updated_at') or ''),
        }

    def _entry(self, item: dict[str, Any], *, full_path: str) -> StorageEntry:
        name = unescape(str(item.get('file_name') or item.get('name') or full_path.rsplit('/', 1)[-1] or '/'))
        return StorageEntry(
            provider_id=self.provider_id,
            path=full_path,
            name=name,
            is_dir=not bool(item.get('file')),
            size=int(item.get('size') or 0),
            modified_at=str(item.get('updated_at') or item.get('l_updated_at') or item.get('created_at') or ''),
            object_id=str(item.get('fid') or ''),
            raw=dict(item),
        )

    def _list_dir_by_id(self, dir_id: str, *, refresh: bool = False) -> list[dict[str, Any]]:
        _ = refresh
        page = 1
        size = 100
        rows: list[dict[str, Any]] = []
        while True:
            data = self._request(
                'GET',
                '/file/sort',
                params={
                    'pdir_fid': dir_id,
                    '_page': page,
                    '_size': size,
                    '_fetch_total': 1,
                    'fetch_all_file': 1,
                    'fetch_risk_file_name': 1,
                    '_sort': 'file_type:asc,file_name:asc',
                },
            )
            chunk = list((data.get('data') or {}).get('list') or [])
            rows.extend(chunk)
            total = int((data.get('metadata') or {}).get('_total') or len(chunk))
            if page * size >= total or not chunk:
                break
            page += 1
        return rows

    async def _list_dir_by_id_async(self, dir_id: str, *, refresh: bool = False) -> list[dict[str, Any]]:
        _ = refresh
        page = 1
        size = 100
        rows: list[dict[str, Any]] = []
        while True:
            data = await self._request_async(
                'GET',
                '/file/sort',
                params={
                    'pdir_fid': dir_id,
                    '_page': page,
                    '_size': size,
                    '_fetch_total': 1,
                    'fetch_all_file': 1,
                    'fetch_risk_file_name': 1,
                    '_sort': 'file_type:asc,file_name:asc',
                },
            )
            chunk = list((data.get('data') or {}).get('list') or [])
            rows.extend(chunk)
            total = int((data.get('metadata') or {}).get('_total') or len(chunk))
            if page * size >= total or not chunk:
                break
            page += 1
        return rows

    def _resolve_path(self, path: str) -> tuple[str, dict[str, Any] | None, str]:
        full_path = self._join_rooted_path(path)
        if full_path == self.root_path:
            root_name = self.root_path.rsplit('/', 1)[-1] or '/'
            return self.root_dir_id, {
                'fid': self.root_dir_id,
                'file_name': root_name,
                'file': False,
                'size': 0,
                'updated_at': 0,
            }, full_path
        rel = full_path[len(self.root_path):].strip('/') if full_path.startswith(self.root_path) else full_path.strip('/')
        parts = [p for p in rel.split('/') if p]
        current_id = self.root_dir_id
        current_path = self.root_path
        current_item: dict[str, Any] | None = None
        for index, part in enumerate(parts):
            found = None
            for item in self._list_dir_by_id(current_id):
                if unescape(str(item.get('file_name') or '')) == part:
                    found = item
                    break
            if not found:
                return '', None, full_path
            current_item = found
            current_id = str(found.get('fid') or '')
            current_path = self.normalize_path(f'{current_path.rstrip("/")}/{part}' if current_path != '/' else f'/{part}')
            if index < len(parts) - 1 and bool(found.get('file')):
                return '', None, full_path
        return current_id, current_item, full_path

    async def _resolve_path_async(self, path: str) -> tuple[str, dict[str, Any] | None, str]:
        full_path = self._join_rooted_path(path)
        if full_path == self.root_path:
            root_name = self.root_path.rsplit('/', 1)[-1] or '/'
            return self.root_dir_id, {
                'fid': self.root_dir_id,
                'file_name': root_name,
                'file': False,
                'size': 0,
                'updated_at': 0,
            }, full_path
        rel = full_path[len(self.root_path):].strip('/') if full_path.startswith(self.root_path) else full_path.strip('/')
        parts = [p for p in rel.split('/') if p]
        current_id = self.root_dir_id
        current_item: dict[str, Any] | None = None
        for index, part in enumerate(parts):
            found = None
            for item in await self._list_dir_by_id_async(current_id):
                if unescape(str(item.get('file_name') or '')) == part:
                    found = item
                    break
            if not found:
                return '', None, full_path
            current_item = found
            current_id = str(found.get('fid') or '')
            if index < len(parts) - 1 and bool(found.get('file')):
                return '', None, full_path
        return current_id, current_item, full_path

    def list_dir(self, ref: StorageRef, *, refresh: bool = False) -> list[dict[str, Any]]:
        dir_id, item, full_path = self._resolve_path(ref.path)
        if not dir_id or (item is not None and bool(item.get('file'))):
            return []
        return [self._item_to_entry_dict(row, parent_path=full_path) for row in self._list_dir_by_id(dir_id, refresh=refresh)]

    async def list_dir_async(self, ref: StorageRef, *, refresh: bool = False) -> list[dict[str, Any]]:
        dir_id, item, full_path = await self._resolve_path_async(ref.path)
        if not dir_id or (item is not None and bool(item.get('file'))):
            return []
        rows = await self._list_dir_by_id_async(dir_id, refresh=refresh)
        return [self._item_to_entry_dict(row, parent_path=full_path) for row in rows]

    def stat(self, ref: StorageRef) -> StorageEntry | None:
        _id, item, full_path = self._resolve_path(ref.path)
        if not item:
            return None
        return self._entry(item, full_path=full_path)

    async def stat_async(self, ref: StorageRef) -> StorageEntry | None:
        _id, item, full_path = await self._resolve_path_async(ref.path)
        if not item:
            return None
        return self._entry(item, full_path=full_path)

    def exists(self, ref: StorageRef) -> bool:
        return self.stat(ref) is not None

    async def exists_async(self, ref: StorageRef) -> bool:
        return await self.stat_async(ref) is not None

    def mkdir(self, ref: StorageRef) -> bool:
        target = self.normalize_path(ref.path)
        parent = target.rsplit('/', 1)[0] or '/'
        name = target.rsplit('/', 1)[-1]
        parent_id, parent_item, _ = self._resolve_path(parent)
        if not parent_id or (parent_item is not None and bool(parent_item.get('file'))):
            self._raise(f'Quark mkdir parent not found: {parent}', code='PARENT_NOT_FOUND')
        self._request('POST', '/file', json_body={
            'dir_init_lock': False,
            'dir_path': '',
            'file_name': name,
            'pdir_fid': parent_id,
        })
        return True

    async def mkdir_async(self, ref: StorageRef) -> bool:
        target = self.normalize_path(ref.path)
        parent = target.rsplit('/', 1)[0] or '/'
        name = target.rsplit('/', 1)[-1]
        parent_id, parent_item, _ = await self._resolve_path_async(parent)
        if not parent_id or (parent_item is not None and bool(parent_item.get('file'))):
            self._raise(f'Quark mkdir parent not found: {parent}', code='PARENT_NOT_FOUND')
        await self._request_async('POST', '/file', json_body={
            'dir_init_lock': False,
            'dir_path': '',
            'file_name': name,
            'pdir_fid': parent_id,
        })
        return True

    def get_download_url(self, ref: StorageRef) -> str:
        item = self.stat(ref)
        if not item:
            self._raise(f'Quark file not found: {ref.path}', code='NOT_FOUND')
        if item.is_dir:
            self._raise(f'Quark path is a directory: {ref.path}', code='IS_DIR')
        data = self._request('POST', '/file/download', json_body={'fids': [item.object_id]})
        rows = list(data.get('data') or [])
        if not rows or not rows[0].get('download_url'):
            self._raise(f'Quark download url missing: {ref.path}', code='DOWNLOAD_URL_MISSING')
        return str(rows[0]['download_url'])

    async def get_download_url_async(self, ref: StorageRef) -> str:
        item = await self.stat_async(ref)
        if not item:
            self._raise(f'Quark file not found: {ref.path}', code='NOT_FOUND')
        if item.is_dir:
            self._raise(f'Quark path is a directory: {ref.path}', code='IS_DIR')
        data = await self._request_async('POST', '/file/download', json_body={'fids': [item.object_id]})
        rows = list(data.get('data') or [])
        if not rows or not rows[0].get('download_url'):
            self._raise(f'Quark download url missing: {ref.path}', code='DOWNLOAD_URL_MISSING')
        return str(rows[0]['download_url'])

    def read_range(self, ref: StorageRef, start: int = 0, end: int = 262143) -> bytes:
        url = self.get_download_url(ref)
        headers = {
            'Range': f'bytes={start}-{end}',
            'Cookie': self.cookie,
            'Referer': self._referer,
            'User-Agent': self._user_agent,
        }
        try:
            with httpx.Client(timeout=60.0, follow_redirects=True) as client:
                response = client.get(url, headers=headers)
                response.raise_for_status()
                return response.content
        except Exception as exc:
            self._raise(f'Quark read range failed: {ref.path}', cause=exc)

    async def read_range_async(self, ref: StorageRef, start: int = 0, end: int = 262143) -> bytes:
        url = await self.get_download_url_async(ref)
        headers = {
            'Range': f'bytes={start}-{end}',
            'Cookie': self.cookie,
            'Referer': self._referer,
            'User-Agent': self._user_agent,
        }
        try:
            async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
                response = await client.get(url, headers=headers)
                response.raise_for_status()
                return response.content
        except Exception as exc:
            self._raise(f'Quark read range failed: {ref.path}', cause=exc)

    async def read_text_async(self, ref: StorageRef) -> str:
        url = await self.get_download_url_async(ref)
        headers = {
            'Cookie': self.cookie,
            'Referer': self._referer,
            'User-Agent': self._user_agent,
        }
        try:
            async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
                response = await client.get(url, headers=headers)
                response.raise_for_status()
                return response.text
        except Exception as exc:
            self._raise(f'Quark read text failed: {ref.path}', cause=exc)

    async def write_bytes_async(self, ref: StorageRef, content: bytes) -> bool:
        target = self.normalize_path(ref.path)
        parent = target.rsplit('/', 1)[0] or '/'
        name = target.rsplit('/', 1)[-1]
        parent_id, parent_item, _ = await self._resolve_path_async(parent)
        if not parent_id or (parent_item is not None and bool(parent_item.get('file'))):
            self._raise(f'Quark upload parent not found: {parent}', code='PARENT_NOT_FOUND')

        size = len(content)
        pre = await self._request_async('POST', '/file/upload/pre', json_body={
            'ccp_hash_update': True,
            'dir_name': '',
            'file_name': name,
            'format_type': 'application/octet-stream',
            'l_created_at': 0,
            'l_updated_at': 0,
            'pdir_fid': parent_id,
            'size': size,
        })
        pre_data = dict(pre.get('data') or {})
        metadata = dict(pre.get('metadata') or {})
        task_id = str(pre_data.get('task_id') or '')
        if not task_id:
            self._raise('Quark upload preflight missing task_id', code='UPLOAD_PRE_FAILED')
        if bool(pre_data.get('finish')):
            return True

        md5_hex = hashlib.md5(content).hexdigest()
        sha1_hex = hashlib.sha1(content).hexdigest()
        hash_resp = await self._request_async('POST', '/file/update/hash', json_body={
            'md5': md5_hex,
            'sha1': sha1_hex,
            'task_id': task_id,
        })
        if bool((hash_resp.get('data') or {}).get('finish')):
            return True

        part_size = int(metadata.get('part_size') or 10485760)
        upload_id = str(pre_data.get('upload_id') or '')
        obj_key = str(pre_data.get('obj_key') or '')
        upload_url = str(pre_data.get('upload_url') or '')
        bucket = str(pre_data.get('bucket') or '')
        auth_info = str(pre_data.get('auth_info') or '')
        callback = dict(pre_data.get('callback') or {})
        if not all([upload_id, obj_key, upload_url, bucket, auth_info]):
            self._raise('Quark upload preflight returned incomplete data', code='UPLOAD_PRE_FAILED')

        etags: list[str] = []
        for index, start in enumerate(range(0, size, part_size), start=1):
            chunk = content[start:start + part_size]
            from email.utils import formatdate
            time_str = formatdate(usegmt=True)
            auth_meta = (
                'PUT\n\napplication/octet-stream\n'
                f'{time_str}\n'
                f'x-oss-date:{time_str}\n'
                'x-oss-user-agent:aliyun-sdk-js/6.6.1 Chrome 98.0.4758.80 on Windows 10 64-bit\n'
                f'/{bucket}/{obj_key}?partNumber={index}&uploadId={upload_id}'
            )
            auth_resp = await self._request_async('POST', '/file/upload/auth', json_body={
                'auth_info': auth_info,
                'auth_meta': auth_meta,
                'task_id': task_id,
            })
            auth_key = str((auth_resp.get('data') or {}).get('auth_key') or '')
            if not auth_key:
                self._raise('Quark upload auth missing auth_key', code='UPLOAD_AUTH_FAILED')

            put_url = f'https://{bucket}.{upload_url[8:]}/{obj_key}' if upload_url.startswith('https://') else f'https://{bucket}.{upload_url[7:]}/{obj_key}'
            headers = {
                'Authorization': auth_key,
                'Content-Type': 'application/octet-stream',
                'Referer': self._referer,
                'x-oss-date': time_str,
                'x-oss-user-agent': 'aliyun-sdk-js/6.6.1 Chrome 98.0.4758.80 on Windows 10 64-bit',
            }
            async with httpx.AsyncClient(timeout=120.0) as client:
                put_res = await client.put(put_url, params={'partNumber': index, 'uploadId': upload_id}, headers=headers, content=chunk)
                put_res.raise_for_status()
                etag = str(put_res.headers.get('Etag') or put_res.headers.get('ETag') or '').strip()
            if not etag:
                self._raise('Quark upload missing part etag', code='UPLOAD_PART_FAILED')
            etags.append(etag)

        xml_parts = ['<?xml version="1.0" encoding="UTF-8"?>', '<CompleteMultipartUpload>']
        for idx, etag in enumerate(etags, start=1):
            xml_parts.append('<Part>')
            xml_parts.append(f'<PartNumber>{idx}</PartNumber>')
            xml_parts.append(f'<ETag>{xml_escape(etag)}</ETag>')
            xml_parts.append('</Part>')
        xml_parts.append('</CompleteMultipartUpload>')
        xml_body = ''.join(xml_parts)
        content_md5 = base64.b64encode(hashlib.md5(xml_body.encode()).digest()).decode()
        callback_base64 = base64.b64encode(httpx._content.json_dumps(callback).encode()).decode() if callback else ''
        from email.utils import formatdate
        commit_time = formatdate(usegmt=True)
        auth_meta = (
            'POST\n'
            f'{content_md5}\n'
            'application/xml\n'
            f'{commit_time}\n'
            f'x-oss-callback:{callback_base64}\n'
            f'x-oss-date:{commit_time}\n'
            'x-oss-user-agent:aliyun-sdk-js/6.6.1 Chrome 98.0.4758.80 on Windows 10 64-bit\n'
            f'/{bucket}/{obj_key}?uploadId={upload_id}'
        )
        commit_auth = await self._request_async('POST', '/file/upload/auth', json_body={
            'auth_info': auth_info,
            'auth_meta': auth_meta,
            'task_id': task_id,
        })
        commit_auth_key = str((commit_auth.get('data') or {}).get('auth_key') or '')
        if not commit_auth_key:
            self._raise('Quark upload commit auth missing auth_key', code='UPLOAD_COMMIT_FAILED')

        commit_url = f'https://{bucket}.{upload_url[8:]}/{obj_key}' if upload_url.startswith('https://') else f'https://{bucket}.{upload_url[7:]}/{obj_key}'
        headers = {
            'Authorization': commit_auth_key,
            'Content-MD5': content_md5,
            'Content-Type': 'application/xml',
            'Referer': self._referer,
            'x-oss-callback': callback_base64,
            'x-oss-date': commit_time,
            'x-oss-user-agent': 'aliyun-sdk-js/6.6.1 Chrome 98.0.4758.80 on Windows 10 64-bit',
        }
        async with httpx.AsyncClient(timeout=120.0) as client:
            commit_res = await client.post(commit_url, params={'uploadId': upload_id}, headers=headers, content=xml_body.encode())
            commit_res.raise_for_status()

        await self._request_async('POST', '/file/upload/finish', json_body={
            'obj_key': obj_key,
            'task_id': task_id,
        })
        return True

    def search(self, keyword: str, *, roots: list[str] | None = None, limit: int = 20) -> list[dict[str, Any]]:
        keyword_lc = str(keyword or '').strip().lower()
        if not keyword_lc:
            return []
        search_roots = [self.normalize_path(root) for root in (roots or [self.root_path])]
        results: list[dict[str, Any]] = []
        seen: set[str] = set()

        def walk(path: str) -> None:
            if len(results) >= limit:
                return
            for item in self.list_dir(StorageRef(provider_id=self.provider_id, path=path)):
                name = str(item.get('name') or '')
                item_path = str(item.get('path') or '')
                if item_path in seen:
                    continue
                seen.add(item_path)
                if keyword_lc in name.lower():
                    results.append(item)
                    if len(results) >= limit:
                        return
                if bool(item.get('is_dir')):
                    walk(item_path)
                    if len(results) >= limit:
                        return

        for root in search_roots:
            walk(root)
            if len(results) >= limit:
                break
        return results[:limit]
