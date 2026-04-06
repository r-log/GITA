"""Tests for src.tools.ai.predictor — deterministic velocity, blocker, and stale PR detection."""

import pytest
from datetime import datetime, timedelta

from src.tools.ai.predictor import _calculate_velocity, _detect_blockers, _detect_stale_prs


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _days_ago(days: int) -> str:
    return _iso(datetime.utcnow() - timedelta(days=days))


class TestCalculateVelocity:
    async def test_no_issues(self):
        result = await _calculate_velocity([])
        assert result.success is True
        assert result.data["total_count"] == 0

    async def test_no_closed_issues(self):
        issues = [
            {"number": 1, "state": "open"},
            {"number": 2, "state": "open"},
        ]
        result = await _calculate_velocity(issues)
        assert result.success is True
        assert result.data["closed_count"] == 0

    async def test_single_closed_issue(self):
        issues = [
            {"number": 1, "state": "closed", "closed_at": _days_ago(3)},
        ]
        result = await _calculate_velocity(issues)
        assert result.success is True
        assert result.data["closed_count"] == 1
        assert result.data["velocity"] > 0

    async def test_multiple_closed_issues(self):
        issues = [
            {"number": 1, "state": "closed", "closed_at": _days_ago(10)},
            {"number": 2, "state": "closed", "closed_at": _days_ago(5)},
            {"number": 3, "state": "closed", "closed_at": _days_ago(1)},
            {"number": 4, "state": "open"},
        ]
        result = await _calculate_velocity(issues)
        assert result.success is True
        assert result.data["closed_count"] == 3
        assert result.data["open_count"] == 1
        assert result.data["completion_pct"] > 0

    async def test_trend_accelerating(self):
        """When recent closes are faster than older ones."""
        now = datetime.utcnow()
        issues = [
            {"number": 1, "state": "closed", "closed_at": _iso(now - timedelta(days=20))},
            {"number": 2, "state": "closed", "closed_at": _iso(now - timedelta(days=18))},
            {"number": 3, "state": "closed", "closed_at": _iso(now - timedelta(days=3))},
            {"number": 4, "state": "closed", "closed_at": _iso(now - timedelta(days=2))},
            {"number": 5, "state": "closed", "closed_at": _iso(now - timedelta(days=1))},
        ]
        result = await _calculate_velocity(issues)
        assert result.success is True
        # Trend should reflect more recent activity
        assert result.data["trend"] in ("accelerating", "steady", "insufficient_data")


class TestDetectBlockers:
    async def test_no_issues(self):
        result = await _detect_blockers([])
        assert result.success is True
        assert result.data["count"] == 0

    async def test_no_stale_issues(self):
        issues = [
            {"number": 1, "state": "open", "updated_at": _days_ago(1), "labels": [], "assignees": []},
        ]
        result = await _detect_blockers(issues, stale_days=14)
        assert result.success is True
        assert result.data["count"] == 0

    async def test_stale_issue_detected(self):
        issues = [
            {"number": 1, "state": "open", "updated_at": _days_ago(20), "title": "Old bug",
             "labels": [{"name": "bug"}], "assignees": [{"login": "dev1"}]},
        ]
        result = await _detect_blockers(issues, stale_days=14)
        assert result.success is True
        assert result.data["count"] == 1
        assert result.data["blockers"][0]["number"] == 1

    async def test_closed_issues_excluded(self):
        issues = [
            {"number": 1, "state": "closed", "updated_at": _days_ago(30), "labels": [], "assignees": []},
        ]
        result = await _detect_blockers(issues, stale_days=14)
        assert result.data["count"] == 0

    async def test_custom_stale_days(self):
        issues = [
            {"number": 1, "state": "open", "updated_at": _days_ago(5), "title": "Recent",
             "labels": [], "assignees": []},
        ]
        result = await _detect_blockers(issues, stale_days=3)
        assert result.data["count"] == 1


class TestDetectStalePrs:
    async def test_no_prs(self):
        result = await _detect_stale_prs([])
        assert result.success is True
        assert result.data["count"] == 0

    async def test_fresh_pr_not_stale(self):
        prs = [
            {"number": 1, "state": "open", "created_at": _days_ago(1),
             "title": "Fresh PR", "user": {"login": "dev"}},
        ]
        result = await _detect_stale_prs(prs, stale_days=7)
        assert result.data["count"] == 0

    async def test_stale_pr_detected(self):
        prs = [
            {"number": 1, "state": "open", "created_at": _days_ago(10),
             "title": "Old PR", "user": {"login": "dev"}},
        ]
        result = await _detect_stale_prs(prs, stale_days=7)
        assert result.data["count"] == 1
        assert result.data["stale_prs"][0]["number"] == 1

    async def test_closed_prs_excluded(self):
        prs = [
            {"number": 1, "state": "closed", "created_at": _days_ago(30),
             "title": "Old", "user": {"login": "dev"}},
        ]
        result = await _detect_stale_prs(prs, stale_days=7)
        assert result.data["count"] == 0
