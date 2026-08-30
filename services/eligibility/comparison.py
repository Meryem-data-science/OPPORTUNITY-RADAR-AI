"""The two comparisons `eligibility-rules-v1` is willing to make, and their
limits.

A comparison is allowed here only when both sides already speak the same
normalized vocabulary and somebody other than this file put them on the same
scale. Everything else answers "not comparable", which the rules turn into
UNKNOWN — a question — rather than into a refusal.

Two things are compared. Neither invents an equivalence.

**CEFR levels.** `A1 < A2 < B1 < B2 < C1 < C2` is the Common European Framework
itself, not a convention of this project, and both sides of the database already
store the label a document wrote. `0013` keeps a posting's `B2` as `B2`, and
`0010`'s structurer keeps a CV's `B2` as `B2` while explicitly refusing to turn
`courant` into `C1` — its own comment says so. Reading the literal token `B2` as
CEFR B2 is therefore not a mapping; it is the label. `Fluent`, `courant`,
`native`, `bilingue`, `good command`: none of them is a CEFR level, none of them
gets one here, and a requirement stated in those words is answered "not
comparable".

**Education levels.** `0012` stores eight, and says in its own comment that
`BAC_PLUS_5` and `MASTER` "are separate levels on purpose … merging them here
would be an equivalence nobody stated". This file keeps that promise: the
`Bac+N` levels form one ordered ladder because the number *is* the order, the
Anglo-Saxon degrees form another because Bachelor precedes Master precedes PhD,
and **nothing compares across the two**. `ENGINEERING_DEGREE` sits on neither:
a French *diplôme d'ingénieur* is not a rung of either ladder, and placing it on
one would decide a question about two education systems that no row in this
database answers.

The practical consequence is deliberate. A posting demanding `BAC_PLUS_5` and a
person holding a verified `MASTER` produce "not comparable" and therefore
UNKNOWN — not SATISFIED, which would claim an equivalence, and above all not
VIOLATED, which would reject somebody over a difference in vocabulary.
"""

from __future__ import annotations

import re

__all__ = [
    "CEFR_LEVELS",
    "EDUCATION_LADDERS",
    "compare_education_levels",
    "compare_cefr",
    "parse_cefr",
]

#: The scale, ascending. Membership in this tuple is the whole definition of
#: "this side stated a CEFR level".
CEFR_LEVELS: tuple[str, ...] = ("A1", "A2", "B1", "B2", "C1", "C2")

_CEFR_RANK = {level: rank for rank, level in enumerate(CEFR_LEVELS)}

#: A CEFR token, and nothing around it but space and the punctuation a document
#: uses to introduce one. `B2` matches; `B2/C1` does not, because a text naming
#: two levels has not named one. `Anglais B2` does not match either: this is
#: fed the proficiency fragment both phases already isolated, never a sentence,
#: so a match here is the whole stated level rather than a level found inside
#: some other claim.
_CEFR_TOKEN = re.compile(r"^\s*(?P<level>[ABC][12])\s*$", re.IGNORECASE)


def parse_cefr(value: str | None) -> str | None:
    """The CEFR level this text *is*, or None when it is anything else.

    None is the answer for `None`, for the empty string, for `Fluent`, for
    `courant`, for `native`, for `B2/C1` and for `at least B2`. Every one of
    those is a real statement by a real document; none of them is a CEFR level
    this engine may act on, and guessing which one somebody meant is the
    invention this whole phase exists to avoid.
    """
    if value is None:
        return None
    match = _CEFR_TOKEN.match(value)
    if match is None:
        return None
    return match.group("level").upper()


def compare_cefr(candidate: str, required: str) -> bool | None:
    """Whether `candidate` reaches `required`. None when either is not CEFR."""
    candidate_rank = _CEFR_RANK.get(candidate.upper())
    required_rank = _CEFR_RANK.get(required.upper())
    if candidate_rank is None or required_rank is None:
        return None
    return candidate_rank >= required_rank


#: The two ordered ladders, ascending within each. A level absent from every
#: ladder — `ENGINEERING_DEGREE` — is comparable only to itself.
EDUCATION_LADDERS: tuple[tuple[str, ...], ...] = (
    ("BAC_PLUS_2", "BAC_PLUS_3", "BAC_PLUS_4", "BAC_PLUS_5"),
    ("BACHELOR", "MASTER", "PHD"),
)

_LADDER_RANK: dict[str, tuple[int, int]] = {
    level: (ladder_index, rank)
    for ladder_index, ladder in enumerate(EDUCATION_LADDERS)
    for rank, level in enumerate(ladder)
}


def compare_education_levels(
    candidate: str, required: str, *, minimum: bool
) -> bool | None:
    """Whether a held level meets a demanded one. None when they are not
    comparable.

    `minimum=True` reads the demand as a floor ("Bac+3 minimum"), so a higher
    rung of the *same* ladder satisfies it. `minimum=False` reads it as the
    level itself ("Master's degree"), and only the identical level satisfies it
    — a PhD is not automatically a Master, because no row in this database says
    the holder of one holds the other.

    Two levels on different ladders, or either level on no ladder, return None:
    that is the refusal `0012` asked for, and the caller must turn it into
    UNKNOWN rather than into either answer.
    """
    if candidate == required:
        return True
    if not minimum:
        # An exact demand is met by that level and by nothing else. Saying so
        # needs no ladder, and a cross-ladder pair is still not comparable.
        candidate_ladder = _LADDER_RANK.get(candidate)
        required_ladder = _LADDER_RANK.get(required)
        if (
            candidate_ladder is None
            or required_ladder is None
            or candidate_ladder[0] != required_ladder[0]
        ):
            return None
        return False
    candidate_position = _LADDER_RANK.get(candidate)
    required_position = _LADDER_RANK.get(required)
    if candidate_position is None or required_position is None:
        return None
    if candidate_position[0] != required_position[0]:
        return None
    return candidate_position[1] >= required_position[1]
