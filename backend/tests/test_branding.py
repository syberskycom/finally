"""Tests for the startup branding/logo."""

import io

from rich.console import Console

from app.branding import LOGO_WORD, _FONT, print_logo, render_logo


class TestBranding:
    """Unit tests for the sybersky ASCII logo."""

    def test_logo_word_glyphs_defined(self):
        """Every letter in the logo word has a glyph defined."""
        for letter in LOGO_WORD:
            assert letter in _FONT

    def test_glyphs_are_rectangular(self):
        """Each glyph is a 7-row grid of equal-width rows."""
        for glyph in _FONT.values():
            assert len(glyph) == 7
            assert len({len(row) for row in glyph}) == 1

    def test_render_logo_produces_seven_lines(self):
        """The rendered logo has one line per glyph row."""
        text = render_logo()
        assert str(text).count("\n") == 6

    def test_print_logo_runs_without_error(self):
        """print_logo() executes against a Console without raising."""
        console = Console(file=io.StringIO())
        print_logo(console)
        output = console.file.getvalue()
        assert "FinAlly" in output
        assert "sybersky" in output
