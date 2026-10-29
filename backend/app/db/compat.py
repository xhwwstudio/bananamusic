from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text, inspect

from app.core.config import settings
from app.db.session import engine, is_sqlite


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_str() -> str:
    return utc_now().strftime('%Y-%m-%d %H:%M UTC')


def utc_days_ago(days: int) -> str:
    return (utc_now() - timedelta(days=int(days))).isoformat()


def ensure_sqlite_parent_dir() -> None:
    if not is_sqlite:
        return
    url = settings.database_url
    if ':memory:' in url:
        return
    prefix = 'sqlite:///'
    if url.startswith(prefix):
        raw = url[len(prefix):]
        if raw:
            Path(raw).resolve().parent.mkdir(parents=True, exist_ok=True)


def json_dumps(value: Any, default: Any) -> str:
    payload = default if value is None else value
    return json.dumps(payload, ensure_ascii=False)


def json_loads(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, bytes):
        value = value.decode('utf-8', errors='ignore')
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return default
        try:
            return json.loads(stripped)
        except Exception:
            return default
    return default


def dt_to_iso(value: Any) -> str:
    """Normalise timestamps to RFC-3339 UTC strings (``...Z``).

    SQLite stores timestamps as ``YYYY-MM-DD HH:MM:SS`` text; Subsonic clients
    (Symfonium, DSub, ...) parse ``created``/``starred``/``changed`` as ISO-8601
    instants, so a space-separated value can abort a sync.
    """
    if value is None:
        return ''
    if isinstance(value, bytes):
        value = value.decode('utf-8', errors='ignore')
    if isinstance(value, datetime):
        moment = value
    else:
        raw = str(value).strip()
        if not raw:
            return ''
        try:
            moment = datetime.fromisoformat(raw.replace(' ', 'T').replace('Z', '+00:00'))
        except ValueError:
            return raw
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def db_json_type() -> str:
    return 'TEXT' if is_sqlite else 'JSONB'


def db_timestamp_type() -> str:
    return 'TEXT' if is_sqlite else 'TIMESTAMPTZ'


def db_bigint_pk() -> str:
    return 'INTEGER PRIMARY KEY AUTOINCREMENT' if is_sqlite else 'BIGSERIAL PRIMARY KEY'


def db_bool(default: bool = False) -> str:
    val = '1' if default else '0'
    return f'BOOLEAN NOT NULL DEFAULT {val}' if is_sqlite else f'BOOLEAN NOT NULL DEFAULT {str(default).upper()}'


def db_now_expr() -> str:
    return "CURRENT_TIMESTAMP" if is_sqlite else 'NOW()'


def db_json_value_expr(param_name: str) -> str:
    return f':{param_name}' if is_sqlite else f'CAST(:{param_name} AS JSONB)'


def has_column(table_name: str, column_name: str, connection=None) -> bool:
    insp = inspect(connection if connection is not None else engine)
    try:
        columns = insp.get_columns(table_name)
    except Exception:
        return False
    return any(str(col.get('name')) == column_name for col in columns)


def add_column_if_missing(connection, table_name: str, column_name: str, column_sql: str) -> None:
    if has_column(table_name, column_name, connection=connection):
        return
    connection.execute(text(f'ALTER TABLE {table_name} ADD COLUMN {column_name} {column_sql}'))


def upsert_app_setting(connection, key: str, value: str) -> None:
    if is_sqlite:
        connection.execute(
            text('''
                INSERT INTO app_settings (key, value, updated_at)
                VALUES (:key, :value, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = CURRENT_TIMESTAMP
            '''),
            {'key': key, 'value': value},
        )
    else:
        connection.execute(
            text('''
                INSERT INTO app_settings (key, value, updated_at)
                VALUES (:key, :value, NOW())
                ON CONFLICT (key) DO UPDATE SET
                    value = EXCLUDED.value,
                    updated_at = NOW()
            '''),
            {'key': key, 'value': value},
        )
