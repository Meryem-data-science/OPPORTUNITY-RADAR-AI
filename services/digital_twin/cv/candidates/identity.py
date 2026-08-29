"""Conservative identity rules over the header block of a CV.

A CV usually opens with its name, a role line and a contact line, before any
heading the Phase 3.2A lexicon recognises. Phase 3.2A keeps that run as the
leading `UNCLASSIFIED` block without naming it; this module proposes candidates
from it, and only where a written rule justifies one.

Three things this module never does, because a CV cannot support them:

- it never builds a name out of an email address, a file name or a URL;
- it never completes a missing first or last name, and never reorders one;
- it never picks a line as "probably the name" when the shape rules fail. The
  extractor reports `NO_IDENTITY_CANDIDATE` and proposes nothing, which a human
  can fix in Phase 3.3 and an invented name cannot.
"""

from __future__ import annotations

from collections.abc import Sequence

from services.digital_twin.cv.candidates.text import fold_words, holds_phrase
from services.digital_twin.cv.sections import SourceLine

#: A name line is written with letters, spaces and these joiners, and nothing
#: else. A digit, an "@", a slash, a comma or a dash makes the line something
#: other than a plain name — a contact line, an address, a "Name — Title"
#: banner — and the rule declines rather than cutting the line up on a guess.
_NAME_JOINERS = "-'’."
_NAME_MIN_WORDS = 2
_NAME_MAX_WORDS = 4
_NAME_MAX_LENGTH = 60

#: Folded labels a CV puts at the very top instead of a name. They have the
#: shape of a name and are not one, so they are refused by name.
_DOCUMENT_LABELS = frozenset(
    {
        "cv",
        "curriculum",
        "curriculum vitae",
        "resume",
        "candidature",
        "dossier de candidature",
        "profil",
        "profile",
    }
)

#: Closed dictionary of role words. A header line holding one of them as whole
#: words carries a professional title. The dictionary is the whole rule: a role
#: this list does not know produces no candidate at all.
TITLE_KEYWORDS: tuple[str, ...] = (
    "data scientist",
    "data analyst",
    "data engineer",
    "data architect",
    "machine learning engineer",
    "ml engineer",
    "mlops engineer",
    "ai engineer",
    "software engineer",
    "software developer",
    "web developer",
    "full stack developer",
    "backend developer",
    "frontend developer",
    "developpeur",
    "developpeuse",
    "developer",
    "ingenieur",
    "ingenieure",
    "engineer",
    "analyste",
    "analyst",
    "consultant",
    "consultante",
    "architecte",
    "architect",
    "chef de projet",
    "project manager",
    "product manager",
    "product owner",
    "scrum master",
    "researcher",
    "chercheur",
    "chercheuse",
    "doctorant",
    "doctorante",
    "etudiant",
    "etudiante",
    "student",
    "stagiaire",
    "intern",
    "apprenti",
    "apprentie",
    "alternant",
    "alternante",
    "technicien",
    "technicienne",
    "technician",
    "administrateur",
    "administratrice",
    "administrator",
    "statisticien",
    "statisticienne",
    "designer",
    "devops",
)

#: A header line carrying one of these is a contact line, not a title line.
_CONTACT_MARKERS = ("@", "://", "www.")


def holds_title_keyword(text: str) -> bool:
    """Say whether the line carries a role word of the closed dictionary."""
    folded = fold_words(text)
    return any(holds_phrase(folded, keyword) for keyword in TITLE_KEYWORDS)


def has_name_shape(text: str) -> bool:
    """Say whether the line is written the way a plain person's name is.

    Every rule is structural and checkable: length, word count, the closed set
    of characters a name uses, and a first letter per word. None of them makes
    the line *be* a name — that is what "candidate" means — they only refuse
    the lines that demonstrably are not one.
    """
    if not text or len(text) > _NAME_MAX_LENGTH:
        return False
    if any(
        not (character.isalpha() or character == " " or character in _NAME_JOINERS)
        for character in text
    ):
        return False
    words = text.split()
    if not _NAME_MIN_WORDS <= len(words) <= _NAME_MAX_WORDS:
        return False
    if any(not word[0].isalpha() for word in words):
        return False
    if fold_words(text) in _DOCUMENT_LABELS:
        return False
    return not holds_title_keyword(text)


def name_line(body: Sequence[SourceLine]) -> SourceLine | None:
    """Return the header line the name rule proposes, or `None`.

    Only the first non-blank line of the header is ever considered. Scanning
    further down would start guessing which of a school, a city, an employer
    and a name a later line holds, and this slice would rather propose nothing.
    """
    for line in body:
        if not line.text:
            continue
        return line if has_name_shape(line.text) else None
    return None


def title_lines(body: Sequence[SourceLine]) -> tuple[SourceLine, ...]:
    """Return the header lines a role keyword appears on, in document order.

    The candidate keeps the whole line as written. The rule states that a role
    word of the dictionary appears on this line — not that the line *is* the
    title — because cutting a title out of "Name — Data Scientist" would be an
    interpretation this slice has no way to justify.
    """
    return tuple(
        line
        for line in body
        if line.text
        and not any(marker in line.text for marker in _CONTACT_MARKERS)
        and holds_title_keyword(line.text)
    )
