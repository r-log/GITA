"""
Tests for the scratchpad-backed onboarding tools.

record_finding must:
- reject generic phrases like "add tests" / "add CI/CD" / "improve docs"
- reject invalid severity and kind values
- reject findings pointing at nonexistent files or out-of-bounds lines
- append valid findings to scratchpad["findings"] with a 1-indexed id

finalize_exploration must set scratchpad["finalized"] = True and store the summary.
"""

from unittest.mock import AsyncMock, patch

import pytest

from src.tools.onboarding.scratchpad_tools import (
    make_record_finding,
    make_finalize_exploration,
)


def _valid_args() -> dict:
    return {
        "file": "src/app.py",
        "line": 42,
        "severity": "medium",
        "kind": "error_handling",
        "finding": "silently swallows ValueError without logging",
        "fix_sketch": "log.warning and re-raise",
    }


@pytest.fixture
def scratchpad() -> dict:
    return {}


@pytest.fixture
def mock_file_exists():
    """Patch the DB lookup so record_finding tests don't need Postgres."""
    with patch(
        "src.tools.onboarding.scratchpad_tools._file_exists_in_index",
        new=AsyncMock(return_value=(True, 200)),
    ) as mock:
        yield mock


class TestRecordFinding:
    @pytest.mark.asyncio
    async def test_valid_finding_is_recorded(self, scratchpad, mock_file_exists):
        tool = make_record_finding(repo_id=1, scratchpad=scratchpad)
        result = await tool.execute(**_valid_args())
        assert result.success is True
        assert len(scratchpad["findings"]) == 1
        entry = scratchpad["findings"][0]
        assert entry["id"] == 1
        assert entry["file"] == "src/app.py"
        assert entry["severity"] == "medium"

    @pytest.mark.asyncio
    async def test_id_increments(self, scratchpad, mock_file_exists):
        tool = make_record_finding(repo_id=1, scratchpad=scratchpad)
        await tool.execute(**_valid_args())
        args2 = _valid_args() | {"finding": "another concrete issue here"}
        await tool.execute(**args2)
        ids = [f["id"] for f in scratchpad["findings"]]
        assert ids == [1, 2]

    @pytest.mark.asyncio
    async def test_rejects_generic_add_tests(self, scratchpad, mock_file_exists):
        tool = make_record_finding(repo_id=1, scratchpad=scratchpad)
        args = _valid_args() | {"finding": "Add unit tests for this module"}
        result = await tool.execute(**args)
        assert result.success is False
        assert "banned generic phrase" in (result.error or "")
        assert scratchpad["findings"] == []

    @pytest.mark.asyncio
    async def test_rejects_generic_add_ci(self, scratchpad, mock_file_exists):
        tool = make_record_finding(repo_id=1, scratchpad=scratchpad)
        args = _valid_args() | {"finding": "Add CI/CD pipeline"}
        result = await tool.execute(**args)
        assert result.success is False

    @pytest.mark.asyncio
    async def test_rejects_improve_documentation(self, scratchpad, mock_file_exists):
        tool = make_record_finding(repo_id=1, scratchpad=scratchpad)
        args = _valid_args() | {"finding": "Improve documentation across the repo"}
        result = await tool.execute(**args)
        assert result.success is False

    @pytest.mark.asyncio
    async def test_rejects_invalid_severity(self, scratchpad, mock_file_exists):
        tool = make_record_finding(repo_id=1, scratchpad=scratchpad)
        args = _valid_args() | {"severity": "extreme"}
        result = await tool.execute(**args)
        assert result.success is False
        assert "severity" in (result.error or "")

    @pytest.mark.asyncio
    async def test_rejects_invalid_kind(self, scratchpad, mock_file_exists):
        tool = make_record_finding(repo_id=1, scratchpad=scratchpad)
        args = _valid_args() | {"kind": "made_up_kind"}
        result = await tool.execute(**args)
        assert result.success is False
        assert "kind" in (result.error or "")

    @pytest.mark.asyncio
    async def test_rejects_unknown_file(self, scratchpad):
        # Simulate "file not in code_index"
        with patch(
            "src.tools.onboarding.scratchpad_tools._file_exists_in_index",
            new=AsyncMock(return_value=(False, None)),
        ):
            tool = make_record_finding(repo_id=1, scratchpad=scratchpad)
            result = await tool.execute(**_valid_args())
        assert result.success is False
        assert "not in the code index" in (result.error or "")

    @pytest.mark.asyncio
    async def test_rejects_out_of_bounds_line(self, scratchpad):
        with patch(
            "src.tools.onboarding.scratchpad_tools._file_exists_in_index",
            new=AsyncMock(return_value=(True, 50)),
        ):
            tool = make_record_finding(repo_id=1, scratchpad=scratchpad)
            args = _valid_args() | {"line": 500}
            result = await tool.execute(**args)
        assert result.success is False
        assert "out of bounds" in (result.error or "")


class TestFinalizeExploration:
    @pytest.mark.asyncio
    async def test_sets_finalized_flag(self, scratchpad):
        tool = make_finalize_exploration(scratchpad)
        result = await tool.execute(
            project_summary="A FastAPI service with async SQLAlchemy and an ARQ worker."
        )
        assert result.success is True
        assert scratchpad["finalized"] is True
        assert "FastAPI" in scratchpad["project_summary"]

    @pytest.mark.asyncio
    async def test_rejects_empty_summary(self, scratchpad):
        tool = make_finalize_exploration(scratchpad)
        result = await tool.execute(project_summary="   ")
        assert result.success is False

    @pytest.mark.asyncio
    async def test_rejects_bad_confidence(self, scratchpad):
        tool = make_finalize_exploration(scratchpad)
        result = await tool.execute(
            project_summary="A web service.", confidence=1.5
        )
        assert result.success is False
