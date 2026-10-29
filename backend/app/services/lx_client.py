from __future__ import annotations

import httpx
import logging
from typing import Any

from app.services.lx_runtime import lx_embedded_manager

logger = logging.getLogger(__name__)


class LxMusicClient:
    """Client for interacting with LX-Server API."""

    def __init__(self, base_url: str | None = None):
        resolved = (base_url or lx_embedded_manager.configured_base_url() or '').strip()
        self.base_url = resolved.rstrip('/')
        self.client = httpx.AsyncClient(timeout=30.0)

    async def get_leaderboard(self, source: str, board_id: str, page: int = 1) -> list[dict[str, Any]]:
        if not self.base_url:
            logger.warning('LX-Server URL is empty, skipping leaderboard fetch.')
            return []
        url = f"{self.base_url}/api/music/leaderboard/list"
        try:
            response = await self.client.get(url, params={"source": source, "bangid": board_id, "page": page})
            response.raise_for_status()
            data = response.json()
            if isinstance(data, dict) and isinstance(data.get('list'), list):
                return data['list']
            if isinstance(data, dict) and isinstance(data.get('data'), dict) and isinstance(data['data'].get('list'), list):
                return data['data']['list']
            logger.warning('Unexpected LX leaderboard format: %s', data)
            return []
        except Exception as exc:
            logger.error('Error fetching LX leaderboard %s: %s', url, exc)
            return []

    async def get_music_url(self, song_info: dict[str, Any], quality: str = '128k') -> str:
        if not self.base_url:
            return ''
        url = f"{self.base_url}/api/music/url"
        try:
            response = await self.client.post(url, json={"songInfo": song_info, "quality": quality}, headers={"Content-Type": "application/json"})
            response.raise_for_status()
            data = response.json()
            return str(data.get('url') or '')
        except Exception as exc:
            logger.error('Failed to get music URL from LX-Server: %s', exc)
            return ''

    async def close(self) -> None:
        await self.client.aclose()
