"""
ARQ worker settings.
This is the entry point for the background worker process:
    python -m arq src.workers.settings.WorkerSettings
"""

from arq.connections import RedisSettings

from src.core.config import settings
from src.workers.tasks import dispatch_event


async def process_webhook(ctx, event_type, action, repo_full_name, installation_id, payload):
    """ARQ task wrapper for dispatch_event."""
    await dispatch_event(event_type, action, repo_full_name, installation_id, payload)


class WorkerSettings:
    functions = [process_webhook]
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    max_jobs = 10
    job_timeout = 300  # 5 minutes max per job
