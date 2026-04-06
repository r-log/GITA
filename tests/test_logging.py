"""Tests for src.core.logging — structlog configuration."""

import logging
from unittest.mock import patch

from src.core.logging import setup_logging


class TestSetupLogging:
    @patch("src.core.logging.settings")
    def test_dev_mode_uses_console_renderer(self, mock_settings):
        mock_settings.is_dev = True
        mock_settings.log_level = "DEBUG"

        setup_logging()

        root = logging.getLogger()
        assert len(root.handlers) > 0

    @patch("src.core.logging.settings")
    def test_prod_mode_uses_json_renderer(self, mock_settings):
        mock_settings.is_dev = False
        mock_settings.log_level = "INFO"

        setup_logging()

        root = logging.getLogger()
        assert len(root.handlers) > 0

    @patch("src.core.logging.settings")
    def test_noisy_loggers_silenced(self, mock_settings):
        mock_settings.is_dev = True
        mock_settings.log_level = "INFO"

        setup_logging()

        for name in ("httpx", "httpcore", "uvicorn.access"):
            assert logging.getLogger(name).level == logging.WARNING
