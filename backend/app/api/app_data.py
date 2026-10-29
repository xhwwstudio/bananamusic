import asyncio
import uuid
import json
from pathlib import Path
from datetime import datetime, timezone

import httpx
import logging
from sqlalchemy import text
from fastapi import APIRouter, BackgroundTasks, Body, HTTPException, Request, Response, File, Form, UploadFile
from fastapi.responses import RedirectResponse, FileResponse, PlainTextResponse

from app.core.config import settings
from app.db.compat import dt_to_iso
from app.db.session import engine
from app.db.compat import utc_days_ago
from app.services.storage import get_storage_service
from app.services.storage.errors import StorageError
from app.services.storage.service import build_storage_service, parse_storage_provider_rows
from app.services.cover_cache import COVER_DIR, get_thumb_path, save_cover_bytes
from app.services.app_settings import get_runtime_settings, get_settings, update_settings, merge_storage_providers_payload
from app.services.lx_runtime import lx_embedded_manager
from app.services.web_auth import COOKIE_NAME, authenticate_web_admin, build_web_token, get_web_session
from app.services.db_data import (
    create_pending_scan_task,
    create_playlist,
    create_scan_task,
    create_subsonic_user,
    delete_playlist,
    ensure_default_system_playlists,
    delete_subsonic_user,
    ensure_admin_subsonic_user,
    get_playlist,
    get_song,
    get_stats,
    get_subsonic_items,
    get_task,
    list_playlists,
    list_recent_plays,
    list_songs,
    list_songs_brief,
    list_tasks_brief,
    delete_songs,
    list_subsonic_users,
    list_tasks,
    list_autofill_failed_uploads,
    export_autofill_failed_uploads_text,
    clear_autofill_failed_uploads,
    list_top_songs,
    update_song,
    update_playlist,
    update_scan_task,
    update_subsonic_user,
)

router = APIRouter(prefix='/api', tags=['app'])
logger = logging.getLogger(__name__)


def _refresh_storage_service_cache() -> None:
    try:
        get_storage_service.cache_clear()
    except Exception:
        pass


def _lx_admin_headers() -> dict[str, str]:
    return {'x-frontend-auth': settings.lx_frontend_password}


def _lx_target_username(request: Request | None = None) -> str:
    runtime = get_runtime_settings()
    configured = str(runtime.get('lx_custom_source_username') or 'open').strip()
    if configured and configured != 'session':
        return configured
    if request is not None:
        session = get_web_session(request)
        if session and session.get('username'):
            return str(session['username'])
    return 'open'


async def _lx_request(method: str, path: str, *, request: Request | None = None, params: dict | None = None, json_payload: dict | None = None, files: dict | None = None, timeout: float = 20.0) -> dict:
    try:
        if lx_embedded_manager.configured_mode() == 'embedded':
            status = lx_embedded_manager.status()
            if status.get('embedded_enabled') and status.get('embedded_available') and not status.get('embedded_running'):
                await lx_embedded_manager.start()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f'LX request failed: {exc}')

    base_url = lx_embedded_manager.configured_base_url().rstrip('/')
    if not base_url:
        raise HTTPException(status_code=400, detail='LX service URL not configured')
    headers = _lx_admin_headers()
    if files is None:
        headers.setdefault('Content-Type', 'application/json')
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.request(method.upper(), f'{base_url}{path}', params=params, json=json_payload, files=files, headers=headers)
            response.raise_for_status()
            if not response.content:
                return {'success': True}
            content_type = response.headers.get('content-type', '')
            if 'application/json' in content_type:
                return response.json()
            return {'raw': response.text, 'success': True}
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text
        raise HTTPException(status_code=exc.response.status_code, detail=detail or 'LX request failed')
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f'LX request failed: {exc}')


async def _run_scan_task(task_id: str, path: str, mode: str, concurrency: int, batch_size: int, source: str = 'alist') -> None:
    current = await asyncio.to_thread(get_task, task_id)
    logs = list(current.get('logs', [])) if current else []
    logs.append({'level': 'info', 'message': '[RUN] 后台任务已接单，开始执行'})
    await asyncio.to_thread(update_scan_task, task_id, status='running', source=source, logs=logs)

    async def progress_callback(**fields) -> None:
        current_task = await asyncio.to_thread(get_task, task_id)
        current_logs = list(current_task.get('logs', [])) if current_task else []
        extra_logs = fields.pop('logs', []) or []
        if extra_logs:
            current_logs.extend(extra_logs)
            fields['logs'] = current_logs
        await asyncio.to_thread(update_scan_task, task_id, **fields)

    try:
        await create_scan_task(
            path=path,
            mode=mode,
            concurrency=concurrency,
            batch_size=batch_size,
            task_id=task_id,
            progress_callback=progress_callback,
            source=source,
            provider_id=source,
        )
    except Exception as exc:
        failed = await asyncio.to_thread(get_task, task_id)
        failed_logs = list(failed.get('logs', [])) if failed else logs
        failed_logs.append({'level': 'warn', 'message': f'[FAIL] 扫描任务执行失败：{exc}'})
        await asyncio.to_thread(
            update_scan_task,
            task_id,
            status='failed',
            source='failed',
            end_time='',
            logs=failed_logs,
        )


async def _run_autofill_task(task_id: str, quality: str, upload_dir: str) -> None:
    current = await asyncio.to_thread(get_task, task_id)
    logs = list(current.get('logs', [])) if current else []
    logs.append({'level': 'info', 'message': '[RUN] 自动补全任务已接单，开始执行'})
    await asyncio.to_thread(update_scan_task, task_id, status='running', source='autofill', logs=logs)

    async def progress_callback(**fields) -> None:
        current_task = await asyncio.to_thread(get_task, task_id)
        current_logs = list(current_task.get('logs', [])) if current_task else []
        extra_logs = fields.pop('logs', []) or []
        if extra_logs:
            current_logs.extend(extra_logs)
            fields['logs'] = current_logs
        await asyncio.to_thread(update_scan_task, task_id, **fields)

    try:
        from app.services.auto_fill_service import run_auto_fill_task
        await run_auto_fill_task(quality, upload_dir, progress_callback=progress_callback, task_id=task_id)
    except Exception as exc:
        failed = await asyncio.to_thread(get_task, task_id)
        failed_logs = list(failed.get('logs', [])) if failed else logs
        failed_logs.append({'level': 'warn', 'message': f'[FAIL] 自动补全任务执行失败：{exc}'})
        await asyncio.to_thread(
            update_scan_task,
            task_id,
            status='failed',
            source='failed',
            end_time=datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'),
            logs=failed_logs,
        )

from app.services.scan_service import cancel_scan_task

@router.get('/tasks/{task_id}')
def get_task_api(task_id: str):
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail='Task not found')
    return task

@router.post('/tasks/{task_id}/cancel')
def cancel_task_api(task_id: str):
    cancel_scan_task(task_id)
    update_scan_task(
        task_id,
        status='failed',
        end_time=datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'),
        logs=[{'level': 'error', 'message': '[STOP] 任务已被手动终止'}]
    )
    return {'message': 'Task cancelled'}

@router.delete('/tasks/{task_id}')
def delete_task_api(task_id: str):
    from app.services.db_data import delete_scan_task
    deleted = delete_scan_task(task_id)
    if not deleted:
        raise HTTPException(status_code=404, detail='Task not found')
    return {'message': 'Task deleted'}

@router.post('/web-auth/login')
def web_login(payload: dict = Body(...)) -> Response:
    username = str(payload.get('username') or '').strip()
    password = str(payload.get('password') or '')
    if not authenticate_web_admin(username, password):
        raise HTTPException(status_code=401, detail='用户名或密码错误')
    response = Response(content='{"ok":true}', media_type='application/json')
    response.set_cookie(COOKIE_NAME, build_web_token(username, password), httponly=True, samesite='lax', path='/')
    return response


@router.post('/web-auth/logout')
def web_logout() -> Response:
    response = Response(content='{"ok":true}', media_type='application/json')
    response.delete_cookie(COOKIE_NAME, path='/')
    return response


@router.get('/web-auth/session')
def web_session(request: Request) -> dict:
    session = get_web_session(request)
    return {'authenticated': bool(session), 'user': session}


@router.get('/stats')
def stats() -> dict:
    return get_stats()


def _cache_cover_from_url(url: str) -> str:
    target = str(url or '').strip()
    if not target or not (target.startswith('http://') or target.startswith('https://')):
        return target
    try:
        with httpx.Client(timeout=15.0, follow_redirects=True) as client:
            res = client.get(target)
            res.raise_for_status()
            content_type = str(res.headers.get('content-type') or '').lower()
            suffix = 'png' if 'png' in content_type else 'jpg'
            return save_cover_bytes(res.content, suffix=suffix)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f'封面下载失败: {exc}')


@router.get('/songs')
def songs() -> dict:
    return {'songs': list_songs_brief()}

@router.put('/songs/{song_id}')
def update_song_api(song_id: str, payload: dict = Body(...)) -> dict:
    next_payload = dict(payload or {})
    cover_value = str(next_payload.get('cover_path') or '').strip()
    if cover_value.startswith('http://') or cover_value.startswith('https://'):
        next_payload['cover_path'] = _cache_cover_from_url(cover_value)
    updated = update_song(song_id, next_payload)
    if not updated:
        raise HTTPException(status_code=404, detail="Song not found")
    updated.pop('cover_art', None)
    return updated


@router.delete('/songs')
def delete_songs_api(payload: dict = Body(...)) -> dict:
    song_ids = payload.get('song_ids', [])
    if not song_ids:
        return {'message': '未选择任何曲目', 'deleted': False}
    deleted = delete_songs(song_ids)
    return {'message': f'已删除 {len(song_ids)} 首曲目', 'deleted': deleted}


@router.get('/songs/top')
def top_songs(limit: int = 10) -> dict:
    return {'songs': list_top_songs(limit=limit)}


@router.get('/songs/{song_id}/stream')
def song_stream(song_id: str):
    song = get_song(song_id)
    if not song:
        raise HTTPException(status_code=404, detail='Song not found')
    path = str(song.get('path') or '').strip()
    if not path:
        raise HTTPException(status_code=404, detail='Cloud path not ready')
    if path.startswith('lx://virtual/'):
        raise HTTPException(status_code=501, detail='Virtual song browser playback is not supported yet')
    try:
        storage = get_storage_service()
        fresh_url = storage.get_download_url(storage.make_ref(path))
    except Exception:
        fresh_url = str(song.get('raw_url') or '').strip()
    if not fresh_url:
        raise HTTPException(status_code=502, detail='Direct stream URL not ready')
    return RedirectResponse(url=fresh_url, status_code=302)


@router.get('/songs/{song_id}')
def get_song_api(song_id: str) -> dict:
    song = get_song(song_id)
    if not song:
        raise HTTPException(status_code=404, detail='Song not found')
    song.pop('cover_art', None)
    return song


def _song_by_cover_filename(filename: str) -> dict | None:
    target = str((COVER_DIR / filename).resolve())
    songs = list_songs()
    return next((song for song in songs if str(song.get('cover_path') or '').strip() == target), None)


def _ensure_virtual_cover_local(song: dict | None) -> str:
    if not song:
        return ''
    existing = str(song.get('cover_path') or '').strip()
    if existing and Path(existing).exists():
        return existing
    raw_url = str(song.get('raw_url') or '').strip()
    if not raw_url:
        return ''
    try:
        data = json.loads(raw_url)
    except Exception:
        return ''
    cover_url = str(data.get('img') or data.get('pic') or '').strip()
    if not cover_url:
        return ''
    try:
        with httpx.Client(timeout=15.0, follow_redirects=True) as client:
            res = client.get(cover_url)
            res.raise_for_status()
            content_type = str(res.headers.get('content-type') or '').lower()
            suffix = 'png' if 'png' in content_type else 'jpg'
            saved_path = save_cover_bytes(res.content, suffix=suffix)
            if saved_path:
                update_song(str(song.get('id') or ''), {'cover_path': saved_path})
                song['cover_path'] = saved_path
                return saved_path
    except Exception:
        return ''
    return ''


def _ensure_song_cover_local(song: dict | None) -> str:
    if not song:
        return ''
    existing = str(song.get('cover_path') or '').strip()
    if existing and Path(existing).exists():
        return existing

    cover_art = str(song.get('cover_art') or '').strip()
    if cover_art:
        try:
            from app.services.cover_cache import save_cover_base64
            saved_path = save_cover_base64(cover_art)
            if saved_path:
                update_song(str(song.get('id') or ''), {'cover_path': saved_path})
                song['cover_path'] = saved_path
                return saved_path
        except Exception:
            pass

    if str(song.get('fmt') or '') == 'VIRTUAL':
        return _ensure_virtual_cover_local(song)

    path = str(song.get('path') or '').strip()
    if not path:
        return ''
    try:
        storage = get_storage_service()
        raw = storage.get_download_url(storage.make_ref(path))
        with httpx.Client(timeout=20.0, follow_redirects=True) as client:
            res = client.get(raw, headers={'Range': 'bytes=0-262143', 'User-Agent': 'Mozilla/5.0'})
            res.raise_for_status()
            from app.services.scan_service import _parse_and_extract
            metadata = _parse_and_extract(res.content)
            saved_path = str(metadata.get('cover_path') or '').strip()
            if saved_path:
                update_song(str(song.get('id') or ''), {'cover_path': saved_path})
                song['cover_path'] = saved_path
                return saved_path
    except Exception:
        return ''
    return ''


@router.get('/covers/thumb/{size}/{filename}')
def cover_thumb(size: int, filename: str):
    thumb_path = get_thumb_path(filename, size)
    if not thumb_path:
        song = _song_by_cover_filename(filename)
        if song:
            _ensure_song_cover_local(song)
            thumb_path = get_thumb_path(Path(str(song.get('cover_path') or '')).name, size) if song.get('cover_path') else None
    if not thumb_path:
        raise HTTPException(status_code=404, detail='Cover not found')
    return FileResponse(thumb_path, media_type='image/webp')

@router.get('/plays/recent')
def recent_plays(limit: int = 10) -> dict:
    return {'plays': list_recent_plays(limit=limit)}


@router.get('/tasks')
def tasks() -> dict:
    return {'tasks': list_tasks_brief()}


@router.get('/tasks/{task_id}')
def task_detail(task_id: str) -> dict:
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail='Task not found')
    return task


@router.get('/autofill/failed-uploads')
def autofill_failed_uploads() -> dict:
    return {'items': list_autofill_failed_uploads()}


@router.get('/autofill/failed-uploads/export')
def export_autofill_failed_uploads() -> PlainTextResponse:
    content = export_autofill_failed_uploads_text()
    headers = {'Content-Disposition': 'attachment; filename="autofill_failed_uploads.txt"'}
    return PlainTextResponse(content or '', headers=headers)


@router.delete('/autofill/failed-uploads')
def clear_autofill_failed_uploads_api() -> dict:
    deleted = clear_autofill_failed_uploads()
    return {'message': f'已清空 {deleted} 条上传失败记录', 'deleted': deleted}


from app.services.scheduler import update_cron_schedule
import asyncio

@router.post('/scan')
async def scan(background_tasks: BackgroundTasks, payload: dict = Body(...)) -> dict:
    path = payload.get('path', '/')
    mode = payload.get('mode', 'incremental')
    concurrency = int(payload.get('concurrency', 10))
    batch_size = int(payload.get('batch_size', 100))
    provider_id = str(payload.get('provider_id') or get_runtime_settings().get('last_scan_provider_id') or get_runtime_settings().get('storage_default_provider_id') or '').strip()
    if not provider_id:
        raise HTTPException(status_code=400, detail='请先选择扫描存储源')
    _refresh_storage_service_cache()
    storage = get_storage_service()
    try:
        storage.get_provider(provider_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f'扫描存储源不存在或未刷新：{provider_id} ({exc})')
    normalized_path = storage.normalize_path(path, provider_id=provider_id)
    await asyncio.to_thread(update_settings, {
        'last_scan_provider_id': provider_id,
        'last_scan_root': 'default',
        'last_scan_path': normalized_path,
    })
    _refresh_storage_service_cache()
    task = await asyncio.to_thread(
        create_pending_scan_task,
        path=normalized_path,
        mode=mode,
        concurrency=concurrency,
        batch_size=batch_size,
        source=provider_id,
    )
    background_tasks.add_task(_run_scan_task, task['id'], normalized_path, mode, concurrency, batch_size, provider_id)
    return {
        'message': '扫描任务已创建，后台执行中',
        'task_id': task['id'],
        'status': task['status'],
        'tasks': await asyncio.to_thread(list_tasks),
    }

@router.post('/autofill')
async def trigger_autofill(background_tasks: BackgroundTasks, payload: dict = Body(...)) -> dict:
    quality = payload.get('quality', 'flac')
    upload_dir = payload.get('upload_dir', '/MusicOn_AutoDownload/')
    provider_id = str(payload.get('provider_id') or get_runtime_settings().get('autofill_provider_id') or get_runtime_settings().get('storage_default_provider_id') or '').strip()
    if not provider_id:
        raise HTTPException(status_code=400, detail='请先选择补全上传存储源')
    _refresh_storage_service_cache()
    storage = get_storage_service()
    try:
        storage.get_provider(provider_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f'补全存储源不存在或未刷新：{provider_id} ({exc})')
    upload_dir = storage.normalize_path(upload_dir, provider_id=provider_id)
    if not upload_dir.endswith('/'):
        upload_dir += '/'
    await asyncio.to_thread(update_settings, {
        'autofill_provider_id': provider_id,
        'autofill_root': 'default',
        'autofill_path': upload_dir,
    })
    _refresh_storage_service_cache()

    # Create a pending task record
    task = await asyncio.to_thread(
        create_pending_scan_task,
        path=f'自动无损补全 ({quality}) -> {upload_dir}',
        mode='autofill',
        concurrency=1,
        batch_size=1,
    )
    
    # Add the actual execution to background tasks
    background_tasks.add_task(_run_autofill_task, task['id'], quality, upload_dir)
    
    return {
        'message': '自动补全任务已创建，后台执行中',
        'task_id': task['id'],
        'status': task['status'],
        'tasks': await asyncio.to_thread(list_tasks),
    }

@router.post('/scan/schedule')
async def schedule_scan(payload: dict = Body(...)) -> dict:
    path = payload.get('path', '/')
    mode = payload.get('mode', 'incremental')
    concurrency = int(payload.get('concurrency', 10))
    batch_size = int(payload.get('batch_size', 100))
    cron_expr = payload.get('cron_expr', '').strip()
    
    update_settings({
        'cron_path': path,
        'cron_mode': mode,
        'cron_concur': str(concurrency),
        'cron_batch': str(batch_size),
        'cron_expr': cron_expr
    })
    
    update_cron_schedule()
    
    if cron_expr:
        return {'message': f'已成功开启定时扫描：{cron_expr}'}
    else:
        return {'message': '已关闭定时扫描'}


@router.get('/settings')
def app_settings() -> dict:
    return get_settings()


@router.post('/settings')
async def save_app_settings(payload: dict = Body(...)) -> dict:
    prev_status = lx_embedded_manager.status()
    prev_mode = str(prev_status.get('mode') or 'embedded')

    updated = update_settings(payload, include_secrets=True, public_view=True)
    _refresh_storage_service_cache()
    username = str(payload.get('subsonic_user') or updated.get('subsonic_user') or '').strip()
    password = str(payload.get('subsonic_pass') or '').strip()
    if username and password:
        ensure_admin_subsonic_user(username=username, password=password)

    next_mode = lx_embedded_manager.configured_mode()
    if prev_mode != next_mode:
        if prev_status.get('embedded_running'):
            await lx_embedded_manager.stop(force=True)
        if next_mode == 'embedded':
            await lx_embedded_manager.start()
    elif next_mode == 'embedded':
        current = lx_embedded_manager.status()
        if current.get('embedded_enabled') and current.get('embedded_available') and not current.get('embedded_running'):
            await lx_embedded_manager.start()

    return updated


def _storage_test_hint(message: str, code: str = '') -> str:
    text_value = f'{code} {message}'.lower()
    if 'missing cookie' in text_value or 'config_invalid' in text_value:
        return '缺少登录态，先补 cookie / token 再测。'
    if 'cooling down' in text_value or 'rate_limited' in text_value:
        return '刚触发限流/冷却，等一会儿再测。'
    if 'provider not found' in text_value or 'provider_not_found' in text_value:
        return 'provider ID 不存在，检查默认 provider 和列表配置。'
    if '403' in text_value or '401' in text_value or 'unauthorized' in text_value:
        return '鉴权失败，八成是 token / cookie 失效了。'
    if 'not found' in text_value or '404' in text_value:
        return '路径或根目录配置不对，检查 root_path / root_dir_id。'
    return '看原始报错，优先排查鉴权、根路径和 provider 配置。'


def _build_storage_from_payload(payload: dict):
    runtime = get_runtime_settings()
    storage_providers_raw = merge_storage_providers_payload(runtime.get('storage_providers'), payload.get('storage_providers'))
    provider_rows = parse_storage_provider_rows(storage_providers_raw)
    if not provider_rows:
        raise HTTPException(status_code=400, detail='Storage provider 配置为空或格式不对')
    provider_id = str(payload.get('provider_id') or '').strip() or provider_rows[0].id
    storage = build_storage_service(provider_rows=provider_rows, default_provider_id=provider_id)
    return storage, provider_id


@router.post('/settings/storage/test')
async def test_storage_provider(payload: dict = Body(...)) -> dict:
    storage, provider_id = _build_storage_from_payload(payload)
    try:
        ref = storage.make_ref('/', provider_id=provider_id)
        stat = await storage.stat_async(ref)
        items = await storage.list_dir_async(ref)
        return {
            'ok': True,
            'provider_id': provider_id,
            'is_dir': bool(stat.is_dir) if stat else True,
            'path': stat.path if stat else '/',
            'count': len(items),
            'sample': [
                {
                    'name': str(item.get('name') or ''),
                    'path': str(item.get('path') or ''),
                    'is_dir': bool(item.get('is_dir')),
                }
                for item in items[:5]
            ],
            'message': f'{provider_id} 连通，根目录列出 {len(items)} 项',
        }
    except StorageError as exc:
        logger.warning('storage test failed provider=%s code=%s error=%s', provider_id, exc.code, exc)
        raise HTTPException(status_code=400, detail={
            'message': str(exc),
            'code': exc.code,
            'provider_id': exc.provider_id or provider_id,
            'hint': _storage_test_hint(str(exc), exc.code),
        })
    except Exception as exc:
        logger.exception('storage test crashed provider=%s', provider_id)
        raise HTTPException(status_code=400, detail={
            'message': f'测试失败: {exc}',
            'code': 'TEST_FAILED',
            'provider_id': provider_id,
            'hint': _storage_test_hint(str(exc), 'TEST_FAILED'),
        })


@router.post('/settings/quark-tv/login/start')
async def start_quark_tv_login(payload: dict = Body(...)) -> dict:
    storage, provider_id = _build_storage_from_payload(payload)
    provider = storage.get_provider(provider_id)
    if getattr(provider, 'provider_type', '') != 'quark_tv':
        raise HTTPException(status_code=400, detail='当前 provider 不是 quark_tv')
    try:
        data = await provider.start_login_async()
        if not data.get('qr_data') or not data.get('query_token'):
            raise HTTPException(status_code=400, detail='夸克TV登录码获取失败，返回数据不完整')
        return {
            'ok': True,
            'provider_id': provider_id,
            **data,
        }
    except StorageError as exc:
        raise HTTPException(status_code=400, detail={
            'message': str(exc),
            'code': exc.code,
            'provider_id': exc.provider_id or provider_id,
            'hint': '登录码获取失败，先检查网络和 quark_tv 配置。',
        })


@router.post('/settings/quark-tv/login/poll')
async def poll_quark_tv_login(payload: dict = Body(...)) -> dict:
    storage, provider_id = _build_storage_from_payload(payload)
    provider = storage.get_provider(provider_id)
    if getattr(provider, 'provider_type', '') != 'quark_tv':
        raise HTTPException(status_code=400, detail='当前 provider 不是 quark_tv')
    query_token = str(payload.get('query_token') or '').strip()
    try:
        data = await provider.poll_login_async(query_token)
        return {
            'provider_id': provider_id,
            **data,
        }
    except StorageError as exc:
        raise HTTPException(status_code=400, detail={
            'message': str(exc),
            'code': exc.code,
            'provider_id': exc.provider_id or provider_id,
            'hint': '轮询登录失败，确认是否已在夸克 App 中授权。',
        })


@router.post('/settings/115/login/start')
async def start_115_login(payload: dict = Body(...)) -> dict:
    from p115client import P115Client

    storage, provider_id = _build_storage_from_payload(payload)
    provider = storage.get_provider(provider_id)
    if getattr(provider, 'provider_type', '') != '115':
        raise HTTPException(status_code=400, detail='当前 provider 不是 115')
    app = str(payload.get('app') or getattr(provider, 'qrcode_source', '') or 'android').strip() or 'android'
    if app == 'linux':
        app = 'os_linux'
    try:
        resp = await asyncio.to_thread(P115Client.login_qrcode_token, app)
        data = resp.get('data') or {}
        uid = str(data.get('uid') or '').strip()
        qrcode = str(data.get('qrcode') or '') or (f'https://115.com/scan/dg-{uid}' if uid else '')
        if not uid or not qrcode:
            raise HTTPException(status_code=400, detail='115 登录码获取失败，返回数据不完整')
        return {
            'ok': True,
            'provider_id': provider_id,
            'app': app,
            'uid': uid,
            'time': data.get('time'),
            'sign': data.get('sign'),
            'qr_data': qrcode,
        }
    except Exception as exc:
        raise HTTPException(status_code=400, detail={
            'message': str(exc),
            'code': 'LOGIN_START_FAILED',
            'provider_id': provider_id,
            'hint': '115 登录码获取失败，先检查网络，必要时稍后重试。',
        })


def _extract_115_cookie(result: dict) -> str:
    payloads: list[dict] = []
    if isinstance(result, dict):
        payloads.append(result)
        data = result.get('data')
        if isinstance(data, dict):
            payloads.append(data)
            nested_cookie = data.get('cookie')
            if isinstance(nested_cookie, dict):
                payloads.append(nested_cookie)
        top_cookie = result.get('cookie')
        if isinstance(top_cookie, dict):
            payloads.append(top_cookie)

    for payload in payloads:
        cookie_parts = []
        for key in ('UID', 'CID', 'SEID', 'KID'):
            value = str(payload.get(key) or '').strip()
            if value:
                cookie_parts.append(f'{key}={value}')
        if cookie_parts:
            return '; '.join(cookie_parts)

    for payload in payloads:
        raw_cookie = payload.get('cookie')
        if isinstance(raw_cookie, str) and raw_cookie.strip():
            return raw_cookie.strip()

    return ''


@router.post('/settings/115/login/poll')
async def poll_115_login(payload: dict = Body(...)) -> dict:
    from p115client import P115Client

    storage, provider_id = _build_storage_from_payload(payload)
    provider = storage.get_provider(provider_id)
    if getattr(provider, 'provider_type', '') != '115':
        raise HTTPException(status_code=400, detail='当前 provider 不是 115')
    uid = str(payload.get('uid') or '').strip()
    sign = str(payload.get('sign') or '').strip()
    time_value = payload.get('time')
    app = str(payload.get('app') or getattr(provider, 'qrcode_source', '') or 'android').strip() or 'android'
    if app == 'linux':
        app = 'os_linux'
    if not uid or not sign or time_value in (None, ''):
        raise HTTPException(status_code=400, detail='115 登录轮询参数不完整')
    token_payload = {'uid': uid, 'time': int(time_value), 'sign': sign}
    try:
        status_resp = await asyncio.to_thread(P115Client.login_qrcode_scan_status, token_payload)
        status = int((status_resp.get('data') or {}).get('status') or -99)
        if status != 2:
            message_map = {
                0: '二维码等待扫码。',
                1: '已扫码，等你在 115 App 里确认。',
                -1: '二维码已过期，请重新获取。',
                -2: '二维码已取消，请重新获取。',
            }
            return {
                'provider_id': provider_id,
                'authorized': False,
                'status': status,
                'message': message_map.get(status, f'当前状态：{status}'),
            }
        result = await asyncio.to_thread(P115Client.login_qrcode_scan_result, uid, app=app)
        cookie = _extract_115_cookie(result)
        if not cookie:
            raise HTTPException(status_code=400, detail={
                'message': '115 登录成功但未取到 cookie',
                'code': 'COOKIE_MISSING',
                'provider_id': provider_id,
                'hint': '115 已授权，但返回体里没解析出 Cookie。需要兼容新的返回结构。',
                'raw_keys': sorted(list(result.keys())) if isinstance(result, dict) else [],
                'data_keys': sorted(list((result.get('data') or {}).keys())) if isinstance(result, dict) and isinstance(result.get('data'), dict) else [],
            })
        return {
            'provider_id': provider_id,
            'authorized': True,
            'status': status,
            'cookie': cookie,
            'cookie_len': len(cookie),
            'app': app,
            'message': '115 登录成功，已拿到 cookie。',
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail={
            'message': str(exc),
            'code': 'LOGIN_POLL_FAILED',
            'provider_id': provider_id,
            'hint': '115 轮询登录失败，确认是否已在 App 中授权。',
        })


@router.get('/lx/status')
def lx_status() -> dict:
    return lx_embedded_manager.status()


@router.get('/lx/custom-sources')
async def lx_custom_sources(request: Request) -> dict:
    username = _lx_target_username(request)
    data = await _lx_request('GET', '/api/custom-source/list', request=request, params={'username': username}, timeout=20.0)
    items = data if isinstance(data, list) else data.get('items') or data.get('list') or []
    return {'items': items, 'status': lx_embedded_manager.status()}


@router.post('/lx/custom-sources/import-url')
async def lx_import_custom_source_url(request: Request, payload: dict = Body(...)) -> dict:
    username = _lx_target_username(request)
    data = await _lx_request(
        'POST',
        '/api/custom-source/import',
        request=request,
        json_payload={
            'url': str(payload.get('url') or '').strip(),
            'filename': str(payload.get('filename') or '').strip(),
            'username': username,
            'allowUnsafeVM': bool(payload.get('allowUnsafeVM')),
        },
        timeout=60.0,
    )
    return data


@router.post('/lx/custom-sources/upload-js')
async def lx_upload_custom_source_js(request: Request, file: UploadFile = File(...), allowUnsafeVM: str = Form('false')) -> dict:
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail='Empty file')
    try:
        script_text = content.decode('utf-8')
    except UnicodeDecodeError:
        try:
            script_text = content.decode('utf-8-sig')
        except UnicodeDecodeError:
            raise HTTPException(status_code=400, detail='JS 文件不是有效的 UTF-8 文本')
    username = _lx_target_username(request)
    data = await _lx_request(
        'POST',
        '/api/custom-source/upload',
        request=request,
        json_payload={
            'filename': file.filename or 'custom-source.js',
            'content': script_text,
            'username': username,
            'allowUnsafeVM': str(allowUnsafeVM).lower() in {'1', 'true', 'yes', 'on'},
        },
        timeout=60.0,
    )
    return data


@router.post('/lx/custom-sources/toggle')
async def lx_toggle_custom_source(request: Request, payload: dict = Body(...)) -> dict:
    username = _lx_target_username(request)
    data = await _lx_request(
        'POST',
        '/api/custom-source/toggle',
        request=request,
        json_payload={
            'id': payload.get('id') or payload.get('sourceId'),
            'enabled': payload.get('enabled'),
            'username': username,
            'allowUnsafeVM': bool(payload.get('allowUnsafeVM')),
        },
        timeout=30.0,
    )
    return data


@router.post('/lx/custom-sources/delete')
async def lx_delete_custom_source(request: Request, payload: dict = Body(...)) -> dict:
    username = _lx_target_username(request)
    data = await _lx_request(
        'POST',
        '/api/custom-source/delete',
        request=request,
        json_payload={
            'id': payload.get('id') or payload.get('sourceId'),
            'username': username,
        },
        timeout=30.0,
    )
    return data


@router.get('/music/metadata-search')
async def metadata_search(keyword: str) -> dict:
    from app.services.app_settings import get_runtime_settings
    import httpx

    query = str(keyword or '').strip()
    if not query:
        return {"results": []}

    settings = get_runtime_settings()
    lx_url = settings.get("lx_server_url", "").rstrip('/')
    if not lx_url:
        raise HTTPException(status_code=400, detail="LX-Server URL not configured")

    platforms = ['tx', 'wy', 'kw', 'kg', 'mg']
    results: list[dict] = []
    seen_keys: set[str] = set()
    errors: list[str] = []

    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            for plat in platforms:
                try:
                    res = await client.get(
                        f"{lx_url}/api/music/search",
                        params={"source": plat, "name": query, "page": 1},
                    )
                    res.raise_for_status()
                    data = res.json()
                    if isinstance(data, list):
                        items = data
                    elif isinstance(data, dict):
                        nested = data.get("data")
                        items = data.get("list") or (nested.get("list") if isinstance(nested, dict) else None) or []
                    else:
                        items = []
                    for item in items:
                        key = f"{item.get('source') or plat}:{item.get('songmid') or item.get('id') or item.get('name')}:{item.get('singer')}"
                        if key in seen_keys:
                            continue
                        seen_keys.add(key)
                        normalized = dict(item)
                        normalized['source'] = normalized.get('source') or plat
                        results.append(normalized)
                        if len(results) >= 30:
                            return {"results": results}
                except Exception as inner:
                    errors.append(f"{plat}: {inner}")
                    continue
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch from LX-Server: {str(e)}")

    if results:
        return {"results": results}
    detail = '; '.join(errors[:3]) if errors else 'empty response'
    raise HTTPException(status_code=500, detail=f"Failed to fetch from LX-Server: {detail}")

@router.get('/music/metadata-lyric')
async def metadata_lyric(source: str, songId: str) -> dict:
    from app.services.app_settings import get_runtime_settings
    import httpx
    settings = get_runtime_settings()
    lx_url = settings.get("lx_server_url", "").rstrip('/')
    if not lx_url:
        raise HTTPException(status_code=400, detail="LX-Server URL not configured")
        
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            res = await client.get(f"{lx_url}/api/music/lyric", params={"source": source, "songId": songId})
            res.raise_for_status()
            data = res.json()
            lyric = data.get("lrc") or data.get("lyric") or data.get("lxlyric") or data.get("data", {}).get("lrc") or ''
            return {"lrc": lyric}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch from LX-Server: {str(e)}")

@router.get('/alist/search')
def alist_search(keyword: str) -> dict:
    storage = get_storage_service()
    provider_id = storage.default_provider_id
    provider = storage.get_provider(provider_id)
    return {
        'items': storage.search(keyword, provider_id=provider_id),
        'provider_id': provider_id,
        'provider_type': provider.provider_type,
    }


@router.get('/subsonic/capabilities')
def subsonic_capabilities() -> dict:
    return {'items': get_subsonic_items()}

def _serialize_playlist_summary(item: dict | None) -> dict | None:
    if not item:
        return None
    return {
        'id': str(item.get('id') or ''),
        'name': str(item.get('name') or ''),
        'comment': str(item.get('comment') or ''),
        'owner': str(item.get('owner') or 'admin'),
        'public': bool(item.get('public')),
        'is_readonly': bool(item.get('is_readonly')),
        'scope': str(item.get('scope') or 'private_user'),
        'song_count': int(item.get('song_count') or 0),
        'duration': int(item.get('duration') or 0),
        'created_at': dt_to_iso(item.get('created_at')),
        'updated_at': dt_to_iso(item.get('updated_at')),
    }



def _serialize_playlist_detail(item: dict | None) -> dict | None:
    summary = _serialize_playlist_summary(item)
    if not summary:
        return None
    summary['entries'] = [
        {
            'id': str(entry.get('id') or ''),
            'title': str(entry.get('title') or ''),
            'artist': str(entry.get('artist') or ''),
            'album': str(entry.get('album') or ''),
            'dur': int(entry.get('dur') or 0),
            'sort_order': int(entry.get('sort_order') or 0),
        }
        for entry in (item.get('entries') or [])
    ]
    return summary


@router.get('/playlists')
def playlists() -> dict:
    return {'playlists': [_serialize_playlist_summary(item) for item in list_playlists()]}


@router.get('/playlists/{playlist_id}')
def playlist_detail(playlist_id: str) -> dict:
    return {'playlist': _serialize_playlist_detail(get_playlist(playlist_id))}


@router.post('/playlists')
def create_playlist_api(payload: dict = Body(...)) -> dict:
    name = str(payload.get('name', '')).strip()
    song_ids = payload.get('song_ids', []) or []
    if not name:
        return {'message': '歌单名不能为空', 'playlist': None}
    playlist = create_playlist(name=name, song_ids=[str(song_id) for song_id in song_ids])
    return {'message': '歌单已创建', 'playlist': _serialize_playlist_detail(playlist)}


@router.post('/playlists/import')
async def import_playlist_api(payload: dict = Body(...)) -> dict:
    url = str(payload.get('url', '')).strip()
    if not url:
        raise HTTPException(status_code=400, detail="链接不能为空")
        
    from app.services.curation_service import import_lx_playlist
    try:
        result = await import_lx_playlist(url)
        return {
            'message': f"成功导入歌单「{result['name']}」，共 {result['count']} 首曲目",
            'playlist_id': result['id']
        }
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"Error importing playlist: {e}", exc_info=True)
        raise HTTPException(status_code=400, detail=str(e))

@router.post('/playlists/{playlist_id}/autofill')
async def trigger_playlist_autofill_api(playlist_id: str) -> dict:
    runtime = get_runtime_settings()
    quality = str(runtime.get('autofill_quality') or 'flac').strip() or 'flac'
    upload_dir = str(runtime.get('autofill_path') or '/MusicOn_AutoDownload/').strip() or '/MusicOn_AutoDownload/'

    playlist = get_playlist(playlist_id)
    if not playlist:
        raise HTTPException(status_code=404, detail='歌单不存在')

    with engine.connect() as conn:
        rows = conn.execute(
            text('''
                SELECT DISTINCT s.id, s.title, s.artist, s.raw_url
                FROM playlist_tracks pt
                JOIN songs s ON s.id = pt.song_id
                WHERE pt.playlist_id = :playlist_id
                  AND s.fmt = 'VIRTUAL'
                ORDER BY s.title ASC, s.artist ASC
            '''),
            {'playlist_id': playlist_id}
        ).mappings().all()
    target_songs = [dict(row) for row in rows]
    if not target_songs:
        return {'message': f'歌单「{playlist.get("name") or playlist_id}」里没有待补全的 VIRTUAL 曲目', 'task_id': ''}

    task = create_pending_scan_task(
        path=f'歌单补全: {playlist.get("name") or playlist_id} ({len(target_songs)} 首)',
        mode='autofill',
        concurrency=1,
        batch_size=1,
        source='autofill',
    )

    async def runner() -> None:
        current = await asyncio.to_thread(get_task, task['id'])
        logs = list(current.get('logs', [])) if current else []
        logs.append({'level': 'info', 'message': f'[RUN] 歌单定向补全已接单：{playlist.get("name") or playlist_id}，共 {len(target_songs)} 首 VIRTUAL'})
        await asyncio.to_thread(update_scan_task, task['id'], status='running', source='autofill', logs=logs)

        async def progress_callback(**fields) -> None:
            current_task = await asyncio.to_thread(get_task, task['id'])
            current_logs = list(current_task.get('logs', [])) if current_task else []
            extra_logs = fields.pop('logs', []) or []
            if extra_logs:
                current_logs.extend(extra_logs)
                fields['logs'] = current_logs
            await asyncio.to_thread(update_scan_task, task['id'], **fields)

        try:
            from app.services.auto_fill_service import run_auto_fill_task
            await run_auto_fill_task(quality, upload_dir, progress_callback=progress_callback, task_id=task['id'], target_songs=target_songs)
        except Exception as exc:
            failed = await asyncio.to_thread(get_task, task['id'])
            failed_logs = list(failed.get('logs', [])) if failed else logs
            failed_logs.append({'level': 'warn', 'message': f'[FAIL] 歌单定向补全执行失败：{exc}'})
            await asyncio.to_thread(update_scan_task, task['id'], status='failed', source='failed', logs=failed_logs)

    asyncio.create_task(runner())
    return {'message': f'歌单「{playlist.get("name") or playlist_id}」定向补全任务已启动，共 {len(target_songs)} 首', 'task_id': task['id']}


@router.patch('/playlists/{playlist_id}')
def update_playlist_api(playlist_id: str, payload: dict = Body(...)) -> dict:
    playlist = update_playlist(
        playlist_id=playlist_id,
        name=str(payload['name']).strip() if payload.get('name') is not None else None,
        comment=str(payload['comment']) if payload.get('comment') is not None else None,
        public=bool(payload['public']) if payload.get('public') is not None else None,
        is_readonly=bool(payload['is_readonly']) if payload.get('is_readonly') is not None else None,
        scope=str(payload['scope']).strip() if payload.get('scope') is not None else None,
        owner=str(payload['owner']).strip() if payload.get('owner') is not None else None,
        song_ids_to_add=[str(song_id) for song_id in (payload.get('song_ids_to_add', []) or [])],
        song_ids_to_remove=[str(song_id) for song_id in (payload.get('song_ids_to_remove', []) or [])],
        song_indexes_to_remove=[int(index) for index in (payload.get('song_indexes_to_remove', []) or [])],
    )
    return {'message': '歌单已更新' if playlist else '歌单不存在', 'playlist': _serialize_playlist_detail(playlist)}


@router.delete('/playlists/{playlist_id}')
def delete_playlist_api(playlist_id: str) -> dict:
    deleted = delete_playlist(playlist_id)
    return {'message': '歌单已删除' if deleted else '歌单不存在', 'deleted': deleted}


@router.post('/playlists/system/defaults')
def create_default_system_playlists_api() -> dict:
    playlists = [_serialize_playlist_detail(item) for item in ensure_default_system_playlists()]
    return {'message': '系统公共歌单已生成', 'playlists': playlists}


@router.get('/subsonic/users')
def subsonic_users() -> dict:
    users = [
        {
            'id': int(item.get('id') or 0),
            'username': str(item.get('username') or ''),
            'role': str(item.get('role') or 'user'),
            'enabled': bool(item.get('enabled')),
            'created_at': dt_to_iso(item.get('created_at')),
            'updated_at': dt_to_iso(item.get('updated_at')),
        }
        for item in list_subsonic_users()
    ]
    return {'users': users}


@router.post('/subsonic/users')
def create_subsonic_user_api(payload: dict = Body(...)) -> dict:
    username = str(payload.get('username') or '').strip()
    password = str(payload.get('password') or '')
    role = str(payload.get('role') or 'user').strip().lower()
    enabled = bool(payload.get('enabled', True))
    user = create_subsonic_user(username=username, password=password, role=role, enabled=enabled)
    if not user:
        return {'message': '用户名和密码不能为空', 'user': None}
    return {
        'message': 'Subsonic 账号已保存',
        'user': {
            'id': int(user.get('id') or 0),
            'username': str(user.get('username') or ''),
            'role': str(user.get('role') or 'user'),
            'enabled': bool(user.get('enabled')),
        },
    }


@router.patch('/subsonic/users/{username}')
def update_subsonic_user_api(username: str, payload: dict = Body(...)) -> dict:
    user = update_subsonic_user(
        username=username,
        password=payload.get('password'),
        role=payload.get('role'),
        enabled=payload.get('enabled'),
    )
    if not user:
        return {'message': '账号不存在', 'user': None}
    return {
        'message': 'Subsonic 账号已更新',
        'user': {
            'id': int(user.get('id') or 0),
            'username': str(user.get('username') or ''),
            'role': str(user.get('role') or 'user'),
            'enabled': bool(user.get('enabled')),
        },
    }


@router.delete('/subsonic/users/{username}')
def delete_subsonic_user_api(username: str) -> dict:
    deleted = delete_subsonic_user(username)
    return {'message': 'Subsonic 账号已删除' if deleted else '账号不存在', 'deleted': deleted}


@router.get('/auth/subsonic-token')
def auth_subsonic_token(password: str, salt: str = 'browsercheck') -> dict:
    runtime = get_runtime_settings()
    raw_password = password or str(runtime.get('subsonic_pass') or settings.subsonic_pass)
    token = hashlib.md5((raw_password + salt).encode()).hexdigest()
    return {'salt': salt, 'token': token}


from app.services.curation_service import generate_curated_playlists

@router.post('/curation/trigger')
async def trigger_curation(payload: dict = Body(None)) -> dict:
    # Trigger background curation
    if payload and payload.get('lx_server_url'):
        update_settings({'lx_server_url': payload['lx_server_url']})
    asyncio.create_task(generate_curated_playlists(trigger_autofill=False, trigger_source='manual'))
    return {'message': '榜单更新任务已在后台启动'}


@router.get('/curation/history')
def get_curation_history(limit: int = 50) -> dict:
    safe_limit = max(1, min(limit, 200))
    with engine.connect() as conn:
        rows = conn.execute(
            text('''
                SELECT id, run_id, playlist_id, playlist_name, trigger_source, status,
                       track_count, added_count, removed_count, changed_count,
                       summary, error, created_at
                FROM curation_update_history
                WHERE created_at >= :since
                ORDER BY created_at DESC
                LIMIT :limit
            '''),
            {'limit': safe_limit, 'since': utc_days_ago(7)}
        ).mappings().all()
    items = []
    for row in rows:
        summary = row.get('summary') or {}
        if isinstance(summary, str):
            try:
                summary = json.loads(summary)
            except Exception:
                summary = {}
        items.append({
            'id': row.get('id'),
            'run_id': row.get('run_id'),
            'playlist_id': row.get('playlist_id'),
            'playlist_name': row.get('playlist_name'),
            'trigger_source': row.get('trigger_source'),
            'status': row.get('status'),
            'track_count': int(row.get('track_count') or 0),
            'added_count': int(row.get('added_count') or 0),
            'removed_count': int(row.get('removed_count') or 0),
            'changed_count': int(row.get('changed_count') or 0),
            'summary': summary,
            'error': row.get('error') or '',
            'created_at': str(row.get('created_at') or ''),
        })
    return {'items': items}
