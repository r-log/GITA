"""Tests for ARQ worker settings and Redis integration.

Split into two groups:
1. **Pure tests** — verify WorkerSettings configuration without Redis.
2. **Redis integration tests** — enqueue/dequeue against real Redis
   from docker-compose. Skipped if Redis is unavailable.
"""
from __future__ import annotations

import pytest

from gita.jobs import ALL_JOBS
from gita.worker import WorkerSettings, _mask_url, _parse_redis_url


# ---------------------------------------------------------------------------
# Pure: _parse_redis_url
# ---------------------------------------------------------------------------
class TestParseRedisUrl:
    def test_default_url(self):
        rs = _parse_redis_url("redis://localhost:6379")
        assert rs.host == "localhost"
        assert rs.port == 6379
        assert rs.database == 0
        assert rs.password is None

    def test_with_database(self):
        rs = _parse_redis_url("redis://localhost:6379/3")
        assert rs.database == 3

    def test_with_password(self):
        rs = _parse_redis_url("redis://:secret@redis.example.com:6380/1")
        assert rs.host == "redis.example.com"
        assert rs.port == 6380
        assert rs.password == "secret"
        assert rs.database == 1

    def test_with_username_and_password(self):
        rs = _parse_redis_url("redis://user:pass@host:6379/0")
        assert rs.username == "user"
        assert rs.password == "pass"


class TestMaskUrl:
    def test_masks_password(self):
        result = _mask_url("redis://user:secret@host:6379/0")
        assert "secret" not in result
        assert "***" in result
        assert "host" in result

    def test_no_password_unchanged(self):
        url = "redis://localhost:6379"
        assert _mask_url(url) == url


# ---------------------------------------------------------------------------
# Pure: WorkerSettings shape
# ---------------------------------------------------------------------------
class TestWorkerSettings:
    def test_functions_match_all_jobs(self):
        assert WorkerSettings.functions is ALL_JOBS

    def test_max_jobs_is_one(self):
        """Sequential processing — one job at a time."""
        assert WorkerSettings.max_jobs == 1

    def test_max_tries(self):
        assert WorkerSettings.max_tries == 3

    def test_has_startup_and_shutdown(self):
        assert WorkerSettings.on_startup is not None
        assert WorkerSettings.on_shutdown is not None

    def test_redis_settings_populated(self):
        rs = WorkerSettings.redis_settings
        assert rs.host is not None
        assert rs.port > 0


# ---------------------------------------------------------------------------
# Redis integration: enqueue + dequeue
# ---------------------------------------------------------------------------
def _redis_available_sync() -> bool:
    """Quick synchronous check if Redis is reachable via raw TCP."""
    import socket
    from gita.worker import _parse_redis_url
    from gita.config import settings
    rs = _parse_redis_url(settings.redis_url)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(2)
    try:
        result = sock.connect_ex((rs.host, rs.port))
        return result == 0
    except Exception:
        return False
    finally:
        sock.close()


_REDIS_UP = _redis_available_sync()

_skip_no_redis = pytest.mark.skipif(
    not _REDIS_UP, reason="Redis not available (docker-compose not running?)"
)


@_skip_no_redis
class TestRedisIntegration:
    @pytest.fixture
    async def pool(self):
        from arq import create_pool
        from gita.config import settings
        # Use settings.redis_url (127.0.0.1) not WorkerSettings.redis_settings
        # which may have been built with stale module-level defaults.
        rs = _parse_redis_url(settings.redis_url)
        p = await create_pool(rs)
        # Flush the test DB to avoid interference from prior runs.
        await p.flushdb()
        yield p
        await p.flushdb()
        await p.aclose()

    async def test_enqueue_job(self, pool):
        """A job can be enqueued and the Job object is returned."""
        job = await pool.enqueue_job(
            "review_pr",
            _job_id="test-review:r-log/amass:1",
            repo_full_name="r-log/AMASS",
            pr_number=1,
        )
        assert job is not None
        assert job.job_id == "test-review:r-log/amass:1"

    async def test_duplicate_job_id_returns_none(self, pool):
        """Wall 3: ARQ rejects duplicate enqueue by job ID."""
        job1 = await pool.enqueue_job(
            "review_pr",
            _job_id="test-review:r-log/amass:2",
            repo_full_name="r-log/AMASS",
            pr_number=2,
        )
        assert job1 is not None

        # Same job ID again — should return None.
        job2 = await pool.enqueue_job(
            "review_pr",
            _job_id="test-review:r-log/amass:2",
            repo_full_name="r-log/AMASS",
            pr_number=2,
        )
        assert job2 is None

    async def test_different_job_ids_both_enqueue(self, pool):
        job1 = await pool.enqueue_job(
            "review_pr",
            _job_id="test-review:r-log/amass:10",
            repo_full_name="r-log/AMASS",
            pr_number=10,
        )
        job2 = await pool.enqueue_job(
            "review_pr",
            _job_id="test-review:r-log/amass:11",
            repo_full_name="r-log/AMASS",
            pr_number=11,
        )
        assert job1 is not None
        assert job2 is not None
        assert job1.job_id != job2.job_id

    async def test_startup_hook_creates_engine(self):
        """Startup hook creates a DB engine and sets initialized flag."""
        from gita.worker import startup
        ctx: dict = {}
        await startup(ctx)
        assert ctx["initialized"] is True
        assert ctx["engine"] is not None
        # Cleanup
        await ctx["engine"].dispose()

    async def test_shutdown_hook_disposes_engine(self):
        """Shutdown hook disposes the engine created by startup."""
        from gita.worker import startup, shutdown
        ctx: dict = {}
        await startup(ctx)
        assert ctx.get("engine") is not None
        await shutdown(ctx)
        # Engine should still be in ctx but disposed (no assertion on internal state,
        # just verify it ran without error)

    async def test_shutdown_without_engine(self):
        """Shutdown is safe when no engine was created."""
        from gita.worker import shutdown
        ctx: dict = {}
        await shutdown(ctx)  # should not raise
