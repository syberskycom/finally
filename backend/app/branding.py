"""FinAlly startup branding — ASCII logo for sybersky.

Renders a small dot-matrix style "SYBERSKY" wordmark using block characters,
colored with the project's accent palette (yellow/blue/purple).
"""

from __future__ import annotations

from rich.console import Console
from rich.text import Text

# 5x7 dot-matrix glyphs, one string per row, '#' = filled cell.
_FONT: dict[str, list[str]] = {
    "S": [
        ".####",
        "#....",
        "#....",
        ".###.",
        "....#",
        "....#",
        "####.",
    ],
    "Y": [
        "#...#",
        "#...#",
        ".#.#.",
        "..#..",
        "..#..",
        "..#..",
        "..#..",
    ],
    "B": [
        "####.",
        "#...#",
        "#...#",
        "####.",
        "#...#",
        "#...#",
        "####.",
    ],
    "E": [
        "#####",
        "#....",
        "#....",
        "####.",
        "#....",
        "#....",
        "#####",
    ],
    "R": [
        "####.",
        "#...#",
        "#...#",
        "####.",
        "#.#..",
        "#..#.",
        "#...#",
    ],
    "K": [
        "#...#",
        "#..#.",
        "#.#..",
        "##...",
        "#.#..",
        "#..#.",
        "#...#",
    ],
}

LOGO_WORD = "SYBERSKY"

# Brand accent colors, left to right gradient across the wordmark.
_GRADIENT = ["#ecad0a", "#ecad0a", "#209dd7", "#209dd7", "#753991", "#753991", "#ecad0a", "#209dd7"]

_BLOCK = "█"


def render_logo(word: str = LOGO_WORD) -> Text:
    """Render `word` as a multi-line Rich Text dot-matrix wordmark."""
    glyphs = [_FONT[letter] for letter in word]
    height = 7
    logo = Text()
    for row in range(height):
        for i, glyph in enumerate(glyphs):
            color = _GRADIENT[i % len(_GRADIENT)]
            line = glyph[row].replace("#", _BLOCK).replace(".", " ")
            logo.append(line, style=color)
            logo.append(" ")
        if row != height - 1:
            logo.append("\n")
    return logo


def print_logo(console: Console | None = None) -> None:
    """Print the FinAlly / sybersky startup banner."""
    console = console or Console()
    console.print()
    console.print(render_logo())
    console.print()
    console.print("  [bold bright_white]FinAlly[/] [bright_black]— AI Trading Workstation[/]")
    console.print("  [bright_black]by sybersky[/]")
    console.print()
