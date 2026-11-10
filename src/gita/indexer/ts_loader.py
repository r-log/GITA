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


def _python():
    import tree_sitter_python as module

    return module.language()


def _javascript():
    import tree_sitter_javascript as module

    return module.language()


def _typescript():
    import tree_sitter_typescript as module

    return module.language_typescript()


def _tsx():
    import tree_sitter_typescript as module

    return module.language_tsx()


def _c():
    import tree_sitter_c as module

    return module.language()


def _cpp():
    import tree_sitter_cpp as module

    return module.language()


def _csharp():
    import tree_sitter_c_sharp as module

    return module.language()


# Language name -> loader of its grammar. Imports are deferred so a broken
# or missing grammar package only disables its own language.
_GRAMMARS = {
    "python": _python,
    "javascript": _javascript,
    "typescript": _typescript,
    "tsx": _tsx,
    "c": _c,
    "cpp": _cpp,
    "csharp": _csharp,
}


def _build_language(name: str) -> Language | None:
    loader = _GRAMMARS.get(name)
    if loader is None:
        logger.warning("tree_sitter_language_unknown language=%s", name)
        return None
    try:
        return Language(loader())
    except Exception as exc:
        logger.warning("tree_sitter_language_load_failed language=%s error=%s", name, exc)
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
    return tuple(_GRAMMARS)
