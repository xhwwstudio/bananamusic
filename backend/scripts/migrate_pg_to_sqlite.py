#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Connection


CURRENT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = CURRENT_DIR.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.db.compat import ensure_sqlite_parent_dir, json_dumps, json_loads  # noqa: E402
from app.services.app_settings import ensure_settings_table  # noqa: E402
from app.services.curation_service import ensure_curation_history_table  # noqa: E402
from app.services.db_data import init_database  # noqa: E402


TABLES_IN_ORDER = [
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

JSON_COLUMNS: dict[str, set[str]] = {
    'scan_tasks': {'logs'},
    'scan_state': {'state'},
    'curation_update_history': {'summary'},
}


@dataclass(frozen=True)
class TablePlan:
    name: str
    pk_columns: tuple[str, ...]
    reset_sequence: bool = False


TABLE_PLANS = {
    'songs': TablePlan('songs', ('id',)),
    'play_history': TablePlan('play_history', ('id',), reset_sequence=True),
    'playlists': TablePlan('playlists', ('id',)),
    'playlist_tracks': TablePlan('playlist_tracks', ('id',), reset_sequence=True),
    'scan_tasks': TablePlan('scan_tasks', ('id',)),
    'scan_state': TablePlan('scan_state', ('root_path',)),
    'file_index': TablePlan('file_index', ('file_path',)),
    'dirty_dirs': TablePlan('dirty_dirs', ('dir_path',)),
    'subsonic_users': TablePlan('subsonic_users', ('id',), reset_sequence=True),
    'starred_items': TablePlan('starred_items', ('id',), reset_sequence=True),
    'ratings': TablePlan('ratings', ('id',), reset_sequence=True),
    'bookmarks': TablePlan('bookmarks', ('id',), reset_sequence=True),
    'app_settings': TablePlan('app_settings', ('key',)),
    'curation_update_history': TablePlan('curation_update_history', ('id',)),
    'autofill_failed_uploads': TablePlan('autofill_failed_uploads', ('key',)),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Migrate MusicOn core data from PostgreSQL to SQLite.')
    parser.add_argument('--pg-url', required=True, help='PostgreSQL SQLAlchemy URL, e.g. postgresql+psycopg://user:pass@host:5432/db')
    parser.add_argument('--sqlite-url', required=True, help='SQLite SQLAlchemy URL, e.g. sqlite:////abs/path/musicon.db')
    parser.add_argument('--batch-size', type=int, default=500, help='Batch size per insert chunk (default: 500)')
    parser.add_argument('--keep-target', action='store_true', help='Keep target rows and upsert instead of truncating target tables first')
    return parser.parse_args()


def make_engine(url: str):
    kwargs: dict[str, Any] = {'future': True}
    if url.startswith('sqlite:'):
        kwargs['connect_args'] = {'check_same_thread': False}
    return create_engine(url, **kwargs)


def ensure_target_schema() -> None:
    ensure_sqlite_parent_dir()
    init_database()
    ensure_settings_table()
    ensure_curation_history_table()


def get_table_columns(connection: Connection, table_name: str) -> list[str]:
    inspector = inspect(connection)
    return [str(col['name']) for col in inspector.get_columns(table_name)]


def table_exists(connection: Connection, table_name: str) -> bool:
    return inspect(connection).has_table(table_name)


def normalize_value(table_name: str, column_name: str, value: Any) -> Any:
    if column_name in JSON_COLUMNS.get(table_name, set()):
        return json_dumps(json_loads(value, {} if column_name in {'state', 'summary'} else []), {} if column_name in {'state', 'summary'} else [])
    if isinstance(value, memoryview):
        return value.tobytes().decode('utf-8', errors='ignore')
    if isinstance(value, bytes):
        return value.decode('utf-8', errors='ignore')
    return value


def fetch_rows(pg_conn: Connection, table_name: str, columns: list[str]) -> list[dict[str, Any]]:
    sql = text(f"SELECT {', '.join(columns)} FROM {table_name}")
    rows = pg_conn.execute(sql).mappings().all()
    payload = []
    for row in rows:
        item = {}
        for column in columns:
            item[column] = normalize_value(table_name, column, row.get(column))
        payload.append(item)
    return payload


def build_upsert_sql(table_name: str, columns: list[str], pk_columns: tuple[str, ...]) -> str:
    cols = ', '.join(columns)
    placeholders = ', '.join(f':{col}' for col in columns)
    if not pk_columns:
        return f'INSERT INTO {table_name} ({cols}) VALUES ({placeholders})'
    update_columns = [col for col in columns if col not in pk_columns]
    if not update_columns:
        return f'INSERT OR IGNORE INTO {table_name} ({cols}) VALUES ({placeholders})'
    update_clause = ', '.join(f'{col}=excluded.{col}' for col in update_columns)
    conflict_cols = ', '.join(pk_columns)
    return (
        f'INSERT INTO {table_name} ({cols}) VALUES ({placeholders}) '
        f'ON CONFLICT({conflict_cols}) DO UPDATE SET {update_clause}'
    )


def clear_target_table(sqlite_conn: Connection, table_name: str) -> None:
    sqlite_conn.execute(text(f'DELETE FROM {table_name}'))


def reset_sqlite_sequence(sqlite_conn: Connection, table_name: str) -> None:
    sqlite_conn.execute(text("DELETE FROM sqlite_sequence WHERE name = :name"), {'name': table_name})


def chunked(seq: list[dict[str, Any]], size: int):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def migrate_table(pg_conn: Connection, sqlite_conn: Connection, plan: TablePlan, batch_size: int, keep_target: bool) -> dict[str, Any]:
    if not table_exists(pg_conn, plan.name):
        return {'table': plan.name, 'status': 'skipped', 'reason': 'missing in PostgreSQL', 'rows': 0}

    columns = get_table_columns(sqlite_conn, plan.name)
    source_columns = get_table_columns(pg_conn, plan.name)
    common_columns = [col for col in columns if col in source_columns]
    if not common_columns:
        return {'table': plan.name, 'status': 'skipped', 'reason': 'no common columns', 'rows': 0}

    rows = fetch_rows(pg_conn, plan.name, common_columns)
    if not keep_target:
        clear_target_table(sqlite_conn, plan.name)
    if not rows:
        if plan.reset_sequence and not keep_target:
            reset_sqlite_sequence(sqlite_conn, plan.name)
        return {'table': plan.name, 'status': 'ok', 'rows': 0}

    sql = text(build_upsert_sql(plan.name, common_columns, plan.pk_columns))
    for batch in chunked(rows, max(1, batch_size)):
        sqlite_conn.execute(sql, batch)
    if plan.reset_sequence:
        reset_sqlite_sequence(sqlite_conn, plan.name)
    return {'table': plan.name, 'status': 'ok', 'rows': len(rows)}


def main() -> int:
    args = parse_args()

    import app.core.config as config_module  # noqa: E402
    import app.db.session as session_module  # noqa: E402

    config_module.settings.database_url = args.sqlite_url
    session_module.database_url = args.sqlite_url
    session_module.is_sqlite = True
    session_module.engine.dispose()
    session_module.engine = make_engine(args.sqlite_url)
    session_module.SessionLocal.configure(bind=session_module.engine)

    ensure_target_schema()

    pg_engine = make_engine(args.pg_url)
    sqlite_engine = session_module.engine

    results: list[dict[str, Any]] = []
    try:
        with pg_engine.connect() as pg_conn, sqlite_engine.begin() as sqlite_conn:
            sqlite_conn.execute(text('PRAGMA foreign_keys = OFF'))
            for table_name in TABLES_IN_ORDER:
                result = migrate_table(pg_conn, sqlite_conn, TABLE_PLANS[table_name], args.batch_size, args.keep_target)
                results.append(result)
            sqlite_conn.execute(text('PRAGMA foreign_keys = ON'))
    finally:
        pg_engine.dispose()
        sqlite_engine.dispose()

    total_rows = sum(int(item.get('rows') or 0) for item in results if item.get('status') == 'ok')
    print(json.dumps({'tables': results, 'total_rows': total_rows}, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
