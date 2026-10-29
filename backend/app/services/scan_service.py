from __future__ import annotations

import base64
import asyncio
from collections import deque
import hashlib
import json
from datetime import datetime, timezone
from io import BytesIO
import inspect
import time
from typing import Any
import uuid
import gc

from mutagen import File as MutagenFile

from app.services.storage import get_storage_service


def _looks_like_dir(item: dict[str, Any]) -> bool:
    if 'is_dir' in item:
        return bool(item.get('is_dir'))
    if item.get('type') == 1:
        return True
    if item.get('fid'):
        return False
    if item.get('cid') and not item.get('fid'):
        return True
    return False

AUDIO_EXTENSIONS = {
    '.flac': 'FLAC',
    '.mp3': 'MP3',
    '.m4a': 'AAC',
    '.aac': 'AAC',
    '.opus': 'OPUS',
    '.ogg': 'OPUS',
    '.wav': 'WAV',
    '.ape': 'APE',
    '.alac': 'ALAC',
    '.dsf': 'DSF',
    '.dff': 'DFF',
}
MAX_SCAN_ITEMS = 50000
SMALL_RANGE_END = 65535
FULL_RANGE_END = 1024 * 1024 * 8 - 1
FLAC_RANGE_END = 1024 * 1024 * 16 - 1
DEFAULT_SCAN_WORKERS = 8
MAX_SCAN_WORKERS = 16
SCAN_STATE_FILENAME = '.musicon-scan-state.json'

# 用于保存已被用户取消的任务 ID
CANCELLED_TASKS: set[str] = set()

def cancel_scan_task(task_id: str) -> None:
    CANCELLED_TASKS.add(task_id)
    # 主动触发一次垃圾回收，释放中断任务后残留的大块内存
    gc.collect()

def _join_alist_path(parent: str, name: str) -> str:
    parent = (parent or '').strip()
    if not parent or parent == '/':
        return '/' + name.strip('/')
    return parent.rstrip('/') + '/' + name.strip('/')


def _split_path(path: str) -> tuple[str, str, str]:
    parts = [item for item in path.strip('/').split('/') if item]
    title = parts[-1] if parts else 'Unknown'
    artist = parts[-3] if len(parts) >= 3 else 'Unknown Artist'
    album = parts[-2] if len(parts) >= 2 else 'Unknown Album'
    title = title.rsplit('.', 1)[0]
    return artist, album, title


def _scan_state_path(root_path: str) -> str:
    return _join_alist_path(root_path, SCAN_STATE_FILENAME)


def _dir_snapshot(items: list[dict[str, Any]]) -> dict[str, Any]:
    rows = []
    size_sum = 0
    latest_mtime = ''
    for item in items:
        name = str(item.get('name') or '')
        if not name or name == SCAN_STATE_FILENAME or name.startswith('.musicon-'):
            continue
        is_dir = _looks_like_dir(item)
        size = int(item.get('size') or 0)
        modified = str(item.get('modified') or item.get('updated_at') or item.get('created') or '')
        rows.append(f"{name}|{1 if is_dir else 0}|{size}|{modified}")
        if not is_dir:
            size_sum += size
        if modified and modified > latest_mtime:
            latest_mtime = modified
    rows.sort()
    raw = '\n'.join(rows)
    return {
        'children_hash': hashlib.sha1(raw.encode('utf-8')).hexdigest(),
        'child_count': len(rows),
        'size_sum': size_sum,
        'latest_mtime': latest_mtime,
    }


async def _read_scan_state(_client: Any, root_path: str) -> dict[str, Any]:
    from app.services.db_data import get_scan_state
    return await asyncio.to_thread(get_scan_state, root_path)


async def _write_scan_state(_client: Any, root_path: str, state: dict[str, Any]) -> None:
    from app.services.db_data import save_scan_state
    await asyncio.to_thread(save_scan_state, root_path, state)


def _copy_snapshot_subtree(prior_dirs: dict[str, Any], target_dirs: dict[str, Any], root_dir: str) -> None:
    prefix = root_dir.rstrip('/') + '/'
    for path, snapshot in prior_dirs.items():
        if path == root_dir or path.startswith(prefix):
            target_dirs[path] = snapshot


def _repair_mojibake(text: str) -> str:
    raw = str(text or '')
    if not raw:
        return ''
    candidates = [raw]
    for encoding in ('gb18030', 'big5', 'utf-8'):
        try:
            repaired = raw.encode('latin1').decode(encoding).strip()
        except Exception:
            continue
        if repaired and repaired not in candidates:
            candidates.append(repaired)
    def score(value: str) -> tuple[int, int, int]:
        weird = sum(ch in 'ÕÅÖÜÔøÍô¦§ª´¿ÏËÈÙ³þ' for ch in value)
        cjk = sum('\u4e00' <= ch <= '\u9fff' for ch in value)
        return (weird, -cjk, len(value))
    return min(candidates, key=score)


def _looks_like_mojibake(value: str, fallback: str = '') -> bool:
    raw = str(value or '').strip()
    fb = str(fallback or '').strip()
    if not raw:
        return False
    weird_hits = sum(ch in 'ÕÅÖÜÔøÍô¦§ª´¿ÏËÈÙ³þ' for ch in raw)
    replacement_hits = raw.count('�')
    cjk_hits = sum('\u4e00' <= ch <= '\u9fff' for ch in raw)
    latin_ext_hits = sum(0x00C0 <= ord(ch) <= 0x024F for ch in raw)
    if replacement_hits > 0 or weird_hits > 0:
        return True
    if fb and latin_ext_hits > 0 and cjk_hits > 0:
        return True
    if fb and cjk_hits > 0 and not any('\u4e00' <= ch <= '\u9fff' for ch in fb):
        return True
    return False


def _safe_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, list) and value:
        value = value[0]
    if isinstance(value, bytes):
        for encoding in ('utf-8', 'gb18030', 'big5', 'latin1'):
            try:
                return value.decode(encoding)
            except Exception:
                continue
    return str(value).strip() or None


def _extract_tags(audio: Any) -> dict[str, Any]:
    tags = getattr(audio, 'tags', None)
    result: dict[str, Any] = {}
    if not tags:
        return result

    tag_groups = {
        'title': ['TIT2', 'TITLE', '\xa9nam'],
        'artist': ['TPE1', 'ARTIST', '\xa9ART'],
        'album': ['TALB', 'ALBUM', '\xa9alb'],
        'genre': ['TCON', 'GENRE', '\xa9gen'],
    }
    for field, keys in tag_groups.items():
        for key in keys:
            try:
                if key in tags:
                    value = tags.get(key)
                    text = _safe_text(getattr(value, 'text', value))
                    if text:
                        result[field] = text
                        break
            except Exception:
                continue

    cover_bytes = None
    try:
        if 'APIC:' in tags:
            cover_bytes = getattr(tags.get('APIC:'), 'data', None)
        if not cover_bytes:
            for key in tags.keys():
                if str(key).startswith('APIC'):
                    cover_bytes = getattr(tags.get(key), 'data', None)
                    if cover_bytes:
                        break
    except Exception:
        pass

    try:
        if not cover_bytes and 'metadata_block_picture' in tags:
            value = tags.get('metadata_block_picture')
            if isinstance(value, list) and value:
                cover_bytes = getattr(value[0], 'data', None)
    except Exception:
        pass

    if not cover_bytes:
        pictures = getattr(audio, 'pictures', None)
        if pictures:
            cover_bytes = getattr(pictures[0], 'data', None)
    if cover_bytes:
        result['cover_bytes'] = cover_bytes

    return result


def _decode_synchsafe(data: bytes) -> int:
    if len(data) != 4:
        return 0
    return ((data[0] & 0x7F) << 21) | ((data[1] & 0x7F) << 14) | ((data[2] & 0x7F) << 7) | (data[3] & 0x7F)


def _flac_header_needed(data: bytes) -> int | None:
    if not data.startswith(b'fLaC'):
        return None
    pos = 4
    total = 4
    while pos + 4 <= len(data):
        header = data[pos]
        is_last = bool(header & 0x80)
        block_len = int.from_bytes(data[pos + 1:pos + 4], 'big')
        total = pos + 4 + block_len
        if is_last:
            return total
        pos += 4 + block_len
    # 如果缓冲读完了还没找到 last block，说明遇到了超级大块，直接返回目前算出来的总长度加上一个安全缓冲
    return total + 102400


def _planned_range_end(full_path: str, head: bytes) -> int:
    lower = full_path.lower()
    if lower.endswith('.mp3') and head.startswith(b'ID3') and len(head) >= 10:
        tag_size = _decode_synchsafe(head[6:10]) + 10
        return min(max(tag_size + 65536, 131072), FULL_RANGE_END)
    if lower.endswith('.flac'):
        flac_need = _flac_header_needed(head)
        if flac_need:
            # 稍微再放大一点 FLAC 的容错空间，防止 block 数据读取不完整导致 mutagen 解析失败
            return min(max(flac_need + 262144, 524288), FLAC_RANGE_END)
    if lower.endswith(('.m4a', '.aac', '.opus', '.ogg')):
        return min(524287, FULL_RANGE_END)
    return FULL_RANGE_END


def _resolve_worker_count(concurrency: int | None = None) -> int:
    if concurrency is not None and int(concurrency) > 0:
        return int(concurrency)
    # 由于云盘 WAF 的严格限制（如 115 的 405 阻断），为了防 CC 默认将并发数压至 1
    return 1


def _stable_song_id(full_path: str) -> str:
    digest = hashlib.sha1(full_path.encode('utf-8')).hexdigest()[:16]
    return f'alist-{digest}'


import concurrent.futures

# 专用线程池：用于处理重度的音频文件解析和封面图片压缩
# 限制在 4 个线程，保证在任何云端低配机器上都不会吃光 CPU，也不会阻塞 FastAPI 的默认线程池（影响播放）
SCAN_THREAD_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix='ScanWorker')


def _parse_and_extract(content: bytes) -> dict[str, Any]:
    try:
        from mutagen import File as MutagenFile
        from io import BytesIO
        audio = MutagenFile(BytesIO(content))
    except Exception:
        audio = None

    if audio is None:
        return {}

    tags = _extract_tags(audio)
    info = getattr(audio, 'info', None)
    duration = 0
    if info and getattr(info, 'length', None):
        duration = int(round(float(info.length)))

    cover_bytes = tags.pop('cover_bytes', None)
    cover_path = ''
    if cover_bytes:
        from app.services.cover_cache import save_cover_bytes
        cover_path = save_cover_bytes(cover_bytes)

    return {
        'duration': duration,
        'cover_path': cover_path,
        **tags
    }


async def _probe_audio_metadata(storage: Any, full_path: str, provider_id: str | None = None) -> dict[str, Any]:
    ref = storage.make_ref(full_path, provider_id=provider_id)
    raw_url = await storage.get_download_url_async(ref)
    head = await storage.read_range_async(ref, 0, SMALL_RANGE_END)
    planned_end = _planned_range_end(full_path, head)
    content = head if planned_end <= SMALL_RANGE_END else await storage.read_range_async(ref, 0, planned_end)
    
    metadata = await asyncio.get_running_loop().run_in_executor(SCAN_THREAD_POOL, _parse_and_extract, content)
    
    return {
        'raw_url': raw_url,
        'range_bytes': len(content),
        **metadata,
    }


async def _map_alist_item(item: dict[str, Any], idx: int, parent_path: str, storage: Any, provider_id: str | None = None) -> tuple[dict[str, Any] | None, list[dict[str, str]]]:
    await asyncio.sleep(0.02)

    name = str(item.get('name') or '')
    if not name or name == SCAN_STATE_FILENAME or name.startswith('.musicon-'):
        return None, []
    lower = name.lower()
    fmt = None
    for ext, mapped in AUDIO_EXTENSIONS.items():
        if lower.endswith(ext):
            fmt = mapped
            break
    if not fmt:
        return None, []
    full_path = _join_alist_path(parent_path, name)
    fallback_artist, fallback_album, fallback_title = _split_path(full_path)
    metadata: dict[str, Any] = {}
    item_logs: list[dict[str, str]] = []
    try:
        metadata = await _probe_audio_metadata(storage, full_path, provider_id=provider_id)
    except Exception as exc:
        cause = repr(exc.__cause__) if getattr(exc, '__cause__', None) is not None else 'None'
        context = repr(exc.__context__) if getattr(exc, '__context__', None) is not None else 'None'
        item_logs.append({
            'level': 'warn',
            'message': (
                f'[WARN] 元数据解析失败，回退路径推断：{name} '
                f'(type={type(exc).__name__}, exc={exc!r}, cause={cause}, context={context})'
            ),
        })
    title = str(metadata.get('title') or '')
    if title:
        title = _repair_mojibake(title)
    if not title or _looks_like_mojibake(title, fallback_title):
        title = fallback_title

    artist = str(metadata.get('artist') or '')
    if artist:
        artist = _repair_mojibake(artist)
    if not artist or _looks_like_mojibake(artist, fallback_artist):
        artist = fallback_artist

    album = str(metadata.get('album') or '')
    if album:
        album = _repair_mojibake(album)
    if not album or _looks_like_mojibake(album, fallback_album):
        album = fallback_album

    genre = _repair_mojibake(str(metadata.get('genre') or ''))
    return {
        'id': _stable_song_id(full_path),
        'title': title,
        'artist': artist,
        'album': album,
        'genre': genre,
        'fmt': fmt,
        'dur': int(metadata.get('duration') or 0),
        'path': full_path,
        'plays': 0,
        'raw_url': metadata.get('raw_url', ''),
        'provider_id': str(provider_id or ''),
        'cover_path': metadata.get('cover_path', ''),
        'range_bytes': int(metadata.get('range_bytes') or 0),
    }, item_logs


async def import_single_alist_file(full_path: str, *, provider_id: str | None = None) -> dict[str, Any] | None:
    storage = get_storage_service()
    full_path = storage.normalize_path(full_path, provider_id=provider_id)
    parent_path = full_path.rsplit('/', 1)[0] if '/' in full_path else '/'
    filename = full_path.rsplit('/', 1)[-1]
    mapped, _ = await _map_alist_item({'name': filename}, 1, parent_path, storage, provider_id=provider_id)
    if not mapped:
        return None
    from app.services.db_data import _upsert_songs_batch, engine
    from sqlalchemy import text
    with engine.connect() as conn:
        row = conn.execute(text("SELECT plays FROM songs WHERE id = :id"), {'id': mapped['id']}).mappings().first()
        if row:
            mapped['plays'] = row['plays']
    await asyncio.to_thread(_upsert_songs_batch, [mapped])
    def finalize() -> int:
        from app.services.db_data import cleanup_virtual_songs
        with engine.begin() as connection:
            return cleanup_virtual_songs(connection)
    deleted_virtuals = await asyncio.to_thread(finalize)
    return {'song': mapped, 'deleted_virtuals': deleted_virtuals}


async def _call_progress(progress_callback: Any | None, **fields: Any) -> None:
    if not progress_callback:
        return
    result = progress_callback(**fields)
    if inspect.isawaitable(result):
        await result


async def _walk_alist(
    path: str,
    logs: list[dict[str, str]],
    mode: str,
    task_id: str | None = None,
    concurrency: int | None = None,
    progress_callback: Any | None = None,
    provider_id: str | None = None,
    root_object_id: str | None = None,
) -> tuple[list[dict[str, Any]], int, int, int, int]:
    worker_count = _resolve_worker_count(concurrency)
    storage = get_storage_service()
    path = storage.normalize_path(path, provider_id=provider_id)
    queue = deque([(path, root_object_id)])
    queued_dirs = {path}
    files: list[dict[str, Any]] = []
    visited_dirs = 0
    seq = 1
    discovered_audio = 0
    skipped_dirs = 0
    skipped_files = 0
    parse_errors: list[str] = []

    semaphore = asyncio.Semaphore(worker_count)
    pending: set[asyncio.Task] = set()
    scan_state = await _read_scan_state(storage, path)
    prior_dirs = dict(scan_state.get('dirs') or {}) if mode == 'incremental' else {}
    next_dirs: dict[str, Any] = {}
    from app.services.db_data import get_file_index, sync_file_index, list_dirty_dirs, clear_dirty_dirs_under
    current_file_index = await asyncio.to_thread(get_file_index, path) if mode == 'incremental' else {}
    dirty_dirs = set(await asyncio.to_thread(list_dirty_dirs, path)) if mode == 'incremental' else set()
    if mode == 'incremental' and dirty_dirs:
        priority_dirs = sorted(dirty_dirs, key=lambda p: (p.count('/'), p))
        for dirty_dir in reversed(priority_dirs):
            if dirty_dir not in queued_dirs:
                queue.append((dirty_dir, None))
                queued_dirs.add(dirty_dir)
    next_file_index: list[dict[str, Any]] = []

    async def emit_progress(message: str) -> None:
        if task_id and task_id in CANCELLED_TASKS:
            raise asyncio.CancelledError('任务被手动取消')
        total_seen = max(discovered_audio, len(files))
        await _call_progress(
            progress_callback,
            status='running',
            source='pending',
            count=total_seen,
            total=total_seen,
            processed=len(files),
            duration='',
            end_time='',
            logs=[{'level': 'info', 'message': message}],
        )

    async def schedule(item: dict[str, Any], idx: int, parent_path: str) -> tuple[int, dict[str, Any] | None, list[dict[str, str]]]:
        async with semaphore:
            mapped, item_logs = await _map_alist_item(item, idx, parent_path, storage, provider_id=provider_id)
            return idx, mapped, item_logs

    async def flush(force_log: bool = False) -> None:
        nonlocal pending
        if not pending:
            return
        done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)

        from app.services.db_data import _upsert_songs_batch
        batch_to_insert = []

        for future in done:
            try:
                _idx, mapped, item_logs = future.result()
                if item_logs:
                    logs.extend(item_logs)
                if mapped:
                    files.append(mapped)
                    batch_to_insert.append(mapped)
                    if force_log or len(files) == 1 or len(files) % 5 == 0:
                        await emit_progress(f'[RUN] 解析并落库进度 {len(files)}/{max(discovered_audio, len(files))}，目录 {visited_dirs} 个')
            except Exception as exc:
                if len(parse_errors) < 5:
                    parse_errors.append(f'{exc}')

        if batch_to_insert:
            ready_batch = []
            id_list = [str(item.get('id') or '') for item in batch_to_insert if str(item.get('id') or '')]
            plays_map: dict[str, int] = {}
            if id_list:
                from app.services.db_data import engine
                from sqlalchemy import text, bindparam
                with engine.connect() as conn:
                    rows = conn.execute(
                        text("SELECT id, plays FROM songs WHERE id IN :ids").bindparams(bindparam('ids', expanding=True)),
                        {'ids': id_list},
                    ).mappings().all()
                    plays_map = {str(row['id']): int(row.get('plays') or 0) for row in rows}
            for item in batch_to_insert:
                db_item = dict(item)
                if db_item['id'] in plays_map:
                    db_item['plays'] = plays_map[db_item['id']]
                ready_batch.append(db_item)
            await asyncio.to_thread(_upsert_songs_batch, ready_batch)

    while queue and seq <= MAX_SCAN_ITEMS:
        if task_id and task_id in CANCELLED_TASKS:
            raise asyncio.CancelledError('任务被手动取消')
        current, current_object_id = queue.pop()
        visited_dirs += 1
        if visited_dirs % 5 == 0:
            await emit_progress(f'[RUN] 正在遍历目录: {current} (已遍历 {visited_dirs} 个)')

        try:
            await asyncio.sleep(0.05)
            force_refresh = False
            if mode == 'incremental' and dirty_dirs:
                prefix = current.rstrip('/') + '/'
                force_refresh = current in dirty_dirs or any(dir_path.startswith(prefix) for dir_path in dirty_dirs)
            items = await storage.list_dir_async(storage.make_ref(current, provider_id=provider_id, object_id=current_object_id), refresh=force_refresh)
            snapshot = _dir_snapshot(items)
            next_dirs[current] = {
                **snapshot,
                'last_scan_at': datetime.now(timezone.utc).isoformat(),
                'last_task_id': task_id or '',
            }
            prior = prior_dirs.get(current) if mode == 'incremental' else None
            if prior and prior.get('children_hash') == snapshot['children_hash'] and prior.get('child_count') == snapshot['child_count']:
                child_dirs: list[tuple[str, str | None]] = []
                for item in items:
                    name = str(item.get('name') or '')
                    if not name or name in {'.', '..'}:
                        continue
                    is_dir = _looks_like_dir(item)
                    if is_dir:
                        child_dirs.append((
                            _join_alist_path(current, name),
                            str(item.get('object_id') or item.get('id') or item.get('cid') or '') or None,
                        ))
                if child_dirs:
                    for child_dir, child_object_id in reversed(child_dirs):
                        if child_dir not in queued_dirs:
                            queue.append((child_dir, child_object_id))
                            queued_dirs.add(child_dir)
                    if visited_dirs % 10 == 0 or skipped_dirs <= 3:
                        await emit_progress(f'[SKIP-DIR] 当前层未变化，跳过本层文件，继续探测子目录：{current} (子目录 {len(child_dirs)} 个)')
                    continue
                refreshed_items = await storage.list_dir_async(storage.make_ref(current, provider_id=provider_id, object_id=current_object_id), refresh=True)
                refreshed_snapshot = _dir_snapshot(refreshed_items)
                next_dirs[current] = {
                    **refreshed_snapshot,
                    'last_scan_at': datetime.now(timezone.utc).isoformat(),
                    'last_task_id': task_id or '',
                }
                if prior.get('children_hash') == refreshed_snapshot['children_hash'] and prior.get('child_count') == refreshed_snapshot['child_count']:
                    skipped_dirs += 1
                    _copy_snapshot_subtree(prior_dirs, next_dirs, current)
                    if visited_dirs % 10 == 0 or skipped_dirs <= 3:
                        await emit_progress(f'[SKIP-DIR] 目录未变化，跳过子树：{current} (已跳过 {skipped_dirs} 个)')
                    continue
                items = refreshed_items
        except Exception as exc:
            err_msg = f'[ERROR] 目录 {current} 扫描失败：{exc}'
            logs.append({'level': 'error', 'message': err_msg})
            await emit_progress(err_msg)
            if current == path:
                raise RuntimeError(err_msg)
            continue

        for item in items:
            is_dir = _looks_like_dir(item)
            name = str(item.get('name') or '')
            if is_dir:
                if name in {'.', '..'}:
                    continue
                child_dir = _join_alist_path(current, name)
                child_object_id = str(item.get('object_id') or item.get('id') or item.get('cid') or '') or None
                if child_dir not in queued_dirs:
                    queue.append((child_dir, child_object_id))
                    queued_dirs.add(child_dir)
                continue
            lower = name.lower()
            if not any(lower.endswith(ext) for ext in AUDIO_EXTENSIONS):
                continue
            full_path = _join_alist_path(current, name)
            file_size = int(item.get('size') or 0)
            file_mtime = str(item.get('modified') or item.get('updated_at') or item.get('created') or '')
            next_file_index.append({
                'file_path': full_path,
                'parent_dir': current,
                'filename': name,
                'size': file_size,
                'mtime': file_mtime,
                'is_audio': True,
                'last_scan_task_id': task_id or '',
            })
            if mode == 'incremental':
                prior_file = current_file_index.get(full_path)
                if prior_file and int(prior_file.get('size') or 0) == file_size and str(prior_file.get('mtime') or '') == file_mtime:
                    skipped_files += 1
                    continue
            idx = seq
            pending.add(asyncio.create_task(schedule(dict(item), idx, current)))
            seq += 1
            discovered_audio += 1
            if discovered_audio == 1 or discovered_audio % 10 == 0:
                await emit_progress(f'[RUN] 已发现 {discovered_audio} 个待解析音频文件，已遍历目录 {visited_dirs} 个')
            if len(pending) >= worker_count * 2:
                await flush(force_log=True)
            if seq > MAX_SCAN_ITEMS:
                break

        if worker_count == 1 and len(pending) > 0:
            await flush(force_log=True)

    while pending:
        await flush(force_log=True)

    try:
        await asyncio.to_thread(sync_file_index, path, next_file_index, task_id or '')
    except Exception as exc:
        logs.append({'level': 'warn', 'message': f'[WARN] 文件索引同步失败：{exc}'})

    if mode == 'full':
        try:
            from app.services.db_data import prune_song_rows_full_rebuild
            keep_ids = [str(song.get('id') or '').strip() for song in files if str(song.get('id') or '').strip()]
            deleted_rows = await asyncio.to_thread(prune_song_rows_full_rebuild, keep_ids)
            logs.append({'level': 'info', 'message': f'[INFO] 全量重建已清理全库旧曲目 {deleted_rows} 条'})
        except Exception as exc:
            logs.append({'level': 'warn', 'message': f'[WARN] 全量重建全库旧曲目清理失败：{exc}'})

    try:
        if mode == 'incremental' and dirty_dirs:
            await asyncio.to_thread(clear_dirty_dirs_under, path)
    except Exception as exc:
        logs.append({'level': 'warn', 'message': f'[WARN] dirty_dirs 清理失败：{exc}'})

    if mode == 'full':
        try:
            from app.services.db_data import reset_all_scan_state_dirs
            await asyncio.to_thread(reset_all_scan_state_dirs)
            logs.append({'level': 'info', 'message': '[INFO] 全量重建已清空全部目录快照，下一次增量不会沿用任何旧跳过状态'})
        except Exception as exc:
            logs.append({'level': 'warn', 'message': f'[WARN] 全量重建目录快照全局清理失败：{exc}'})
    else:
        try:
            scan_state['version'] = 1
            scan_state['root'] = path
            scan_state['updated_at'] = datetime.now(timezone.utc).isoformat()
            scan_state['last_task_id'] = task_id or ''
            scan_state['dirs'] = next_dirs
            await _write_scan_state(storage, path, scan_state)
        except Exception as exc:
            logs.append({'level': 'warn', 'message': f'[WARN] 目录快照状态写入失败：{exc}'})

    if parse_errors:
        logs.append({'level': 'warn', 'message': f"[WARN] 并发解析阶段出现异常样本：{' / '.join(parse_errors)}"})
    for file in files:
        if 'cover_path' not in file:
            file['cover_path'] = ''
    files.sort(key=lambda song: song['id'])
    return files, visited_dirs, worker_count, skipped_dirs, skipped_files


async def collect_scan_result(
    path: str,
    mode: str,
    task_id: str | None = None,
    concurrency: int | None = None,
    progress_callback: Any | None = None,
    provider_id: str | None = None,
) -> dict[str, Any]:
    logs: list[dict[str, str]] = []
    songs: list[dict[str, Any]] = []
    storage = get_storage_service()
    scan_ref = storage.make_ref(path, provider_id=provider_id)
    source = scan_ref.provider_id
    status = 'done'
    duration = '0s'
    started = time.monotonic()
    try:
        root_object_id: str | None = None
        try:
            provider = storage.get_provider(source)
            if getattr(provider, 'provider_type', '') == '115':
                resolver = getattr(provider, 'resolve_scan_root_id_async', None)
                if callable(resolver):
                    root_object_id = await resolver(path)
                    logs.append({'level': 'info', 'message': f'[INFO] 115 专用递归扫描已启用，根目录 cid={root_object_id}'})
        except Exception as exc:
            logs.append({'level': 'warn', 'message': f'[WARN] 115 根目录 cid 预热失败，将回退普通模式：{exc}'})
        songs, visited_dirs, worker_count, skipped_dirs, skipped_files = await _walk_alist(path, logs, mode=mode, task_id=task_id, concurrency=concurrency, progress_callback=progress_callback, provider_id=source, root_object_id=root_object_id)
        if songs:
            accurate_durations = sum(1 for song in songs if song.get('dur', 0) > 0)
            total_range_bytes = sum(int(song.get('range_bytes', 0) or 0) for song in songs)
            avg_range_kb = round(total_range_bytes / len(songs) / 1024) if songs else 0
            logs.append({'level': 'ok', 'message': f'[OK] {source} 递归扫描成功，目录 {visited_dirs} 个，命中 {len(songs)} 个音频文件'})
            logs.append({'level': 'info', 'message': f'[INFO] 目录级增量跳过 {skipped_dirs} 个未变化目录'})
            logs.append({'level': 'info', 'message': f'[INFO] 并发解析 worker {worker_count} 个'})
            logs.append({'level': 'info', 'message': f'[INFO] 文件索引命中过滤 {skipped_files} 首未变化音频'})
            logs.append({'level': 'info', 'message': f'[INFO] 已解析真实时长 {accurate_durations} 首'})
            logs.append({'level': 'info', 'message': f'[INFO] 动态 Range 平均读取 {avg_range_kb} KB/首'})
        else:
            if mode == 'incremental' and (skipped_dirs > 0 or skipped_files > 0):
                logs.append({'level': 'ok', 'message': f'[OK] 增量扫描未发现新增音频；目录 {visited_dirs} 个，跳过未变化目录 {skipped_dirs} 个，跳过未变化文件 {skipped_files} 首'})
            else:
                status = 'failed'
                logs.append({'level': 'error', 'message': f'[ERROR] {source} 可访问，但目录 {visited_dirs} 个内未命中音频文件；已拒绝回退 demo 数据'})
    except Exception as exc:
        status = 'failed'
        logs.append({'level': 'error', 'message': f'[ERROR] {source} 扫描失败：{exc}'})

    duration = f'{max(0, int(time.monotonic() - started))}s'
    now = datetime.now(timezone.utc)
    task = {
        'id': f'T-{uuid.uuid4().hex[:12]}',
        'path': path,
        'mode': '全量' if mode == 'full' else '增量',
        'count': len(songs),
        'total': len(songs),
        'processed': len(songs),
        'duration': duration,
        'status': status,
        'end': now.strftime('%Y-%m-%d %H:%M UTC'),
        'logs': [
            {'level': 'info', 'message': f'[INIT] 开始扫描 {path}，模式 {mode}'},
            *logs,
            {'level': 'ok' if status == 'done' else 'error', 'message': f'[DONE] 扫描结束，当前来源：{source}，写入 {len(songs)} 条曲目'},
        ],
    }
    
    # 扫描结束时，主动进行一次内存垃圾回收，释放解析大文件带来的残留碎片
    gc.collect()
    
    # 新扫描入库后，主动触发一次榜单数据的重新匹配（将 VIRTUAL 虚拟占位符替换为真实本地歌曲）
    try:
        from app.services.curation_service import generate_curated_playlists
        import asyncio
        asyncio.create_task(generate_curated_playlists(trigger_autofill=False, trigger_source='post_scan'))
    except Exception as e:
        logger.error(f"Failed to trigger post-scan curation update: {e}")
    
    return {'task': task, 'songs': songs, 'source': source}
