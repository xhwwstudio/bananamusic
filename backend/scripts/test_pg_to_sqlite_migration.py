#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = PROJECT_ROOT / 'backend'
SCRIPT_PATH = BACKEND_DIR / 'scripts' / 'migrate_pg_to_sqlite.py'

TABLES = [
    'songs',
    'play_history',
    'playlists',
    'playlist_tracks',
    'scan_tasks',
    'scan_state',
    'file_index',
    'dirty_dirs',
    'subsonic_users',
    'starred_items',
    'ratings',
    'bookmarks',
    'app_settings',
    'curation_update_history',
    'autofill_failed_uploads',
]

EXPECTED_COUNTS = {
    'songs': 2,
    'play_history': 3,
    'playlists': 2,
    'playlist_tracks': 3,
    'scan_tasks': 2,
    'scan_state': 1,
    'file_index': 2,
    'dirty_dirs': 2,
    'subsonic_users': 2,
    'starred_items': 2,
    'ratings': 2,
    'bookmarks': 2,
    'app_settings': 3,
    'curation_update_history': 1,
    'autofill_failed_uploads': 1,
}


def sh(command: str, *, env: dict[str, str] | None = None, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, shell=True, text=True, cwd=str(cwd or PROJECT_ROOT), env=env, capture_output=True, check=check)


def require_docker() -> None:
    probe = sh('docker ps >/dev/null 2>&1', check=False)
    if probe.returncode != 0:
        raise RuntimeError('docker 不可用，没法起 PostgreSQL 自测容器。')


def docker_cleanup(container_name: str) -> None:
    sh(f'docker rm -f {container_name} >/dev/null 2>&1 || true', check=False)


def seed_postgres(pg_url: str, *, env: dict[str, str]) -> None:
    python_code = r"""
import json
from sqlalchemy import create_engine, text

pg_url = '__PG_URL__'
engine = create_engine(pg_url, future=True)

with engine.begin() as conn:
    ddl = [
        "DROP TABLE IF EXISTS playlist_tracks",
        "DROP TABLE IF EXISTS play_history",
        "DROP TABLE IF EXISTS starred_items",
        "DROP TABLE IF EXISTS ratings",
        "DROP TABLE IF EXISTS bookmarks",
        "DROP TABLE IF EXISTS subsonic_users",
        "DROP TABLE IF EXISTS songs",
        "DROP TABLE IF EXISTS playlists",
        "DROP TABLE IF EXISTS scan_tasks",
        "DROP TABLE IF EXISTS scan_state",
        "DROP TABLE IF EXISTS file_index",
        "DROP TABLE IF EXISTS dirty_dirs",
        "DROP TABLE IF EXISTS app_settings",
        "DROP TABLE IF EXISTS curation_update_history",
        "DROP TABLE IF EXISTS autofill_failed_uploads",
    ]
    for sql in ddl:
        conn.execute(text(sql))

    conn.execute(text('''
        CREATE TABLE songs (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            artist TEXT NOT NULL,
            album TEXT NOT NULL,
            fmt TEXT NOT NULL,
            dur INTEGER NOT NULL,
            path TEXT NOT NULL,
            plays INTEGER NOT NULL DEFAULT 0,
            raw_url TEXT NOT NULL DEFAULT '',
            cover_path TEXT NOT NULL DEFAULT '',
            cover_art TEXT NOT NULL DEFAULT '',
            genre TEXT NOT NULL DEFAULT '',
            album_artist TEXT NOT NULL DEFAULT '',
            lyrics TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    '''))
    conn.execute(text('''
        CREATE TABLE play_history (
            id BIGSERIAL PRIMARY KEY,
            song_id TEXT NOT NULL,
            played_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            source TEXT NOT NULL DEFAULT 'subsonic',
            submission BOOLEAN NOT NULL DEFAULT FALSE,
            client TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    '''))
    conn.execute(text('''
        CREATE TABLE playlists (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            comment TEXT NOT NULL DEFAULT '',
            owner TEXT NOT NULL DEFAULT 'admin',
            public BOOLEAN NOT NULL DEFAULT FALSE,
            song_count INTEGER NOT NULL DEFAULT 0,
            duration INTEGER NOT NULL DEFAULT 0,
            is_readonly BOOLEAN NOT NULL DEFAULT FALSE,
            scope TEXT NOT NULL DEFAULT 'private_user',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    '''))
    conn.execute(text('''
        CREATE TABLE playlist_tracks (
            id BIGSERIAL PRIMARY KEY,
            playlist_id TEXT NOT NULL,
            song_id TEXT NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    '''))
    conn.execute(text('''
        CREATE TABLE scan_tasks (
            id TEXT PRIMARY KEY,
            path TEXT NOT NULL,
            mode TEXT NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            total INTEGER NOT NULL DEFAULT 0,
            processed INTEGER NOT NULL DEFAULT 0,
            duration TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'demo',
            end_time TEXT NOT NULL,
            logs JSONB NOT NULL DEFAULT '[]'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    '''))
    conn.execute(text('''
        CREATE TABLE scan_state (
            root_path TEXT PRIMARY KEY,
            state JSONB NOT NULL DEFAULT '{}'::jsonb,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    '''))
    conn.execute(text('''
        CREATE TABLE file_index (
            root_path TEXT NOT NULL,
            file_path TEXT PRIMARY KEY,
            parent_dir TEXT NOT NULL DEFAULT '',
            filename TEXT NOT NULL DEFAULT '',
            size BIGINT NOT NULL DEFAULT 0,
            mtime TEXT NOT NULL DEFAULT '',
            is_audio BOOLEAN NOT NULL DEFAULT TRUE,
            last_scan_task_id TEXT NOT NULL DEFAULT '',
            last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    '''))
    conn.execute(text('''
        CREATE TABLE dirty_dirs (
            dir_path TEXT PRIMARY KEY,
            reason TEXT NOT NULL DEFAULT '',
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    '''))
    conn.execute(text('''
        CREATE TABLE subsonic_users (
            id BIGSERIAL PRIMARY KEY,
            username TEXT NOT NULL UNIQUE,
            password TEXT NOT NULL DEFAULT '',
            role TEXT NOT NULL DEFAULT 'user',
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    '''))
    conn.execute(text('''
        CREATE TABLE starred_items (
            id BIGSERIAL PRIMARY KEY,
            username TEXT NOT NULL,
            item_type TEXT NOT NULL,
            item_id TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (username, item_type, item_id)
        )
    '''))
    conn.execute(text('''
        CREATE TABLE ratings (
            id BIGSERIAL PRIMARY KEY,
            username TEXT NOT NULL,
            song_id TEXT NOT NULL,
            rating INTEGER NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (username, song_id)
        )
    '''))
    conn.execute(text('''
        CREATE TABLE bookmarks (
            id BIGSERIAL PRIMARY KEY,
            username TEXT NOT NULL,
            song_id TEXT NOT NULL,
            position_ms BIGINT NOT NULL DEFAULT 0,
            comment TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (username, song_id)
        )
    '''))
    conn.execute(text('''
        CREATE TABLE app_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    '''))
    conn.execute(text('''
        CREATE TABLE curation_update_history (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            playlist_id TEXT NOT NULL,
            playlist_name TEXT NOT NULL,
            trigger_source TEXT NOT NULL DEFAULT 'manual',
            status TEXT NOT NULL DEFAULT 'done',
            track_count INTEGER NOT NULL DEFAULT 0,
            added_count INTEGER NOT NULL DEFAULT 0,
            removed_count INTEGER NOT NULL DEFAULT 0,
            changed_count INTEGER NOT NULL DEFAULT 0,
            summary JSONB NOT NULL DEFAULT '{}'::jsonb,
            error TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    '''))
    conn.execute(text('''
        CREATE TABLE autofill_failed_uploads (
            key TEXT PRIMARY KEY,
            task_id TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            artist TEXT NOT NULL DEFAULT '',
            filename TEXT NOT NULL DEFAULT '',
            alist_path TEXT NOT NULL DEFAULT '',
            reason TEXT NOT NULL DEFAULT '',
            fail_count INTEGER NOT NULL DEFAULT 1,
            last_error TEXT NOT NULL DEFAULT '',
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    '''))

    conn.execute(text('''
        INSERT INTO songs (id, title, artist, album, fmt, dur, path, plays, raw_url, cover_path, cover_art, genre, album_artist, lyrics, created_at, updated_at)
        VALUES
        ('song-1', 'Blue Hour', 'Artist A', 'Album Alpha', 'FLAC', 245, '/mnt/music/a.flac', 12, 'https://raw.example/a', '/covers/a.webp', 'art-a', 'Pop', 'Artist A', 'la-la', '2026-04-01T12:00:00+00:00', '2026-04-02T12:00:00+00:00'),
        ('song-2', 'Night Drive', 'Artist B', 'Album Beta', 'MP3', 198, '/mnt/music/b.mp3', 5, 'https://raw.example/b', '/covers/b.webp', 'art-b', 'Synthwave', 'Artist B', '', '2026-04-03T12:00:00+00:00', '2026-04-04T12:00:00+00:00')
    '''))
    conn.execute(text('''
        INSERT INTO play_history (id, song_id, played_at, source, submission, client, created_at)
        VALUES
        (1, 'song-1', '2026-04-10T09:00:00+00:00', 'subsonic', TRUE, 'DSub', '2026-04-10T09:00:00+00:00'),
        (2, 'song-1', '2026-04-11T09:00:00+00:00', 'web', FALSE, 'Web', '2026-04-11T09:00:00+00:00'),
        (3, 'song-2', '2026-04-12T09:00:00+00:00', 'subsonic', TRUE, 'Symfonium', '2026-04-12T09:00:00+00:00')
    '''))
    conn.execute(text('''
        INSERT INTO playlists (id, name, comment, owner, public, song_count, duration, is_readonly, scope, created_at, updated_at)
        VALUES
        ('pl-1', 'Favorites', 'private mix', 'alice', FALSE, 2, 443, FALSE, 'private_user', '2026-04-05T12:00:00+00:00', '2026-04-06T12:00:00+00:00'),
        ('pl-2', 'Editor Picks', 'system board', 'system', TRUE, 1, 245, TRUE, 'public_system', '2026-04-07T12:00:00+00:00', '2026-04-08T12:00:00+00:00')
    '''))
    conn.execute(text('''
        INSERT INTO playlist_tracks (id, playlist_id, song_id, sort_order, created_at)
        VALUES
        (1, 'pl-1', 'song-1', 1, '2026-04-05T12:00:00+00:00'),
        (2, 'pl-1', 'song-2', 2, '2026-04-05T12:05:00+00:00'),
        (3, 'pl-2', 'song-1', 1, '2026-04-07T12:05:00+00:00')
    '''))
    conn.execute(text('''
        INSERT INTO scan_tasks (id, path, mode, count, total, processed, duration, status, source, end_time, logs, created_at)
        VALUES
        ('scan-1', '/mnt/music', 'full', 2, 2, 2, '15s', 'done', 'manual', '2026-04-09T09:00:00+00:00', '[{"level":"info","message":"done"}]'::jsonb, '2026-04-09T08:59:00+00:00'),
        ('scan-2', '/mnt/music', 'incremental', 1, 1, 1, '4s', 'done', 'cron', '2026-04-10T09:00:00+00:00', '[{"level":"warn","message":"skip"}]'::jsonb, '2026-04-10T08:59:00+00:00')
    '''))
    conn.execute(text('''
        INSERT INTO scan_state (root_path, state, updated_at)
        VALUES ('/mnt/music', '{"cursor":"abc123","last_scan":"2026-04-10T09:00:00+00:00"}'::jsonb, '2026-04-10T09:00:00+00:00')
    '''))
    conn.execute(text('''
        INSERT INTO file_index (root_path, file_path, parent_dir, filename, size, mtime, is_audio, last_scan_task_id, last_seen_at, updated_at)
        VALUES
        ('/mnt/music', '/mnt/music/a.flac', '/mnt/music', 'a.flac', 12345678, '2026-04-01T00:00:00+00:00', TRUE, 'scan-2', '2026-04-10T09:00:00+00:00', '2026-04-10T09:00:00+00:00'),
        ('/mnt/music', '/mnt/music/b.mp3', '/mnt/music', 'b.mp3', 3456789, '2026-04-02T00:00:00+00:00', TRUE, 'scan-2', '2026-04-10T09:00:00+00:00', '2026-04-10T09:00:00+00:00')
    '''))
    conn.execute(text('''
        INSERT INTO dirty_dirs (dir_path, reason, updated_at, created_at)
        VALUES
        ('/mnt/music/new', 'new files', '2026-04-10T09:01:00+00:00', '2026-04-10T09:01:00+00:00'),
        ('/mnt/music/fix', 'tag changed', '2026-04-10T09:02:00+00:00', '2026-04-10T09:02:00+00:00')
    '''))
    conn.execute(text('''
        INSERT INTO subsonic_users (id, username, password, role, enabled, created_at, updated_at)
        VALUES
        (1, 'admin', 'secret', 'admin', TRUE, '2026-04-01T00:00:00+00:00', '2026-04-01T00:00:00+00:00'),
        (2, 'alice', 'pw', 'user', TRUE, '2026-04-02T00:00:00+00:00', '2026-04-02T00:00:00+00:00')
    '''))
    conn.execute(text('''
        INSERT INTO starred_items (id, username, item_type, item_id, created_at)
        VALUES
        (1, 'alice', 'song', 'song-1', '2026-04-11T00:00:00+00:00'),
        (2, 'alice', 'playlist', 'pl-1', '2026-04-11T00:01:00+00:00')
    '''))
    conn.execute(text('''
        INSERT INTO ratings (id, username, song_id, rating, created_at, updated_at)
        VALUES
        (1, 'alice', 'song-1', 5, '2026-04-11T00:00:00+00:00', '2026-04-11T00:00:00+00:00'),
        (2, 'admin', 'song-2', 4, '2026-04-12T00:00:00+00:00', '2026-04-12T00:00:00+00:00')
    '''))
    conn.execute(text('''
        INSERT INTO bookmarks (id, username, song_id, position_ms, comment, created_at, updated_at)
        VALUES
        (1, 'alice', 'song-1', 91234, 'bridge', '2026-04-11T00:00:00+00:00', '2026-04-11T00:00:00+00:00'),
        (2, 'admin', 'song-2', 45678, 'solo', '2026-04-12T00:00:00+00:00', '2026-04-12T00:00:00+00:00')
    '''))
    conn.execute(text('''
        INSERT INTO app_settings (key, value, updated_at)
        VALUES
        ('subsonic_user', 'admin', '2026-04-01T00:00:00+00:00'),
        ('autofill_quality', 'flac', '2026-04-01T00:00:00+00:00'),
        ('lx_server_url', 'http://lx.local', '2026-04-01T00:00:00+00:00')
    '''))
    conn.execute(text('''
        INSERT INTO curation_update_history (id, run_id, playlist_id, playlist_name, trigger_source, status, track_count, added_count, removed_count, changed_count, summary, error, created_at)
        VALUES
        ('ch-1', 'run-1', 'pl-2', 'Editor Picks', 'manual', 'done', 2, 1, 0, 1, '{"top_tracks":[{"song_id":"song-1"}],"added":[{"song_id":"song-1"}],"removed":[]}'::jsonb, '', '2026-04-12T12:00:00+00:00')
    '''))
    conn.execute(text('''
        INSERT INTO autofill_failed_uploads (key, task_id, title, artist, filename, alist_path, reason, fail_count, last_error, updated_at, created_at)
        VALUES
        ('af-1', 'scan-2', 'Night Drive', 'Artist B', 'night-drive.flac', '/autofill/night-drive.flac', 'upload failed', 3, '429 too many requests', '2026-04-12T13:00:00+00:00', '2026-04-12T12:30:00+00:00')
    '''))

print(json.dumps({'seeded': True}, ensure_ascii=False))
"""
    python_code = python_code.replace('__PG_URL__', pg_url)
    proc = subprocess.run([sys.executable, '-c', python_code], cwd=str(BACKEND_DIR), env=env, text=True, capture_output=True, check=True)
    print(proc.stdout.strip())


def assert_sqlite(sqlite_path: Path) -> dict:
    conn = sqlite3.connect(sqlite_path)
    conn.row_factory = sqlite3.Row
    try:
        counts = {}
        for table in TABLES:
            counts[table] = conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
        for table, expected in EXPECTED_COUNTS.items():
            actual = counts.get(table)
            if actual != expected:
                raise AssertionError(f'{table} count mismatch: expected {expected}, got {actual}')

        top_song = conn.execute('''
            SELECT s.id, s.title, COUNT(ph.id) AS play_count
            FROM songs s
            LEFT JOIN play_history ph ON ph.song_id = s.id
            GROUP BY s.id, s.title
            ORDER BY play_count DESC, s.id ASC
            LIMIT 1
        ''').fetchone()
        if dict(top_song) != {'id': 'song-1', 'title': 'Blue Hour', 'play_count': 2}:
            raise AssertionError(f'top song mismatch: {dict(top_song)}')

        playlist_rows = conn.execute('''
            SELECT p.id, p.name, COUNT(pt.id) AS track_count
            FROM playlists p
            LEFT JOIN playlist_tracks pt ON pt.playlist_id = p.id
            GROUP BY p.id, p.name
            ORDER BY p.id ASC
        ''').fetchall()
        playlist_summary = [dict(row) for row in playlist_rows]
        if playlist_summary != [
            {'id': 'pl-1', 'name': 'Favorites', 'track_count': 2},
            {'id': 'pl-2', 'name': 'Editor Picks', 'track_count': 1},
        ]:
            raise AssertionError(f'playlist summary mismatch: {playlist_summary}')

        summary_json = conn.execute("SELECT summary FROM curation_update_history WHERE id = 'ch-1'").fetchone()[0]
        summary = json.loads(summary_json)
        if summary['top_tracks'][0]['song_id'] != 'song-1':
            raise AssertionError(f'curation summary mismatch: {summary}')

        scan_state_json = conn.execute("SELECT state FROM scan_state WHERE root_path = '/mnt/music'").fetchone()[0]
        scan_state = json.loads(scan_state_json)
        if scan_state['cursor'] != 'abc123':
            raise AssertionError(f'scan_state mismatch: {scan_state}')

        autofill = dict(conn.execute("SELECT fail_count, last_error FROM autofill_failed_uploads WHERE key = 'af-1'").fetchone())
        if autofill != {'fail_count': 3, 'last_error': '429 too many requests'}:
            raise AssertionError(f'autofill mismatch: {autofill}')

        return {
            'counts': counts,
            'top_song': dict(top_song),
            'playlist_summary': playlist_summary,
            'curation_summary': summary,
            'scan_state': scan_state,
            'autofill': autofill,
        }
    finally:
        conn.close()


def wait_for_pg(container_name: str, user: str) -> None:
    for _ in range(60):
        probe = sh(f'docker exec {container_name} pg_isready -U {user}', check=False)
        if probe.returncode == 0:
            return
    raise RuntimeError('PostgreSQL 容器半天起不来，烦。')


def main() -> int:
    require_docker()
    container_name = 'musicon-pg-migration-test'
    docker_cleanup(container_name)

    with tempfile.TemporaryDirectory(prefix='musicon-migrate-test-') as tmpdir:
        tmp_path = Path(tmpdir)
        sqlite_path = tmp_path / 'musicon-migrated.db'
        pg_user = 'musicon'
        pg_password = 'change_me'
        pg_db = 'musicon'
        pg_port = '55432'
        pg_url = f'postgresql+psycopg://{pg_user}:{pg_password}@127.0.0.1:{pg_port}/{pg_db}'
        sqlite_url = f'sqlite:///{sqlite_path}'

        sh(
            ' '.join([
                'docker run -d',
                f'--name {container_name}',
                f'-e POSTGRES_USER={pg_user}',
                f'-e POSTGRES_PASSWORD={pg_password}',
                f'-e POSTGRES_DB={pg_db}',
                f'-p {pg_port}:5432',
                'postgres:16-alpine',
            ])
        )

        try:
            wait_for_pg(container_name, pg_user)
            env = os.environ.copy()
            env['PYTHONPATH'] = str(BACKEND_DIR)
            env['DATABASE_URL'] = sqlite_url
            seed_postgres(pg_url, env=env)

            migrate = subprocess.run(
                [sys.executable, str(SCRIPT_PATH), '--pg-url', pg_url, '--sqlite-url', sqlite_url],
                cwd=str(BACKEND_DIR),
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )
            print(migrate.stdout.strip())

            verification = assert_sqlite(sqlite_path)
            print(json.dumps({'ok': True, 'sqlite_path': str(sqlite_path), 'verification': verification}, ensure_ascii=False, indent=2))
        finally:
            docker_cleanup(container_name)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
