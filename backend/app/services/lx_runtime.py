from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
import subprocess
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.services.app_settings import get_runtime_settings

logger = logging.getLogger(__name__)


class LxEmbeddedManager:
    def __init__(self) -> None:
        self.process: subprocess.Popen[str] | None = None
        self.root = self._resolve_root()
        self.vendor_dir = self._resolve_vendor_dir()
        self.data_dir = self._resolve_data_dir()
        self.enabled = bool(settings.lx_embedded_enabled)
        self.default_port = int(settings.lx_embedded_port)
        self.default_host = str(settings.lx_embedded_host)
        self.frontend_password = str(settings.lx_frontend_password)
        self.admin_user = str(settings.lx_admin_user)
        self.admin_password = str(settings.lx_admin_password)

    def _resolve_root(self) -> Path:
        current = Path(__file__).resolve()
        candidates: list[Path] = [Path('/app'), Path.cwd()]
        candidates.extend(parent for parent in current.parents)
        seen: set[str] = set()
        for candidate in candidates:
            key = str(candidate)
            if key in seen:
                continue
            seen.add(key)
            if (candidate / 'vendor' / 'lxserver').exists() or (candidate / 'backend').exists():
                return candidate
        return current.parent

    def _resolve_vendor_dir(self) -> Path:
        current = Path(__file__).resolve()
        candidates: list[Path] = [self.root / 'vendor' / 'lxserver', Path('/app/vendor/lxserver')]
        candidates.extend(parent / 'vendor' / 'lxserver' for parent in current.parents)
        seen: set[str] = set()
        for candidate in candidates:
            key = str(candidate)
            if key in seen:
                continue
            seen.add(key)
            if (candidate / 'index.js').exists() and (candidate / 'server').exists():
                return candidate
        return self.root / 'vendor' / 'lxserver'

    def _resolve_data_dir(self) -> Path:
        for candidate in [self.root / 'data' / 'lxserver', Path('/app/data/lxserver')]:
            parent = candidate.parent
            if parent.exists() or str(candidate).startswith('/app/'):
                return candidate
        return self.root / 'data' / 'lxserver'

    def configured_mode(self) -> str:
        runtime = get_runtime_settings()
        mode = str(runtime.get('lx_service_mode') or settings.lx_service_mode or 'embedded').strip().lower()
        return mode if mode in {'embedded', 'remote'} else 'embedded'

    def configured_base_url(self) -> str:
        runtime = get_runtime_settings()
        mode = self.configured_mode()
        if mode == 'remote':
            url = str(runtime.get('lx_remote_url') or runtime.get('lx_server_url') or '').strip().rstrip('/')
            return url
        return f'http://{self.default_host}:{self.default_port}'

    def status(self) -> dict[str, Any]:
        mode = self.configured_mode()
        base_url = self.configured_base_url()
        proc_alive = self.process is not None and self.process.poll() is None
        available = self.vendor_dir.exists() and (self.vendor_dir / 'index.js').exists()
        return {
            'mode': mode,
            'embedded_enabled': self.enabled,
            'embedded_available': available,
            'embedded_running': bool(proc_alive),
            'embedded_host': self.default_host,
            'embedded_port': self.default_port,
            'remote_url': str(get_runtime_settings().get('lx_remote_url') or '').strip(),
            'active_url': base_url,
        }

    def _port_open(self, host: str, port: int) -> bool:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            return False

    async def _wait_ready(self, host: str, port: int, timeout: float = 25.0) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if self.process is not None and self.process.poll() is not None:
                logger.error('embedded lx-server exited early with code=%s', self.process.returncode)
                return False
            if await asyncio.to_thread(self._port_open, host, port):
                return True
            await asyncio.sleep(0.5)
        return False

    async def start(self) -> None:
        self.root = self._resolve_root()
        self.vendor_dir = self._resolve_vendor_dir()
        self.data_dir = self._resolve_data_dir()
        if not self.enabled:
            logger.info('embedded lx-server disabled by env')
            return
        if self.configured_mode() != 'embedded':
            logger.info('lx mode is not embedded, skip spawning embedded lx-server')
            return
        if not self.vendor_dir.exists() or not (self.vendor_dir / 'index.js').exists():
            logger.warning('vendor/lxserver not found, embedded lx-server unavailable')
            return
        if self.process is not None and self.process.poll() is None:
            logger.info('embedded lx-server already running')
            return
        if self._port_open(self.default_host, self.default_port):
            logger.warning('embedded lx-server port %s already open, assume existing service', self.default_port)
            return

        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / 'users' / '_open').mkdir(parents=True, exist_ok=True)
        (self.data_dir / 'users' / 'source' / '_open').mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env.update({
            'NODE_ENV': 'production',
            'BIND_IP': self.default_host,
            'PORT': str(self.default_port),
            'DATA_PATH': str(self.data_dir),
            'FRONTEND_PASSWORD': self.frontend_password,
            f'LX_USER_{self.admin_user}': self.admin_password,
        })

        logger.info('starting embedded lx-server on %s:%s', self.default_host, self.default_port)
        self.process = subprocess.Popen(
            ['node', 'index.js'],
            cwd=str(self.vendor_dir),
            env=env,
            stdout=None,
            stderr=None,
            start_new_session=True,
            text=True,
        )
        ready = await self._wait_ready(self.default_host, self.default_port)
        if not ready:
            await self.stop(force=True)
            raise RuntimeError('embedded lx-server failed to become ready')
        logger.info('embedded lx-server ready at %s', self.configured_base_url())

    async def stop(self, force: bool = False) -> None:
        if self.process is None:
            return
        if self.process.poll() is not None:
            self.process = None
            return
        try:
            pgid = os.getpgid(self.process.pid)
            os.killpg(pgid, signal.SIGKILL if force else signal.SIGTERM)
            try:
                self.process.wait(timeout=8 if not force else 3)
            except subprocess.TimeoutExpired:
                if not force:
                    os.killpg(pgid, signal.SIGKILL)
                    self.process.wait(timeout=3)
        except Exception as exc:
            logger.warning('failed stopping embedded lx-server: %s', exc)
        finally:
            self.process = None


lx_embedded_manager = LxEmbeddedManager()
