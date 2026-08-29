"""Deterministic extraction of skill mentions from a recognised SKILLS section.

A `SKILL` candidate means "this mention appears in a section the CV titled with
a skills heading". It does not mean the person has the skill, and it carries no
level of any kind: this module has no notion of beginner, intermediate,
advanced or expert, no proficiency scale and no score, and it never derives one
from a neighbouring word, a years count or the presence of a product name. A
line reading "Python (avancé)" produces one mention whose text is
`Python (avancé)`, unsplit and uninterpreted — a human decides in Phase 3.3
what it means, and Phase 3.4 owns levels and aliases.

Nothing is expanded either: an umbrella mention stays the umbrella mention it
is, and no component technology is added to the list because it usually goes
with it.
"""

from __future__ import annotations

from collections.abc import Iterator

from services.digital_twin.cv.candidates.models import ExtractionRule
from services.digital_twin.cv.candidates.text import trim_fragment

#: Separators a CV uses inside a list of skills. "/" is not one of them, so
#: "CI/CD" and "TCP/IP" survive as written.
SKILL_SEPARATORS = ",;|•"
#: A mention longer than this is a sentence about the person, not a mention.
MAX_SKILL_LENGTH = 60
#: A category label ("Langages", "Outils") is at most this long.
_MAX_LABEL_LENGTH = 40


def _split_label(text: str) -> tuple[str, bool]:
    """Drop a leading "category:" when what follows is demonstrably a list.

    The colon is only read as a label separator when the right-hand side still
    holds a list separator. Without that evidence the line is kept whole, so
    "Python: 5 ans" stays one mention rather than losing "Python" and turning
    "5 ans" into a skill.
    """
    label, separator, rest = text.partition(":")
    if not separator:
        return text, False
    stripped_rest = rest.strip()
    if not stripped_rest or len(label) > _MAX_LABEL_LENGTH:
        return text, False
    if any(character in label for character in SKILL_SEPARATORS):
        return text, False
    if not any(character in stripped_rest for character in SKILL_SEPARATORS):
        return text, False
    return stripped_rest, True


def _is_mention(fragment: str) -> bool:
    return (
        bool(fragment)
        and len(fragment) <= MAX_SKILL_LENGTH
        and any(character.isalnum() for character in fragment)
    )


def skill_mentions(line_text: str) -> Iterator[tuple[ExtractionRule, str]]:
    """Yield the mentions one skills line carries, with the rule that cut it.

    The text of each mention is what the CV wrote, minus the list decoration
    and the spaces around it. Nothing else is removed, added or rewritten.
    """
    listed, had_label = _split_label(trim_fragment(line_text))
    fragments = [
        trim_fragment(fragment)
        for fragment in _split_on_separators(listed)
    ]
    mentions = [fragment for fragment in fragments if _is_mention(fragment)]
    if not mentions:
        return
    if had_label:
        rule_id = ExtractionRule.SKILLS_LABELLED_LIST_LINE
    elif len(mentions) > 1:
        rule_id = ExtractionRule.SKILLS_SEPARATED_LIST_LINE
    else:
        rule_id = ExtractionRule.SKILLS_PLAIN_LINE
    for mention in mentions:
        yield rule_id, mention


def _split_on_separators(text: str) -> list[str]:
    fragments: list[str] = []
    current: list[str] = []
    for character in text:
        if character in SKILL_SEPARATORS:
            fragments.append("".join(current))
            current = []
        else:
            current.append(character)
    fragments.append("".join(current))
    return fragments
