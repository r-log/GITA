"""
ARQ worker settings.
This is the entry point for the background worker process:
    python -m arq src.workers.settings.WorkerSettings
"""

from arq.connections import RedisSettings
from arq.cron import cron

from src.core.config import settings
from src.workers.tasks import dispatch_event
from src.workers.context_updater import process_context_update
from src.workers.reconciliation import reconcile_all_repos
from src.agents.setup import register_all_agents


async def startup(ctx):
    """Called when the worker starts — register agents."""
    register_all_agents()


async def process_webhook(ctx, event_type, action, repo_full_name, installation_id, payload):
    """ARQ task wrapper for dispatch_event."""
    await dispatch_event(event_type, action, repo_full_name, installation_id, payload)


async def run_reconciliation(ctx):
    """ARQ cron task — reconcile all tracked repos."""
    await reconcile_all_repos()


class WorkerSettings:
    functions = [process_webhook, process_context_update, run_reconciliation]
    cron_jobs = [
        cron(run_reconciliation, hour={0, 6, 12, 18}, minute=0),  # every 6 hours
    ]
    on_startup = startup
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    max_jobs = 10
    job_timeout = 1200  # 20 minutes max per job
