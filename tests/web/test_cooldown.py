"""Tests for the per-repo cooldown rate limiter.

Pure in-memory tests — no DB, no Redis, no HTTP. Uses monkeypatching
to control ``time.monotonic`` for deterministic timing.
"""
from __future__ import annotations

import pytest

from gita.web import cooldown


@pytest.fixture(autouse=True)
def _clean_cooldown():
    """Reset cooldown state before and after each test."""
    cooldown.reset()
    yield
    cooldown.reset()


class TestCheckCooldown:
    def test_first_request_not_in_cooldown(self):
        assert cooldown.check_cooldown("r-log/AMASS") is False

    def test_in_cooldown_after_record(self):
        cooldown.record_enqueue("r-log/AMASS")
        assert cooldown.check_cooldown("r-log/AMASS") is True

    def test_different_repo_not_in_cooldown(self):
        cooldown.record_enqueue("r-log/AMASS")
        assert cooldown.check_cooldown("other/repo") is False

    def test_cooldown_expires(self, monkeypatch):
        """After the window passes, the repo should no longer be in cooldown."""
        fake_time = [100.0]
        monkeypatch.setattr("gita.web.cooldown.time.monotonic", lambda: fake_time[0])

        cooldown.record_enqueue("r-log/AMASS")
        assert cooldown.check_cooldown("r-log/AMASS") is True

        # Advance time past the default 60s window.
        fake_time[0] = 161.0
        assert cooldown.check_cooldown("r-log/AMASS") is False

    def test_cooldown_still_active_within_window(self, monkeypatch):
        fake_time = [100.0]
        monkeypatch.setattr("gita.web.cooldown.time.monotonic", lambda: fake_time[0])

        cooldown.record_enqueue("r-log/AMASS")

        # 30 seconds later — still within 60s window.
        fake_time[0] = 130.0
        assert cooldown.check_cooldown("r-log/AMASS") is True

    def test_custom_window(self, monkeypatch):
        fake_time = [100.0]
        monkeypatch.setattr("gita.web.cooldown.time.monotonic", lambda: fake_time[0])

        cooldown.record_enqueue("r-log/AMASS")

        # 15 seconds later, within a 10s window → expired.
        fake_time[0] = 115.0
        assert cooldown.check_cooldown("r-log/AMASS", window=10) is False

        # But within a 20s window → still active.
        assert cooldown.check_cooldown("r-log/AMASS", window=20) is True

    def test_case_insensitive(self):
        cooldown.record_enqueue("R-Log/AMASS")
        assert cooldown.check_cooldown("r-log/amass") is True


class TestRecordEnqueue:
    def test_record_enables_cooldown(self):
        assert cooldown.check_cooldown("r-log/AMASS") is False
        cooldown.record_enqueue("r-log/AMASS")
        assert cooldown.check_cooldown("r-log/AMASS") is True

    def test_record_updates_timestamp(self, monkeypatch):
        """A second record pushes the cooldown window forward."""
        fake_time = [100.0]
        monkeypatch.setattr("gita.web.cooldown.time.monotonic", lambda: fake_time[0])

        cooldown.record_enqueue("r-log/AMASS")

        # 50 seconds later — still in cooldown from first record.
        fake_time[0] = 150.0
        assert cooldown.check_cooldown("r-log/AMASS") is True

        # Record again at 150s, pushing the window to 210s.
        cooldown.record_enqueue("r-log/AMASS")

        # 165s — would be past first window but within second.
        fake_time[0] = 165.0
        assert cooldown.check_cooldown("r-log/AMASS") is True

        # 215s — past second window too.
        fake_time[0] = 215.0
        assert cooldown.check_cooldown("r-log/AMASS") is False


class TestReset:
    def test_reset_clears_all(self):
        cooldown.record_enqueue("r-log/AMASS")
        cooldown.record_enqueue("other/repo")
        cooldown.reset()
        assert cooldown.check_cooldown("r-log/AMASS") is False
        assert cooldown.check_cooldown("other/repo") is False
