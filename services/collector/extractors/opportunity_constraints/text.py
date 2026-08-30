"""Turning a collected description into text the closed rules can read.

This module exists because of what the collectors actually store. Greenhouse
puts its `content` field into `opportunities.description` verbatim, and that
field is HTML — usually with its angle brackets escaped as entities, so a
description can arrive looking like `&lt;p&gt;Minimum 3 years&lt;/p&gt;`.
Nothing upstream unescapes or strips it: the LinkedIn alert collector produces
no description at all, so no other path ever needed to. A rule looking for
`minimum 3 years` in that string would find nothing, and the extractor would
report UNKNOWN for a posting that stated the requirement plainly.

So normalization is not cosmetic here; it is the difference between reading the
real corpus and reading only fixtures. It does three things and no more:

* unescape HTML entities, at most twice — a value escaped once is the common
  case, and one more pass covers a source that escaped an already-escaped
  document. A third pass would start turning ordinary text like `&amp;amp;`
  into something the posting did not write;
* remove tags, putting a line break where a block element ended so that two
  bullet points do not weld into one sentence;
* collapse runs of whitespace, keeping single line breaks as separators.

It changes no word, corrects no spelling, expands no abbreviation and
translates nothing. Case is preserved: the rules fold case themselves when they
need to, and evidence has to quote the posting as written.
"""

from __future__ import annotations

import re
from html import unescape

__all__ = [
    "SEGMENT_SEPARATOR",
    "normalize_description",
    "normalize_field",
    "segments",
    "shorten_evidence",
]

#: Tags that end a thought. A `</li>` between two requirements is the only
#: thing keeping them apart once the markup is gone.
_BLOCK_TAGS = (
    "p", "div", "li", "ul", "ol", "br", "tr", "td", "th", "table",
    "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "header",
    "footer", "blockquote", "pre",
)
_BLOCK_BOUNDARY = re.compile(
    r"</?(?:" + "|".join(_BLOCK_TAGS) + r")\b[^>]*>", re.IGNORECASE
)
_ANY_TAG = re.compile(r"<[^>]*>")
_SCRIPT_OR_STYLE = re.compile(
    r"<(script|style)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL
)
_HORIZONTAL_SPACE = re.compile(r"[^\S\n]+")
_BLANK_LINES = re.compile(r"\n\s*\n+")
_ENTITY = re.compile(r"&(?:#\d+|#[xX][0-9a-fA-F]+|[a-zA-Z][a-zA-Z0-9]{1,31});")

#: What `segments` splits on, and what a caller joins with to rebuild the text.
SEGMENT_SEPARATOR = "\n"


def _unescape_twice(value: str) -> str:
    """Unescape entities, once, and once more only if entities remain."""
    once = unescape(value)
    if _ENTITY.search(once) is None:
        return once
    return unescape(once)


def normalize_description(value: str | None) -> str:
    """Return the readable text of a collected description, or `''`.

    `''` for a missing description, so every caller handles absence the same
    way: a posting with no description has no text, not a `None` to guard.
    """
    if value is None:
        return ""
    text = _unescape_twice(str(value))
    text = _SCRIPT_OR_STYLE.sub(" ", text)
    text = _BLOCK_BOUNDARY.sub("\n", text)
    text = _ANY_TAG.sub(" ", text)
    # A second unescape pass would be wrong here: entities that survived the
    # markup are the posting's own words, `&` written as `&amp;` included.
    text = text.replace(" ", " ").replace("​", "")
    text = _HORIZONTAL_SPACE.sub(" ", text)
    text = _BLANK_LINES.sub("\n", text)
    return "\n".join(line.strip() for line in text.split("\n")).strip()


def normalize_field(value: str | None) -> str:
    """Return a short collected field — a title, a location — as one line."""
    if value is None:
        return ""
    text = _unescape_twice(str(value))
    text = _ANY_TAG.sub(" ", text)
    return " ".join(text.split())


def segments(text: str) -> tuple[str, ...]:
    """Split normalized text into the units the rules match against.

    A rule reads one segment at a time, and that is what keeps evidence
    minimal and keeps two unrelated sentences from being read as one claim.
    Splitting is on line breaks and on sentence-ending punctuation followed by
    a space — not on every period, because `Bac+2.5` and `3.5 years` exist.
    """
    if not text:
        return ()
    parts: list[str] = []
    for line in text.split(SEGMENT_SEPARATOR):
        for piece in re.split(r"(?<=[.!?;])\s+(?=[A-ZÀ-ÖØ-Þ(])", line):
            trimmed = piece.strip()
            if trimmed:
                parts.append(trimmed)
    return tuple(parts)


def shorten_evidence(fragment: str, limit: int) -> str:
    """Trim a matched fragment to the stored size, on a word boundary.

    Evidence is a pointer, not a copy. When a match is longer than the limit
    the tail is dropped at the last space and an ellipsis marks the cut, so
    what is stored is still readable and still visibly partial.
    """
    text = " ".join(str(fragment).split())
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    spaced = cut.rsplit(" ", 1)[0] if " " in cut else cut
    return (spaced or cut).rstrip() + "…"
