"""Which model reviews a pull request: decided in code, before any call.

Nearly all of a review's cost is the findings call, and the models GITA
can use differ by up to ~80x in price. The router picks a tier from facts
known before that call, and records why; no model is asked. Each tier maps
to a model in configuration (``AI_MODEL_SMALL`` / ``_STANDARD`` /
``_LARGE``); an unset tier uses ``AI_DEFAULT_MODEL``, so routing changes
nothing until the tiers are set.

* ``small``: the review summary (a few sentences for a verdict already
  decided), and pull requests that change no product code (docs, tests,
  build files). The cheapest model never reviews product code.
* ``large``: product code that is sensitive (authentication, crypto,
  sessions, SQL and persistence, wire protocols, concurrency, sandboxing,
  payments), widely included, or large.
* ``standard``: all other product code.

The thresholds are starting points. The evals report results per tier,
and the thresholds should follow them.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from gita.views.diff_context import FileContext, file_role

TIERS = ("small", "standard", "large")

# Whole words in a path that mark code where a defect is expensive. Matched
# on path tokens (directories, file name, camelCase parts), not substrings,
# so "Clock" isn't "lock" and "Tokenizer" isn't "token".
SENSITIVE_WORDS = frozenset(
    {
        "auth", "authentication", "login", "logout", "password", "passwords",
        "credential", "credentials", "secret", "secrets", "token", "tokens",
        "crypto", "cryptography", "cipher", "srp", "srp6", "hmac", "sha1", "jwt",
        "session", "sessions", "permission", "permissions", "privilege",
        "privileges", "acl", "sandbox",
        "sql", "database", "db", "repository", "repositories", "persistence",
        "migration", "migrations",
        "socket", "sockets", "packet", "packets", "protocol", "wire",
        "serializer", "deserializer",
        "thread", "threads", "mutex", "lock", "locks", "atomic", "concurrency",
        "payment", "payments", "billing", "wallet",
    }
)  # fmt: skip
LARGE_ADDED_LINES = 500
LARGE_SOURCE_FILES = 10
LARGE_FAN_IN = 50

_TOKEN = re.compile(r"[A-Z]?[a-z]+\d*|[A-Z]+\d*(?![a-z])|\d+")


def path_words(path: str) -> set[str]:
    """``src/game/session/WorldSession.cpp`` -> {src, game, session, world, cpp}."""
    return {t.lower() for part in re.split(r"[^A-Za-z0-9]+", path) for t in _TOKEN.findall(part)}


@dataclass(frozen=True)
class ModelTiers:
    small: str
    standard: str
    large: str

    @classmethod
    def from_settings(cls, settings: Any) -> ModelTiers:
        default = settings.ai_default_model
        return cls(
            small=settings.ai_model_small or default,
            standard=settings.ai_model_standard or default,
            large=settings.ai_model_large or default,
        )

    @classmethod
    def single(cls, model: str) -> ModelTiers:
        """Every tier the same model (an explicit override)."""
        return cls(small=model, standard=model, large=model)

    def model_for(self, tier: str) -> str:
        return getattr(self, tier)


@dataclass(frozen=True)
class Route:
    tier: str
    model: str
    reasons: tuple[str, ...]


def route_review(files: Sequence[FileContext], tiers: ModelTiers) -> Route:
    """Pick the tier for a review's findings call, with the reasons."""
    source = [
        f
        for f in files
        if f.diff_hunk.additions > 0 and file_role(f.file_path, f.language) == "source"
    ]
    if not source:
        return Route("small", tiers.small, ("no product code added (docs, tests or build only)",))

    added = sum(f.diff_hunk.additions for f in source)
    sensitive = sorted({w for f in source for w in path_words(f.file_path) & SENSITIVE_WORDS})
    fan_in = max(len(f.imported_by) for f in source)
    reasons = [f"{len(source)} source file{'s' if len(source) != 1 else ''}, {added} added lines"]
    large = False
    if sensitive:
        reasons.append("sensitive: " + ", ".join(sensitive))
        large = True
    if added >= LARGE_ADDED_LINES or len(source) >= LARGE_SOURCE_FILES:
        reasons.append("large change")
        large = True
    if fan_in >= LARGE_FAN_IN:
        reasons.append(f"included by {fan_in} files")
        large = True
    tier = "large" if large else "standard"
    return Route(tier, tiers.model_for(tier), tuple(reasons))
