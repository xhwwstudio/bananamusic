from fastapi import APIRouter

from app.db.session import check_database
from app.services.lx_runtime import lx_embedded_manager

router = APIRouter(tags=['health'])


@router.get('/health')
def healthcheck() -> dict:
    try:
        db_ok = check_database()
        lx = lx_embedded_manager.status()
        lx_ok = True
        if lx.get('mode') == 'embedded' and lx.get('embedded_enabled'):
            lx_ok = bool(lx.get('embedded_running') or not lx.get('embedded_available'))
        return {
            'status': 'ok' if (db_ok and lx_ok) else 'degraded',
            'database': 'ok' if db_ok else 'failed',
            'lx': lx,
        }
    except Exception as exc:
        return {
            'status': 'degraded',
            'database': 'failed',
            'detail': str(exc),
        }
