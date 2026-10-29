from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.app_data import router as app_router
from app.api.health import router as health_router
from app.api.subsonic import response_format_var, router as subsonic_router
from app.core.config import settings
from app.services.app_settings import ensure_settings_table
from app.services.curation_service import ensure_curated_playlists
from app.services.db_data import init_database
from app.services.web_auth import get_web_session
from app.services.scheduler import scheduler, setup_scheduler
from app.services.lx_runtime import lx_embedded_manager

ensure_settings_table()
init_database()
ensure_curated_playlists()
# 不要在启动阶段强制初始化 storage。
# 空库首启时 storage_providers 还没配好，若这里直接 get_storage_service()
# 会导致 SQLite / PostgreSQL 都在 import 阶段启动即死，用户甚至进不了设置页。
# 改成按需懒加载：等用户在 Web 里完成存储配置后，再由具体接口使用。


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_scheduler()
    try:
        await lx_embedded_manager.start()
    except Exception as exc:
        print(f'[lx-server] embedded startup failed: {exc}')
    yield
    await lx_embedded_manager.stop()
    if scheduler.running:
        scheduler.shutdown(wait=False)


app = FastAPI(title=settings.app_name, version='1.9.1', lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=['*'],
    allow_credentials=True,
    allow_methods=['*'],
    allow_headers=['*'],
)


@app.middleware('http')
async def subsonic_json_hint(request: Request, call_next):
    response_format_var.set('json' if request.query_params.get('f', '').strip().lower() == 'json' else 'xml')

    if request.url.path.startswith('/rest/') or request.url.path == '/rest':
        raw_pairs = []
        for key, value in request.query_params.multi_items():
            if key in {'t', 'p', 's'}:
                raw_pairs.append((key, '***'))
            else:
                raw_pairs.append((key, value))
        safe_query = urlencode(raw_pairs, doseq=True)
        print(f"[subsonic] {request.method} {request.url.path}{'?' + safe_query if safe_query else ''}")

    if request.url.path.startswith('/api/') and not request.url.path.startswith('/api/web-auth/'):
        session = get_web_session(request)
        if session is None:
            # The SPA keeps its cookie until an API call fails, so a stale or
            # invalidated session must produce a message the UI can display.
            return JSONResponse({'detail': '登录已失效，请重新登录后再操作'}, status_code=401)

    return await call_next(request)


app.include_router(health_router)
app.include_router(app_router)
app.include_router(subsonic_router)

APP_ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = APP_ROOT / 'static'
if not (STATIC_DIR / 'index.html').exists():
    # When running from a source checkout the Vite bundle lives in
    # `frontend/dist`; the Docker image copies it to `backend/static`.
    for candidate in (APP_ROOT.parent / 'frontend' / 'dist', APP_ROOT / 'frontend' / 'dist'):
        if (candidate / 'index.html').exists():
            STATIC_DIR = candidate
            break
FRONTEND_INDEX = STATIC_DIR / 'index.html'

ASSETS_DIR = STATIC_DIR / 'assets'
if ASSETS_DIR.exists():
    app.mount('/assets', StaticFiles(directory=ASSETS_DIR), name='assets')

DATA_DIR = APP_ROOT / 'data'
if DATA_DIR.exists():
    app.mount('/app/data', StaticFiles(directory=DATA_DIR), name='data')


@app.get('/', response_model=None)
def root():
    if FRONTEND_INDEX.exists():
        return FileResponse(FRONTEND_INDEX)
    return {
        'name': settings.app_name,
        'status': 'bootstrapped',
        'phase': 'package-01',
    }


@app.get('/app-meta')
def app_meta() -> dict:
    return {
        'name': settings.app_name,
        'status': 'bootstrapped',
        'phase': 'package-01',
    }


@app.get('/favicon.ico')
def favicon() -> dict:
    return {}


@app.get('/{frontend_path:path}', response_model=None)
def frontend_fallback(frontend_path: str):
    if not FRONTEND_INDEX.exists():
        return {
            'name': settings.app_name,
            'status': 'bootstrapped',
            'phase': 'package-01',
        }

    blocked_prefixes = ('api/', 'rest/', 'reset/', 'assets/', 'app/data/')
    if frontend_path.startswith(blocked_prefixes):
        raise HTTPException(status_code=404, detail='Not Found')

    if '.' in frontend_path.split('/')[-1]:
        raise HTTPException(status_code=404, detail='Not Found')

    return FileResponse(FRONTEND_INDEX)
