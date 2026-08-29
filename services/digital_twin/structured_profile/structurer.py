"""Deterministic, local, conservative structuring of one verified fact.

The whole module is a pure function of its input and of
`STRUCTURED_PROFILE_VERSION`: no clock, no counter, no environment variable, no
network, no model, no external service, no database. The same fact value always
produces the same reading, in this process and in the next one.

What it does, and the complete list of it:

1. reads the **punctuation the document actually wrote** — a pipe-delimited
   header for an experience, a colon after an optional list marker for a
   project — and cuts the first line on it;
2. recognises a temporal fragment only in a **closed** set of explicit forms;
3. keeps every fragment it names exactly as written, trimmed and nothing else;
4. falls back to `UNPARSED_V1`, with every fragment `None`, whenever the
   wording is not unambiguous.

What it refuses to do, deliberately and by absence rather than by flag: it
derives no employer from a sentence, no role from a technology, no seniority
from the word "stage", no level, no duration, no calendar date from a school
year, no skill from a description, no certification and no language. It does no
fuzzy matching, no similarity, no stemming, no edit distance and no splitting
of prose, and it calls no model and no API.

A rule applies or it does not. There is no partial credit and no scoring: a
header that names two of the three things the rule needs is not "mostly
structured", it is unstructured, and `UNPARSED_V1` says so honestly. The fact
keeps the full wording either way, so nothing is ever lost by declining.
"""

from __future__ import annotations

import re
import unicodedata

from services.digital_twin.structured_profile.models import (
    STRUCTURED_PROFILE_VERSION,
    StructuredExperience,
    StructuredProject,
    StructuringRule,
)

#: Characters this package reads as a list marker opening a line. The set is a
#: local, closed copy on purpose: this package imports no CV module, because a
#: fact reaches it only through a decision a human took, never through a parser.
BULLET_MARKERS = "-–—*•·▪◦‣⁃>"

#: The literal character read as an explicit part separator, compared as
#: itself: no other vertical bar or box-drawing character is folded into it.
PIPE_SEPARATOR = "|"

#: How many written segments the first line must hold before it counts as a
#: header. Two segments are how an ordinary sentence happens to use the
#: character; three written segments are a structure the document put there.
MIN_PIPE_SEGMENTS = 3

#: The months a temporal endpoint may name, in French and English, full forms
#: and the usual abbreviations. Closed: a form absent from this tuple is not
#: recognised, and the fragment then stays `None` rather than being guessed.
MONTH_NAMES: tuple[str, ...] = (
    "janvier", "février", "fevrier", "mars", "avril", "mai", "juin",
    "juillet", "août", "aout", "septembre", "octobre", "novembre",
    "décembre", "decembre",
    "january", "february", "march", "april", "may", "june", "july",
    "august", "september", "october", "november", "december",
    "jan", "fév", "fev", "feb", "mar", "avr", "apr", "jun", "jul", "juil",
    "aug", "aoû", "aou", "sep", "sept", "oct", "nov", "déc", "dec",
)

#: The words that close an open-ended range. Closed, and never turned into a
#: date: `2023 - présent` is stored as the four-year-old document wrote it, and
#: no "current" flag, no end date and no duration is derived from it.
OPEN_END_MARKERS: tuple[str, ...] = (
    "présent", "present", "aujourd'hui", "aujourd’hui", "today", "now",
    "actuel", "actuelle", "en cours", "ongoing",
)

#: The characters accepted between the two ends of a range.
RANGE_DASHES = "-–—"

_YEAR = r"(?:19|20)\d{2}"
_NUMERIC_MONTH_YEAR = rf"(?:0[1-9]|1[0-2])/{_YEAR}"
_NAMED_MONTH_YEAR = (
    rf"(?:{'|'.join(re.escape(month) for month in MONTH_NAMES)})\.?\s+{_YEAR}"
)
#: One end of a period: a bare year, a numeric month and year, or a named month
#: and year. Nothing shorter — a lone `09` names no year and no month reliably.
_ENDPOINT = rf"(?:{_NAMED_MONTH_YEAR}|{_NUMERIC_MONTH_YEAR}|{_YEAR})"
_DASH = rf"\s*[{re.escape(RANGE_DASHES)}]\s*"
_OPEN_END = "|".join(re.escape(marker) for marker in OPEN_END_MARKERS)
#: A range whose two ends the document wrote: two endpoints separated by a
#: dash, or a school year written `2023/2024`.
_CLOSED_RANGE = rf"(?:{_ENDPOINT}{_DASH}{_ENDPOINT}|{_YEAR}/{_YEAR})"

#: The complete grammar of an explicit period, matched against a **whole**
#: fragment: one endpoint, a closed range of two endpoints, a range left open
#: by one of the closed markers, a school year written `2023/2024`, or a closed
#: range followed by a parenthesis holding **exactly** one of those same closed
#: markers — `2025-2026 (en cours)`. That last form is still a period the
#: source wrote whole: both ends are written, and the parenthesis is a word
#: from the closed registry, not free text. Nothing is read out of it — no
#: `current` flag, no end date, no duration and no employment status — and
#: `period_text` stays the fragment as written. A parenthesis holding anything
#: else is free text, so `2022-2024 (6 mois)`, `(stage)`, `(Paris)` and
#: `(approx.)` are not periods. A fragment holding anything else — prose around
#: a year, an unbounded "depuis", a season, a bare month — is not one either.
_TEMPORAL_FRAGMENT = re.compile(
    rf"(?:{_CLOSED_RANGE}\s*\((?:{_OPEN_END})\)"
    rf"|{_ENDPOINT}{_DASH}(?:{_ENDPOINT}|{_OPEN_END})"
    rf"|{_YEAR}/{_YEAR}"
    rf"|{_ENDPOINT})"
)

#: A project title may hand its period over only in this one shape: a final
#: parenthesis holding a closed range of two four-digit years. It is stricter
#: than `_TEMPORAL_FRAGMENT` on purpose. In a pipe header the document itself
#: delimited the segment, so a lone year standing in its own segment is
#: unambiguous; inside a free-text title a lone `(2024)` could be a version, an
#: edition, a promotion or a cohort, so only the unmistakable range is split.
_TRAILING_YEAR_RANGE = re.compile(
    rf"^(?P<title>.*\S)\s*\((?P<period>{_YEAR}{_DASH}{_YEAR})\)$"
)


def comparison_form(value: str) -> str:
    """The form a fragment is *compared* on. No stored value is built from it.

    NFKC, inner whitespace runs collapsed to one space, casefold — and nothing
    else. Accents and punctuation survive, because they are what distinguishes
    a real word from another one.
    """
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def is_explicit_period(fragment: str) -> bool:
    """Say whether the whole fragment is one of the closed temporal forms."""
    return bool(_TEMPORAL_FRAGMENT.fullmatch(comparison_form(fragment)))


def _lines(source_value: str) -> list[str]:
    return source_value.strip().splitlines()


def _joined_rest(rest: list[str]) -> str | None:
    """The lines after the header, kept as written. Absence stays `None`.

    Only whitespace surrounding the block is removed — enough for the schema's
    trimmed-text rule — and no line is reflowed, dedented, re-punctuated or
    summarised. What the CV wrote between two lines stays between them.
    """
    joined = "\n".join(rest).strip()
    return joined or None


def _strip_one_bullet(text: str) -> str:
    """Remove at most one list marker, and the spaces it is followed by."""
    if text and text[0] in BULLET_MARKERS:
        return text[1:].lstrip()
    return text


def structure_experience(source_value: str) -> StructuredExperience:
    """Read one EXPERIENCE fact with `EXPERIENCE_PIPE_HEADER_V1`, or decline.

    The rule applies when, and only when, the first line is written as an
    explicit pipe header:

    * the line does not open with a list marker — a bullet says the document
      wrote a list item, and a header is not a list item;
    * at least `MIN_PIPE_SEGMENTS` segments, all holding text once trimmed;
    * neither of the first two segments is an explicit period — a date standing
      where the role is written means the header is not the shape this rule
      knows;
    * **exactly one** of the remaining segments is an explicit period. Zero is
      a header with no date, several is a header this rule cannot read without
      choosing, and choosing is inventing.

    Then `role_text` is the first segment, `organization_text` the second and
    `period_text` the temporal one, each exactly as written and trimmed, and
    `description_text` is the rest of the fact. A segment the rule does not
    name — a city, a contract type, a department — is simply not projected: the
    fact keeps it, and this slice adds no column for it.

    In every other case the fact is still read, as `UNPARSED_V1` with every
    fragment `None`. Nothing is deduced to fill it in.
    """
    lines = _lines(source_value)
    header = lines[0] if lines else ""
    segments = [segment.strip() for segment in header.split(PIPE_SEPARATOR)]
    opens_a_list = bool(header) and header[0] in BULLET_MARKERS
    if not opens_a_list and len(segments) >= MIN_PIPE_SEGMENTS and all(segments):
        periods = [
            index
            for index, segment in enumerate(segments)
            if is_explicit_period(segment)
        ]
        # Ascending by construction, so one entry at index 2 or beyond is
        # "exactly one period, and it is not where the role or the
        # organization is written".
        if len(periods) == 1 and periods[0] >= 2:
            return StructuredExperience(
                source_value=source_value,
                role_text=segments[0],
                organization_text=segments[1],
                period_text=segments[periods[0]],
                description_text=_joined_rest(lines[1:]),
                structurer_version=STRUCTURED_PROFILE_VERSION,
                structuring_rule_id=StructuringRule.EXPERIENCE_PIPE_HEADER_V1,
            )
    return StructuredExperience(
        source_value=source_value,
        role_text=None,
        organization_text=None,
        period_text=None,
        description_text=None,
        structurer_version=STRUCTURED_PROFILE_VERSION,
        structuring_rule_id=StructuringRule.UNPARSED_V1,
    )


def _colon_index(header: str) -> int | None:
    """Where the explicit separator is, or `None`.

    The first `:` wins, except one immediately followed by `/`: `https://` is a
    scheme, not a title. No other colon is treated specially, and a header with
    no usable colon is simply not this rule's shape.
    """
    for index, character in enumerate(header):
        if character != ":":
            continue
        if header[index + 1 : index + 2] == "/":
            continue
        return index
    return None


def structure_project(source_value: str) -> StructuredProject:
    """Read one PROJECT fact with `PROJECT_BULLET_COLON_V1`, or decline.

    The rule applies when, and only when, the first line — after at most one
    list marker has been removed — carries an explicit `:` with text on both
    sides. `title_text` is then the left side and `description_text` the right
    side followed by the rest of the fact, each exactly as written.

    A period is split out of the title in one shape only: a final parenthesis
    holding a closed range of two four-digit years, and only if a non-empty
    title survives its removal. Anything else — a lone year, a season, a month
    with no year, a parenthesis holding something else — stays part of the
    title, and `period_text` stays `None`. Guessing which parenthesis is a date
    is exactly the interpretation this slice refuses.

    In every other case the fact is read as `UNPARSED_V1`, with every fragment
    `None` and the fact still linked.
    """
    lines = _lines(source_value)
    header = _strip_one_bullet(lines[0] if lines else "")
    index = _colon_index(header)
    if index is not None:
        left = header[:index].strip()
        right = header[index + 1 :].strip()
        if left and right:
            title, period = _split_trailing_period(left)
            return StructuredProject(
                source_value=source_value,
                title_text=title,
                period_text=period,
                description_text=_joined_rest([right, *lines[1:]]),
                structurer_version=STRUCTURED_PROFILE_VERSION,
                structuring_rule_id=StructuringRule.PROJECT_BULLET_COLON_V1,
            )
    return StructuredProject(
        source_value=source_value,
        title_text=None,
        period_text=None,
        description_text=None,
        structurer_version=STRUCTURED_PROFILE_VERSION,
        structuring_rule_id=StructuringRule.UNPARSED_V1,
    )


def _split_trailing_period(left: str) -> tuple[str, str | None]:
    """Split a final `(YYYY - YYYY)` off a title, or leave the title whole."""
    match = _TRAILING_YEAR_RANGE.fullmatch(left)
    if match is None:
        return left, None
    return match.group("title").strip(), match.group("period").strip()
