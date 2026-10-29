from __future__ import annotations

import base64
import hashlib
import io
import os
from pathlib import Path

try:
    from PIL import Image
except ImportError:
    Image = None


def _data_root() -> Path:
    """Resolve the writable data directory.

    Docker images use ``/app/data``; a source checkout keeps everything under
    ``backend/data`` so running outside the container does not try to write to
    ``C:\\app`` / ``/app``.
    """
    override = os.environ.get('MUSICON_DATA_DIR')
    if override:
        return Path(override)
    if Path('/app').is_dir():
        return Path('/app/data')
    return Path(__file__).resolve().parents[2] / 'data'


COVER_DIR = _data_root() / 'covers'
THUMB_DIR = COVER_DIR / 'thumbs'
COVER_DIR.mkdir(parents=True, exist_ok=True)
THUMB_DIR.mkdir(parents=True, exist_ok=True)


def save_cover_bytes(data: bytes, suffix: str = 'jpg') -> str:
    digest = hashlib.sha1(data).hexdigest()
    # 为了节省硬盘空间，统一转换为 WEBP 并限制最大尺寸
    filename = f'{digest}.webp'
    path = COVER_DIR / filename
    
    if not path.exists():
        if Image:
            try:
                with Image.open(io.BytesIO(data)) as img:
                    # 限制最大尺寸为 800x800，极大缩小无损海报体积
                    img.thumbnail((800, 800), Image.Resampling.LANCZOS)
                    if img.mode not in ('RGB', 'RGBA'):
                        img = img.convert('RGBA')
                    img.save(path, format='WEBP', quality=80)
            except Exception:
                # 遇到解析错误则回退保存原图
                fallback_ext = suffix.lower().strip('.') or 'jpg'
                fallback_path = COVER_DIR / f'{digest}.{fallback_ext}'
                if not fallback_path.exists():
                    fallback_path.write_bytes(data)
                return str(fallback_path)
        else:
            fallback_ext = suffix.lower().strip('.') or 'jpg'
            fallback_path = COVER_DIR / f'{digest}.{fallback_ext}'
            if not fallback_path.exists():
                fallback_path.write_bytes(data)
            return str(fallback_path)
            
    return str(path)


def save_cover_base64(data: str, suffix: str = 'jpg') -> str:
    if not data:
        return ''
    return save_cover_bytes(base64.b64decode(data), suffix=suffix)


def get_thumb_path(filename: str, size: int = 64) -> str | None:
    if not filename or not Image:
        return None
    
    source_path = COVER_DIR / filename
    if not source_path.exists():
        return None
        
    size = max(32, min(size, 512))
    thumb_filename = f'{source_path.stem}_{size}.webp'
    thumb_path = THUMB_DIR / thumb_filename
    
    if not thumb_path.exists():
        try:
            with Image.open(source_path) as img:
                img.thumbnail((size, size), Image.Resampling.LANCZOS)
                img.save(thumb_path, format='WEBP', quality=85)
        except Exception:
            return None
            
    return str(thumb_path)
