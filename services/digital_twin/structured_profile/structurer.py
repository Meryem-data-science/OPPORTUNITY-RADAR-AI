"""Deterministic, local, conservative structuring of one verified fact.

The whole module is a pure function of its input and of
`STRUCTURED_PROFILE_VERSION`: no clock, no counter, no environment variable, no
network, no model, no external service, no database. The same fact value always
produces the same reading, in this process and in the next one.

What it does, and the complete list of it:

1. reads the **punctuation the document actually wrote** — a pipe-delimited
   header for an experience or a diploma, a colon after an optional list marker
   for a project, an explicit label for a certification, an explicit separator
   for a language — and cuts the first line on it;
2. recognises a temporal fragment only in a **closed** set of explicit forms,
   and an institution marker, a certification label and a proficiency wording
   only in **closed**, tiny, documented registries;
3. keeps every fragment it names exactly as written, trimmed and nothing else;
4. falls back to the type's `*_UNPARSED_V1` rule, with every fragment `None`,
   whenever the wording is not unambiguous.

Punctuation proves that segments exist. It never proves what they are about,
and the education rules are built on that distinction: a line holding pipes is
a line the document structured, not a promise that its first segment is a
diploma and its second a school. Where the two cannot be told apart by a closed
marker, only the period is kept and both stay `None`.

What it refuses to do, deliberately and by absence rather than by flag: it
derives no employer from a sentence, no role from a technology, no seniority
from the word "stage", no level, no duration, no calendar date from a school
year, no skill from a description, no diploma from an institution, no
institution from a diploma, no `Bac+N` from the word "Master", no obtained
certification from a stated intention, no issuer and no expiry date, no
language from a text written in one, and no CEFR level from "courant" or
"fluent". It does no fuzzy comparison, no similarity, no stemming, no edit
distance and no splitting of prose, and it calls no model and no API.

A rule applies or it does not. There is no partial credit and no scoring: a
header that names two of the three things the rule needs is not "mostly
structured", it is unstructured, and the fallback says so honestly. The one
place a reading stops halfway is `EDUCATION_PIPE_PERIOD_ONLY_V1`, and it is not
partial credit either: the period there is *certain*, and what is uncertain is
`None` rather than approximated. The fact keeps the full wording in every case,
so nothing is ever lost by declining.
"""

from __future__ import annotations

import re
import unicodedata

from services.digital_twin.structured_profile.models import (
    STRUCTURED_PROFILE_VERSION,
    StructuredCertification,
    StructuredEducation,
    StructuredExperience,
    StructuredLanguage,
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


# --------------------------------------------------------------------------
# Phase 3.4B2: education, certifications and languages
#
# The three readings below add fact types and change none of the two above.
# They share the same temporal grammar, the same trimming and the same refusal
# to guess, and they add one idea of their own: **punctuation proves that
# segments exist, it never proves what they are about**. A line holding pipes
# is a line the document structured; it is not a promise that the first segment
# is a diploma and the second a school. So where Phase 3.4B1 could rely on the
# position of a segment — a CV writes `role | employer | dates` and only that —
# the education rule refuses to, and asks a closed marker registry instead.
# When the registry cannot answer, the period alone is kept and the rest stays
# `None`.
# --------------------------------------------------------------------------

#: The closed registry that tells a school from a programme, and the whole of
#: it. A segment "names an institution" when one of its **whole words**, folded
#: for comparison only, is one of these. It is deliberately tiny: recognising
#: every school in the world is not the goal, and a registry that grows by
#: guesswork is a lexicon of hallucinations. There is no fuzzy comparison, no
#: edit distance, no stemming and no plural folding — a word absent from this
#: tuple simply does not name an institution, and the reading then declines.
INSTITUTION_MARKERS: tuple[str, ...] = (
    "université", "universite", "university",
    "école", "ecole", "school",
    "institut", "institute",
    "faculté", "faculte", "faculty",
    "college", "collège", "collége",
)

#: The closed registry that proves a segment names a *programme*, and the whole
#: of it. Same rules as `INSTITUTION_MARKERS`: whole words, folded for
#: comparison only, no stemming, no plural folding, no fuzzy comparison.
#:
#: It exists because a marker on one segment proves nothing on its own. A
#: segment reading `University Diploma in AI` carries `university`, and it is a
#: programme; the school it belongs to may be written next to it with no marker
#: at all. Requiring a **positive** proof of each of the two roles — and
#: requiring each proof to be exclusive — is what stops the two fields being
#: filled the wrong way round.
#:
#: It is deliberately tiny, and holds only words that name a credential rather
#: than a subject: recognising every diploma in the world is not the goal, and
#: a registry that grows by guesswork is a lexicon of hallucinations. A word
#: absent from this tuple simply does not prove a programme, and the reading
#: then declines.
PROGRAM_MARKERS: tuple[str, ...] = (
    "master", "mastère", "mastere",
    "bachelor", "licence",
    "diplôme", "diplome", "diploma", "degree",
    "ingénieur", "ingenieur", "ingénieure", "ingenieure",
    "doctorat", "doctorate",
)

#: The labels a document may use to name a certification, folded for
#: comparison. Closed, and required: this rule reads a label the document
#: wrote, never a position and never a guess about what a phrase is about.
CERTIFICATION_LABELS: tuple[str, ...] = (
    "certification", "certificat", "certificate",
)

#: The labels a document may use to name who issued a certification. Closed,
#: and optional: an issuer nobody wrote stays `None`, and none is ever deduced
#: from the certification's own name.
ISSUER_LABELS: tuple[str, ...] = (
    "délivré par", "délivrée par", "delivre par", "delivree par",
    "émis par", "emis par", "issued by", "issuer", "organisme", "éditeur",
    "editeur",
)

#: Words that state an intention rather than an achievement. A fact holding one
#: of them, anywhere, is never read as a certification: "préparation au TOEIC"
#: and "objectif AWS" are plans, and a plan is not a credential. The check is a
#: refusal, never an interpretation — the fact is still projected, with every
#: fragment `None`.
INTENTION_MARKERS: tuple[str, ...] = (
    "préparation", "preparation", "préparer", "preparer",
    "objectif", "objectifs", "objective", "goal",
    "prévu", "prevu", "prévue", "prevue", "planned", "futur", "future",
)

#: The closed registry of proficiency wordings, folded for comparison. It is
#: used to decide **whether** a fragment is a level the document wrote, never
#: to translate one: what is stored is the wording itself, trimmed and
#: otherwise untouched. "courant" is stored as "courant" and never as `C1`,
#: "fluent" never becomes `C2`, and no CEFR level is computed from anything.
PROFICIENCY_FORMS: tuple[str, ...] = (
    "a1", "a2", "b1", "b2", "c1", "c2",
    "débutant", "debutant",
    "intermédiaire", "intermediaire",
    "avancé", "avance",
    "courant", "fluent",
    "native", "natif",
    "bilingual", "bilingue",
)

#: How many written segments an *education* first line must hold before it
#: counts as a header. Lower than `MIN_PIPE_SEGMENTS` on purpose, and only
#: here. The experience rule needs three because it reads segments by position
#: — `role | employer | dates` — so two segments leave it nothing to anchor on,
#: and a sentence that happens to hold one pipe would be read as a job. The
#: education rules never read a segment by its position: they ask the closed
#: marker registry which segment is the school. Two written segments are then
#: enough to be answerable, or not, on exactly the same evidence as three.
MIN_EDUCATION_PIPE_SEGMENTS = 2

#: Whole words, for comparison against the closed registries above. Letters
#: only: digits and punctuation separate words and belong to neither.
_WORDS = re.compile(r"[^\W\d_]+")

#: The characters a language fact may use to hand its level over, besides the
#: colon and the trailing parenthesis. A dash counts only when the document
#: spaced it on both sides: `Anglais - C1` is a separator, `Franco-Anglais` is
#: a name.
_SPACED_DASH = re.compile(rf"\s[{re.escape(RANGE_DASHES)}]\s")

#: A trailing parenthesis holding a single fragment, for a language line. The
#: fragment still has to be a whole form of the closed registry to be read.
_TRAILING_PARENTHESIS = re.compile(r"^(?P<left>.*\S)\s*\((?P<right>[^()]+)\)$")


def _words(value: str) -> list[str]:
    """The whole words of a fragment, folded for comparison only."""
    return _WORDS.findall(comparison_form(value))


def names_an_institution(segment: str) -> bool:
    """Say whether one whole word of the segment is a closed institution marker."""
    return any(word in INSTITUTION_MARKERS for word in _words(segment))


def names_a_program(segment: str) -> bool:
    """Say whether one whole word of the segment is a closed programme marker."""
    return any(word in PROGRAM_MARKERS for word in _words(segment))


def states_an_intention(value: str) -> bool:
    """Say whether the fact states a plan rather than something already held."""
    return any(word in INTENTION_MARKERS for word in _words(value))


def is_explicit_proficiency(fragment: str) -> bool:
    """Say whether the whole fragment is one closed proficiency wording."""
    return comparison_form(fragment) in PROFICIENCY_FORMS


def _pipe_header(
    source_value: str, *, minimum: int = MIN_PIPE_SEGMENTS
) -> tuple[list[str], list[str]] | None:
    """The segments of an explicit pipe header, and the lines after it.

    `None` when the first line is not one: it opens with a list marker, it
    holds fewer than `minimum` segments, or one of its segments is empty.

    `minimum` is how many written segments count as a structure the document
    put there, and it is a property of the *rule* rather than of the
    punctuation. Three for a rule that reads segments by position; two for the
    education rules, which read none of them by position — see
    `MIN_EDUCATION_PIPE_SEGMENTS`.
    """
    lines = _lines(source_value)
    header = lines[0] if lines else ""
    if not header or header[0] in BULLET_MARKERS:
        return None
    segments = [segment.strip() for segment in header.split(PIPE_SEPARATOR)]
    if len(segments) < minimum or not all(segments):
        return None
    return segments, lines[1:]


def _unparsed_education(source_value: str) -> StructuredEducation:
    return StructuredEducation(
        source_value=source_value,
        institution_text=None,
        program_text=None,
        period_text=None,
        description_text=None,
        structurer_version=STRUCTURED_PROFILE_VERSION,
        structuring_rule_id=StructuringRule.EDUCATION_UNPARSED_V1,
    )


def _institution_and_program(segments: list[str]) -> tuple[str, str] | None:
    """Split exactly two segments into `(institution, programme)`, or refuse.

    Every education rule goes through here, and the answer comes from the two
    closed registries and from nothing else. No position, no capitalisation, no
    length, no comma counting and no similarity to anything takes part.

    Both roles need a **positive, exclusive** proof. One segment must name an
    institution and **not** a programme; the other must name a programme and
    **not** an institution. Everything else is a refusal.

    A marker on one segment is not proof about the other one, which is the
    whole reason this is not "the marked segment is the school". In
    `University Diploma in AI | Sorbonne` the first segment carries
    `university` and is nevertheless the programme — it carries `diploma` too —
    while the second carries no marker at all. Reading the marker alone would
    fill both fields the wrong way round and store an inversion as a structured
    fact; requiring an exclusive proof of each role leaves both `None` instead.
    A segment carrying markers of both roles proves neither, and a role nobody
    wrote a marker for is not deduced from the other segment having one.
    """
    if len(segments) != 2:
        return None
    roles = [
        (names_an_institution(segment), names_a_program(segment))
        for segment in segments
    ]
    for index, role in enumerate(roles):
        if role == (True, False) and roles[1 - index] == (False, True):
            return segments[index], segments[1 - index]
    return None


def structure_education(source_value: str) -> StructuredEducation:
    """Read one EDUCATION fact with the three closed education rules, or decline.

    All three start from an explicit pipe header — a first line not opened by a
    list marker, whose `|`-separated segments all carry text once trimmed — and
    none of them ever reads a segment by its position. Two segments are named
    only when each of the two roles carries its own **exclusive** proof: one
    segment marked as an institution and not as a programme, the other marked
    as a programme and not as an institution. See `_institution_and_program`
    for why one marker is never enough. What separates the rules is what else
    the document proved.

    `EDUCATION_PIPE_EXPLICIT_V1` applies when the header holds **exactly one**
    explicit period, exactly two segments remain beside it, and those two carry
    the exclusive proofs above. Zero periods is not this rule's shape; several
    is a header this package cannot read without choosing, and choosing is
    inventing.

    `EDUCATION_PIPE_INSTITUTION_PROGRAM_V1` applies to the shorter shape a CV
    writes just as often: a header of **exactly two** segments, **neither** of
    which is an explicit period, carrying the same exclusive proofs. There is no
    date to read, so `period_text` stays `None` — a header with no date is a
    header with no date, not a reason to decline the part that is certain, and
    not a reason to go looking for a year inside the words. Requiring both
    segments to be free of an explicit period is what keeps this honest: in
    `Université Exemple | 2020 - 2022` the unmarked segment is a date, and
    calling it a programme would be exactly the invention this package refuses,
    so that fact stays unparsed.

    `EDUCATION_PIPE_PERIOD_ONLY_V1` applies whenever a single explicit period
    is certain and the institution/programme distinction is not: a role with no
    proof, a segment proving both roles at once, both segments proving the same
    role, or more than two segments remaining. The period is kept verbatim,
    the following lines are kept as the
    description, and `institution_text` and `program_text` stay `None`. The
    certain part is preserved without the uncertain part being invented.

    Everything else is `EDUCATION_UNPARSED_V1`, with every fragment `None`. In
    no case is a diploma deduced from a school, a school from a sentence, a
    `Bac+N` or any study level from the word "Master", or a calendar date from
    a school year: `2023/2024` is stored as `2023/2024`.
    """
    header = _pipe_header(source_value, minimum=MIN_EDUCATION_PIPE_SEGMENTS)
    if header is None:
        return _unparsed_education(source_value)
    segments, rest = header
    periods = [
        index for index, segment in enumerate(segments) if is_explicit_period(segment)
    ]

    if not periods:
        # No date anywhere, so only the two-segment shape can say anything.
        named = _institution_and_program(segments)
        if named is None:
            return _unparsed_education(source_value)
        institution, program = named
        return StructuredEducation(
            source_value=source_value,
            institution_text=institution,
            program_text=program,
            period_text=None,
            description_text=_joined_rest(rest),
            structurer_version=STRUCTURED_PROFILE_VERSION,
            structuring_rule_id=(
                StructuringRule.EDUCATION_PIPE_INSTITUTION_PROGRAM_V1
            ),
        )

    if len(periods) != 1 or len(segments) < MIN_PIPE_SEGMENTS:
        # Several dates, or a two-segment header one of whose segments is a
        # date: in both cases naming the rest would mean choosing.
        return _unparsed_education(source_value)

    others = [
        segment for index, segment in enumerate(segments) if index != periods[0]
    ]
    named = _institution_and_program(others)
    institution, program = named if named is not None else (None, None)
    rule = (
        StructuringRule.EDUCATION_PIPE_EXPLICIT_V1
        if named is not None
        else StructuringRule.EDUCATION_PIPE_PERIOD_ONLY_V1
    )
    return StructuredEducation(
        source_value=source_value,
        institution_text=institution,
        program_text=program,
        period_text=segments[periods[0]],
        description_text=_joined_rest(rest),
        structurer_version=STRUCTURED_PROFILE_VERSION,
        structuring_rule_id=rule,
    )


def _labelled_segment(segment: str) -> tuple[str, str] | None:
    """Read one certification segment as `(role, value)`, or refuse it.

    A segment is readable in exactly two shapes: a whole explicit period, or
    an explicit `label: value` whose label is a whole entry of one of the two
    closed label registries and whose value is not empty. A segment that is
    neither is not "probably the certification name" — it is unreadable, and
    one unreadable segment makes the whole line unreadable.
    """
    if is_explicit_period(segment):
        return "period", segment
    index = _colon_index(segment)
    if index is None:
        return None
    label = comparison_form(segment[:index])
    value = segment[index + 1 :].strip()
    if not value:
        return None
    if label in CERTIFICATION_LABELS:
        return "certification", value
    if label in ISSUER_LABELS:
        return "issuer", value
    return None


def _unparsed_certification(source_value: str) -> StructuredCertification:
    return StructuredCertification(
        source_value=source_value,
        certification_text=None,
        issuer_text=None,
        period_text=None,
        description_text=None,
        structurer_version=STRUCTURED_PROFILE_VERSION,
        structuring_rule_id=StructuringRule.CERTIFICATION_UNPARSED_V1,
    )


def structure_certification(source_value: str) -> StructuredCertification:
    """Read one CERTIFICATION fact with `CERTIFICATION_EXPLICIT_V1`, or decline.

    No real certification fact exists yet, so this rule is written against the
    syntax a document would have to use rather than against data, and it is
    written as narrowly as that allows. It applies when, and only when:

    * the fact states no intention — no whole word of `INTENTION_MARKERS`
      appears anywhere in it. "Préparation au TOEIC" and "Objectif : AWS" are
      plans, and this package never turns a plan into a credential;
    * the first line, after at most one list marker, splits into segments that
      are **all** readable: each is either a whole explicit period or an
      explicit `label: value` whose label is a closed registry entry;
    * a certification label appears exactly once, and no label or period
      repeats — two names, two issuers or two dates on one line are a shape
      this rule cannot read without choosing.

    `certification_text` is then what the certification label introduced,
    `issuer_text` what an issuer label introduced or `None`, `period_text` the
    period segment or `None`, and `description_text` the remaining lines.

    Everything else is `CERTIFICATION_UNPARSED_V1`, with every fragment `None`.
    Nothing here records that a certification was obtained, when it was
    obtained, when it expires, or by whom it was issued — the schema has no
    column for any of those, and the word "certification" in a sentence proves
    none of them.
    """
    if states_an_intention(source_value):
        return _unparsed_certification(source_value)
    lines = _lines(source_value)
    header = _strip_one_bullet(lines[0] if lines else "")
    if not header:
        return _unparsed_certification(source_value)
    read: dict[str, str] = {}
    for segment in header.split(PIPE_SEPARATOR):
        labelled = _labelled_segment(segment.strip())
        if labelled is None or labelled[0] in read:
            return _unparsed_certification(source_value)
        read[labelled[0]] = labelled[1]
    if "certification" not in read:
        return _unparsed_certification(source_value)
    return StructuredCertification(
        source_value=source_value,
        certification_text=read["certification"],
        issuer_text=read.get("issuer"),
        period_text=read.get("period"),
        description_text=_joined_rest(lines[1:]),
        structurer_version=STRUCTURED_PROFILE_VERSION,
        structuring_rule_id=StructuringRule.CERTIFICATION_EXPLICIT_V1,
    )


def _unparsed_language(source_value: str) -> StructuredLanguage:
    return StructuredLanguage(
        source_value=source_value,
        language_text=None,
        proficiency_text=None,
        structurer_version=STRUCTURED_PROFILE_VERSION,
        structuring_rule_id=StructuringRule.LANGUAGE_UNPARSED_V1,
    )


def _language_split(line: str) -> tuple[str, str] | None:
    """Cut a language line on the one explicit separator it wrote, or refuse.

    The accepted separators are a closed set: a trailing parenthesis, a colon
    that is not a URL scheme, a pipe, and a dash the document spaced on both
    sides. Whichever comes first wins, and both sides must hold text.
    """
    parenthesised = _TRAILING_PARENTHESIS.fullmatch(line)
    if parenthesised is not None:
        return parenthesised.group("left").strip(), parenthesised.group("right").strip()
    cuts = [index for index in (_colon_index(line),) if index is not None]
    if PIPE_SEPARATOR in line:
        cuts.append(line.index(PIPE_SEPARATOR))
    dash = _SPACED_DASH.search(line)
    if dash is not None:
        # `_SPACED_DASH` matches the space, the dash and the space, so the
        # dash itself — the one character the split has to drop — is at
        # `start() + 1`.
        cuts.append(dash.start() + 1)
    if not cuts:
        return None
    cut = min(cuts)
    left = line[:cut].strip()
    right = line[cut + 1 :].strip()
    if not left or not right:
        return None
    return left, right


def structure_language(source_value: str) -> StructuredLanguage:
    """Read one LANGUAGE fact with `LANGUAGE_EXPLICIT_PROFICIENCY_V1`, or decline.

    The rule applies when, and only when, the fact is a single line, that line
    carries one explicit separator from the closed set, and everything on the
    separator's right is a **whole** form of the closed `PROFICIENCY_FORMS`
    registry. `language_text` is then the left side and `proficiency_text` the
    right side, both exactly as written and trimmed.

    At most **one** list marker is removed first, with the same conservative
    helper the project rule uses. A CV writes its languages as a bulleted list
    as often as not, and the Phase 3.2B extractor keeps the source text of a
    bullet block, so a fact can reach here reading `• Anglais : C1`. The
    marker is punctuation the list wrote, not part of the language's name, and
    storing `• Anglais` would be storing the layout. Exactly one is removed,
    never two: a second marker is content, and it stays in `language_text`
    where the document put it. Nothing else about the line is touched.

    The registry decides only whether the fragment is a level the document
    wrote. It never translates one: `courant` is stored as `courant`, `fluent`
    as `fluent`, and no CEFR level is computed from either. Case is folded for
    the comparison alone, so `C1`, `c1` and `Courant` are all recognised and
    all stored exactly as the document typed them.

    Everything else — no separator, a right side the registry does not hold
    whole, an empty side, or a fact spanning several lines, which this table
    has no column to keep — is `LANGUAGE_UNPARSED_V1` with both fragments
    `None`. No language is deduced from a project written in English, no level
    from a diploma, and no level from anything at all.
    """
    lines = _lines(source_value)
    if len(lines) != 1:
        return _unparsed_language(source_value)
    split = _language_split(_strip_one_bullet(lines[0]))
    if split is None or not is_explicit_proficiency(split[1]):
        return _unparsed_language(source_value)
    return StructuredLanguage(
        source_value=source_value,
        language_text=split[0],
        proficiency_text=split[1],
        structurer_version=STRUCTURED_PROFILE_VERSION,
        structuring_rule_id=StructuringRule.LANGUAGE_EXPLICIT_PROFICIENCY_V1,
    )
