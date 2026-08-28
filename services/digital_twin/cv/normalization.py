"""Conservative text normalization for extracted CV pages.

Normalization only removes artefacts a PDF text layer creates: line-ending
conventions, exotic space characters, the padding a layout engine leaves
between glyph runs, and stretches of blank lines. It never rewrites the CV. No
line is reordered, translated, reworded, spell-checked, de-hyphenated or
dropped, and the separation into lines — the only structure the parser has to
work with — is preserved exactly.
"""

from __future__ import annotations

import unicodedata

#: Space-like characters a PDF text layer emits that must read as plain spaces.
_SPACE_LIKE = "\t               　"
#: Zero-width characters that carry no text and would break heading matching.
_ZERO_WIDTH = "​‌‍﻿"
_LINE_BREAKS = ("\r\n", "\r", " ", " ", "\f", "\v")

_TRANSLATION = {ord(character): " " for character in _SPACE_LIKE}
_TRANSLATION.update({ord(character): None for character in _ZERO_WIDTH})


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
    text = unicodedata.normalize("NFC", value)
    for line_break in _LINE_BREAKS:
        text = text.replace(line_break, "\n")
    lines = [normalize_line(line) for line in text.split("\n")]

    normalized: list[str] = []
    for line in lines:
        if line or (normalized and normalized[-1]):
            normalized.append(line)
    while normalized and not normalized[-1]:
        normalized.pop()
    return "\n".join(normalized)


def normalize_lines(value: str) -> tuple[str, ...]:
    """Return the normalized block as its lines, blank separators included."""
    normalized = normalize_text(value)
    return () if not normalized else tuple(normalized.split("\n"))
