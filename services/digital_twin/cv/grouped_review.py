"""The vocabulary of the grouped CV review: its groups and its command grammar.

Reviewing a real CV one keystroke at a time is safe and unbearable: a document
yielding fifty-nine candidates asks fifty-nine questions, most of them about a
list of skills the reader took in at a glance. This module is the half of the
answer that decides nothing — it says which group a fact belongs to, in which
order the groups are walked, and what a typed command means — so that
`review_cli.py` stays the one place where a decision is taken.

Two refusals shape it.

**A group is read off the canonical type, never off the value.** The mapping
below is keyed by `ProfileFactType` and by nothing else; no line of this module
looks inside `value` or `normalized_value`. A group is a way of laying facts out
for a human, and if it were derived from the text it would quietly become a
second, unreviewed classifier — the exact thing the rest of this package
refuses. A type nobody has grouped lands in `OTHER`, where it is still shown
and still asked about, because disappearing is the one thing a fact must never
do.

**A command means what it says or it means nothing.** `parse_group_command`
returns `None` for anything it does not recognise, and the caller changes
nothing when it does. There is no tidying up, no closest match and no default,
so no typo can decide anything — and in particular nothing malformed can ever
resolve to an acceptance.

The largest scope any command here can name is one group, which the reader has
already been shown. There is no command for the whole CV, and adding one would
contradict the only invariant this review has: nothing becomes `ACCEPTED`
without an explicit human action.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from services.digital_twin.cv.fact_bridge import ImportedCandidate
from services.digital_twin.facts.models import ProfileFactType


class FactGroup(StrEnum):
    """The sections a review is laid out in, in the order it walks them.

    They are named for what a reader recognises on a CV, not for a fact type:
    one group can gather several types — identity and contacts do — and one
    type never belongs to two groups.
    """

    IDENTITY_CONTACT = "IDENTITY / CONTACT"
    EDUCATION = "EDUCATION"
    EXPERIENCE = "EXPERIENCE"
    PROJECTS = "PROJECTS"
    SKILLS = "SKILLS"
    CERTIFICATIONS = "CERTIFICATIONS"
    LANGUAGES = "LANGUAGES"
    #: Everything nobody has grouped yet, including a fact type added later.
    OTHER = "OTHER"


#: The order the groups are shown in, and the only one. It is written out
#: rather than taken from the enum's declaration order so that reordering the
#: enum for any other reason cannot silently reorder a review.
GROUP_ORDER: tuple[FactGroup, ...] = (
    FactGroup.IDENTITY_CONTACT,
    FactGroup.EDUCATION,
    FactGroup.EXPERIENCE,
    FactGroup.PROJECTS,
    FactGroup.SKILLS,
    FactGroup.CERTIFICATIONS,
    FactGroup.LANGUAGES,
    FactGroup.OTHER,
)

#: Which group each canonical fact type is shown in. Keyed by the type's own
#: value, because `ProfileFact.fact_type` is stored as text and a fact written
#: by a later version of this project may carry a type this one does not know.
#:
#: `PREFERENCE`, `AVAILABILITY`, `MOBILITY` and `CAREER_OBJECTIVE` are mapped to
#: `OTHER` although no CV rule proposes one today: they exist in the taxonomy,
#: and a fact of one of those types reaching this review must be shown rather
#: than dropped.
FACT_TYPE_TO_GROUP: dict[str, FactGroup] = {
    ProfileFactType.NAME.value: FactGroup.IDENTITY_CONTACT,
    ProfileFactType.PROFESSIONAL_TITLE.value: FactGroup.IDENTITY_CONTACT,
    ProfileFactType.EMAIL.value: FactGroup.IDENTITY_CONTACT,
    ProfileFactType.PHONE.value: FactGroup.IDENTITY_CONTACT,
    ProfileFactType.LINKEDIN_URL.value: FactGroup.IDENTITY_CONTACT,
    ProfileFactType.GITHUB_URL.value: FactGroup.IDENTITY_CONTACT,
    ProfileFactType.PORTFOLIO_URL.value: FactGroup.IDENTITY_CONTACT,
    ProfileFactType.PROFESSIONAL_URL.value: FactGroup.IDENTITY_CONTACT,
    ProfileFactType.EDUCATION.value: FactGroup.EDUCATION,
    ProfileFactType.EXPERIENCE.value: FactGroup.EXPERIENCE,
    ProfileFactType.PROJECT.value: FactGroup.PROJECTS,
    ProfileFactType.SKILL.value: FactGroup.SKILLS,
    ProfileFactType.CERTIFICATION.value: FactGroup.CERTIFICATIONS,
    ProfileFactType.LANGUAGE.value: FactGroup.LANGUAGES,
    ProfileFactType.PREFERENCE.value: FactGroup.OTHER,
    ProfileFactType.AVAILABILITY.value: FactGroup.OTHER,
    ProfileFactType.MOBILITY.value: FactGroup.OTHER,
    ProfileFactType.CAREER_OBJECTIVE.value: FactGroup.OTHER,
}


class GroupAction(StrEnum):
    """What a well-formed command asks for. Nothing else is an action."""

    ACCEPT = "a"
    REJECT = "r"
    CORRECT = "c"
    SKIP = "s"
    QUIT = "q"
    HELP = "?"


#: The actions that name facts. `a` may be typed alone — it then means the ones
#: still undecided in the group on screen — and `r` may not: rejecting a whole
#: group on one letter is close enough to a mistyped `a` to be worth refusing.
ACTIONS_TAKING_INDICES = frozenset(
    {GroupAction.ACCEPT, GroupAction.REJECT, GroupAction.CORRECT}
)

GROUP_ACTION_PROMPT = (
    "[a]ccept [r]eject <n> [c]orrect <n> [s]kip [q]uit, "
    "indices like 1,3-5 (? for help): "
)

GROUP_HELP_TEXT = """\
  a           accept every fact of THIS group that is still shown as PROPOSED
  a 1,3-5     accept only these facts of this group
  r 2,4       reject only these facts of this group (indices are required)
  c 3         correct exactly one fact of this group
  s           skip: leave the rest of this group PROPOSED and move on
  q           quit: everything still PROPOSED stays PROPOSED
  ?           show this help

  `a` never reaches beyond the group printed above it: it decides nothing that
  was not just shown, nothing from another group, and nothing already decided.
  There is no command that accepts the whole CV."""

INVALID_COMMAND_NOTICE = "not a command; nothing was decided (type ? for the list)"
REJECT_NEEDS_INDICES_NOTICE = (
    "rejecting needs indices, as in `r 2,4`; nothing was decided"
)
CORRECT_NEEDS_ONE_INDEX_NOTICE = (
    "correcting takes exactly one index, as in `c 3`; nothing was decided"
)
UNKNOWN_INDEX_NOTICE = (
    "no such undecided fact in this group; nothing was decided"
)
NOTHING_PENDING_NOTICE = "nothing is still proposed in this group"

__all__ = [
    "ACTIONS_TAKING_INDICES",
    "CORRECT_NEEDS_ONE_INDEX_NOTICE",
    "FACT_TYPE_TO_GROUP",
    "GROUP_ACTION_PROMPT",
    "GROUP_HELP_TEXT",
    "GROUP_ORDER",
    "FactGroup",
    "GroupAction",
    "GroupCommand",
    "INVALID_COMMAND_NOTICE",
    "NOTHING_PENDING_NOTICE",
    "REJECT_NEEDS_INDICES_NOTICE",
    "ReviewGroup",
    "UNKNOWN_INDEX_NOTICE",
    "build_review_groups",
    "group_for_fact_type",
    "parse_group_command",
    "selection_is_reviewable",
]


def group_for_fact_type(fact_type: str) -> FactGroup:
    """Which group shows this canonical type. An unknown one is `OTHER`.

    Unknown here means a fact type this version has no opinion about — one
    written by a later version of the project, say. It is grouped rather than
    dropped, because a fact that vanishes from a review is a fact nobody can
    accept or refuse.
    """
    return FACT_TYPE_TO_GROUP.get(fact_type, FactGroup.OTHER)


@dataclass(frozen=True)
class ReviewGroup:
    """One group of undecided facts, in the order the document produced them."""

    group: FactGroup
    entries: tuple[ImportedCandidate, ...]

    @property
    def label(self) -> str:
        return self.group.value

    def __len__(self) -> int:
        return len(self.entries)


def build_review_groups(
    entries: Iterable[ImportedCandidate],
) -> tuple[ReviewGroup, ...]:
    """Lay these entries out in groups, deterministically and losslessly.

    The groups come back in `GROUP_ORDER`, empty ones omitted, and inside a
    group the entries keep the order they arrived in — which is the extraction's
    own document order, so a reader walks the CV the way it was written. Nothing
    is sorted by value: an alphabetical list of skills would lose the provenance
    order the rest of the review is read against.

    Every entry given comes back in exactly one group.
    """
    collected: dict[FactGroup, list[ImportedCandidate]] = {}
    for entry in entries:
        collected.setdefault(group_for_fact_type(entry.fact.fact_type), []).append(
            entry
        )
    return tuple(
        ReviewGroup(group=group, entries=tuple(collected[group]))
        for group in GROUP_ORDER
        if collected.get(group)
    )


@dataclass(frozen=True)
class GroupCommand:
    """One well-formed command: what to do, and to which displayed indices.

    `indices` is empty for `a` typed alone, which means "the ones still
    proposed in the group on screen", and for the actions that name nothing.
    When it is not empty it is sorted and free of duplicates, so `a 1,1,2` and
    `a 2,1` are the same command and neither can decide a fact twice.
    """

    action: GroupAction
    indices: tuple[int, ...] = ()


def _parse_index(text: str) -> int | None:
    """One decimal index, written the one way. `01`, `+1` and `` are not one."""
    if text == "" or not text.isascii() or not text.isdigit():
        return None
    if len(text) > 1 and text.startswith("0"):
        return None
    return int(text)


def _parse_selection(text: str) -> tuple[int, ...] | None:
    """`1,3-5,9` as ascending unique indices, or None if it is not that.

    A range is inclusive and must be written low to high: `5-1` is refused
    rather than read backwards, because guessing which end the person meant is
    guessing. An index of `0` parses here and is refused later by the group,
    which is the only place that knows what is on screen.
    """
    selected: set[int] = set()
    for part in text.split(","):
        bounds = part.split("-")
        if len(bounds) == 1:
            index = _parse_index(bounds[0])
            if index is None:
                return None
            selected.add(index)
            continue
        if len(bounds) != 2:
            return None
        first = _parse_index(bounds[0])
        last = _parse_index(bounds[1])
        if first is None or last is None or first > last:
            return None
        selected.update(range(first, last + 1))
    if not selected:
        return None
    return tuple(sorted(selected))


def parse_group_command(text: str) -> GroupCommand | None:
    """Read one typed line. `None` means "not a command", and decides nothing.

    The line is matched exactly: one action letter, optionally one space and one
    selection, and nothing else. A capital, a trailing space or a second space
    is not a command — the same strictness the one-by-one review applies to its
    five answers, kept here because `a` now stands for more than one fact.
    """
    if text != text.strip() or text == "":
        return None
    head, separator, tail = text.partition(" ")
    try:
        action = GroupAction(head)
    except ValueError:
        return None
    if separator == "":
        if action is GroupAction.REJECT or action is GroupAction.CORRECT:
            # Both name facts, and neither has a meaning without them.
            return None
        return GroupCommand(action=action)
    if action not in ACTIONS_TAKING_INDICES:
        return None
    indices = _parse_selection(tail)
    if indices is None:
        return None
    if action is GroupAction.CORRECT and len(indices) != 1:
        return None
    return GroupCommand(action=action, indices=indices)


def selection_is_reviewable(
    indices: Sequence[int], pending_indices: Sequence[int]
) -> bool:
    """True when every index named is one of the group's still-undecided ones.

    An index out of range and an index whose fact somebody already decided are
    refused the same way, and refuse the whole command with them: deciding the
    subset that happens to be valid would act on a selection nobody typed.
    """
    return bool(indices) and set(indices).issubset(set(pending_indices))
