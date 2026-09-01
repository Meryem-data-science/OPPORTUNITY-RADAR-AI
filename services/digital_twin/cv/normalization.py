"""Conservative text normalization for extracted CV pages.

Normalization touches exactly one closed list of artefacts a PDF text layer
creates: line-ending conventions, space-like and zero-width characters, the
padding a layout engine leaves between glyph runs, and stretches of blank
lines. Those are the only characters it removes or replaces: every character
that carries visible text comes through untouched, and nothing is reordered,
translated, reworded, spell-checked or de-hyphenated. The separation into
lines, the only structure the parser has to work with, survives: a line holding
text is never merged into another, never split, and never emptied.

Layout padding is dropped from the text, which is the right call for the text:
the glyph positions of a line say nothing a reader of that line needs. They do
say something about the *document*, though, and that signal must not be lost
with them, so the split into lines is exposed here as its own two steps —
`split_raw_lines` and `kept_line_indexes` — and `normalize_text` is written on
top of them. A caller that read one layout fact per extracted line can then
carry those facts through exactly this normalization, position for position,
instead of re-deriving where a line begins with a second, competing rule.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence

#: Space-like characters a PDF text layer emits that must read as plain spaces.
_SPACE_LIKE = "\t               　"
#: Zero-width characters that carry no text and would break heading matching.
_ZERO_WIDTH = "​‌‍﻿"
_LINE_BREAKS = ("\r\n", "\r", " ", " ", "\f", "\v")

_TRANSLATION = {ord(character): " " for character in _SPACE_LIKE}
_TRANSLATION.update({ord(character): None for character in _ZERO_WIDTH})


def split_raw_lines(value: str) -> tuple[str, ...]:
    """Split one extracted block into raw lines, exactly where `normalize_text` does.

    Composition and the line-break conventions are applied first, so the result
    is the sequence `normalize_text` goes on to normalize line by line. Nothing
    else is touched: a returned line still carries its layout padding, and a
    blank line is still a blank line. This exists so a caller holding one layout
    fact per extracted line can line those facts up against the normalized text
    instead of re-deriving where a line begins with a second, competing rule.
    """
    text = unicodedata.normalize("NFC", value)
    for line_break in _LINE_BREAKS:
        text = text.replace(line_break, "\n")
    return tuple(text.split("\n"))


def kept_line_indexes(lines: Sequence[str]) -> tuple[int, ...]:
    """Return the positions of `lines` that survive `normalize_text`, in order.

    `normalize_text` collapses any run of blank lines to a single blank line and
    drops the leading and trailing ones. This states which positions that keeps,
    so a caller can carry a per-line value through the very same collapse rather
    than guessing at the correspondence afterwards.
    """
    kept: list[int] = []
    for index, line in enumerate(lines):
        if line or (kept and lines[kept[-1]]):
            kept.append(index)
    while kept and not lines[kept[-1]]:
        kept.pop()
    return tuple(kept)


def normalize_line(value: str) -> str:
    """Normalize one line: space-like characters, then collapsed runs of spaces.

    Leading and trailing spaces go too. PDF extraction indentation reflects the
    glyph positions of the original layout rather than any structure of the
    document, so keeping it would only add noise.
    """
    collapsed = value.translate(_TRANSLATION).split(" ")
    return " ".join(part for part in collapsed if part)


def normalize_text(value: str) -> str:
    """Return the conservative normal form of one extracted text block.

    Unicode is composed to NFC so that a heading written with combining accents
    and the same heading written with precomposed ones are the same string. Any
    run of blank lines collapses to a single blank line, and the block is
    stripped of its leading and trailing blank lines; every other line survives
    with its content intact.
    """
    lines = [normalize_line(line) for line in split_raw_lines(value)]
    return "\n".join(lines[index] for index in kept_line_indexes(lines))


def normalize_lines(value: str) -> tuple[str, ...]:
    """Return the normalized block as its lines, blank separators included."""
    normalized = normalize_text(value)
    return () if not normalized else tuple(normalized.split("\n"))
