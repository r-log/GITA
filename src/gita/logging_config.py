"""Process-wide logging setup for the worker and the webhook server.

Neither ``arq`` nor ``uvicorn`` configures the root logger, so without
this every ``logger.info`` in GITA — shadow-mode decisions, guardrail
drops, dedupe hits, token usage — is silently discarded.
"""

from __future__ import annotations

import logging

_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def configure_logging(level: str | int = "INFO") -> None:
    """Attach a stream handler to the root logger once, at ``level``.

    Idempotent, and leaves an already-configured root logger alone (so
    pytest's capture and embedding applications keep their own setup).
    """
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(format=_FORMAT)
    logging.getLogger("gita").setLevel(level if isinstance(level, int) else level.upper())
