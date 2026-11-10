"""Tests for neutralising model-written text before it is posted."""

from __future__ import annotations

from gita.agents.render import inline, neutralize, quote_lines


class TestNeutralize:
    def test_mentions(self):
        out = neutralize("thanks @alice and @org/team")
        assert "@alice" not in out and "@org/team" not in out
        assert out.replace("\u200b", "") == "thanks @alice and @org/team"

    def test_email_like_text_is_harmless_either_way(self):
        assert neutralize("a@b.io").replace("\u200b", "") == "a@b.io"

    def test_html_tags(self):
        for tag in ("<img src=x>", "<details>", "</summary>", "<!-- hide -->", "<?xml"):
            assert tag not in neutralize(tag)

    def test_comparisons_are_untouched(self):
        assert neutralize("if x < 3 and y <= 4") == "if x < 3 and y <= 4"

    def test_images(self):
        assert "![" not in neutralize("![pixel](https://example.com/p.gif)")


class TestInline:
    def test_collapses_whitespace_and_newlines(self):
        assert inline("  Fix\n\n## Heading  ") == "Fix ## Heading"


class TestQuoteLines:
    def test_every_line_is_quoted(self):
        assert quote_lines("one\ntwo", prefix="* ") == ["> * one", "> two"]

    def test_empty_text(self):
        assert quote_lines("") == ["> "]
