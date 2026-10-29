from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
from datetime import datetime, timezone
from typing import Any
import uuid

from sqlalchemy import text, bindparam

from app.core.config import settings
from app.core.slugs import album_id, artist_id
from app.db.session import engine, is_sqlite
from app.db.compat import (
    add_column_if_missing,
    db_bigint_pk,
    db_json_type,
    db_json_value_expr,
    db_now_expr,
    db_timestamp_type,
    dt_to_iso,
    json_dumps,
    json_loads,
)
from app.services.cover_cache import save_cover_base64
from app.services.demo_data import SUBSONIC_ITEMS
from app.services.scan_service import collect_scan_result


FMT_COLORS = {'FLAC': '#34d399', 'MP3': '#a78bfa', 'AAC': '#fbbf24', 'OPUS': '#38bdf8'}


def _normalize_playlist_policy(
    *,
    owner: str | None,
    public: bool | None,
    is_readonly: bool | None,
    scope: str | None,
) -> dict[str, Any]:
    next_owner = str(owner or 'admin').strip() or 'admin'
    next_scope = str(scope or '').strip() or 'private_user'
    next_public = bool(public) if public is not None else False
    next_readonly = bool(is_readonly) if is_readonly is not None else False

    if next_scope == 'public_system' or next_public:
        return {
            'owner': 'system',
            'public': True,
            'is_readonly': True,
            'scope': 'public_system',
        }

    return {
        'owner': next_owner,
        'public': False,
        'is_readonly': False,
        'scope': 'private_user',
    }


def init_database() -> None:
    with engine.begin() as connection:
        connection.execute(text(f'''
            CREATE TABLE IF NOT EXISTS songs (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                artist TEXT NOT NULL,
                album TEXT NOT NULL,
                fmt TEXT NOT NULL,
                dur INTEGER NOT NULL,
                path TEXT NOT NULL,
                plays INTEGER NOT NULL DEFAULT 0,
                raw_url TEXT NOT NULL DEFAULT '',
                provider_id TEXT NOT NULL DEFAULT '',
                cover_path TEXT NOT NULL DEFAULT '',
                cover_art TEXT NOT NULL DEFAULT '',
                genre TEXT NOT NULL DEFAULT '',
                created_at {db_timestamp_type()} NOT NULL DEFAULT {db_now_expr()},
                updated_at {db_timestamp_type()} NOT NULL DEFAULT {db_now_expr()}
            )
        '''))
        add_column_if_missing(connection, 'songs', 'updated_at', f"{db_timestamp_type()} NOT NULL DEFAULT {db_now_expr()}")
        add_column_if_missing(connection, 'songs', 'raw_url', "TEXT NOT NULL DEFAULT ''")
        add_column_if_missing(connection, 'songs', 'provider_id', "TEXT NOT NULL DEFAULT ''")
        add_column_if_missing(connection, 'songs', 'cover_path', "TEXT NOT NULL DEFAULT ''")
        add_column_if_missing(connection, 'songs', 'cover_art', "TEXT NOT NULL DEFAULT ''")
        add_column_if_missing(connection, 'songs', 'genre', "TEXT NOT NULL DEFAULT ''")
        add_column_if_missing(connection, 'songs', 'album_artist', "TEXT NOT NULL DEFAULT ''")
        add_column_if_missing(connection, 'songs', 'lyrics', "TEXT NOT NULL DEFAULT ''")

        try:
            runtime_rows = connection.execute(text("SELECT key, value FROM app_settings WHERE key IN ('last_scan_provider_id', 'storage_default_provider_id')")).mappings().all()
            runtime_map = {str(row['key']): str(row['value'] or '') for row in runtime_rows}
            fallback_provider_id = (runtime_map.get('last_scan_provider_id') or runtime_map.get('storage_default_provider_id') or '').strip()
            if fallback_provider_id:
                connection.execute(
                    text("UPDATE songs SET provider_id = :provider_id WHERE TRIM(COALESCE(provider_id, '')) = ''"),
                    {'provider_id': fallback_provider_id},
                )
        except Exception:
            pass

        connection.execute(text(f'''
            CREATE TABLE IF NOT EXISTS scan_tasks (
                id TEXT PRIMARY KEY,
                path TEXT NOT NULL,
                mode TEXT NOT NULL,
                count INTEGER NOT NULL DEFAULT 0,
                total INTEGER NOT NULL DEFAULT 0,
                processed INTEGER NOT NULL DEFAULT 0,
                duration TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'alist',
                end_time TEXT NOT NULL,
                logs {db_json_type()} NOT NULL DEFAULT '{json_dumps([], [])}',
                created_at {db_timestamp_type()} NOT NULL DEFAULT {db_now_expr()}
            )
        '''))
        add_column_if_missing(connection, 'scan_tasks', 'source', "TEXT NOT NULL DEFAULT 'alist'")

        connection.execute(text(f'''
            CREATE TABLE IF NOT EXISTS scan_state (
                root_path TEXT PRIMARY KEY,
                state {db_json_type()} NOT NULL DEFAULT '{json_dumps({}, {})}',
                updated_at {db_timestamp_type()} NOT NULL DEFAULT {db_now_expr()}
            )
        '''))

        connection.execute(text(f'''
            CREATE TABLE IF NOT EXISTS file_index (
                root_path TEXT NOT NULL,
                file_path TEXT PRIMARY KEY,
                parent_dir TEXT NOT NULL DEFAULT '',
                filename TEXT NOT NULL DEFAULT '',
                size BIGINT NOT NULL DEFAULT 0,
                mtime TEXT NOT NULL DEFAULT '',
                is_audio BOOLEAN NOT NULL DEFAULT TRUE,
                last_scan_task_id TEXT NOT NULL DEFAULT '',
                last_seen_at {db_timestamp_type()} NOT NULL DEFAULT {db_now_expr()},
                updated_at {db_timestamp_type()} NOT NULL DEFAULT {db_now_expr()}
            )
        '''))
        connection.execute(text("CREATE INDEX IF NOT EXISTS idx_file_index_root_path ON file_index(root_path)"))

        connection.execute(text(f'''
            CREATE TABLE IF NOT EXISTS dirty_dirs (
                dir_path TEXT PRIMARY KEY,
                reason TEXT NOT NULL DEFAULT '',
                updated_at {db_timestamp_type()} NOT NULL DEFAULT {db_now_expr()},
                created_at {db_timestamp_type()} NOT NULL DEFAULT {db_now_expr()}
            )
        '''))

        connection.execute(text(f'''
            CREATE TABLE IF NOT EXISTS autofill_failed_uploads (
                key TEXT PRIMARY KEY,
                task_id TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                artist TEXT NOT NULL DEFAULT '',
                filename TEXT NOT NULL DEFAULT '',
                alist_path TEXT NOT NULL DEFAULT '',
                reason TEXT NOT NULL DEFAULT '',
                fail_count INTEGER NOT NULL DEFAULT 1,
                last_error TEXT NOT NULL DEFAULT '',
                updated_at {db_timestamp_type()} NOT NULL DEFAULT {db_now_expr()},
                created_at {db_timestamp_type()} NOT NULL DEFAULT {db_now_expr()}
            )
        '''))
        add_column_if_missing(connection, 'autofill_failed_uploads', 'task_id', "TEXT NOT NULL DEFAULT ''")
        add_column_if_missing(connection, 'autofill_failed_uploads', 'title', "TEXT NOT NULL DEFAULT ''")
        add_column_if_missing(connection, 'autofill_failed_uploads', 'artist', "TEXT NOT NULL DEFAULT ''")
        add_column_if_missing(connection, 'autofill_failed_uploads', 'filename', "TEXT NOT NULL DEFAULT ''")
        add_column_if_missing(connection, 'autofill_failed_uploads', 'alist_path', "TEXT NOT NULL DEFAULT ''")
        add_column_if_missing(connection, 'autofill_failed_uploads', 'reason', "TEXT NOT NULL DEFAULT ''")
        add_column_if_missing(connection, 'autofill_failed_uploads', 'fail_count', 'INTEGER NOT NULL DEFAULT 1')
        add_column_if_missing(connection, 'autofill_failed_uploads', 'last_error', "TEXT NOT NULL DEFAULT ''")

        connection.execute(text(f'''
            CREATE TABLE IF NOT EXISTS play_history (
                id {db_bigint_pk()},
                song_id TEXT NOT NULL,
                played_at {db_timestamp_type()} NOT NULL DEFAULT {db_now_expr()},
                source TEXT NOT NULL DEFAULT 'subsonic',
                submission BOOLEAN NOT NULL DEFAULT FALSE,
                client TEXT NOT NULL DEFAULT '',
                created_at {db_timestamp_type()} NOT NULL DEFAULT {db_now_expr()}
            )
        '''))

        connection.execute(text(f'''
            CREATE TABLE IF NOT EXISTS playlists (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                comment TEXT NOT NULL DEFAULT '',
                owner TEXT NOT NULL DEFAULT 'admin',
                public BOOLEAN NOT NULL DEFAULT FALSE,
                song_count INTEGER NOT NULL DEFAULT 0,
                duration INTEGER NOT NULL DEFAULT 0,
                created_at {db_timestamp_type()} NOT NULL DEFAULT {db_now_expr()},
                updated_at {db_timestamp_type()} NOT NULL DEFAULT {db_now_expr()}
            )
        '''))
        add_column_if_missing(connection, 'playlists', 'is_readonly', 'BOOLEAN NOT NULL DEFAULT FALSE')
        add_column_if_missing(connection, 'playlists', 'scope', "TEXT NOT NULL DEFAULT 'private_user'")
        connection.execute(text("UPDATE playlists SET owner = 'system', scope = 'public_system', is_readonly = TRUE, public = TRUE WHERE public = TRUE OR scope = 'public_system'"))

        connection.execute(text(f'''
            CREATE TABLE IF NOT EXISTS playlist_tracks (
                id {db_bigint_pk()},
                playlist_id TEXT NOT NULL,
                song_id TEXT NOT NULL,
                sort_order INTEGER NOT NULL DEFAULT 0,
                created_at {db_timestamp_type()} NOT NULL DEFAULT {db_now_expr()}
            )
        '''))

        connection.execute(text(f'''
            CREATE TABLE IF NOT EXISTS subsonic_users (
                id {db_bigint_pk()},
                username TEXT NOT NULL UNIQUE,
                password TEXT NOT NULL DEFAULT '',
                role TEXT NOT NULL DEFAULT 'user',
                enabled BOOLEAN NOT NULL DEFAULT TRUE,
                created_at {db_timestamp_type()} NOT NULL DEFAULT {db_now_expr()},
                updated_at {db_timestamp_type()} NOT NULL DEFAULT {db_now_expr()}
            )
        '''))
        add_column_if_missing(connection, 'subsonic_users', 'password', "TEXT NOT NULL DEFAULT ''")

        connection.execute(text(f'''
            CREATE TABLE IF NOT EXISTS starred_items (
                id {db_bigint_pk()},
                username TEXT NOT NULL,
                item_type TEXT NOT NULL,
                item_id TEXT NOT NULL,
                created_at {db_timestamp_type()} NOT NULL DEFAULT {db_now_expr()},
                UNIQUE (username, item_type, item_id)
            )
        '''))

        connection.execute(text(f'''
            CREATE TABLE IF NOT EXISTS ratings (
                id {db_bigint_pk()},
                username TEXT NOT NULL,
                song_id TEXT NOT NULL,
                rating INTEGER NOT NULL,
                created_at {db_timestamp_type()} NOT NULL DEFAULT {db_now_expr()},
                updated_at {db_timestamp_type()} NOT NULL DEFAULT {db_now_expr()},
                UNIQUE (username, song_id)
            )
        '''))

        connection.execute(text(f'''
            CREATE TABLE IF NOT EXISTS bookmarks (
                id {db_bigint_pk()},
                username TEXT NOT NULL,
                song_id TEXT NOT NULL,
                position_ms BIGINT NOT NULL DEFAULT 0,
                comment TEXT NOT NULL DEFAULT '',
                created_at {db_timestamp_type()} NOT NULL DEFAULT {db_now_expr()},
                updated_at {db_timestamp_type()} NOT NULL DEFAULT {db_now_expr()},
                UNIQUE (username, song_id)
            )
        '''))

        user_count = connection.execute(text('SELECT COUNT(*) FROM subsonic_users')).scalar_one()
        if user_count == 0:
            connection.execute(
                text('''
                    INSERT INTO subsonic_users (username, password, role, enabled)
                    VALUES (:username, :password, 'admin', TRUE)
                '''),
                {
                    'username': settings.subsonic_user,
                    'password': settings.subsonic_pass,
                },
            )



def _upsert_songs(connection, songs: list[dict[str, Any]]) -> None:
    payload = [{
        **song, 
        'genre': str(song.get('genre') or ''), 
        'cover_art': str(song.get('cover_art') or ''),
        'provider_id': str(song.get('provider_id') or ''),
        'album_artist': str(song.get('album_artist') or ''),
        'lyrics': str(song.get('lyrics') or '')
    } for song in songs]
    connection.execute(
        text(f'''
            INSERT INTO songs (id, title, artist, album, fmt, dur, path, plays, raw_url, provider_id, cover_path, cover_art, genre, album_artist, lyrics)
            VALUES (:id, :title, :artist, :album, :fmt, :dur, :path, :plays, :raw_url, :provider_id, :cover_path, :cover_art, :genre, :album_artist, :lyrics)
            ON CONFLICT (id) DO UPDATE SET
                title = EXCLUDED.title,
                artist = EXCLUDED.artist,
                album = EXCLUDED.album,
                fmt = EXCLUDED.fmt,
                dur = EXCLUDED.dur,
                path = EXCLUDED.path,
                plays = EXCLUDED.plays,
                raw_url = EXCLUDED.raw_url,
                provider_id = EXCLUDED.provider_id,
                cover_path = EXCLUDED.cover_path,
                cover_art = EXCLUDED.cover_art,
                genre = EXCLUDED.genre,
                album_artist = EXCLUDED.album_artist,
                lyrics = EXCLUDED.lyrics,
                updated_at = {db_now_expr()}
        '''),
        payload,
    )



def _build_existing_song_index(connection) -> dict[str, dict[str, Any]]:
    rows = connection.execute(
        text('''
            SELECT id, path, plays, updated_at
            FROM songs
            WHERE id LIKE 'alist-%'
            ORDER BY path ASC, plays DESC, updated_at DESC, id DESC
        ''')
    ).mappings().all()
    indexed: dict[str, dict[str, Any]] = {}
    for row in rows:
        path = str(row.get('path') or '')
        if path and path not in indexed:
            indexed[path] = dict(row)
    return indexed



def _relink_play_history(connection, old_song_id: str, new_song_id: str) -> None:
    if old_song_id == new_song_id:
        return
    connection.execute(
        text('''
            UPDATE play_history
            SET song_id = :new_song_id
            WHERE song_id = :old_song_id
        '''),
        {'old_song_id': old_song_id, 'new_song_id': new_song_id},
    )



def _path_scope_clause(root_path: str) -> tuple[str, dict[str, Any]]:
    normalized = ('/' + str(root_path or '').strip('/')) if str(root_path or '').strip('/') else '/'
    prefix = normalized.rstrip('/') + '/%'
    if normalized == '/':
        return 'path LIKE :path_prefix', {'path_prefix': '/%'}
    return '(path = :path_exact OR path LIKE :path_prefix)', {
        'path_exact': normalized,
        'path_prefix': prefix,
    }



def _delete_existing_alist_song_rows(connection, *, provider_id: str | None = None, root_path: str | None = None) -> None:
    provider = str(provider_id or '').strip()
    root = str(root_path or '/').strip() or '/'
    where_sql = []
    params: dict[str, Any] = {}
    if provider:
        where_sql.append('provider_id = :provider_id')
        params['provider_id'] = provider
    scope_sql, scope_params = _path_scope_clause(root)
    where_sql.append(scope_sql)
    params.update(scope_params)
    connection.execute(text(f"DELETE FROM songs WHERE {' AND '.join(where_sql)}"), params)



def prune_song_rows_full_rebuild(keep_ids: list[str]) -> int:
    keep = [str(item).strip() for item in keep_ids if str(item).strip()]
    with engine.begin() as connection:
        if keep:
            stmt_keep = bindparam('keep_ids', expanding=True)
            result = connection.execute(
                text('DELETE FROM songs WHERE id NOT IN :keep_ids').bindparams(stmt_keep),
                {'keep_ids': keep},
            )
        else:
            result = connection.execute(text('DELETE FROM songs'))
    return int(getattr(result, 'rowcount', 0) or 0)



def _cleanup_norm_text(value: str) -> str:
    if not value:
        return ""
    value = str(value).strip().lower()
    value = value.translate(str.maketrans({
        '（': '(', '）': ')', '【': '[', '】': ']', '｛': '{', '｝': '}',
        '，': ',', '。': '.', '、': ',', '；': ';', '：': ':', '！': '!', '？': '?',
        '“': '"', '”': '"', '‘': "'", '’': "'",
        '／': '/', '｜': '|', '－': '-', '—': '-', '–': '-', '·': ' ', '　': ' ',
        '＆': '&', '＋': '+', '＠': '@', '＃': '#', '％': '%'
    }))
    value = re.sub(r'\s+', ' ', value)
    value = re.sub(r'\s*([,.;:!?/|&+\-()\[\]{}])\s*', r'\1', value)
    return value.strip()


def _cleanup_artist_token(token: str) -> str:
    token = _cleanup_norm_text(token)
    if not token:
        return ""
    has_cjk = bool(re.search(r'[\u4e00-\u9fff]', token))
    has_ascii = bool(re.search(r'[a-z]', token))
    if has_cjk and has_ascii:
        token = re.sub(r'[a-z0-9._\-]+', '', token)
        token = re.sub(r'\s+', ' ', token).strip()
    return token


def _cleanup_split_artists(value: str) -> list[str]:
    if not value:
        return []
    parts = re.split(r'[/&,、|]+', str(value))
    cleaned = [_cleanup_artist_token(p) for p in parts if _cleanup_artist_token(p)]
    return sorted(cleaned)


def _cleanup_parse_dur(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    s = str(value).strip()
    if not s:
        return 0
    if s.isdigit():
        return int(s)
    m = re.match(r'^(\d{1,2}):(\d{2})$', s)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    return 0


def _same_song_for_cleanup(virtual_row: dict[str, Any], real_row: dict[str, Any]) -> bool:
    vt = _cleanup_norm_text(virtual_row.get('title') or '')
    rt = _cleanup_norm_text(real_row.get('title') or '')
    if not vt or vt != rt:
        return False

    va = _cleanup_split_artists(virtual_row.get('artist') or '')
    ra = _cleanup_split_artists(real_row.get('artist') or '')
    if not va or not ra or va != ra:
        return False

    vd = _cleanup_parse_dur(virtual_row.get('dur'))
    rd = _cleanup_parse_dur(real_row.get('dur'))
    if vd > 0 and rd > 0:
        title = str(real_row.get('title') or virtual_row.get('title') or '')
        lower_title = title.lower()
        tolerant = any(flag in lower_title for flag in ('dj', 'remix', 'mix', 'live', '版', '伴奏'))
        max_diff = 15 if tolerant else 12
        if abs(vd - rd) > max_diff:
            return False

    return True


def merge_virtual_song_to_real(connection, virtual_song_id: str, real_song_id: str) -> int:
    if not virtual_song_id or not real_song_id or virtual_song_id == real_song_id:
        return 0
    virtual_row = connection.execute(
        text("SELECT id, fmt FROM songs WHERE id = :song_id LIMIT 1"),
        {'song_id': virtual_song_id},
    ).mappings().first()
    real_row = connection.execute(
        text("SELECT id, fmt FROM songs WHERE id = :song_id LIMIT 1"),
        {'song_id': real_song_id},
    ).mappings().first()
    if not virtual_row or not real_row:
        return 0
    if str(virtual_row.get('fmt') or '').upper() != 'VIRTUAL':
        return 0
    if str(real_row.get('fmt') or '').upper() == 'VIRTUAL':
        return 0

    _relink_play_history(connection, virtual_song_id, real_song_id)
    connection.execute(text("DELETE FROM playlist_tracks WHERE song_id = :song_id"), {'song_id': virtual_song_id})
    connection.execute(text("DELETE FROM ratings WHERE song_id = :song_id"), {'song_id': virtual_song_id})
    connection.execute(text("DELETE FROM bookmarks WHERE song_id = :song_id"), {'song_id': virtual_song_id})
    connection.execute(text("DELETE FROM songs WHERE id = :song_id"), {'song_id': virtual_song_id})
    return 1


def cleanup_virtual_songs(connection) -> int:
    rows = connection.execute(text("SELECT id, title, artist, dur FROM songs WHERE fmt = 'VIRTUAL' ORDER BY updated_at DESC, id DESC")).mappings().all()
    real_rows = connection.execute(text("SELECT id, title, artist, dur FROM songs WHERE fmt != 'VIRTUAL' ORDER BY updated_at DESC, id DESC")).mappings().all()
    deleted = 0
    for row in rows:
        if not str(row.get('title') or '').strip():
            continue
        real_id = None
        for real_row in real_rows:
            if _same_song_for_cleanup(row, real_row):
                real_id = real_row['id']
                break
        if not real_id:
            continue
        virtual_id = row['id']
        deleted += merge_virtual_song_to_real(connection, virtual_id, real_id)
    return deleted



def merge_virtual_song_by_id(virtual_song_id: str, real_song_id: str) -> int:
    with engine.begin() as connection:
        return merge_virtual_song_to_real(connection, virtual_song_id, real_song_id)


def get_stats() -> dict[str, Any]:
    with engine.connect() as connection:
        total = connection.execute(text('SELECT COUNT(*) FROM songs')).scalar_one()
        total_albums = connection.execute(text('SELECT COUNT(DISTINCT album) FROM songs')).scalar_one()
        total_artists = connection.execute(text('SELECT COUNT(DISTINCT artist) FROM songs')).scalar_one()
        monthly_plays = connection.execute(text('SELECT COALESCE(SUM(plays), 0) FROM songs')).scalar_one()
        fmt_rows = connection.execute(text('SELECT fmt, COUNT(*) AS count FROM songs GROUP BY fmt ORDER BY count DESC, fmt ASC')).mappings().all()
    formats = [
        {
            'name': row['fmt'],
            'value': round((row['count'] / total) * 100) if total else 0,
            'color': FMT_COLORS.get(row['fmt'], '#38bdf8'),
        }
        for row in fmt_rows
    ]
    return {
        'total_songs': total,
        'total_albums': total_albums,
        'total_artists': total_artists,
        'monthly_plays': monthly_plays,
        'formats': formats,
    }


def list_songs() -> list[dict[str, Any]]:
    with engine.connect() as connection:
        rows = connection.execute(text('''
            SELECT id, title, artist, album, fmt, dur, path, plays, raw_url, provider_id, cover_path, cover_art, genre, album_artist, lyrics, created_at, updated_at
            FROM songs
            ORDER BY updated_at DESC, id ASC
        ''')).mappings().all()
    return [dict(row) for row in rows]


def list_songs_brief() -> list[dict[str, Any]]:
    with engine.connect() as connection:
        rows = connection.execute(text('''
            SELECT id, title, artist, album, fmt, dur, path, plays, cover_path, provider_id
            FROM songs
            ORDER BY updated_at DESC, id ASC
        ''')).mappings().all()
    return [dict(row) for row in rows]


def delete_songs(song_ids: list[str]) -> bool:
    if not song_ids:
        return False
    stmt_song_id = bindparam('song_ids', expanding=True)
    with engine.begin() as connection:
        connection.execute(text('DELETE FROM play_history WHERE song_id IN :song_ids').bindparams(stmt_song_id), {'song_ids': song_ids})
        connection.execute(text('DELETE FROM playlist_tracks WHERE song_id IN :song_ids').bindparams(stmt_song_id), {'song_ids': song_ids})
        connection.execute(text('DELETE FROM ratings WHERE song_id IN :song_ids').bindparams(stmt_song_id), {'song_ids': song_ids})
        connection.execute(text('DELETE FROM bookmarks WHERE song_id IN :song_ids').bindparams(stmt_song_id), {'song_ids': song_ids})
        result = connection.execute(text('DELETE FROM songs WHERE id IN :song_ids').bindparams(stmt_song_id), {'song_ids': song_ids})
    return result.rowcount > 0


def list_tasks() -> list[dict[str, Any]]:
    with engine.connect() as connection:
        rows = connection.execute(text('''
            SELECT id, path, mode, count, total, processed, duration, status, source, end_time, created_at, logs
            FROM scan_tasks
            ORDER BY created_at DESC, id DESC
        ''')).mappings().all()
    tasks: list[dict[str, Any]] = []
    for row in rows:
        logs = row['logs'] if isinstance(row['logs'], list) else json.loads(row['logs'])
        tasks.append(
            {
                'id': row['id'],
                'path': row['path'],
                'mode': row['mode'],
                'count': row['count'],
                'total': row['total'],
                'processed': row['processed'],
                'duration': row['duration'],
                'status': row['status'],
                'source': row['source'],
                'end': row['end_time'],
                'created_at': row['created_at'],
                'logs': logs,
            }
        )
    return tasks


def list_tasks_brief() -> list[dict[str, Any]]:
    with engine.connect() as connection:
        rows = connection.execute(text('''
            SELECT id, path, mode, count, total, processed, duration, status, source, end_time, created_at, logs
            FROM scan_tasks
            ORDER BY created_at DESC, id DESC
        ''')).mappings().all()
    tasks: list[dict[str, Any]] = []
    for row in rows:
        logs = row['logs'] if isinstance(row['logs'], list) else json.loads(row['logs'])
        last_logs = logs[-5:] if isinstance(logs, list) else []
        tasks.append(
            {
                'id': row['id'],
                'path': row['path'],
                'mode': row['mode'],
                'count': row['count'],
                'total': row['total'],
                'processed': row['processed'],
                'duration': row['duration'],
                'status': row['status'],
                'source': row['source'],
                'end': row['end_time'],
                'created_at': row['created_at'],
                'logs': last_logs,
            }
        )
    return tasks


def create_pending_scan_task(path: str, mode: str, concurrency: int, batch_size: int, source: str = 'pending') -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    task = {
        'id': f'T-{uuid.uuid4().hex[:12]}',
        'path': path,
        'mode': '全量' if mode == 'full' else '增量',
        'count': 0,
        'total': 0,
        'processed': 0,
        'duration': '',
        'status': 'queued',
        'source': source,
        'end': '',
        'logs': [
            {'level': 'info', 'message': f'[INIT] 扫描任务已创建，等待执行：{path}，模式 {mode}'},
            {'level': 'info', 'message': f'[INIT] 计划并发 {concurrency}，批大小 {batch_size}'},
        ],
    }
    with engine.begin() as connection:
        connection.execute(
            text(f'''
                INSERT INTO scan_tasks (id, path, mode, count, total, processed, duration, status, source, end_time, logs)
                VALUES (:id, :path, :mode, :count, :total, :processed, :duration, :status, :source, :end_time, {db_json_value_expr('logs')})
            '''),
            {
                'id': task['id'],
                'path': task['path'],
                'mode': task['mode'],
                'count': task['count'],
                'total': task['total'],
                'processed': task['processed'],
                'duration': task['duration'],
                'status': task['status'],
                'source': task['source'],
                'end_time': task['end'],
                'logs': json_dumps(task['logs'], []),
            },
        )
    return task


def update_scan_task(task_id: str, **fields: Any) -> None:
    allowed = {'count', 'total', 'processed', 'duration', 'status', 'source', 'end_time', 'logs'}
    sets = []
    params = {'task_id': task_id}
    for key, value in fields.items():
        if key not in allowed:
            continue
        sets.append(f"{key} = :{key}")
        if key == 'logs':
            params[key] = json_dumps(value, [])
        else:
            params[key] = value
    if not sets:
        return
    sql = f"UPDATE scan_tasks SET {', '.join(sets)} WHERE id = :task_id"
    with engine.begin() as connection:
        sql = sql.replace('logs = :logs', f"logs = {db_json_value_expr('logs')}")
        connection.execute(text(sql), params)


def get_task(task_id: str) -> dict[str, Any] | None:
    with engine.connect() as connection:
        row = connection.execute(text('''
            SELECT id, path, mode, count, total, processed, duration, status, source, end_time, created_at, logs
            FROM scan_tasks
            WHERE id = :task_id
        '''), {'task_id': task_id}).mappings().first()
    if not row:
        return None
    logs = row['logs'] if isinstance(row['logs'], list) else json.loads(row['logs'])
    return {
        'id': row['id'],
        'path': row['path'],
        'mode': row['mode'],
        'count': row['count'],
        'total': row['total'],
        'processed': row['processed'],
        'duration': row['duration'],
        'status': row['status'],
        'source': row['source'],
        'end': row['end_time'],
        'created_at': row['created_at'],
        'logs': logs,
    }


def delete_scan_task(task_id: str) -> bool:
    with engine.begin() as connection:
        result = connection.execute(text('DELETE FROM scan_tasks WHERE id = :task_id'), {'task_id': task_id})
        return result.rowcount > 0


def get_scan_state(root_path: str) -> dict[str, Any]:
    with engine.connect() as connection:
        row = connection.execute(
            text('SELECT state FROM scan_state WHERE root_path = :root_path'),
            {'root_path': root_path},
        ).scalar_one_or_none()
    if row is None:
        return {'version': 1, 'root': root_path, 'dirs': {}}
    data = row if isinstance(row, dict) else json.loads(row)
    if not isinstance(data, dict):
        return {'version': 1, 'root': root_path, 'dirs': {}}
    if not isinstance(data.get('dirs'), dict):
        data['dirs'] = {}
    return data


def save_scan_state(root_path: str, state: dict[str, Any]) -> None:
    with engine.begin() as connection:
        connection.execute(text(f'''
            INSERT INTO scan_state (root_path, state, updated_at)
            VALUES (:root_path, {db_json_value_expr('state')}, {db_now_expr()})
            ON CONFLICT (root_path) DO UPDATE SET
                state = EXCLUDED.state,
                updated_at = {db_now_expr()}
        '''), {
            'root_path': root_path,
            'state': json_dumps(state, {}),
        })



def reset_all_scan_state_dirs() -> None:
    with engine.begin() as connection:
        rows = connection.execute(text('SELECT root_path, state FROM scan_state')).mappings().all()
        for row in rows:
            root_path = str(row.get('root_path') or '')
            raw_state = row.get('state')
            try:
                state = json_loads(raw_state or '{}', {}) if not isinstance(raw_state, dict) else dict(raw_state)
            except Exception:
                state = {}
            state['version'] = 1
            state['root'] = root_path
            state['dirs'] = {}
            connection.execute(text(f'''
                UPDATE scan_state
                SET state = {db_json_value_expr('state')}, updated_at = {db_now_expr()}
                WHERE root_path = :root_path
            '''), {
                'root_path': root_path,
                'state': json_dumps(state, {}),
            })


def get_file_index(root_path: str) -> dict[str, dict[str, Any]]:
    with engine.connect() as connection:
        rows = connection.execute(text('''
            SELECT file_path, parent_dir, filename, size, mtime, is_audio, last_scan_task_id
            FROM file_index
            WHERE root_path = :root_path
        '''), {'root_path': root_path}).mappings().all()
    return {str(row['file_path']): dict(row) for row in rows}


def sync_file_index(root_path: str, files: list[dict[str, Any]], task_id: str = '') -> None:
    indexed_paths = {str(item.get('file_path') or '') for item in files if str(item.get('file_path') or '')}
    with engine.begin() as connection:
        if indexed_paths:
            connection.execute(text('''
                DELETE FROM file_index
                WHERE root_path = :root_path AND file_path NOT IN :paths
            ''').bindparams(bindparam('paths', expanding=True)), {
                'root_path': root_path,
                'paths': list(indexed_paths),
            })
        else:
            connection.execute(text('DELETE FROM file_index WHERE root_path = :root_path'), {'root_path': root_path})
        for item in files:
            file_path = str(item.get('file_path') or '')
            if not file_path:
                continue
            connection.execute(text(f'''
                INSERT INTO file_index (root_path, file_path, parent_dir, filename, size, mtime, is_audio, last_scan_task_id, last_seen_at, updated_at)
                VALUES (:root_path, :file_path, :parent_dir, :filename, :size, :mtime, :is_audio, :last_scan_task_id, {db_now_expr()}, {db_now_expr()})
                ON CONFLICT (file_path) DO UPDATE SET
                    root_path = EXCLUDED.root_path,
                    parent_dir = EXCLUDED.parent_dir,
                    filename = EXCLUDED.filename,
                    size = EXCLUDED.size,
                    mtime = EXCLUDED.mtime,
                    is_audio = EXCLUDED.is_audio,
                    last_scan_task_id = EXCLUDED.last_scan_task_id,
                    last_seen_at = {db_now_expr()},
                    updated_at = {db_now_expr()}
            '''), {
                'root_path': root_path,
                'file_path': file_path,
                'parent_dir': str(item.get('parent_dir') or ''),
                'filename': str(item.get('filename') or ''),
                'size': int(item.get('size') or 0),
                'mtime': str(item.get('mtime') or ''),
                'is_audio': bool(item.get('is_audio', True)),
                'last_scan_task_id': task_id or str(item.get('last_scan_task_id') or ''),
            })


def mark_dirty_dir(dir_path: str, reason: str = '') -> None:
    with engine.begin() as connection:
        connection.execute(text(f'''
            INSERT INTO dirty_dirs (dir_path, reason, updated_at)
            VALUES (:dir_path, :reason, {db_now_expr()})
            ON CONFLICT (dir_path) DO UPDATE SET
                reason = EXCLUDED.reason,
                updated_at = {db_now_expr()}
        '''), {
            'dir_path': dir_path,
            'reason': reason,
        })


def list_dirty_dirs(root_path: str) -> list[str]:
    prefix = root_path.rstrip('/') + '/%'
    with engine.connect() as connection:
        rows = connection.execute(text('''
            SELECT dir_path
            FROM dirty_dirs
            WHERE dir_path = :root_path OR dir_path LIKE :prefix
            ORDER BY dir_path ASC
        '''), {
            'root_path': root_path,
            'prefix': prefix,
        }).scalars().all()
    return [str(row) for row in rows]


def clear_dirty_dirs_under(root_path: str) -> None:
    prefix = root_path.rstrip('/') + '/%'
    with engine.begin() as connection:
        connection.execute(text('''
            DELETE FROM dirty_dirs
            WHERE dir_path = :root_path OR dir_path LIKE :prefix
        '''), {
            'root_path': root_path,
            'prefix': prefix,
        })


def upsert_autofill_failed_upload(
    *,
    task_id: str,
    title: str,
    artist: str,
    filename: str,
    alist_path: str,
    reason: str,
    last_error: str,
) -> None:
    key_src = f"{str(title or '').strip().lower()}|{str(artist or '').strip().lower()}|{str(filename or '').strip().lower()}|{str(alist_path or '').strip().lower()}"
    key = hashlib.sha1(key_src.encode('utf-8')).hexdigest()
    with engine.begin() as connection:
        connection.execute(text(f'''
            INSERT INTO autofill_failed_uploads (key, task_id, title, artist, filename, alist_path, reason, fail_count, last_error)
            VALUES (:key, :task_id, :title, :artist, :filename, :alist_path, :reason, 1, :last_error)
            ON CONFLICT (key) DO UPDATE SET
                task_id = EXCLUDED.task_id,
                reason = EXCLUDED.reason,
                last_error = EXCLUDED.last_error,
                fail_count = autofill_failed_uploads.fail_count + 1,
                updated_at = {db_now_expr()}
        '''), {
            'key': key,
            'task_id': task_id,
            'title': title,
            'artist': artist,
            'filename': filename,
            'alist_path': alist_path,
            'reason': reason,
            'last_error': last_error,
        })


def clear_autofill_failed_upload(
    *,
    title: str,
    artist: str,
    filename: str,
    alist_path: str,
) -> None:
    key_src = f"{str(title or '').strip().lower()}|{str(artist or '').strip().lower()}|{str(filename or '').strip().lower()}|{str(alist_path or '').strip().lower()}"
    key = hashlib.sha1(key_src.encode('utf-8')).hexdigest()
    with engine.begin() as connection:
        connection.execute(text('DELETE FROM autofill_failed_uploads WHERE key = :key'), {'key': key})


def list_autofill_failed_uploads() -> list[dict[str, Any]]:
    with engine.connect() as connection:
        rows = connection.execute(text('''
            SELECT key, task_id, title, artist, filename, alist_path, reason, fail_count, last_error, updated_at, created_at
            FROM autofill_failed_uploads
            ORDER BY updated_at DESC, created_at DESC
        ''')).mappings().all()
    return [dict(row) for row in rows]


def export_autofill_failed_uploads_text() -> str:
    rows = list_autofill_failed_uploads()
    lines = []
    for row in rows:
        lines.append(
            f"{row['title']} - {row['artist']}\t{row['filename']}\t{row['alist_path']}\t{row['reason']}\t次数={row['fail_count']}\t最近={row['updated_at']}"
        )
    return '\n'.join(lines) + ('\n' if lines else '')


def clear_autofill_failed_uploads() -> int:
    with engine.begin() as connection:
        result = connection.execute(text('DELETE FROM autofill_failed_uploads'))
    return int(getattr(result, 'rowcount', 0) or 0)


async def create_scan_task(
    path: str,
    mode: str,
    concurrency: int,
    batch_size: int,
    task_id: str | None = None,
    progress_callback: Any | None = None,
    source: str = 'alist',
    provider_id: str | None = None,
) -> dict[str, Any]:
    if progress_callback:
        result = progress_callback(
            status='running',
            source=source,
            count=0,
            total=0,
            processed=0,
            duration='',
            end_time='',
            logs=[{'level': 'info', 'message': '[RUN] 开始扫描目录与解析元数据'}],
        )
        if inspect.isawaitable(result):
            await result
    
    from app.services.scan_service import CANCELLED_TASKS
    if task_id and task_id in CANCELLED_TASKS:
        return {'status': 'cancelled', 'source': 'failed'}
        
    result = await collect_scan_result(path=path, mode=mode, task_id=task_id, concurrency=concurrency, progress_callback=progress_callback, provider_id=provider_id or source)
    task = result['task']
    if task_id:
        task['id'] = task_id
    task['logs'].insert(1, {'level': 'info', 'message': f'[INIT] 并发 {concurrency}，批大小 {batch_size}'})

    if task.get('status') != 'done' or result.get('source') != 'alist':
        await asyncio.to_thread(
            update_scan_task,
            task['id'],
            count=task['count'],
            total=task['total'],
            processed=task['processed'],
            duration=task['duration'],
            status=task['status'],
            source=result['source'],
            end_time=task['end'],
            logs=task['logs'],
        )
        return {
            **task,
            'source': result['source'],
        }

    batch_target = max(1, int(batch_size or 100))
    pending_batch: list[dict[str, Any]] = []
    total_songs = len(result['songs'])
    cover_hits = 0
    duration_hits = 0
    cover_miss_samples: list[str] = []
    duration_miss_samples: list[str] = []
    batch_count = 0

    existing_song_index: dict[str, dict[str, Any]] = {}
    if result['source'] == 'alist':
        def prepare_existing() -> dict[str, dict[str, Any]]:
            with engine.begin() as connection:
                existing = _build_existing_song_index(connection)
                return existing

        existing_song_index = await asyncio.to_thread(prepare_existing)
        if task_id and task_id in CANCELLED_TASKS:
            return {'status': 'cancelled', 'source': 'failed'}
            
        if progress_callback:
            progress = progress_callback(
                status='running',
                source=source,
                count=total_songs,
                total=total_songs,
                processed=0,
                duration='',
                end_time='',
                logs=[{'level': 'info', 'message': '[RUN] 已完成曲库预处理，准备写入扫描结果'}],
            )
            if inspect.isawaitable(progress):
                await progress

    for idx, song in enumerate(result['songs'], start=1):
        if task_id and task_id in CANCELLED_TASKS:
            return {'status': 'cancelled', 'source': 'failed'}
            
        item = dict(song)
        if result['source'] == 'alist':
            previous = existing_song_index.get(str(item.get('path') or ''))
            if previous:
                item['plays'] = int(previous.get('plays') or 0)

        cover_art = item.get('cover_art', '')
        if not item.get('cover_path'):
            item['cover_path'] = await asyncio.to_thread(save_cover_base64, cover_art) if cover_art else ''
        if item['cover_path']:
            cover_hits += 1
        elif len(cover_miss_samples) < 5:
            cover_miss_samples.append(item.get('title') or item.get('path') or item.get('id') or 'unknown')
        if int(item.get('dur') or 0) > 0:
            duration_hits += 1
        elif len(duration_miss_samples) < 5:
            duration_miss_samples.append(item.get('title') or item.get('path') or item.get('id') or 'unknown')

        pending_batch.append(item)
        if len(pending_batch) >= batch_target:
            if mode == 'full':
                await asyncio.to_thread(_upsert_songs_batch, pending_batch)
            batch_count += 1
            pending_batch.clear()
            
            # 为了让用户在解析大库时不必等到所有几万首歌全解析完才落库
            # 每当落库了一批，我们就把最新的进度刷新到数据库，这样前端可以直接看到正在“边落库边增长”
            if progress_callback:
                progress = progress_callback(
                    status='running',
                    source=source,
                    count=total_songs,
                    total=total_songs,
                    processed=idx,
                    duration='',
                    end_time='',
                    logs=[{'level': 'info', 'message': f'[RUN] 已写入第 {batch_count} 批，进度 {idx}/{total_songs}'}],
                )
                if inspect.isawaitable(progress):
                    await progress

    if pending_batch:
        if mode == 'full':
            await asyncio.to_thread(_upsert_songs_batch, pending_batch)
        batch_count += 1
        pending_batch.clear()
        if progress_callback:
            progress = progress_callback(
                status='running',
                source=source,
                count=total_songs,
                total=total_songs,
                processed=total_songs,
                duration='',
                end_time='',
                logs=[{'level': 'info', 'message': f'[RUN] 已写入尾批，进度 {total_songs}/{total_songs}'}],
            )
            if inspect.isawaitable(progress):
                await progress

    if result['source'] == 'alist' and total_songs:
        def finalize_alist_write() -> int:
            with engine.begin() as connection:
                return cleanup_virtual_songs(connection)

        deleted_virtuals = await asyncio.to_thread(finalize_alist_write)
        task['logs'].append({'level': 'info', 'message': f'[INFO] 封面命中 {cover_hits}/{total_songs} 首'})
        task['logs'].append({'level': 'info', 'message': f'[INFO] 时长命中 {duration_hits}/{total_songs} 首'})
        if cover_miss_samples:
            task['logs'].append({'level': 'warn', 'message': f"[WARN] 未命中封面样本：{' / '.join(cover_miss_samples)}"})
        if duration_miss_samples:
            task['logs'].append({'level': 'warn', 'message': f"[WARN] 未命中时长样本：{' / '.join(duration_miss_samples)}"})
        task['logs'].append({'level': 'info', 'message': f'[INFO] 分批写库计划：{batch_target} 条/批'})
        task['logs'].append({'level': 'info', 'message': f'[INFO] 分批写库完成：{batch_count} 批，批大小 {batch_target}'})
        if deleted_virtuals > 0:
            task['logs'].append({'level': 'ok', 'message': f'[OK] 已清理 {deleted_virtuals} 条已被真实曲目替代的 VIRTUAL 记录'})

    final_logs = task['logs']
    if progress_callback:
        latest_task = await asyncio.to_thread(get_task, task['id'])
        latest_logs = list(latest_task.get('logs', [])) if latest_task else []
        if latest_logs:
            merged_logs = list(latest_logs)
            existing_messages = {entry.get('message') for entry in merged_logs}
            for entry in task['logs']:
                if entry.get('message') not in existing_messages:
                    merged_logs.append(entry)
            final_logs = merged_logs

    await asyncio.to_thread(
        update_scan_task,
        task['id'],
        count=task['count'],
        total=task['total'],
        processed=task['processed'],
        duration=task['duration'],
        status=task['status'],
        source=result['source'],
        end_time=task['end'],
        logs=final_logs,
    )

    return {
        **task,
        'source': result['source'],
    }


def _upsert_songs_batch(songs: list[dict[str, Any]]) -> None:
    with engine.begin() as connection:
        _upsert_songs(connection, songs)


def get_subsonic_items() -> list[dict[str, str]]:
    return [dict(item) for item in SUBSONIC_ITEMS]


def _to_epoch_seconds(value: Any) -> int:
    """Best-effort conversion of the stored timestamp formats to epoch seconds."""
    if value is None:
        return 0
    if isinstance(value, datetime):
        moment = value
    else:
        raw = str(value).strip()
        if not raw:
            return 0
        try:
            moment = datetime.fromisoformat(raw.replace('Z', '+00:00'))
        except ValueError:
            return 0
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return int(moment.timestamp())


def build_indexes() -> dict[str, Any]:
    songs = list_songs()
    grouped: dict[str, list[dict[str, Any]]] = {}
    artist_map: dict[str, list[dict[str, Any]]] = {}
    last_modified = 0
    for song in songs:
        artist_name = str(song.get('artist') or '').strip()
        if not artist_name:
            continue
        artist_map.setdefault(artist_name, []).append(song)
        last_modified = max(last_modified, _to_epoch_seconds(song.get('updated_at')))
    for artist in sorted(artist_map.keys()):
        # Subsonic index groups are a single A-Z letter or '#'; non-latin
        # names must not leak a CJK character as the index name.
        head = artist[0].upper()
        letter = head if 'A' <= head <= 'Z' else '#'
        artist_songs = artist_map[artist]
        grouped.setdefault(letter, []).append(
            {
                'id': artist_id(artist),
                'name': artist,
                'albumCount': len({song['album'] for song in artist_songs}),
                'coverArt': next((song['id'] for song in artist_songs if song.get('cover_path')), ''),
            }
        )
    ordered = sorted(grouped.items(), key=lambda item: (item[0] == '#', item[0]))
    return {
        'lastModified': last_modified * 1000,
        'ignoredArticles': 'The El La Los Las Le Les',
        'index': [{'name': key, 'artist': value} for key, value in ordered],
    }


def build_album_list() -> list[dict[str, Any]]:
    songs = list_songs()
    album_map: dict[str, list[dict[str, Any]]] = {}
    for song in songs:
        album_map.setdefault(str(song['album']), []).append(song)

    albums: list[dict[str, Any]] = []
    for album_name, album_songs in album_map.items():
        first_song = album_songs[0]
        created = max(
            (dt_to_iso(item.get('created_at')) for item in album_songs if item.get('created_at')),
            default='',
        )
        albums.append(
            {
                'id': album_id(album_name),
                'name': album_name,
                'title': album_name,
                'artist': first_song['artist'],
                'artistId': artist_id(first_song['artist']),
                'songCount': len(album_songs),
                'duration': sum(int(item.get('dur') or 0) for item in album_songs),
                'created': created,
                'year': 0,
                'genre': str(first_song.get('genre') or ''),
                'coverArt': next((item['id'] for item in album_songs if item.get('cover_path')), first_song['id'] if first_song.get('cover_path') else ''),
            }
        )
    return albums


def get_song(song_id: str) -> dict[str, Any] | None:
    with engine.connect() as connection:
        row = connection.execute(text('''
            SELECT id, title, artist, album, fmt, dur, path, plays, raw_url, provider_id, cover_path, cover_art, genre, album_artist, lyrics
            FROM songs
            WHERE id = :song_id
        '''), {'song_id': song_id}).mappings().first()
    return dict(row) if row else None

def update_song(song_id: str, fields: dict[str, Any]) -> dict[str, Any] | None:
    with engine.begin() as connection:
        updates = []
        payload = {'song_id': song_id}
        for k, v in fields.items():
            if k in ['title', 'artist', 'album', 'genre', 'cover_art', 'cover_path', 'album_artist', 'lyrics', 'raw_url']:
                updates.append(f"{k} = :{k}")
                payload[k] = str(v) if v is not None else ''
        if not updates:
            return get_song(song_id)
            
        updates.append(f"updated_at = {db_now_expr()}")
        set_clause = ", ".join(updates)
        
        connection.execute(text(f'''
            UPDATE songs SET {set_clause} WHERE id = :song_id
        '''), payload)
        
    return get_song(song_id)



def increment_song_plays(song_id: str, amount: int = 1) -> dict[str, Any] | None:
    step = max(1, int(amount or 1))
    with engine.begin() as connection:
        connection.execute(
            text(f'''
                UPDATE songs
                SET plays = plays + :step,
                    updated_at = {db_now_expr()}
                WHERE id = :song_id
            '''),
            {'song_id': song_id, 'step': step},
        )
    return get_song(song_id)



def record_play_event(
    song_id: str,
    source: str = 'subsonic',
    submission: bool = False,
    client: str = '',
    amount: int = 1,
) -> dict[str, Any] | None:
    step = max(1, int(amount or 1))
    with engine.begin() as connection:
        connection.execute(
            text('''
                INSERT INTO play_history (song_id, source, submission, client)
                VALUES (:song_id, :source, :submission, :client)
            '''),
            {
                'song_id': song_id,
                'source': source,
                'submission': bool(submission),
                'client': client,
            },
        )
        connection.execute(
            text(f'''
                UPDATE songs
                SET plays = plays + :step,
                    updated_at = {db_now_expr()}
                WHERE id = :song_id
            '''),
            {'song_id': song_id, 'step': step},
        )
    return get_song(song_id)



def _play_timestamp_text(value: Any) -> str:
    if value is None:
        return ''
    if isinstance(value, datetime):
        return value.strftime('%Y-%m-%d %H:%M UTC')
    # SQLite stores timestamps as TEXT, so `.strftime` would blow up.
    return str(value).strip()


def list_recent_plays(limit: int = 10) -> list[dict[str, Any]]:
    page_size = max(1, min(int(limit or 10), 50))
    with engine.connect() as connection:
        rows = connection.execute(
            text('''
                SELECT ph.id, ph.song_id, ph.played_at, ph.source, ph.submission, ph.client,
                       s.title, s.artist, s.album, s.fmt, s.path, s.cover_path, s.plays
                FROM play_history ph
                JOIN songs s ON s.id = ph.song_id
                ORDER BY ph.played_at DESC, ph.id DESC
                LIMIT :limit
            '''),
            {'limit': page_size},
        ).mappings().all()
    return [
        {
            'id': row['id'],
            'song_id': row['song_id'],
            'played_at': _play_timestamp_text(row['played_at']),
            'source': row['source'],
            'submission': row['submission'],
            'client': row['client'],
            'title': row['title'],
            'artist': row['artist'],
            'album': row['album'],
            'fmt': row['fmt'],
            'path': row['path'],
            'cover_path': row['cover_path'],
            'plays': row['plays'],
        }
        for row in rows
    ]



def list_top_songs(limit: int = 10) -> list[dict[str, Any]]:
    page_size = max(1, min(int(limit or 10), 50))
    with engine.connect() as connection:
        rows = connection.execute(
            text('''
                SELECT id, title, artist, album, fmt, dur, path, plays, raw_url, provider_id, cover_path, cover_art, genre, album_artist, lyrics
                FROM songs
                ORDER BY plays DESC, updated_at DESC, id ASC
                LIMIT :limit
            '''),
            {'limit': page_size},
        ).mappings().all()
    return [dict(row) for row in rows]



def list_random_songs(limit: int = 10) -> list[dict[str, Any]]:
    page_size = max(1, min(int(limit or 10), 200))
    with engine.connect() as connection:
        rows = connection.execute(
            text('''
                SELECT id, title, artist, album, fmt, dur, path, plays, raw_url, provider_id, cover_path, cover_art, genre, album_artist, lyrics
                FROM songs
                ORDER BY RANDOM()
                LIMIT :limit
            '''),
            {'limit': page_size},
        ).mappings().all()
    return [dict(row) for row in rows]



def list_newest_songs(limit: int = 10) -> list[dict[str, Any]]:
    page_size = max(1, min(int(limit or 10), 200))
    with engine.connect() as connection:
        rows = connection.execute(
            text('''
                SELECT id, title, artist, album, fmt, dur, path, plays, raw_url, provider_id, cover_path, cover_art, genre, album_artist, lyrics
                FROM songs
                ORDER BY created_at DESC, updated_at DESC, id ASC
                LIMIT :limit
            '''),
            {'limit': page_size},
        ).mappings().all()
    return [dict(row) for row in rows]



def set_starred_item(username: str, item_type: str, item_id: str, starred: bool) -> None:
    payload = {'username': str(username or '').strip(), 'item_type': str(item_type or '').strip(), 'item_id': str(item_id or '').strip()}
    if not all(payload.values()):
        return
    with engine.begin() as connection:
        if starred:
            connection.execute(
                text('''
                    INSERT INTO starred_items (username, item_type, item_id)
                    VALUES (:username, :item_type, :item_id)
                    ON CONFLICT (username, item_type, item_id) DO NOTHING
                '''),
                payload,
            )
        else:
            connection.execute(
                text('''
                    DELETE FROM starred_items
                    WHERE username = :username AND item_type = :item_type AND item_id = :item_id
                '''),
                payload,
            )



def list_starred_items(username: str) -> dict[str, list[dict[str, Any]]]:
    uname = str(username or '').strip()
    result = {'song': [], 'album': [], 'artist': []}
    if not uname:
        return result
    songs = list_songs()
    song_map = {str(song.get('id') or ''): song for song in songs}
    album_map: dict[str, dict[str, Any]] = {}
    artist_map: dict[str, dict[str, Any]] = {}
    for song in songs:
        album_map.setdefault(album_id(str(song.get('album') or '')), song)
        artist_map.setdefault(artist_id(str(song.get('artist') or '')), song)
    with engine.connect() as connection:
        rows = connection.execute(
            text('''
                SELECT item_type, item_id, created_at
                FROM starred_items
                WHERE username = :username
                ORDER BY created_at DESC, id DESC
            '''),
            {'username': uname},
        ).mappings().all()
    for row in rows:
        item_type = str(row.get('item_type') or '')
        item_id = str(row.get('item_id') or '')
        if item_type == 'song' and item_id in song_map:
            result['song'].append(dict(song_map[item_id]))
        elif item_type == 'album' and item_id in album_map:
            result['album'].append(dict(album_map[item_id]))
        elif item_type == 'artist' and item_id in artist_map:
            result['artist'].append(dict(artist_map[item_id]))
    return result



def set_song_rating(username: str, song_id: str, rating: int) -> None:
    uname = str(username or '').strip()
    sid = str(song_id or '').strip()
    if not uname or not sid:
        return
    score = max(0, min(int(rating or 0), 5))
    with engine.begin() as connection:
        if score <= 0:
            connection.execute(text('DELETE FROM ratings WHERE username = :username AND song_id = :song_id'), {'username': uname, 'song_id': sid})
        else:
            connection.execute(
                text(f'''
                    INSERT INTO ratings (username, song_id, rating)
                    VALUES (:username, :song_id, :rating)
                    ON CONFLICT (username, song_id)
                    DO UPDATE SET rating = EXCLUDED.rating, updated_at = {db_now_expr()}
                '''),
                {'username': uname, 'song_id': sid, 'rating': score},
            )



def get_song_rating(username: str, song_id: str) -> int:
    uname = str(username or '').strip()
    sid = str(song_id or '').strip()
    if not uname or not sid:
        return 0
    with engine.connect() as connection:
        value = connection.execute(
            text('SELECT rating FROM ratings WHERE username = :username AND song_id = :song_id'),
            {'username': uname, 'song_id': sid},
        ).scalar()
    return int(value or 0)



def save_bookmark(username: str, song_id: str, position_ms: int, comment: str = '') -> None:
    uname = str(username or '').strip()
    sid = str(song_id or '').strip()
    if not uname or not sid:
        return
    with engine.begin() as connection:
        connection.execute(
            text(f'''
                INSERT INTO bookmarks (username, song_id, position_ms, comment)
                VALUES (:username, :song_id, :position_ms, :comment)
                ON CONFLICT (username, song_id)
                DO UPDATE SET position_ms = EXCLUDED.position_ms, comment = EXCLUDED.comment, updated_at = {db_now_expr()}
            '''),
            {'username': uname, 'song_id': sid, 'position_ms': max(0, int(position_ms or 0)), 'comment': str(comment or '')},
        )



def list_bookmarks(username: str) -> list[dict[str, Any]]:
    uname = str(username or '').strip()
    if not uname:
        return []
    with engine.connect() as connection:
        rows = connection.execute(
            text('''
                SELECT b.song_id, b.position_ms, b.comment, b.updated_at,
                       s.id, s.title, s.artist, s.album, s.fmt, s.dur, s.path, s.plays, s.raw_url, s.cover_path, s.cover_art, s.genre
                FROM bookmarks b
                JOIN songs s ON s.id = b.song_id
                WHERE b.username = :username
                ORDER BY b.updated_at DESC, b.id DESC
            '''),
            {'username': uname},
        ).mappings().all()
    return [dict(row) for row in rows]



def get_bookmark(username: str, song_id: str) -> dict[str, Any] | None:
    uname = str(username or '').strip()
    sid = str(song_id or '').strip()
    if not uname or not sid:
        return None
    with engine.connect() as connection:
        row = connection.execute(
            text('''
                SELECT song_id, position_ms, comment, updated_at
                FROM bookmarks
                WHERE username = :username AND song_id = :song_id
            '''),
            {'username': uname, 'song_id': sid},
        ).mappings().first()
    return dict(row) if row else None



def is_starred_item(username: str, item_type: str, item_id: str) -> bool:
    uname = str(username or '').strip()
    if not uname or not item_type or not item_id:
        return False
    with engine.connect() as connection:
        value = connection.execute(
            text('''
                SELECT 1 FROM starred_items
                WHERE username = :username AND item_type = :item_type AND item_id = :item_id
                LIMIT 1
            '''),
            {'username': uname, 'item_type': item_type, 'item_id': item_id},
        ).scalar()
    return bool(value)


def get_starred_at(username: str, item_type: str, item_id: str) -> str:
    """Return the ISO-8601 timestamp of a starred item, or '' when not starred.

    Subsonic represents ``starred`` as a date string in JSON, not as a boolean;
    returning the real timestamp keeps strict clients (Symfonium) happy and
    still works as a truthiness check.
    """
    uname = str(username or '').strip()
    itype = str(item_type or '').strip()
    iid = str(item_id or '').strip()
    if not uname or not itype or not iid:
        return ''
    with engine.connect() as connection:
        value = connection.execute(
            text('''
                SELECT created_at FROM starred_items
                WHERE username = :username AND item_type = :item_type AND item_id = :item_id
                LIMIT 1
            '''),
            {'username': uname, 'item_type': itype, 'item_id': iid},
        ).scalar()
    return dt_to_iso(value)



def list_genres() -> list[dict[str, Any]]:
    with engine.connect() as connection:
        rows = connection.execute(
            text('''
                SELECT genre, COUNT(*) AS song_count
                FROM songs
                WHERE COALESCE(TRIM(genre), '') <> ''
                GROUP BY genre
                ORDER BY song_count DESC, genre ASC
            ''')
        ).mappings().all()
    return [dict(row) for row in rows]



def list_songs_by_genre(genre: str, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
    wanted = str(genre or '').strip()
    page_size = max(1, min(int(limit or 50), 500))
    page_offset = max(0, int(offset or 0))
    if not wanted:
        return []
    with engine.connect() as connection:
        rows = connection.execute(
            text('''
                SELECT id, title, artist, album, fmt, dur, path, plays, raw_url, provider_id, cover_path, cover_art, genre, album_artist, lyrics
                FROM songs
                WHERE genre = :genre
                ORDER BY plays DESC, updated_at DESC, id ASC
                LIMIT :limit OFFSET :offset
            '''),
            {'genre': wanted, 'limit': page_size, 'offset': page_offset},
        ).mappings().all()
    return [dict(row) for row in rows]



def _playlist_summary_row(connection, playlist_id: str) -> dict[str, Any] | None:
    row = connection.execute(
        text('''
            SELECT id, name, comment, owner, public, is_readonly, scope, song_count, duration, created_at, updated_at
            FROM playlists
            WHERE id = :playlist_id
        '''),
        {'playlist_id': playlist_id},
    ).mappings().first()
    return dict(row) if row else None



def _playlist_entry_rows(connection, playlist_id: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        text('''
            SELECT pt.song_id, pt.sort_order,
                   s.id, s.title, s.artist, s.album, s.fmt, s.dur, s.path, s.plays, s.raw_url, s.cover_path, s.cover_art
            FROM playlist_tracks pt
            JOIN songs s ON s.id = pt.song_id
            WHERE pt.playlist_id = :playlist_id
            ORDER BY pt.sort_order ASC, pt.id ASC
        '''),
        {'playlist_id': playlist_id},
    ).mappings().all()
    return [dict(row) for row in rows]



def _refresh_playlist_meta(connection, playlist_id: str) -> dict[str, Any] | None:
    rows = _playlist_entry_rows(connection, playlist_id)
    song_count = len(rows)
    duration = sum(int(row.get('dur') or 0) for row in rows)
    connection.execute(
        text(f'''
            UPDATE playlists
            SET song_count = :song_count,
                duration = :duration,
                updated_at = {db_now_expr()}
            WHERE id = :playlist_id
        '''),
        {'playlist_id': playlist_id, 'song_count': song_count, 'duration': duration},
    )
    return _playlist_summary_row(connection, playlist_id)



def list_playlists() -> list[dict[str, Any]]:
    with engine.connect() as connection:
        rows = connection.execute(
            text('''
                SELECT id, name, comment, owner, public, is_readonly, scope, song_count, duration, created_at, updated_at
                FROM playlists
                ORDER BY updated_at DESC, created_at DESC, id ASC
            ''')
        ).mappings().all()
    return [dict(row) for row in rows]



def get_playlist(playlist_id: str) -> dict[str, Any] | None:
    with engine.connect() as connection:
        playlist = _playlist_summary_row(connection, playlist_id)
        if not playlist:
            return None
        playlist['entries'] = _playlist_entry_rows(connection, playlist_id)
    return playlist



def create_playlist(
    name: str,
    song_ids: list[str] | None = None,
    comment: str = '',
    owner: str = 'admin',
    public: bool = False,
    is_readonly: bool = False,
    scope: str = 'private_user',
) -> dict[str, Any]:
    import uuid

    playlist_id = uuid.uuid4().hex[:16]
    valid_song_ids = [song_id for song_id in (song_ids or []) if get_song(song_id)]
    policy = _normalize_playlist_policy(owner=owner, public=public, is_readonly=is_readonly, scope=scope)
    with engine.begin() as connection:
        connection.execute(
            text('''
                INSERT INTO playlists (id, name, comment, owner, public, is_readonly, scope)
                VALUES (:id, :name, :comment, :owner, :public, :is_readonly, :scope)
            '''),
            {
                'id': playlist_id,
                'name': name,
                'comment': comment,
                'owner': policy['owner'],
                'public': policy['public'],
                'is_readonly': policy['is_readonly'],
                'scope': policy['scope'],
            },
        )
        for index, song_id in enumerate(valid_song_ids):
            connection.execute(
                text('''
                    INSERT INTO playlist_tracks (playlist_id, song_id, sort_order)
                    VALUES (:playlist_id, :song_id, :sort_order)
                '''),
                {'playlist_id': playlist_id, 'song_id': song_id, 'sort_order': index},
            )
        _refresh_playlist_meta(connection, playlist_id)
    return get_playlist(playlist_id) or {'id': playlist_id, 'name': name, 'entries': []}



def update_playlist(
    playlist_id: str,
    name: str | None = None,
    comment: str | None = None,
    public: bool | None = None,
    is_readonly: bool | None = None,
    scope: str | None = None,
    owner: str | None = None,
    song_ids_to_add: list[str] | None = None,
    song_indexes_to_remove: list[int] | None = None,
    song_ids_to_remove: list[str] | None = None,
) -> dict[str, Any] | None:
    with engine.begin() as connection:
        playlist = _playlist_summary_row(connection, playlist_id)
        if not playlist:
            return None

        if name is not None:
            connection.execute(text('UPDATE playlists SET name = :name WHERE id = :playlist_id'), {'playlist_id': playlist_id, 'name': name})
        if comment is not None:
            connection.execute(text('UPDATE playlists SET comment = :comment WHERE id = :playlist_id'), {'playlist_id': playlist_id, 'comment': comment})

        effective_public = public if public is not None else bool(playlist.get('public'))
        effective_readonly = is_readonly if is_readonly is not None else bool(playlist.get('is_readonly'))
        effective_scope = scope if scope is not None else str(playlist.get('scope') or 'private_user')
        effective_owner = owner if owner is not None else str(playlist.get('owner') or 'admin')
        policy = _normalize_playlist_policy(
            owner=effective_owner,
            public=effective_public,
            is_readonly=effective_readonly,
            scope=effective_scope,
        )
        connection.execute(
            text('''
                UPDATE playlists
                SET owner = :owner,
                    public = :public,
                    is_readonly = :is_readonly,
                    scope = :scope
                WHERE id = :playlist_id
            '''),
            {
                'playlist_id': playlist_id,
                'owner': policy['owner'],
                'public': policy['public'],
                'is_readonly': policy['is_readonly'],
                'scope': policy['scope'],
            },
        )

        if song_indexes_to_remove:
            rows = connection.execute(
                text('''
                    SELECT id, sort_order
                    FROM playlist_tracks
                    WHERE playlist_id = :playlist_id
                    ORDER BY sort_order ASC, id ASC
                '''),
                {'playlist_id': playlist_id},
            ).mappings().all()
            delete_ids = [row['id'] for row in rows if int(row['sort_order']) in {int(idx) for idx in song_indexes_to_remove}]
            if delete_ids:
                connection.execute(
                    text('DELETE FROM playlist_tracks WHERE id IN :ids').bindparams(bindparam('ids', expanding=True)),
                    {'ids': delete_ids},
                )

        if song_ids_to_remove:
            connection.execute(
                text('DELETE FROM playlist_tracks WHERE playlist_id = :playlist_id AND song_id IN :song_ids').bindparams(bindparam('song_ids', expanding=True)),
                {'playlist_id': playlist_id, 'song_ids': song_ids_to_remove},
            )

        if song_ids_to_add:
            current_count = connection.execute(
                text('SELECT COUNT(*) FROM playlist_tracks WHERE playlist_id = :playlist_id'),
                {'playlist_id': playlist_id},
            ).scalar_one()
            valid_song_ids = [song_id for song_id in song_ids_to_add if get_song(song_id)]
            for offset, song_id in enumerate(valid_song_ids):
                connection.execute(
                    text('''
                        INSERT INTO playlist_tracks (playlist_id, song_id, sort_order)
                        VALUES (:playlist_id, :song_id, :sort_order)
                    '''),
                    {'playlist_id': playlist_id, 'song_id': song_id, 'sort_order': current_count + offset},
                )

        rows = connection.execute(
            text('''
                SELECT id
                FROM playlist_tracks
                WHERE playlist_id = :playlist_id
                ORDER BY sort_order ASC, id ASC
            '''),
            {'playlist_id': playlist_id},
        ).mappings().all()
        for index, row in enumerate(rows):
            connection.execute(
                text('UPDATE playlist_tracks SET sort_order = :sort_order WHERE id = :id'),
                {'id': row['id'], 'sort_order': index},
            )

        _refresh_playlist_meta(connection, playlist_id)
    return get_playlist(playlist_id)



def delete_playlist(playlist_id: str) -> bool:
    with engine.begin() as connection:
        connection.execute(text('DELETE FROM playlist_tracks WHERE playlist_id = :playlist_id'), {'playlist_id': playlist_id})
        result = connection.execute(text('DELETE FROM playlists WHERE id = :playlist_id'), {'playlist_id': playlist_id})
    return result.rowcount > 0



def ensure_system_playlist(
    *,
    name: str,
    song_ids: list[str],
    comment: str = '',
) -> dict[str, Any] | None:
    valid_song_ids = [song_id for song_id in song_ids if get_song(song_id)]
    with engine.begin() as connection:
        existing = connection.execute(
            text('''
                SELECT id
                FROM playlists
                WHERE name = :name AND scope = 'public_system'
                ORDER BY updated_at DESC, created_at DESC, id DESC
                LIMIT 1
            '''),
            {'name': name},
        ).mappings().first()

        if existing:
            playlist_id = str(existing['id'])
            connection.execute(
                text(f'''
                    UPDATE playlists
                    SET comment = :comment,
                        owner = 'system',
                        public = TRUE,
                        is_readonly = TRUE,
                        scope = 'public_system',
                        updated_at = {db_now_expr()}
                    WHERE id = :playlist_id
                '''),
                {'playlist_id': playlist_id, 'comment': comment},
            )
            connection.execute(text('DELETE FROM playlist_tracks WHERE playlist_id = :playlist_id'), {'playlist_id': playlist_id})
            for index, song_id in enumerate(valid_song_ids):
                connection.execute(
                    text('''
                        INSERT INTO playlist_tracks (playlist_id, song_id, sort_order)
                        VALUES (:playlist_id, :song_id, :sort_order)
                    '''),
                    {'playlist_id': playlist_id, 'song_id': song_id, 'sort_order': index},
                )
            _refresh_playlist_meta(connection, playlist_id)
            return get_playlist(playlist_id)

    return create_playlist(
        name=name,
        song_ids=valid_song_ids,
        comment=comment,
        owner='system',
        public=True,
        is_readonly=True,
        scope='public_system',
    )



def ensure_default_system_playlists() -> list[dict[str, Any]]:
    top_song_ids = [str(song.get('id') or '') for song in list_top_songs(limit=30) if song.get('id')]
    recent_song_ids = [str(item.get('song_id') or '') for item in list_recent_plays(limit=30) if item.get('song_id')]

    created: list[dict[str, Any]] = []
    if top_song_ids:
        playlist = ensure_system_playlist(
            name='热门歌曲',
            song_ids=top_song_ids,
            comment='系统自动生成：按播放次数排序',
        )
        if playlist:
            created.append(playlist)
    if recent_song_ids:
        seen = set()
        unique_recent = []
        for song_id in recent_song_ids:
            if song_id in seen:
                continue
            seen.add(song_id)
            unique_recent.append(song_id)
        playlist = ensure_system_playlist(
            name='最近在听',
            song_ids=unique_recent,
            comment='系统自动生成：按最近播放记录整理',
        )
        if playlist:
            created.append(playlist)
    return created


def list_subsonic_users() -> list[dict[str, Any]]:
    with engine.connect() as connection:
        rows = connection.execute(
            text('''
                SELECT id, username, role, enabled, created_at, updated_at
                FROM subsonic_users
                ORDER BY role DESC, username ASC, id ASC
            ''')
        ).mappings().all()
    return [dict(row) for row in rows]


def get_subsonic_user(username: str) -> dict[str, Any] | None:
    with engine.connect() as connection:
        row = connection.execute(
            text('''
                SELECT id, username, password, role, enabled, created_at, updated_at
                FROM subsonic_users
                WHERE username = :username
            '''),
            {'username': username},
        ).mappings().first()
    return dict(row) if row else None


def create_subsonic_user(username: str, password: str, role: str = 'user', enabled: bool = True) -> dict[str, Any] | None:
    username = str(username or '').strip()
    password = str(password or '')
    role = 'admin' if str(role or '').strip().lower() == 'admin' else 'user'
    if not username or not password:
        return None
    with engine.begin() as connection:
        connection.execute(
            text(f'''
                INSERT INTO subsonic_users (username, password, role, enabled, updated_at)
                VALUES (:username, :password, :role, :enabled, {db_now_expr()})
                ON CONFLICT (username) DO UPDATE SET
                    password = EXCLUDED.password,
                    role = EXCLUDED.role,
                    enabled = EXCLUDED.enabled,
                    updated_at = {db_now_expr()}
            '''),
            {
                'username': username,
                'password': password,
                'role': role,
                'enabled': bool(enabled),
            },
        )
    return get_subsonic_user(username)


def ensure_admin_subsonic_user(username: str, password: str) -> dict[str, Any] | None:
    return create_subsonic_user(username=username, password=password, role='admin', enabled=True)


def update_subsonic_user(username: str, *, password: str | None = None, role: str | None = None, enabled: bool | None = None) -> dict[str, Any] | None:
    current = get_subsonic_user(username)
    if not current:
        return None
    next_password = str(password) if password is not None and str(password) != '' else str(current.get('password') or '')
    next_role = 'admin' if str(role or current.get('role') or 'user').strip().lower() == 'admin' else 'user'
    next_enabled = bool(current.get('enabled')) if enabled is None else bool(enabled)
    with engine.begin() as connection:
        connection.execute(
            text(f'''
                UPDATE subsonic_users
                SET password = :password,
                    role = :role,
                    enabled = :enabled,
                    updated_at = {db_now_expr()}
                WHERE username = :username
            '''),
            {
                'username': username,
                'password': next_password,
                'role': next_role,
                'enabled': next_enabled,
            },
        )
    return get_subsonic_user(username)


def delete_subsonic_user(username: str) -> bool:
    with engine.begin() as connection:
        result = connection.execute(text('DELETE FROM subsonic_users WHERE username = :username'), {'username': username})
    return result.rowcount > 0
