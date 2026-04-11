"""Tree-sitter language and parser loader with caching.

Each supported language is loaded from its individual ``tree-sitter-<lang>``
package on first use and cached. Load failures are remembered in
``_BROKEN_LANGUAGES`` so we never spam the log on repeated attempts — this
pattern was forced on v1 by tree-sitter-language-pack packaging bugs; we keep
it as defense-in-depth even though individual packages are more reliable.
"""
from __future__ import annotations

import logging

from tree_sitter import Language, Parser

logger = logging.getLogger(__name__)

_LANGUAGE_CACHE: dict[str, Language] = {}
_PARSER_CACHE: dict[str, Parser] = {}
_BROKEN_LANGUAGES: set[str] = set()


def _build_language(name: str) -> Language | None:
    try:
        if name == "python":
            import tree_sitter_python as module

            return Language(module.language())
        if name == "javascript":
            import tree_sitter_javascript as module

            return Language(module.language())
        if name == "typescript":
            import tree_sitter_typescript as module

            return Language(module.language_typescript())
        if name == "tsx":
            import tree_sitter_typescript as module

            return Language(module.language_tsx())
    except Exception as exc:
        logger.warning(
            "tree_sitter_language_load_failed language=%s error=%s", name, exc
        )
        return None
    logger.warning("tree_sitter_language_unknown language=%s", name)
    return None


def get_language(name: str) -> Language | None:
    """Return a cached Tree-sitter Language, or None if unavailable."""
    if name in _LANGUAGE_CACHE:
        return _LANGUAGE_CACHE[name]
    if name in _BROKEN_LANGUAGES:
        return None
    lang = _build_language(name)
    if lang is None:
        _BROKEN_LANGUAGES.add(name)
        return None
    _LANGUAGE_CACHE[name] = lang
    return lang


def load_parser(name: str) -> Parser | None:
    """Return a cached Parser bound to the language, or None if unavailable."""
    if name in _PARSER_CACHE:
        return _PARSER_CACHE[name]
    lang = get_language(name)
    if lang is None:
        return None
    parser = Parser(lang)
    _PARSER_CACHE[name] = parser
    return parser


def supported_languages() -> tuple[str, ...]:
    return ("python", "javascript", "typescript", "tsx")
