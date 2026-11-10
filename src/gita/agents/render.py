"""Make model-written text safe to post under the GitHub App's identity.

Everything an agent posts — finding descriptions, fix sketches,
summaries, milestone titles — is LLM output, and the LLM read
attacker-controllable input (PR descriptions, diffs, repository
content). Posted verbatim it could:

* ping users or whole teams (``@org/everyone``) from the bot account,
* embed images (tracking pixels) or raw HTML (``<details>``, comments
  that hide text, fake "approved" badges),
* break out of the blockquote/card layout with embedded newlines and
  inject headings or fake sections.

These helpers neutralise those while keeping the text readable: a
zero-width space after ``@`` and before tag-like ``<`` defeats mention
and HTML parsing without visibly changing the text, including inside
code spans.
"""

from __future__ import annotations

import re

_ZWSP = "​"
_MENTION_RE = re.compile(r"@(?=[A-Za-z0-9])")
_TAG_START_RE = re.compile(r"<(?=[A-Za-z/!?])")
_IMAGE_RE = re.compile(r"!\[")
_WHITESPACE_RE = re.compile(r"\s+")


def neutralize(text: str) -> str:
    """Defuse mentions, HTML tags, and markdown images in ``text``."""
    text = _MENTION_RE.sub("@" + _ZWSP, text)
    text = _TAG_START_RE.sub("<" + _ZWSP, text)
    text = _IMAGE_RE.sub("!" + _ZWSP + "[", text)
    return text


def inline(text: str) -> str:
    """Neutralised, single-line text for titles, headings, list items."""
    return _WHITESPACE_RE.sub(" ", neutralize(text)).strip()


def quote_lines(text: str, prefix: str = "") -> list[str]:
    """Render ``text`` as blockquote lines that can't escape the quote.

    Every line of a multi-line value gets its own ``> `` marker, so an
    embedded ``\\n## Approved`` stays inside the quote instead of
    becoming a heading.
    """
    lines = neutralize(text).strip().splitlines() or [""]
    first, *rest = lines
    return [f"> {prefix}{first}"] + [f"> {line}" for line in rest]
