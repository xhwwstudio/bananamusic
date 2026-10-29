import asyncio
import json
import logging
import re
import os
import random
import httpx
from urllib.parse import quote
from typing import Any
from sqlalchemy import text
import mutagen
from mutagen.flac import FLAC, Picture
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, TIT2, TPE1, TALB, APIC, TPE2, USLT, ID3NoHeaderError
from mutagen.mp4 import MP4, MP4Cover

from app.db.session import engine
from app.services.app_settings import get_runtime_settings
from app.services.db_data import upsert_autofill_failed_upload, clear_autofill_failed_upload, mark_dirty_dir, merge_virtual_song_by_id
from app.services.storage import get_storage_service

logger = logging.getLogger(__name__)


def _clean_text(t: str) -> str:
    """Normalize text for strict matching: keep semantics, only unify spaces/punctuation/width/case."""
    if not t:
        return ""
    t = str(t).strip().lower()
    t = t.translate(str.maketrans({
        '（': '(', '）': ')', '【': '[', '】': ']', '｛': '{', '｝': '}',
        '，': ',', '。': '.', '、': ',', '；': ';', '：': ':', '！': '!', '？': '?',
        '“': '"', '”': '"', '‘': "'", '’': "'",
        '／': '/', '｜': '|', '－': '-', '—': '-', '–': '-', '·': ' ', '　': ' ',
        '＆': '&', '＋': '+', '＠': '@', '＃': '#', '％': '%'
    }))
    t = re.sub(r'\s+', ' ', t)
    t = re.sub(r'\s*([,.;:!?/|&+\-()\[\]{}])\s*', r'\1', t)
    return t.strip()

def _normalize_artist_token(token: str) -> str:
    token = _clean_text(token)
    if not token:
        return ""
    has_cjk = bool(re.search(r'[\u4e00-\u9fff]', token))
    has_ascii = bool(re.search(r'[a-z]', token))
    if has_cjk and has_ascii:
        token = re.sub(r'[a-z0-9._\-]+', '', token)
        token = re.sub(r'\s+', ' ', token).strip()
    return token


def _split_artists(a: str) -> list[str]:
    """Split artist string into normalized artist tokens; order is ignored at compare time."""
    if not a:
        return []
    parts = re.split(r'[/&,、|]+', str(a))
    cleaned = [_normalize_artist_token(p) for p in parts if _normalize_artist_token(p)]
    return sorted(cleaned)

def _parse_dur(d: int | str) -> int:
    if d is None:
        return 0
    if isinstance(d, int):
        return d
    s = str(d).strip()
    if not s:
        return 0
    if s.isdigit():
        return int(s)
    m = re.match(r'^(\d{1,2}):(\d{2})$', s)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    return 0

def _title_core(text: str) -> str:
    t = _clean_text(text)
    t = re.sub(r'\([^)]*\)', '', t)
    t = re.sub(r'\[.*?\]', '', t)
    t = re.sub(r'\b(dj|remix|mix|live|explicit|版|伴奏|cover|demo)\b', '', t)
    t = re.sub(r'\s+', ' ', t).strip(' -_')
    return t


def _looks_like_similar_real_song(existing: dict, *, title: str, artist: str, dur: int) -> bool:
    if str(existing.get('fmt') or '').upper() == 'VIRTUAL':
        return False
    if _split_artists(existing.get('artist', '')) != _split_artists(artist):
        return False

    existing_title = str(existing.get('title') or '')
    if _title_core(existing_title) != _title_core(title):
        return False

    existing_dur = _parse_dur(existing.get('dur', 0))
    title_lc = _clean_text(title)
    tolerant = any(flag in title_lc for flag in ('dj', 'remix', 'mix', 'live', '版', '伴奏'))
    max_diff = 15 if tolerant else 12
    if dur > 0 and existing_dur > 0 and abs(existing_dur - dur) > max_diff:
        return False

    return True


def find_similar_real_song(*, title: str, artist: str, dur: int, exclude_path: str = '') -> dict | None:
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT id,title,artist,album,fmt,dur,path FROM songs WHERE fmt != 'VIRTUAL'")
        ).mappings().all()
    for row in rows:
        current_path = str(row.get('path') or '')
        if exclude_path and current_path == exclude_path:
            continue
        if _looks_like_similar_real_song(dict(row), title=title, artist=artist, dur=dur):
            return dict(row)
    return None


def is_same_song(orig_item: dict, search_item: dict) -> bool:
    """Cross-platform match: title + normalized artists are hard constraints; album is reference-only; duration remains a guardrail."""
    orig_title = _clean_text(orig_item.get('name', ''))
    search_title = _clean_text(search_item.get('name', ''))
    if not orig_title or orig_title != search_title:
        return False

    orig_artists = _split_artists(orig_item.get('singer', ''))
    search_artists = _split_artists(search_item.get('singer', ''))
    if not orig_artists or not search_artists:
        return False
    if orig_artists != search_artists:
        return False

    orig_dur = _parse_dur(orig_item.get('interval', 0))
    search_dur = _parse_dur(search_item.get('interval', 0))
    if orig_dur > 0 and search_dur > 0 and abs(orig_dur - search_dur) > 3:
        return False

    return True

async def get_stream_candidates(client: httpx.AsyncClient, lx_url: str, original_item: dict, quality: str, skip_original: bool = False) -> list[tuple[str, dict, str, str]]:
    """Collect stream candidates in priority order. Returns (url, item, platform, album_note)."""
    candidates: list[tuple[str, dict, str, str]] = []
    seen_urls: set[str] = set()

    def _push(url: str, item: dict, platform: str, album_note: str):
        if not url or url in seen_urls:
            return
        seen_urls.add(url)
        candidates.append((url, item, platform, album_note))

    if not skip_original:
        try:
            res = await client.post(
                f"{lx_url}/api/music/url",
                json={"songInfo": original_item, "quality": quality},
                headers={"Content-Type": "application/json"}
            )
            if res.status_code == 200 and res.json().get("url"):
                _push(res.json().get("url"), original_item, str(original_item.get('source', '') or 'origin'), 'album-origin')
        except Exception as e:
            logger.debug(f"Origin stream fetch error: {e}")

    title = original_item.get('name', '')
    artist = original_item.get('singer', '')
    if not title:
        return candidates

    keyword = f"{title} {artist}".strip()
    orig_source = original_item.get('source', '')
    platforms = ['tx', 'wy', 'kw', 'mg', 'kg']
    if orig_source in platforms:
        platforms.remove(orig_source)

    for plat in platforms:
        logger.info(f"Hunting [{keyword}] on {plat} for {quality}...")
        try:
            search_res = await client.get(
                f"{lx_url}/api/music/search?source={plat}&name={quote(keyword)}&page=1&limit=10"
            )
            if search_res.status_code != 200:
                logger.info(f"Hunter SEARCH FAIL: {plat} status={search_res.status_code} keyword={keyword}")
                continue

            search_body = search_res.json()
            list_data = search_body if isinstance(search_body, list) else search_body.get('list') or search_body.get('data', {}).get('list', [])
            for item in list_data:
                if is_same_song(original_item, item):
                    orig_album = _clean_text(original_item.get('albumName') or original_item.get('album') or '')
                    found_album = _clean_text(item.get('albumName') or item.get('album') or '')
                    album_note = 'album-match' if orig_album and found_album and orig_album == found_album else 'album-diff'
                    url_res = await client.post(
                        f"{lx_url}/api/music/url",
                        json={"songInfo": item, "quality": quality},
                        headers={"Content-Type": "application/json"}
                    )
                    if url_res.status_code == 200 and url_res.json().get("url"):
                        logger.info(f"Hunter CANDIDATE: Found {quality} on {plat}! [{album_note}] {original_item.get('name','')} / {original_item.get('singer','')} -> {item.get('albumName') or item.get('album') or ''}")
                        _push(url_res.json().get("url"), item, plat, album_note)
        except Exception as e:
            logger.debug(f"Hunter error on {plat}: {e}")

    return candidates

def sniff_magic_bytes(chunk: bytes, default_quality: str) -> str:
    """Sniff real file extension from HTTP stream chunk."""
    if chunk.startswith(b'fLaC'):
        return 'flac'
    if chunk.startswith(b'ID3') or chunk.startswith(b'\xff\xfb') or chunk.startswith(b'\xff\xf3') or chunk.startswith(b'\xff\xfa'):
        return 'mp3'
    if len(chunk) > 8 and chunk[4:8] == b'ftyp':
        return 'm4a'
    if chunk.startswith(b'OggS'):
        return 'ogg'
    if chunk.startswith(b'RIFF') and len(chunk) > 12 and chunk[8:12] == b'WAVE':
        return 'wav'

    head = chunk[:64].lstrip()
    if head.startswith((b'{', b'[')):
        return 'json'
    lowered = head.lower()
    if lowered.startswith((b'<!doctype', b'<html', b'<?xml')):
        return 'text'

    return 'unknown'

def sanitize_filename(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', '_', name).strip()


def _build_candidate_request_headers(platform: str) -> dict[str, str]:
    browser_headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36',
        'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
        'Sec-Fetch-Site': 'cross-site',
        'Sec-Fetch-Mode': 'cors',
        'Sec-Fetch-Dest': 'empty',
        'Accept': '*/*',
    }
    if platform == 'kw':
        return {
            **browser_headers,
            'Origin': 'https://www.kuwo.cn',
            'Referer': 'https://www.kuwo.cn/',
        }
    return browser_headers


def _safe_preview_text(chunk: bytes, limit: int = 60) -> str:
    text = chunk[:256].decode('utf-8', errors='replace')
    text = text.replace('\x00', '')
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text[:limit]


def inject_metadata(file_path: str, ext: str, title: str, artist: str, album: str, album_artist: str, lyrics: str | None, cover_data: bytes | None) -> bool:
    """Inject ID3/Vorbis metadata and cover art into downloaded audio file."""
    try:
        if ext == 'mp3':
            try:
                audio = MP3(file_path, ID3=ID3)
            except ID3NoHeaderError:
                audio = MP3(file_path)
                audio.add_tags()

            audio.tags.add(TIT2(encoding=3, text=title))
            audio.tags.add(TPE1(encoding=3, text=artist))
            audio.tags.add(TALB(encoding=3, text=album))
            audio.tags.add(TPE2(encoding=3, text=album_artist))
            if lyrics:
                audio.tags.add(USLT(encoding=3, lang='xxx', desc='', text=lyrics))
            if cover_data:
                audio.tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover_data))
            audio.save()

        elif ext == 'flac':
            audio = FLAC(file_path)
            audio["title"] = [title]
            audio["artist"] = [artist]
            audio["album"] = [album]
            audio["ALBUMARTIST"] = [album_artist]
            if lyrics:
                audio["LYRICS"] = [lyrics]
                audio["UNSYNCEDLYRICS"] = [lyrics]
                audio["SYNCEDLYRICS"] = [lyrics]
            if cover_data:
                pic = Picture()
                pic.type = 3
                pic.mime = "image/jpeg"
                pic.desc = "Cover"
                pic.data = cover_data
                audio.add_picture(pic)
            audio.save()

        elif ext == 'm4a':
            audio = MP4(file_path)
            audio.tags["\xa9nam"] = title
            audio.tags["\xa9ART"] = artist
            audio.tags["\xa9alb"] = album
            audio.tags["aART"] = album_artist
            if lyrics:
                audio.tags["\xa9lyr"] = lyrics
            if cover_data:
                audio.tags["covr"] = [MP4Cover(cover_data, imageformat=MP4Cover.FORMAT_JPEG)]
            audio.save()

        return True
    except Exception as e:
        logger.warning(f"Failed to inject metadata into {file_path}: {e}")
        return False


async def storage_path_exists(storage, path: str, *, provider_id: str) -> bool:
    try:
        return await storage.exists_async(storage.make_ref(path, provider_id=provider_id))
    except Exception:
        return False


async def ensure_storage_dir(storage, path: str, created_dirs_cache: set[str], *, provider_id: str) -> tuple[bool, str]:
    normalized = storage.normalize_path(path, provider_id=provider_id)
    if not normalized or normalized == '/':
        return True, '/'

    current = ''
    for segment in [seg for seg in normalized.split('/') if seg]:
        current += '/' + segment
        if current in created_dirs_cache:
            continue
        if await storage_path_exists(storage, current, provider_id=provider_id):
            created_dirs_cache.add(current)
            continue
        try:
            ok = await storage.mkdir_async(storage.make_ref(current, provider_id=provider_id))
            if not ok:
                return False, f"mkdir failed: {current}"
            if not await storage_path_exists(storage, current, provider_id=provider_id):
                return False, f"mkdir verify failed: {current}"
            created_dirs_cache.add(current)
        except Exception as e:
            return False, str(e)
    return True, normalized


async def verify_storage_file(
    storage,
    target_path: str,
    expected_size: int,
    *,
    provider_id: str,
    retries: int = 6,
) -> tuple[bool, str]:
    parent_dir = '/' + '/'.join([seg for seg in str(target_path or '').split('/') if seg][:-1])
    filename = str(target_path or '').split('/')[-1]
    last_err = 'not found'

    async def check_parent_list(refresh: bool) -> tuple[bool, str]:
        try:
            content = await storage.list_dir_async(storage.make_ref(parent_dir, provider_id=provider_id), refresh=refresh)
        except Exception as exc:
            return False, f"list error refresh={refresh}: {exc}"
        for item in content:
            if str(item.get('name') or '') == filename:
                size = int(item.get('size') or 0)
                if size > 0 and (expected_size <= 0 or abs(size - expected_size) <= 16):
                    return True, f"list ok size={size} refresh={refresh}"
                if size > 0:
                    return False, f"list size mismatch expected={expected_size} actual={size} refresh={refresh}"
        return False, f"not found refresh={refresh}"

    for attempt in range(retries):
        try:
            stat = await storage.stat_async(storage.make_ref(target_path, provider_id=provider_id))
            if stat is not None:
                size = int(stat.size or 0)
                if size > 0 and (expected_size <= 0 or abs(size - expected_size) <= 16):
                    return True, f"stat ok size={size}"
                if size > 0:
                    return False, f"size mismatch expected={expected_size} actual={size}"

            if parent_dir and filename:
                ok, msg = await check_parent_list(refresh=False)
                if ok:
                    return True, msg
                if 'size mismatch' in msg:
                    return False, msg

                ok, msg = await check_parent_list(refresh=True)
                if ok:
                    return True, msg
                if 'size mismatch' in msg:
                    return False, msg
                last_err = msg
        except Exception as e:
            last_err = str(e)

        if attempt < retries - 1:
            await asyncio.sleep(3 + attempt * 3)

    if 'not found' in str(last_err):
        last_err = f"visibility delay suspected: {last_err}"
    return False, last_err

async def run_auto_fill_task(quality: str, upload_dir: str, progress_callback=None, task_id: str = '', target_songs: list[dict[str, Any]] | None = None):
    """
    Background task to download missing VIRTUAL songs and upload them to AList.

    Concurrency model:
    - task preparation is lightweight and scheduled centrally
    - source access is limited to 1 concurrent request per source
    - directory existence checks / mkdir are serialized to avoid provider inconsistency
    - upload is limited to 2 concurrent workers
    """
    created_dirs_cache = set()
    storage = get_storage_service()
    upload_ref_provider = str(get_runtime_settings().get('autofill_provider_id') or get_runtime_settings().get('storage_default_provider_id') or '').strip()
    if not upload_ref_provider:
        raise RuntimeError('未配置补全上传存储源')
    settings = get_runtime_settings()
    upload_dir = storage.normalize_path(str(upload_dir or '').strip() or '/MusicOn_AutoDownload/', provider_id=upload_ref_provider)
    lx_url = settings.get("lx_server_url", "").rstrip('/')
    library_provider_id = str(settings.get('last_scan_provider_id') or upload_ref_provider).strip() or upload_ref_provider
    library_root = storage.normalize_path(str(settings.get("last_scan_path") or '').strip() or upload_dir, provider_id=library_provider_id)

    started_at = asyncio.get_running_loop().time()

    total_count = 0
    success_count = 0
    failed_count = 0
    skipped_count = 0
    delayed_count = 0
    lossy_only_count = 0
    source_miss_count = 0
    upload_failed_count = 0
    processed_count = 0

    progress_lock = asyncio.Lock()
    state_lock = asyncio.Lock()
    dir_lock = asyncio.Lock()
    verify_lock = asyncio.Lock()
    upload_semaphore = asyncio.Semaphore(2)
    source_limiters: dict[str, asyncio.Semaphore] = {}
    source_failures: dict[str, int] = {}
    source_circuit_until: dict[str, float] = {}
    bad_candidate_cache: dict[str, float] = {}
    circuit_threshold = 3
    circuit_cooldown_sec = 30 * 60
    bad_candidate_ttl_sec = 6 * 60 * 60
    verify_batch_size = 5
    verify_round_delays = [8, 20, 30]
    pending_verify: list[dict[str, Any]] = []

    def _duration_text() -> str:
        duration_sec = max(0, int(asyncio.get_running_loop().time() - started_at))
        return f"{duration_sec // 60}m {duration_sec % 60}s" if duration_sec >= 60 else f"{duration_sec}s"

    async def emit(logs=None, status: str | None = None):
        if not progress_callback:
            return
        payload = {
            "processed": processed_count,
            "count": success_count,
            "total": total_count,
            "duration": _duration_text(),
            "logs": logs or [],
            "extra": {
                "delayed": delayed_count,
                "lossy_only": lossy_only_count,
                "source_miss": source_miss_count,
                "upload_failed": upload_failed_count,
                "failed": failed_count,
                "skipped": skipped_count,
            },
        }
        if status is not None:
            payload["status"] = status
        await progress_callback(**payload)

    async def bump(*, success=0, failed=0, skipped=0, delayed=0, lossy_only=0, source_miss=0, upload_failed=0, processed=1, logs=None, status: str | None = None):
        nonlocal success_count, failed_count, skipped_count, delayed_count, lossy_only_count, source_miss_count, upload_failed_count, processed_count
        async with progress_lock:
            success_count += success
            failed_count += failed
            skipped_count += skipped
            delayed_count += delayed
            lossy_only_count += lossy_only
            source_miss_count += source_miss
            upload_failed_count += upload_failed
            processed_count += processed
            await emit(logs=logs, status=status)

    async def get_source_limiter(source: str) -> asyncio.Semaphore:
        async with state_lock:
            limiter = source_limiters.get(source)
            if limiter is None:
                limiter = asyncio.Semaphore(1)
                source_limiters[source] = limiter
            return limiter

    async def mark_source_failure(source: str, status_code: int) -> tuple[int, bool]:
        async with state_lock:
            hit = source_failures.get(source, 0) + 1
            source_failures[source] = hit
            opened = False
            if hit >= circuit_threshold:
                source_circuit_until[source] = asyncio.get_running_loop().time() + circuit_cooldown_sec
                opened = True
            return hit, opened

    async def clear_source_failure(source: str):
        async with state_lock:
            source_failures[source] = 0

    async def source_circuit_remaining(source: str) -> int:
        async with state_lock:
            remain = int(source_circuit_until.get(source, 0) - asyncio.get_running_loop().time())
            return max(0, remain)

    async def record_failed_upload(*, title: str, artist: str, filename: str, alist_path: str, reason: str, last_error: str):
        await asyncio.to_thread(
            upsert_autofill_failed_upload,
            task_id=task_id,
            title=title,
            artist=artist,
            filename=filename,
            alist_path=alist_path,
            reason=reason,
            last_error=last_error,
        )

    async def clear_failed_upload(*, title: str, artist: str, filename: str, alist_path: str):
        await asyncio.to_thread(
            clear_autofill_failed_upload,
            title=title,
            artist=artist,
            filename=filename,
            alist_path=alist_path,
        )

    async def mark_parent_dirty(dir_path: str, reason: str):
        await asyncio.to_thread(mark_dirty_dir, dir_path, reason)

    async def merge_virtual_to_real(*, virtual_song_id: str | None, real_song_id: str | None) -> int:
        if not virtual_song_id or not real_song_id:
            return 0
        return await asyncio.to_thread(merge_virtual_song_by_id, str(virtual_song_id), str(real_song_id))

    async def try_direct_import(*, virtual_song_id: str | None, library_alist_path: str, library_parent_dir: str, filename: str, expected_size: int, context: str) -> str:
        try:
            verify_msg = 'not checked'
            from app.services.scan_service import import_single_alist_file
            imported = await import_single_alist_file(library_alist_path, provider_id=library_provider_id)
            if imported and imported.get('song'):
                imported_song = dict(imported.get('song') or {})
                deleted_virtuals = int(imported.get('deleted_virtuals') or 0)
                merged_virtuals = await merge_virtual_to_real(virtual_song_id=virtual_song_id, real_song_id=imported_song.get('id'))
                total_merged = max(deleted_virtuals, merged_virtuals)
                message = '；已直接入库'
                if total_merged > 0:
                    message += f'，并清理 {total_merged} 条 VIRTUAL'
                return message

            ok, verify_msg = await verify_storage_file(
                storage,
                library_alist_path,
                expected_size,
                provider_id=library_provider_id,
                retries=2,
            )
            if ok:
                imported = await import_single_alist_file(library_alist_path, provider_id=library_provider_id)
                if imported and imported.get('song'):
                    imported_song = dict(imported.get('song') or {})
                    deleted_virtuals = int(imported.get('deleted_virtuals') or 0)
                    merged_virtuals = await merge_virtual_to_real(virtual_song_id=virtual_song_id, real_song_id=imported_song.get('id'))
                    total_merged = max(deleted_virtuals, merged_virtuals)
                    message = f'；入库侧延迟可见后已直接入库 ({verify_msg})'
                    if total_merged > 0:
                        message += f'，并清理 {total_merged} 条 VIRTUAL'
                    return message

            await mark_parent_dirty(library_parent_dir, f'{context}_library_pending')
            return f'；上传侧已完成，但入库侧暂未命中 ({verify_msg})，已标记后续校准'
        except Exception as import_exc:
            logger.warning(f"Direct single-file import failed for {filename} [{context}]: {import_exc}")
            await mark_parent_dirty(library_parent_dir, f'{context}_library_error')
            return '；上传侧已完成，但入库侧暂未可见，已标记后续校准'

    async def flush_pending_verify(force: bool = False):
        nonlocal pending_verify
        async with verify_lock:
            if not pending_verify:
                return
            if not force and len(pending_verify) < verify_batch_size:
                return
            batch = pending_verify[:verify_batch_size] if not force else pending_verify[:]
            pending_verify = pending_verify[len(batch):]

        await emit(logs=[{'level': 'info', 'message': f"[VERIFY-BATCH] 上传已完成 {len(batch)} 首，准备进行 3 轮校验（8s / 20s / 30s）"}])

        for round_idx, delay_sec in enumerate(verify_round_delays, start=1):
            await emit(logs=[{'level': 'info', 'message': f"[VERIFY-ROUND] 第 {round_idx}/3 轮校验，等待 {delay_sec}s 后开始"}])
            await asyncio.sleep(delay_sec)
            remaining = []
            for item in batch:
                ok, verify_msg = await verify_storage_file(
                    storage, item['alist_path'], item['file_size'], provider_id=upload_ref_provider, retries=2
                )
                if ok:
                    logger.info(f"Verified uploaded file {item['filename']}: {verify_msg}")
                    await clear_failed_upload(
                        title=item['title'],
                        artist=item['artist'],
                        filename=item['filename'],
                        alist_path=item['alist_path'],
                    )
                    await mark_parent_dirty(str(item.get('library_parent_dir') or str(item.get('library_alist_path') or item['alist_path']).rsplit('/', 1)[0]), 'autofill_verify_ok')
                    direct_import_msg = await try_direct_import(
                        virtual_song_id=item.get('virtual_song_id'),
                        library_alist_path=item.get('library_alist_path') or item['alist_path'],
                        library_parent_dir=item.get('library_parent_dir') or str(item.get('library_alist_path') or item['alist_path']).rsplit('/', 1)[0],
                        filename=item['filename'],
                        expected_size=int(item.get('file_size') or 0),
                        context='autofill_verify_ok',
                    )
                    await bump(success=1, logs=[{'level': 'ok', 'message': f"[OK] {item['filename']} 上传成功 ({verify_msg}){direct_import_msg}"}], processed=1)
                else:
                    item['_last_verify_msg'] = verify_msg
                    remaining.append(item)
            batch = remaining
            if not batch:
                break

        for item in batch:
            verify_msg = str(item.get('_last_verify_msg') or 'unknown')
            if 'visibility delay suspected' in str(verify_msg) or 'not found' in str(verify_msg):
                logger.warning(f"Upload visibility delayed for {item['filename']}: {verify_msg}")
                await mark_parent_dirty(str(item.get('library_parent_dir') or str(item.get('library_alist_path') or item['alist_path']).rsplit('/', 1)[0]), 'autofill_visibility_delay')
                await record_failed_upload(
                    title=item['title'],
                    artist=item['artist'],
                    filename=item['filename'],
                    alist_path=item['alist_path'],
                    reason='upload_visibility_delay',
                    last_error=verify_msg or 'unknown',
                )
                await bump(delayed=1, logs=[{'level': 'warn', 'message': f"[WAIT-CALIBRATE] {item['filename']} 三轮校验仍未命中 ({verify_msg})；已加入上传失败列表并标记后续校准"}], processed=1)
            else:
                logger.error(f"Upload verify failed for {item['filename']}: {verify_msg}")
                await record_failed_upload(
                    title=item['title'],
                    artist=item['artist'],
                    filename=item['filename'],
                    alist_path=item['alist_path'],
                    reason='upload_verify_failed',
                    last_error=verify_msg or 'unknown',
                )
                await bump(failed=1, upload_failed=1, logs=[{'level': 'error', 'message': f"[FAIL-UPLOAD-VERIFY] {item['filename']} 上传后校验失败 ({verify_msg or 'unknown'})"}], processed=1)

    if not lx_url:
        logger.error("Auto-fill aborted: Missing LX-Server configuration.")
        await bump(processed=0, failed=1, logs=[{'level': 'error', 'message': '[FAIL] 缺少 LX-Server 配置，任务终止'}], status='failed')
        return

    if target_songs is None:
        with engine.connect() as conn:
            virtual_songs = conn.execute(
                text("SELECT id, title, artist, raw_url FROM songs WHERE fmt = 'VIRTUAL'")
            ).mappings().fetchall()
    else:
        virtual_songs = target_songs

    total_count = len(virtual_songs)

    if not virtual_songs:
        logger.info("No VIRTUAL songs found to auto-fill.")
        await emit(logs=[{'level': 'info', 'message': '[DONE] 没有待补全的 VIRTUAL 曲目'}], status='done')
        return

    logger.info(f"Starting auto-fill for {len(virtual_songs)} songs in {quality} quality...")
    await emit(logs=[{'level': 'info', 'message': f'[RUN] 开始补全，共 {total_count} 首，目标目录 {upload_dir}'}], status='running')

    if not upload_dir.endswith('/'):
        upload_dir += '/'
    if not library_root.endswith('/'):
        library_root += '/'

    def join_root_path(root: str, relative_path: str) -> str:
        return f"{root.rstrip('/')}/{relative_path.lstrip('/')}"

    timeout_config = httpx.Timeout(1800.0, connect=30.0)
    async with httpx.AsyncClient(timeout=timeout_config, follow_redirects=True) as client:

        async def process_song(song: dict):
            tmp_path = None
            try:
                raw_url = song['raw_url']
                if not raw_url:
                    await bump(skipped=1, logs=[{'level': 'warn', 'message': f"[SKIP] {song['title']} - {song['artist']} 缺少 raw_url，已跳过"}])
                    return

                lx_item = json.loads(raw_url)

                song_source = str(lx_item.get('source') or '').strip() or 'unknown'
                remain_sec = await source_circuit_remaining(song_source)
                skip_original = remain_sec > 0
                if skip_original:
                    await emit(logs=[{'level': 'warn', 'message': f"[HUNTER] 原音源 {song_source} 熔断中，跳过直连，继续跨平台搜索：{song['title']} - {song['artist']}"}])

                source_limiter = await get_source_limiter(song_source)
                async with source_limiter:
                    remain_sec = await source_circuit_remaining(song_source)
                    skip_original = remain_sec > 0
                    if skip_original:
                        await emit(logs=[{'level': 'warn', 'message': f"[HUNTER] 原音源 {song_source} 熔断中，继续跨平台搜索：{song['title']} - {song['artist']}"}])

                    candidates = await get_stream_candidates(client, lx_url, lx_item, quality, skip_original=skip_original)
                    if not candidates:
                        logger.info(f"[{quality}] not found anywhere for {song['title']} - {song['artist']}")
                        await bump(failed=1, source_miss=1, logs=[{'level': 'warn', 'message': f"[MISS-SOURCE] {song['title']} - {song['artist']} 未找到可用音源"}])
                        return

                    tmp_path = None
                    real_ext = None
                    filename = None
                    raw_artist = song['artist']
                    raw_album = song.get('album') or 'Unknown Album'
                    raw_title = song['title']
                    album_artist = raw_artist
                    lyrics_text = None
                    cover_data = None
                    matched_item = None
                    selected_platform = ''
                    selected_album_note = ''

                    for stream_url, candidate_item, candidate_platform, album_note in candidates:
                        candidate_key = f"{candidate_platform}:{candidate_item.get('songmid') or candidate_item.get('id') or stream_url}:{quality}"
                        now_ts = asyncio.get_running_loop().time()
                        if bad_candidate_cache.get(candidate_key, 0) > now_ts:
                            await emit(logs=[{'level': 'info', 'message': f"[SKIP-CANDIDATE] {song['title']} - {song['artist']} 跳过已知坏候选 {candidate_platform}"}])
                            continue

                        matched_item = candidate_item
                        selected_platform = candidate_platform
                        selected_album_note = album_note
                        raw_artist = matched_item.get('singer', song['artist']) or 'Unknown Artist'
                        raw_album = matched_item.get('albumName') or matched_item.get('album') or 'Unknown Album'
                        raw_title = matched_item.get('name', song['title']) or 'Unknown Title'
                        album_artist = '群星' if '/' in raw_artist or '&' in raw_artist or ',' in raw_artist or '、' in raw_artist else raw_artist

                        artist = sanitize_filename(raw_artist)
                        album = sanitize_filename(raw_album)
                        title = sanitize_filename(raw_title)
                        album_artist_dir = sanitize_filename(album_artist)
                        cover_url = matched_item.get('pic') or matched_item.get('img', '')
                        logger.info(f"Connecting to {title} stream from {candidate_platform} [{album_note}]...")

                        lyrics_text = None
                        try:
                            lyric_res = await client.get(
                                f"{lx_url}/api/music/lyric?source={matched_item.get('source', '')}&songId={matched_item.get('songmid') or matched_item.get('id')}",
                                timeout=10.0
                            )
                            if lyric_res.status_code == 200:
                                lyric_data = lyric_res.json()
                                lyrics_text = (
                                    lyric_data.get('lyric')
                                    or lyric_data.get('lxlyric')
                                    or lyric_data.get('lrc')
                                    or ''
                                ).strip()
                                if lyrics_text in ('暂无歌词', '[00:00.00]暂无歌词'):
                                    lyrics_text = None
                        except Exception as le:
                            logger.warning(f"Failed to fetch lyrics for {title}: {le}")

                        cover_data = None
                        if cover_url:
                            try:
                                cover_res = await client.get(cover_url, timeout=10.0)
                                if cover_res.status_code == 200:
                                    cover_data = cover_res.content
                            except Exception as ce:
                                logger.warning(f"Failed to fetch cover art for {title}: {ce}")

                        request_headers = _build_candidate_request_headers(candidate_platform)
                        async with client.stream('GET', stream_url, headers=request_headers) as r:
                            try:
                                r.raise_for_status()
                            except httpx.HTTPStatusError as he:
                                status_code = he.response.status_code if he.response is not None else 0
                                candidate_source = str(matched_item.get('source') or song_source or '').strip() or song_source
                                if status_code in (403, 429):
                                    hit, opened = await mark_source_failure(candidate_source, status_code)
                                    if opened:
                                        logger.warning(f"Source {candidate_source} circuit opened for {circuit_cooldown_sec}s after {hit} consecutive {status_code}s")
                                        await emit(logs=[{'level': 'error', 'message': f"[FAIL-CANDIDATE] {song['title']} - {song['artist']} 候选源 {candidate_source} 被 {status_code} 拒绝，已熔断 30 分钟，继续尝试其他候选"}])
                                    else:
                                        await emit(logs=[{'level': 'warn', 'message': f"[RETRY-GUARD] {song['title']} - {song['artist']} 候选源 {candidate_source} 被 {status_code} 拒绝；连续失败 {hit}/{circuit_threshold}，继续尝试其他候选"}])
                                    continue
                                raise

                            iterator = r.aiter_bytes(chunk_size=8192)
                            try:
                                first_chunk = await iterator.__anext__()
                            except StopAsyncIteration:
                                await emit(logs=[{'level': 'warn', 'message': f"[FAIL-CANDIDATE] {song['title']} - {song['artist']} 候选流为空，继续尝试其他候选"}])
                                continue

                            real_ext = sniff_magic_bytes(first_chunk, quality)
                            if real_ext in ('json', 'text', 'unknown'):
                                bad_candidate_cache[candidate_key] = asyncio.get_running_loop().time() + bad_candidate_ttl_sec
                                preview = _safe_preview_text(first_chunk)
                                await emit(logs=[{'level': 'warn', 'message': f"[FAIL-CANDIDATE] {song['title']} - {song['artist']} 候选 {candidate_platform} 返回非音频内容({real_ext})：{preview}，继续尝试其他候选"}])
                                continue
                            if quality in ('flac', 'wav') and real_ext not in ('flac', 'wav'):
                                bad_candidate_cache[candidate_key] = asyncio.get_running_loop().time() + bad_candidate_ttl_sec
                                await emit(logs=[{'level': 'warn', 'message': f"[LOSSLESS-REJECT] {song['title']} - {song['artist']} 请求 {quality}，候选 {candidate_platform} 实际返回 {real_ext}，继续搜索其他候选"}])
                                continue

                            filename = f"{artist} - {title}.{real_ext}"
                            tmp_path = f"/tmp/autofill_{filename}"
                            logger.info(f"Sniffed as {real_ext}. Downloading to temporary capsule...")

                            with open(tmp_path, 'wb') as f:
                                f.write(first_chunk)
                                async for chunk in iterator:
                                    f.write(chunk)

                        injected_ok = await asyncio.to_thread(
                            inject_metadata,
                            file_path=tmp_path,
                            ext=real_ext,
                            title=raw_title,
                            artist=raw_artist,
                            album=raw_album,
                            album_artist=album_artist,
                            lyrics=lyrics_text,
                            cover_data=cover_data,
                        )
                        if not injected_ok:
                            bad_candidate_cache[candidate_key] = asyncio.get_running_loop().time() + bad_candidate_ttl_sec
                            if os.path.exists(tmp_path):
                                try:
                                    os.remove(tmp_path)
                                except Exception:
                                    pass
                            await emit(logs=[{'level': 'warn', 'message': f"[BAD-FLAC] {song['title']} - {song['artist']} 候选 {selected_platform or song_source} 元数据注入失败，继续尝试其他候选"}])
                            tmp_path = None
                            filename = None
                            real_ext = None
                            matched_item = None
                            continue
                        break

                    if not tmp_path or not filename or not real_ext or not matched_item:
                        await bump(failed=1, lossy_only=1, logs=[{'level': 'warn', 'message': f"[MISS-LOSSLESS] {song['title']} - {song['artist']} 未找到可用无损音源（候选仅返回有损或无效内容）"}])
                        return

                album_artist_dir = sanitize_filename(album_artist)
                relative_path = f"{album_artist_dir}/{album}/{filename}"
                upload_parent_dir = join_root_path(upload_dir, f"{album_artist_dir}/{album}")
                upload_alist_path = join_root_path(upload_dir, relative_path)
                library_parent_dir = join_root_path(library_root, f"{album_artist_dir}/{album}")
                library_alist_path = join_root_path(library_root, relative_path)

                similar_real = await asyncio.to_thread(
                    find_similar_real_song,
                    title=raw_title,
                    artist=raw_artist,
                    dur=_parse_dur(matched_item.get('interval', 0)),
                    exclude_path=library_alist_path,
                )
                if similar_real:
                    if tmp_path and os.path.exists(tmp_path):
                        try:
                            os.remove(tmp_path)
                        except Exception:
                            pass
                    await clear_failed_upload(
                        title=song['title'],
                        artist=song['artist'],
                        filename=filename,
                        alist_path=upload_alist_path,
                    )
                    merged_virtuals = await merge_virtual_to_real(virtual_song_id=song.get('id'), real_song_id=similar_real.get('id'))
                    merge_note = f'；已合并 {merged_virtuals} 条 VIRTUAL' if merged_virtuals > 0 else ''
                    await bump(
                        skipped=1,
                        logs=[{'level': 'info', 'message': f"[SKIP-DUP] {filename} 命中相似真实曲目，跳过补全上传 -> {similar_real.get('path') or similar_real.get('title')}{merge_note}"}]
                    )
                    return

                async with dir_lock:
                    if await storage_path_exists(storage, upload_alist_path, provider_id=upload_ref_provider):
                        if tmp_path and os.path.exists(tmp_path):
                            try:
                                os.remove(tmp_path)
                            except Exception:
                                pass
                        await clear_failed_upload(
                            title=song['title'],
                            artist=song['artist'],
                            filename=filename,
                            alist_path=upload_alist_path,
                        )
                        await mark_parent_dirty(library_parent_dir, 'autofill_skip_existing')
                        direct_import_msg = await try_direct_import(
                            virtual_song_id=song.get('id'),
                            library_alist_path=library_alist_path,
                            library_parent_dir=library_parent_dir,
                            filename=filename,
                            expected_size=0,
                            context='autofill_skip_existing',
                        )
                        await bump(skipped=1, logs=[{'level': 'info', 'message': f"[SKIP] {filename} 已存在，跳过上传{direct_import_msg}"}])
                        return

                    ok_dir, dir_msg = await ensure_storage_dir(storage, upload_parent_dir, created_dirs_cache, provider_id=upload_ref_provider)
                    if not ok_dir:
                        if tmp_path and os.path.exists(tmp_path):
                            try:
                                os.remove(tmp_path)
                            except Exception:
                                pass
                        logger.error(f"Failed to ensure directory {upload_parent_dir}: {dir_msg}")
                        await bump(failed=1, logs=[{'level': 'error', 'message': f"[FAIL] 目录校验失败：{upload_parent_dir} ({dir_msg})"}])
                        return

                async with upload_semaphore:
                    sleep_seconds = round(random.uniform(0.8, 2.0), 2)
                    await asyncio.sleep(sleep_seconds)

                    logger.info(f"Uploading injected capsule via provider {upload_ref_provider}: {upload_alist_path}")
                    max_retries = 3
                    upload_accepted = False
                    verify_msg = ''

                    file_size = os.path.getsize(tmp_path)
                    with open(tmp_path, 'rb') as fp:
                        file_bytes = fp.read()

                    for attempt in range(max_retries):
                        try:
                            upload_accepted = await storage.write_bytes_async(storage.make_ref(upload_alist_path, provider_id=upload_ref_provider), file_bytes)
                            if upload_accepted:
                                verify_msg = 'queued for batch verify'
                                break
                            verify_msg = 'provider write failed'
                            logger.warning(f"Upload attempt {attempt + 1}/{max_retries} failed for {filename}: {verify_msg}")
                        except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.ConnectTimeout, httpx.ReadError, httpx.RemoteProtocolError, httpx.ConnectError) as re:
                            err_detail = f"{type(re).__name__}: {re!r}"
                            if attempt == max_retries - 1:
                                logger.error(f"Upload failed after {max_retries} attempts for {filename}: {err_detail}")
                                verify_msg = err_detail
                                break
                            backoff_time = 5 * (3 ** attempt)
                            logger.warning(f"Upload IO error on attempt {attempt+1}/{max_retries} for {filename}: {err_detail}. Retrying in {backoff_time}s...")
                            await asyncio.sleep(backoff_time)

                await clear_source_failure(song_source)

                if tmp_path and os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except Exception as del_e:
                        logger.warning(f"Failed to clean up {tmp_path}: {del_e}")

                if upload_accepted:
                    async with verify_lock:
                        pending_verify.append({
                            'title': song['title'],
                            'artist': song['artist'],
                            'virtual_song_id': song.get('id'),
                            'filename': filename,
                            'alist_path': upload_alist_path,
                            'library_alist_path': library_alist_path,
                            'library_parent_dir': library_parent_dir,
                            'file_size': file_size,
                        })
                        queued_count = len(pending_verify)
                    await emit(logs=[{'level': 'info', 'message': f"[UPLOAD-QUEUED] {filename} 已上传，待批量校验 ({queued_count}/{verify_batch_size})"}])
                    await flush_pending_verify(force=False)
                else:
                    logger.error(f"Failed to pipe {filename}: verify={verify_msg or 'unknown'}")
                    await record_failed_upload(
                        title=song['title'],
                        artist=song['artist'],
                        filename=filename,
                        alist_path=upload_alist_path,
                        reason='upload_failed',
                        last_error=verify_msg or 'unknown',
                    )
                    await bump(failed=1, upload_failed=1, logs=[{'level': 'error', 'message': f"[FAIL] {filename} 上传失败 ({verify_msg or 'unknown'})"}])

                sleep_seconds = round(random.uniform(1.5, 4.5), 2)
                logger.info(f"Cooling down for {sleep_seconds}s before next song...")
                await asyncio.sleep(sleep_seconds)

            except Exception as e:
                if tmp_path and os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except Exception:
                        pass
                logger.exception(f"Error processing {song['title']} - {song['artist']}: {e}")
                await bump(failed=1, logs=[{'level': 'error', 'message': f"[FAIL] {song['title']} - {song['artist']}：{type(e).__name__}: {e}"}])

        tasks = [asyncio.create_task(process_song(song)) for song in virtual_songs]
        await asyncio.gather(*tasks)
        await flush_pending_verify(force=True)

    await emit(logs=[{'level': 'ok', 'message': f'[DONE] 自动补全完成：成功 {success_count}，待校准 {delayed_count}，失败 {failed_count}（无损缺失 {lossy_only_count} / 音源缺失 {source_miss_count} / 上传失败 {upload_failed_count}），跳过 {skipped_count}，总计 {total_count}'}], status='done')

    if success_count > 0:
        logger.info(f"Auto-fill complete. Downloaded {success_count} songs. Direct import enabled; skip full post-autofill scan.")
