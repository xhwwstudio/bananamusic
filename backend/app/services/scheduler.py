import asyncio
import logging
import uuid
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.services.app_settings import get_runtime_settings
from app.services.db_data import create_pending_scan_task
from app.services.curation_service import generate_curated_playlists
from app.services.storage import get_storage_service
from app.services.storage.errors import StorageError

logger = logging.getLogger(__name__)

SCHED_TZ = ZoneInfo('Asia/Shanghai')
scheduler = AsyncIOScheduler(timezone=SCHED_TZ)

async def _run_cron_scan_task(task_id: str, path: str, mode: str, concurrency: int, batch_size: int):
    # To avoid circular imports, import locally
    from app.api.app_data import _run_scan_task
    try:
        await _run_scan_task(task_id, path, mode, concurrency, batch_size)
    except Exception as e:
        logger.error(f"Cron scan task failed: {e}")

async def trigger_cron_job():
    settings = get_runtime_settings()
    path = settings.get('cron_path')
    if not path:
        return
    try:
        path = get_storage_service().normalize_path(str(path))
    except StorageError as exc:
        logger.warning('Skip cron scan because storage is not configured yet: %s', exc)
        return
    mode = settings.get('cron_mode', 'incremental')
    concurrency = int(settings.get('cron_concur', 10))
    batch_size = int(settings.get('cron_batch', 100))
    
    task_id = f'T-{uuid.uuid4().hex[:12]}'
    create_pending_scan_task(
        task_id=task_id,
        path=path,
        mode=mode,
        concurrency=concurrency,
        batch_size=batch_size,
    )
    # create_task to run the scan
    asyncio.create_task(_run_cron_scan_task(task_id, path, mode, concurrency, batch_size))

async def trigger_lx_curation_job():
    try:
        await generate_curated_playlists(trigger_autofill=True, trigger_source='scheduler')

        settings = get_runtime_settings()
        quality = str(settings.get('autofill_quality') or 'flac').strip() or 'flac'
        try:
            upload_dir = get_storage_service().normalize_path(str(settings.get('autofill_path') or '/MusicOn_AutoDownload/').strip() or '/MusicOn_AutoDownload/')
        except StorageError as exc:
            logger.warning('Skip scheduler autofill because storage is not configured yet: %s', exc)
            return
        if not upload_dir.endswith('/'):
            upload_dir += '/'

        from app.services.db_data import create_pending_scan_task, list_tasks
        from app.api.app_data import _run_autofill_task

        active_autofill = [
            t for t in list_tasks()
            if t.get('status') in ('queued', 'running')
            and (
                t.get('source') == 'autofill'
                or t.get('mode') == 'autofill'
                or '自动补全' in str(t.get('path') or '')
            )
        ]
        if active_autofill:
            logger.info(f"LX curation finished, skip autofill because active task exists: {active_autofill[0].get('id')}")
            return

        task = create_pending_scan_task(
            path=f'定时榜单刷新后自动补全 ({quality}) -> {upload_dir}',
            mode='autofill',
            concurrency=1,
            batch_size=1,
        )
        logger.info(f"LX curation finished, starting autofill task {task['id']} -> {upload_dir} [{quality}]")
        asyncio.create_task(_run_autofill_task(task['id'], quality, upload_dir))
    except Exception as e:
        logger.error(f"LX curation task failed: {e}", exc_info=True)

def setup_scheduler():
    global scheduler

    if scheduler.running:
        scheduler.shutdown(wait=False)
    loop = asyncio.get_running_loop()
    scheduler = AsyncIOScheduler(event_loop=loop, timezone=SCHED_TZ)

    settings = get_runtime_settings()
    cron_expr = settings.get('cron_expr', '').strip()
    lx_cron_expr = settings.get('lx_cron_expr', '5,3 0,12 * * *').strip()

    logger.info(f"Scheduler init: cron_expr='{cron_expr}', lx_cron_expr='{lx_cron_expr}'")

    if cron_expr:
        try:
            scheduler.add_job(
                trigger_cron_job,
                CronTrigger.from_crontab(cron_expr, timezone=SCHED_TZ),
                id='cron_scan',
                replace_existing=True
            )
            logger.info(f"Scheduled cron_scan with expression: {cron_expr}")
        except Exception as e:
            logger.error(f"Invalid cron expression '{cron_expr}': {e}")

    if lx_cron_expr:
        try:
            if lx_cron_expr == '5,3 0,12 * * *':
                scheduler.add_job(
                    trigger_lx_curation_job,
                    CronTrigger.from_crontab('5 0 * * *', timezone=SCHED_TZ),
                    id='lx_curation_1',
                    replace_existing=True
                )
                scheduler.add_job(
                    trigger_lx_curation_job,
                    CronTrigger.from_crontab('3 12 * * *', timezone=SCHED_TZ),
                    id='lx_curation_2',
                    replace_existing=True
                )
                logger.info("Scheduled lx_curation at 00:05 and 12:03")
            else:
                scheduler.add_job(
                    trigger_lx_curation_job,
                    CronTrigger.from_crontab(lx_cron_expr, timezone=SCHED_TZ),
                    id='lx_curation',
                    replace_existing=True
                )
                logger.info(f"Scheduled lx_curation with expression: {lx_cron_expr}")
        except Exception as e:
            logger.error(f"Invalid LX cron expression '{lx_cron_expr}': {e}")

    scheduler.start()
    jobs = scheduler.get_jobs()
    logger.info(f"Scheduler started with {len(jobs)} jobs")

def update_cron_schedule():
    setup_scheduler()
