"""Conservative candidates introduced by exact inline labels.

The colon and the closed labels below are the evidence.  Values are never
looked up in a language, vendor, or certification dictionary.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass

from services.digital_twin.cv.sections import SourceLine

LANGUAGE_LABELS = frozenset({"language", "languages", "langue", "langues"})
PLANNED_CERTIFICATION_LABELS = frozenset(
    {
        "planned certification",
        "planned certifications",
        "certification planned",
        "certifications planned",
    }
)
PREPARING_LABELS = frozenset(
    {
        "currently preparing",
        "preparing",
        "certification preparation",
        "certifications preparation",
    }
)
_CERTIFICATION_WORD = re.compile(
    r"(?<!\w)(?:certified|certification|certifications|certificate|certificates|"
    r"certifie|certifiee|certifies|certifiees|certificat|certificats)(?!\w)",
    re.IGNORECASE,
)
_LANGUAGE_SEPARATOR = re.compile(r"\s*[,;|]\s*")
_CERTIFICATION_SEPARATOR = re.compile(r"\s*[;|]\s*")
_TERMINAL_PUNCTUATION = frozenset(".!?")


@dataclass(frozen=True)
class CertificationListItem:
    """One item proven by an exact professional-development list label."""

    meaning: str
    text: str
    page_numbers: tuple[int, ...]


def _fold_label(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    plain = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(plain.casefold().split())


def labelled_parts(text: str) -> tuple[str, str] | None:
    """Return the exact text either side of the first colon, when both exist."""
    if ":" not in text:
        return None
    label, value = text.split(":", 1)
    if not label.strip() or not value.strip():
        return None
    return _fold_label(label), value.strip()


def language_items(text: str) -> tuple[str, ...]:
    parts = labelled_parts(text)
    if parts is None or parts[0] not in LANGUAGE_LABELS:
        return ()
    return tuple(item for item in _LANGUAGE_SEPARATOR.split(parts[1]) if item)


def certification_items(
    block: Sequence[SourceLine],
) -> tuple[str, tuple[str, ...]] | None:
    """Read an explicitly introduced certification list from a logical block.

    A continuation line remains joined to the final item with its source line
    break.  Generic preparation items must each carry their own closed lexical
    evidence; an explicit certification label does not need that second test.
    """
    if not block:
        return None
    parts = labelled_parts(block[0].text)
    if parts is None:
        return None
    label, value = parts
    if len(block) > 1:
        value = "\n".join((value, *(line.text for line in block[1:])))
    items = tuple(item for item in _CERTIFICATION_SEPARATOR.split(value) if item)
    if label in PLANNED_CERTIFICATION_LABELS:
        return "planned", items
    if label in PREPARING_LABELS:
        supported = tuple(item for item in items if _CERTIFICATION_WORD.search(item))
        return ("preparing", supported) if supported else None
    return None


def is_language_labelled_line(text: str) -> bool:
    return bool(language_items(text))


def _certification_label(text: str) -> tuple[str, str] | None:
    parts = labelled_parts(text)
    if parts is None:
        return None
    label, value = parts
    if label in PLANNED_CERTIFICATION_LABELS:
        return "planned", value
    if label in PREPARING_LABELS:
        return "preparing", value
    return None


def _has_separator(text: str) -> bool:
    return ";" in text or "|" in text


def scan_certification_lists(
    body: Sequence[SourceLine],
) -> tuple[CertificationListItem, ...]:
    """Scan exact labelled lists, carrying only punctuation-proven wraps.

    A label line may consume its immediately following physical line only when
    the label line already established a multi-item list, its final item has no
    terminal punctuation, and the following line supplies another explicit
    list separator.  Thus the second line both completes the open item and
    proves the next boundary; adjacency or a newline alone proves nothing.
    """
    found: list[CertificationListItem] = []
    index = 0
    while index < len(body):
        line = body[index]
        labelled = _certification_label(line.text)
        if labelled is None:
            index += 1
            continue

        meaning, value = labelled
        lines = [line]
        final_fragment = _CERTIFICATION_SEPARATOR.split(value)[-1].rstrip()
        may_be_open = (
            _has_separator(value)
            and bool(final_fragment)
            and final_fragment[-1] not in _TERMINAL_PUNCTUATION
        )
        if may_be_open and index + 1 < len(body):
            continuation = body[index + 1]
            # Blank separation, a new exact certification label, and an exact
            # language label all fail this test before any text is consumed.
            if (
                continuation.text
                and _certification_label(continuation.text) is None
                and labelled_parts(continuation.text) is None
                and _has_separator(continuation.text)
            ):
                value = f"{value}\n{continuation.text}"
                lines.append(continuation)
                index += 1

        pages = tuple(sorted({source.page_number for source in lines}))
        items = tuple(
            item for item in _CERTIFICATION_SEPARATOR.split(value) if item
        )
        if meaning == "preparing":
            items = tuple(item for item in items if _CERTIFICATION_WORD.search(item))
        found.extend(
            CertificationListItem(meaning, item, pages) for item in items
        )
        index += 1
    return tuple(found)
