from __future__ import annotations

import json
from typing import Any

SENSITIVE_PROVIDER_CONFIG_KEYS = {'token', 'cookie', 'password', 'refresh_password'}

from sqlalchemy import text

from app.core.config import settings
from app.db.session import engine, is_sqlite
from app.db.compat import add_column_if_missing, db_now_expr, db_timestamp_type, upsert_app_setting

DEFAULT_SETTINGS = {
    'alist_url': settings.alist_url,
    'alist_token': settings.alist_token,
    'alist_username': '',
    'alist_password': '',
    'storage_default_provider_id': '',
    'storage_providers': '',
    'provider_test_statuses': '{}',
    'subsonic_user': settings.subsonic_user,
    'subsonic_pass': settings.subsonic_pass,
    'range_size_kb': '1024',
    'cache_ttl_min': '30',
    'cache_max_gb': '10',
    'last_scan_provider_id': '',
    'last_scan_root': 'default',
    'last_scan_path': '/阿里云盘/Music',
    'cron_expr': '',
    'cron_path': '',
    'cron_mode': 'incremental',
    'cron_concur': '10',
    'cron_batch': '100',
    'lx_server_url': f'http://{settings.lx_embedded_host}:{settings.lx_embedded_port}',
    'lx_service_mode': settings.lx_service_mode,
    'lx_remote_url': settings.lx_remote_url,
    'lx_custom_source_username': 'open',
    'lx_cron_expr': '5,3 0,12 * * *', # 默认北京时间每天 00:05 和 12:03 更新榜单
    'autofill_provider_id': '',
    'autofill_root': 'default',
    'autofill_path': '/MusicOn_AutoDownload/',
    'autofill_quality': 'flac',
}

SECRET_KEYS = {'alist_token', 'alist_password', 'subsonic_pass'}
ALLOWED_KEYS = set(DEFAULT_SETTINGS.keys())


def _parse_storage_providers(raw: Any, default_provider_id: str) -> list[dict[str, Any]]:
    providers: list[dict[str, Any]] = []
    if isinstance(raw, str) and raw.strip():
        try:
            decoded = json.loads(raw)
            if isinstance(decoded, list):
                for item in decoded:
                    if not isinstance(item, dict):
                        continue
                    provider_id = str(item.get('id') or '').strip()
                    provider_type = str(item.get('type') or '').strip()
                    if not provider_id or not provider_type:
                        continue
                    providers.append({
                        'id': provider_id,
                        'type': provider_type,
                        'name': str(item.get('name') or provider_id).strip() or provider_id,
                    })
        except Exception:
            providers = []
    return providers


def _load_provider_payload(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, str) or not raw.strip():
        return []
    try:
        decoded = json.loads(raw)
    except Exception:
        return []
    if not isinstance(decoded, list):
        return []
    result: list[dict[str, Any]] = []
    for item in decoded:
        if not isinstance(item, dict):
            continue
        provider_id = str(item.get('id') or '').strip()
        provider_type = str(item.get('type') or '').strip()
        if not provider_id or not provider_type:
            continue
        result.append({
            'id': provider_id,
            'type': provider_type,
            'name': str(item.get('name') or provider_id).strip() or provider_id,
            'config': dict(item.get('config') or {}),
        })
    return result


def _ensure_default_alist_provider(settings_data: dict[str, Any]) -> dict[str, Any]:
    providers = _load_provider_payload(settings_data.get('storage_providers'))
    changed = False

    existing_ids = [str(item.get('id') or '').strip() for item in providers if str(item.get('id') or '').strip()]
    first_provider_id = existing_ids[0] if existing_ids else ''

    default_provider_id = str(settings_data.get('storage_default_provider_id') or '').strip()
    if default_provider_id not in existing_ids:
        settings_data['storage_default_provider_id'] = first_provider_id
        changed = True

    if str(settings_data.get('last_scan_provider_id') or '').strip() not in existing_ids:
        settings_data['last_scan_provider_id'] = first_provider_id
        changed = True

    if str(settings_data.get('autofill_provider_id') or '').strip() not in existing_ids:
        settings_data['autofill_provider_id'] = first_provider_id
        changed = True

    normalized_raw = json.dumps(providers, ensure_ascii=False)
    if normalized_raw != str(settings_data.get('storage_providers') or ''):
        settings_data['storage_providers'] = normalized_raw
        changed = True

    if changed:
        with engine.begin() as connection:
            for key in ('storage_providers', 'storage_default_provider_id', 'last_scan_provider_id', 'autofill_provider_id'):
                upsert_app_setting(connection, key, str(settings_data.get(key) or ''))
    return settings_data


def _mask_provider_configs(raw: Any) -> str:
    if not isinstance(raw, str) or not raw.strip():
        return ''
    try:
        decoded = json.loads(raw)
    except Exception:
        return ''
    if not isinstance(decoded, list):
        return ''
    sanitized: list[dict[str, Any]] = []
    for item in decoded:
        if not isinstance(item, dict):
            continue
        config = dict(item.get('config') or {})
        config_meta: dict[str, bool] = {}
        for key in list(config.keys()):
            if key in SENSITIVE_PROVIDER_CONFIG_KEYS:
                config_meta[f'has_{key}'] = bool(str(config.get(key) or '').strip())
                config[key] = ''
        sanitized.append({
            'id': item.get('id'),
            'type': item.get('type'),
            'name': item.get('name'),
            'config': config,
            'config_meta': config_meta,
        })
    return json.dumps(sanitized, ensure_ascii=False)


def _merge_provider_secrets(current_raw: Any, incoming_raw: Any) -> str:
    if not isinstance(incoming_raw, str) or not incoming_raw.strip():
        return str(current_raw or '')
    try:
        incoming = json.loads(incoming_raw)
    except Exception:
        return str(current_raw or '')
    if not isinstance(incoming, list):
        return str(current_raw or '')
    current_map: dict[str, dict[str, Any]] = {}
    try:
        decoded_current = json.loads(current_raw) if isinstance(current_raw, str) and current_raw.strip() else []
        if isinstance(decoded_current, list):
            for item in decoded_current:
                if isinstance(item, dict):
                    current_map[str(item.get('id') or '').strip()] = item
    except Exception:
        current_map = {}
    merged: list[dict[str, Any]] = []
    for item in incoming:
        if not isinstance(item, dict):
            continue
        provider_id = str(item.get('id') or '').strip()
        config = dict(item.get('config') or {})
        current_item = current_map.get(provider_id) or {}
        current_config = dict(current_item.get('config') or {})
        for key in SENSITIVE_PROVIDER_CONFIG_KEYS:
            if key in config and not str(config.get(key) or '').strip() and str(current_config.get(key) or '').strip():
                config[key] = current_config.get(key)
        merged.append({
            'id': provider_id,
            'type': str(item.get('type') or '').strip(),
            'name': str(item.get('name') or provider_id).strip(),
            'config': config,
        })
    return json.dumps(merged, ensure_ascii=False)


def merge_storage_providers_payload(current_raw: Any, incoming_raw: Any) -> str:
    return _merge_provider_secrets(current_raw, incoming_raw)


def ensure_settings_table() -> None:
    with engine.begin() as connection:
        connection.execute(text(f'''
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at {db_timestamp_type()} NOT NULL DEFAULT {db_now_expr()}
            )
        '''))
        if is_sqlite:
            add_column_if_missing(connection, 'app_settings', 'updated_at', f"{db_timestamp_type()} NOT NULL DEFAULT {db_now_expr()}")
        for key, value in DEFAULT_SETTINGS.items():
            if is_sqlite:
                connection.execute(
                    text('''
                        INSERT INTO app_settings (key, value)
                        VALUES (:key, :value)
                        ON CONFLICT(key) DO NOTHING
                    '''),
                    {'key': key, 'value': value},
                )
            else:
                connection.execute(
                    text('''
                        INSERT INTO app_settings (key, value)
                        VALUES (:key, :value)
                        ON CONFLICT (key) DO NOTHING
                    '''),
                    {'key': key, 'value': value},
                )


def _load_merged_settings() -> dict[str, Any]:
    ensure_settings_table()
    with engine.connect() as connection:
        rows = connection.execute(text('SELECT key, value FROM app_settings')).mappings().all()
    data = {row['key']: row['value'] for row in rows}
    merged = {**DEFAULT_SETTINGS, **data}
    merged = _ensure_default_alist_provider(merged)
    return {
        'alist_url': merged['alist_url'],
        'alist_token': merged['alist_token'],
        'alist_username': merged['alist_username'],
        'alist_password': merged['alist_password'],
        'storage_default_provider_id': merged['storage_default_provider_id'],
        'storage_providers': merged['storage_providers'],
        'provider_test_statuses': merged['provider_test_statuses'],
        'subsonic_user': merged['subsonic_user'],
        'subsonic_pass': merged['subsonic_pass'],
        'range_size_kb': int(merged['range_size_kb']),
        'cache_ttl_min': int(merged['cache_ttl_min']),
        'cache_max_gb': int(merged['cache_max_gb']),
        'last_scan_provider_id': merged['last_scan_provider_id'],
        'last_scan_root': merged['last_scan_root'],
        'last_scan_path': merged['last_scan_path'],
        'cron_expr': merged['cron_expr'],
        'cron_path': merged['cron_path'],
        'cron_mode': merged['cron_mode'],
        'cron_concur': int(merged['cron_concur']),
        'cron_batch': int(merged['cron_batch']),
        'lx_server_url': merged['lx_server_url'],
        'lx_service_mode': merged['lx_service_mode'],
        'lx_remote_url': merged['lx_remote_url'],
        'lx_custom_source_username': merged['lx_custom_source_username'],
        'lx_cron_expr': merged['lx_cron_expr'],
        'autofill_provider_id': merged['autofill_provider_id'],
        'autofill_root': merged['autofill_root'],
        'autofill_path': merged['autofill_path'],
        'autofill_quality': merged['autofill_quality'],
    }


def get_runtime_settings() -> dict[str, Any]:
    return _load_merged_settings()


def get_public_settings() -> dict[str, Any]:
    settings_data = _load_merged_settings()
    default_provider_id = str(settings_data.get('storage_default_provider_id') or '').strip() or 'alist-main'
    providers = _parse_storage_providers(settings_data.get('storage_providers'), default_provider_id)
    provider_payload = _load_provider_payload(settings_data.get('storage_providers'))
    alist_main = next((item for item in provider_payload if str(item.get('type') or '').strip() == 'alist'), None) or {}
    alist_cfg = dict(alist_main.get('config') or {})
    return {
        **settings_data,
        'alist_url': str(alist_cfg.get('base_url') or ''),
        'alist_username': str(alist_cfg.get('refresh_username') or alist_cfg.get('username') or ''),
        'alist_token': '',
        'alist_password': '',
        'storage_providers': _mask_provider_configs(settings_data.get('storage_providers')),
        'storage': {
            'default_provider_id': default_provider_id,
            'providers': providers,
        },
        'provider_test_statuses': settings_data.get('provider_test_statuses') or '{}',
        'subsonic_pass': '',
        'has_alist_token': bool(str(alist_cfg.get('token') or '').strip()),
        'has_alist_password': bool(str(alist_cfg.get('refresh_password') or alist_cfg.get('password') or '').strip()),
        'has_subsonic_pass': bool(settings_data.get('subsonic_pass')),
    }


def get_settings() -> dict[str, Any]:
    return get_public_settings()


def update_settings(payload: dict[str, Any], *, include_secrets: bool = True, public_view: bool = True) -> dict[str, Any]:
    ensure_settings_table()
    current = _load_merged_settings()
    rows = []
    for key, value in payload.items():
        if key not in ALLOWED_KEYS:
            continue
        if key in SECRET_KEYS:
            text_value = str(value or '').strip()
            if not include_secrets or not text_value:
                continue
            rows.append({'key': key, 'value': text_value})
            continue
        if key == 'storage_providers':
            merged_value = _merge_provider_secrets(current.get('storage_providers'), value)
            rows.append({'key': key, 'value': merged_value})
            continue
        rows.append({'key': key, 'value': str(value)})
    if rows:
        with engine.begin() as connection:
            for row in rows:
                upsert_app_setting(connection, row['key'], row['value'])
    if public_view:
        return get_public_settings()
    return get_runtime_settings()
