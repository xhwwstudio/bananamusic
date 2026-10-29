from __future__ import annotations

import hashlib
from typing import Any

from app.services.app_settings import get_runtime_settings

COOKIE_NAME = 'musicon_web_session'


def _current_admin() -> tuple[str, str]:
    runtime = get_runtime_settings()
    username = str(runtime.get('subsonic_user') or '').strip()
    password = str(runtime.get('subsonic_pass') or '').strip()
    return username, password


def build_web_token(username: str, password: str) -> str:
    digest = hashlib.sha256(f'{username}|{password}|musicon-web'.encode()).hexdigest()
    return f'{username}:{digest}'


def authenticate_web_admin(username: str, password: str) -> bool:
    admin_user, admin_pass = _current_admin()
    return bool(username and password and username == admin_user and password == admin_pass)


def get_web_session(request) -> dict[str, Any] | None:
    token = str(request.cookies.get(COOKIE_NAME) or '').strip()
    if not token or ':' not in token:
        return None
    username, digest = token.split(':', 1)
    admin_user, admin_pass = _current_admin()
    if not admin_user or not admin_pass or username != admin_user:
        return None
    expected = build_web_token(admin_user, admin_pass)
    if token != expected:
        return None
    return {'username': admin_user, 'role': 'admin'}
