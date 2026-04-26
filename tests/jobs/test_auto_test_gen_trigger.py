"""Tests for the post-reindex auto-test-generation trigger (Week 9).

Covers ``_maybe_enqueue_test_gen_jobs`` end-to-end. Each test sets up
a tmp_path mini-repo on disk so Stage A can scan it, an indexed Repo
row + CodeIndex rows in the test DB so Stage B can answer feasibility
questions, and a ``FakeRedisPool`` that captures every ``enqueue_job``
call so we can assert on what would have been enqueued.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from gita.config import settings
from gita.db.models import CodeIndex, Repo
from gita.indexer.ingest import IngestResult
from gita.jobs.runners import _maybe_enqueue_test_gen_jobs


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class FakeRedisPool:
    """Captures enqueue_job calls. Optionally returns None on dedupe."""

    def __init__(self, dedupe_after: int | None = None) -> None:
        self.calls: list[dict] = []
        self.dedupe_after = dedupe_after

    async def enqueue_job(
        self,
        function_name: str,
        *,
        _job_id: str,
        **kwargs,
    ):
        self.calls.append(
            {"function": function_name, "job_id": _job_id, "kwargs": kwargs}
        )
        if (
            self.dedupe_after is not None
            and len(self.calls) > self.dedupe_after
        ):
            return None
        return object()  # truthy marker — emulates an arq Job


# ---------------------------------------------------------------------------
# DB fixture wiring — same pattern as test_reindex_runner.py
# ---------------------------------------------------------------------------
@pytest_asyncio.fixture
async def _patch_runner_session(db_session: AsyncSession, monkeypatch):
    @asynccontextmanager
    async def _fake_session_local():
        yield db_session

    monkeypatch.setattr(
        "gita.jobs.runners.SessionLocal", _fake_session_local
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _write(p: Path, content: str = "x = 1\n") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


async def _make_repo(
    session, *, name: str = "fixture_repo", auto: bool = True
) -> Repo:
    repo = Repo(name=name, root_path="/tmp/no", auto_test_generation=auto)
    session.add(repo)
    await session.flush()
    return repo


async def _make_file(
    session,
    repo: Repo,
    file_path: str,
    *,
    structure: dict | None = None,
    content: str = "",
    line_count: int = 30,
):
    row = CodeIndex(
        repo_id=repo.id,
        file_path=file_path,
        language="python",
        content=content,
        line_count=line_count,
        structure=structure or {"functions": [{"name": "go"}]},
    )
    session.add(row)
    await session.flush()


def _ingest_result(
    *, mode: str = "incremental", added_files: list[str] | None = None,
    head_sha: str = "abc1234567890",
) -> IngestResult:
    return IngestResult(
        repo_id="ignored",
        files_indexed=len(added_files or []),
        functions_extracted=0,
        classes_extracted=0,
        edges_total=0,
        edges_resolved=0,
        head_sha=head_sha,
        mode=mode,
        added_files=added_files or [],
    )


@pytest.fixture
def opt_in_env(monkeypatch):
    """Both global + per-repo flags ON; cap at 5 (high enough for most tests)."""
    monkeypatch.setattr(settings, "auto_test_gen_enabled", True)
    monkeypatch.setattr(settings, "auto_test_gen_max_per_reindex", 5)


# ---------------------------------------------------------------------------
# Skip-path tests — every "no-fire" reason
# ---------------------------------------------------------------------------
class TestSkipPaths:
    async def test_redis_none_skips(
        self, db_session, _patch_runner_session, tmp_path: Path, opt_in_env
    ):
        repo = await _make_repo(db_session)
        result = await _maybe_enqueue_test_gen_jobs(
            repo_full_name="owner/repo",
            repo_id=repo.id,
            repo_auto_test_gen=True,
            repo_default_branch="main",
            root_path=tmp_path,
            ingest_result=_ingest_result(added_files=["src/x.py"]),
            redis=None,
        )
        assert result["status"] == "skipped"
        assert result["reason"] == "no_redis_pool"

    async def test_global_kill_switch_off_skips(
        self,
        db_session,
        _patch_runner_session,
        tmp_path: Path,
        monkeypatch,
    ):
        monkeypatch.setattr(settings, "auto_test_gen_enabled", False)
        repo = await _make_repo(db_session)
        pool = FakeRedisPool()
        result = await _maybe_enqueue_test_gen_jobs(
            repo_full_name="owner/repo",
            repo_id=repo.id,
            repo_auto_test_gen=True,
            repo_default_branch="main",
            root_path=tmp_path,
            ingest_result=_ingest_result(added_files=["src/x.py"]),
            redis=pool,
        )
        assert result["reason"] == "global_kill_switch_off"
        assert pool.calls == []

    async def test_repo_opt_in_off_skips(
        self,
        db_session,
        _patch_runner_session,
        tmp_path: Path,
        opt_in_env,
    ):
        repo = await _make_repo(db_session, auto=False)
        pool = FakeRedisPool()
        result = await _maybe_enqueue_test_gen_jobs(
            repo_full_name="owner/repo",
            repo_id=repo.id,
            repo_auto_test_gen=False,
            repo_default_branch="main",
            root_path=tmp_path,
            ingest_result=_ingest_result(added_files=["src/x.py"]),
            redis=pool,
        )
        assert result["reason"] == "repo_opt_in_off"
        assert pool.calls == []

    async def test_full_mode_skips(
        self,
        db_session,
        _patch_runner_session,
        tmp_path: Path,
        opt_in_env,
    ):
        repo = await _make_repo(db_session)
        pool = FakeRedisPool()
        result = await _maybe_enqueue_test_gen_jobs(
            repo_full_name="owner/repo",
            repo_id=repo.id,
            repo_auto_test_gen=True,
            repo_default_branch="main",
            root_path=tmp_path,
            ingest_result=_ingest_result(
                mode="full", added_files=["src/x.py"]
            ),
            redis=pool,
        )
        assert result["reason"] == "mode=full"
        assert pool.calls == []

    async def test_no_added_files_skips(
        self,
        db_session,
        _patch_runner_session,
        tmp_path: Path,
        opt_in_env,
    ):
        repo = await _make_repo(db_session)
        pool = FakeRedisPool()
        result = await _maybe_enqueue_test_gen_jobs(
            repo_full_name="owner/repo",
            repo_id=repo.id,
            repo_auto_test_gen=True,
            repo_default_branch="main",
            root_path=tmp_path,
            ingest_result=_ingest_result(added_files=[]),
            redis=pool,
        )
        assert result["reason"] == "no_added_files"
        assert pool.calls == []


# ---------------------------------------------------------------------------
# Stage A + Stage B filtering inside the trigger
# ---------------------------------------------------------------------------
class TestStageFiltering:
    async def test_stage_a_filters_files_with_existing_tests(
        self,
        db_session,
        _patch_runner_session,
        tmp_path: Path,
        opt_in_env,
    ):
        """One file has a sibling test → filtered. Other passes both."""
        repo = await _make_repo(db_session)
        # File 1: has sibling test (Stage A blocks)
        _write(tmp_path / "src" / "myapp" / "with_tests.py")
        _write(tmp_path / "src" / "myapp" / "test_with_tests.py")
        await _make_file(db_session, repo, "src/myapp/with_tests.py")

        # File 2: no tests anywhere (Stage A passes)
        _write(tmp_path / "src" / "myapp" / "without_tests.py")
        await _make_file(db_session, repo, "src/myapp/without_tests.py")

        pool = FakeRedisPool()
        result = await _maybe_enqueue_test_gen_jobs(
            repo_full_name="owner/repo",
            repo_id=repo.id,
            repo_auto_test_gen=True,
            repo_default_branch="main",
            root_path=tmp_path,
            ingest_result=_ingest_result(
                added_files=[
                    "src/myapp/with_tests.py",
                    "src/myapp/without_tests.py",
                ]
            ),
            redis=pool,
        )
        assert "src/myapp/with_tests.py" not in result["after_stage_a"]
        assert "src/myapp/without_tests.py" in result["after_stage_a"]
        assert len(pool.calls) == 1
        assert pool.calls[0]["kwargs"]["target_file"] == (
            "src/myapp/without_tests.py"
        )

    async def test_stage_b_filters_infeasible_files(
        self,
        db_session,
        _patch_runner_session,
        tmp_path: Path,
        opt_in_env,
    ):
        """Stage A passes (no tests on disk) but Stage B rejects: no
        public symbols / not in index."""
        repo = await _make_repo(db_session)
        _write(tmp_path / "src" / "internals.py")
        # File is in the index but only private symbols.
        await _make_file(
            db_session,
            repo,
            "src/internals.py",
            structure={
                "functions": [{"name": "_helper"}],
                "classes": [],
            },
        )
        pool = FakeRedisPool()
        result = await _maybe_enqueue_test_gen_jobs(
            repo_full_name="owner/repo",
            repo_id=repo.id,
            repo_auto_test_gen=True,
            repo_default_branch="main",
            root_path=tmp_path,
            ingest_result=_ingest_result(
                added_files=["src/internals.py"]
            ),
            redis=pool,
        )
        assert result["after_stage_a"] == ["src/internals.py"]
        assert result["after_stage_b"] == []
        assert result["status"] == "no_candidates"
        assert pool.calls == []


# ---------------------------------------------------------------------------
# Cap + enqueue + dedupe
# ---------------------------------------------------------------------------
class TestCapAndEnqueue:
    async def test_cap_enforced_alphabetically(
        self,
        db_session,
        _patch_runner_session,
        tmp_path: Path,
        monkeypatch,
    ):
        """Cap=2 with 4 viable candidates → first 2 alphabetically."""
        monkeypatch.setattr(settings, "auto_test_gen_enabled", True)
        monkeypatch.setattr(settings, "auto_test_gen_max_per_reindex", 2)

        repo = await _make_repo(db_session)
        for name in ("delta", "alpha", "charlie", "bravo"):
            _write(tmp_path / "src" / f"{name}.py")
            await _make_file(
                db_session,
                repo,
                f"src/{name}.py",
                structure={"functions": [{"name": "do_thing"}]},
            )

        pool = FakeRedisPool()
        result = await _maybe_enqueue_test_gen_jobs(
            repo_full_name="owner/repo",
            repo_id=repo.id,
            repo_auto_test_gen=True,
            repo_default_branch="main",
            root_path=tmp_path,
            ingest_result=_ingest_result(
                added_files=[
                    "src/delta.py",
                    "src/alpha.py",
                    "src/charlie.py",
                    "src/bravo.py",
                ]
            ),
            redis=pool,
        )
        targets = [c["kwargs"]["target_file"] for c in pool.calls]
        assert targets == ["src/alpha.py", "src/bravo.py"]
        assert result["dropped_over_cap"] == 2
        assert result["status"] == "enqueued"

    async def test_job_id_is_deterministic(
        self,
        db_session,
        _patch_runner_session,
        tmp_path: Path,
        opt_in_env,
    ):
        """Same (repo, file, sha7) → same job_id every time."""
        repo = await _make_repo(db_session)
        _write(tmp_path / "src" / "foo.py")
        await _make_file(
            db_session,
            repo,
            "src/foo.py",
            structure={"functions": [{"name": "go"}]},
        )

        pool = FakeRedisPool()
        await _maybe_enqueue_test_gen_jobs(
            repo_full_name="Owner/REPO",
            repo_id=repo.id,
            repo_auto_test_gen=True,
            repo_default_branch="main",
            root_path=tmp_path,
            ingest_result=_ingest_result(
                added_files=["src/foo.py"], head_sha="abc1234deadbeef"
            ),
            redis=pool,
        )
        assert len(pool.calls) == 1
        # repo lowered, sha truncated to 7
        assert pool.calls[0]["job_id"] == (
            "generate-tests:owner/repo:src/foo.py:abc1234"
        )
        # Confirm the runner-level kwargs are correct.
        kwargs = pool.calls[0]["kwargs"]
        assert kwargs["repo_full_name"] == "Owner/REPO"
        assert kwargs["target_repo"] == "Owner/REPO"
        assert kwargs["base_sha"] == "abc1234deadbeef"
        assert kwargs["base_branch"] == "main"

    async def test_repo_default_branch_flows_into_enqueue(
        self,
        db_session,
        _patch_runner_session,
        tmp_path: Path,
        opt_in_env,
    ):
        """Week 10: enqueued job picks up the repo's default_branch
        instead of a hardcoded 'main' (so master/develop repos work)."""
        repo = await _make_repo(db_session)
        _write(tmp_path / "src" / "foo.py")
        await _make_file(
            db_session,
            repo,
            "src/foo.py",
            structure={"functions": [{"name": "go"}]},
        )

        pool = FakeRedisPool()
        await _maybe_enqueue_test_gen_jobs(
            repo_full_name="owner/repo",
            repo_id=repo.id,
            repo_auto_test_gen=True,
            repo_default_branch="develop",
            root_path=tmp_path,
            ingest_result=_ingest_result(added_files=["src/foo.py"]),
            redis=pool,
        )
        assert pool.calls[0]["kwargs"]["base_branch"] == "develop"

    async def test_arq_dedupe_marked_in_summary(
        self,
        db_session,
        _patch_runner_session,
        tmp_path: Path,
        opt_in_env,
    ):
        """When ARQ returns None (existing job_id) we record deduped=True."""
        repo = await _make_repo(db_session)
        _write(tmp_path / "src" / "foo.py")
        await _make_file(
            db_session,
            repo,
            "src/foo.py",
            structure={"functions": [{"name": "go"}]},
        )

        pool = FakeRedisPool(dedupe_after=0)  # every call returns None
        result = await _maybe_enqueue_test_gen_jobs(
            repo_full_name="owner/repo",
            repo_id=repo.id,
            repo_auto_test_gen=True,
            repo_default_branch="main",
            root_path=tmp_path,
            ingest_result=_ingest_result(added_files=["src/foo.py"]),
            redis=pool,
        )
        assert len(result["enqueued"]) == 1
        assert result["enqueued"][0]["deduped"] is True
