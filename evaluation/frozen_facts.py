"""The frozen record facts Phase 10.4 and Phase 10.5 both read. One derivation.

Phase 10.4 measures the cohort's *data* and Phase 10.5 projects the cohort onto
independent membership questions. Two of those questions — "is this posting
explicitly out of the target country?" and "is it explicitly out of the Data/AI
scope?" — are the same reading of the same frozen fields that
`business_metrics/formulas.py` already performs to compute
`TARGET_COUNTRY_MATCH_RATE` and `DATA_AI_RATE`.

So the reading lives **here**, once, and both phases call it. The alternative
was the reason this module exists: Phase 10.5 copying the rules, or reaching
into a private helper of Phase 10.4. Either would produce two implementations
of one semantic, free to drift — and the first drift would be invisible,
because each phase would be internally consistent while disagreeing with the
other about which postings are out of target.

## What this module is

Pure functions over the mappings a frozen Phase 10.1 record is held as. That is
the whole of it:

* **no database** — no connection, no cursor, no path;
* **no metric** — nothing here divides, counts a cohort or produces a rate;
* **no experiment concept** — no projection, no overlap, no run, no fingerprint;
* **no live resolver** — the frozen Phase 7A segments and the frozen
  qualification block, never a re-resolution of a location string or a
  reclassification of a description;
* **no country** — `target_verdict_of` takes the target as an argument. Morocco
  is a property of one profile and of one declared source map, and a country
  named here would make every caller a Morocco caller whether or not the
  profile said so;
* **no policy** — no clock, no randomness, no fallback field. A record that
  states nothing states nothing.

## Why the caller supplies its own refusal

Every function takes a `refuse` factory: the exception type its caller raises
when frozen evidence contradicts itself. That is not indirection for its own
sake. Phase 10.4 raises `BusinessMetricBindingError` and Phase 10.5 raises
`ExperimentBindingError`, each of which its own tests and its own callers
already catch by type; a shared module that raised a third type of its own
would force both phases to either re-wrap it or change what they promise.
Making the refusal an argument keeps this module free of both phases' error
vocabularies while leaving each phase's failures exactly what they were.

What it must never become is a *decision*: `refuse` chooses how to say no, never
whether to. Every rule below fails closed, in the caller's own words.

## The rules, stated once

**A segment's status and its country must agree.** `RESOLVED` named exactly one
country; `AMBIGUOUS` found several places the text could mean; `UNKNOWN` found
no geographic signal at all. A `RESOLVED` segment that names no country, or an
`AMBIGUOUS` or `UNKNOWN` one that names a country, is a hard error: the two
halves of it cannot both be true.

**UNKNOWN is not OUT_OF_TARGET, and AMBIGUOUS is not either.** A posting is out
of target only when segments exist, every one of them resolved, and none of them
to the target. Anything less certain is `UNKNOWN`, which is a value and not an
absence.

**An unread posting is UNCLASSIFIED, never OUT_OF_SCOPE.** `qualification is
None` is Phase 10.1's statement that the Data/AI classifier never read the
posting. That is a fact about how far the pipeline has run, and it is
emphatically not a negative verdict about the posting.

**A vocabulary this build has not heard of is a hard error.** Not an "other"
bucket, and above all not folded into `UNCLASSIFIED` — that would report an
unknown classification as an unrun classifier.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Callable

from evaluation.dataset import EvaluationDatasetError

__all__ = [
    "FROZEN_DATA_AI_STATE_NAMES",
    "FROZEN_SEGMENT_STATUS_NAMES",
    "FrozenFactsError",
    "FrozenDataAiState",
    "FrozenRecordGeography",
    "FrozenSegmentStatus",
    "FrozenTargetVerdict",
    "Refusal",
    "data_ai_state_of",
    "frozen_block_field",
    "frozen_block_sequence",
    "frozen_country_code",
    "frozen_optional_block",
    "frozen_text",
    "record_geography_of",
    "target_verdict_of",
]


class FrozenFactsError(EvaluationDatasetError):
    """Raised when this module itself is misused — never for frozen evidence.

    A contradiction in the records is reported through the caller's own `refuse`
    factory, in the caller's own error vocabulary. This type covers the other
    case: a `refuse` that is not callable, or one that returns something that
    cannot be raised. Reporting that through `refuse` would be circular.
    """


#: The exception factory a caller supplies. Called with a finished message and
#: expected to return the exception to raise; it decides *how* the refusal is
#: spelled, never whether there is one.
Refusal = Callable[[str], BaseException]


def _raise(refuse: Any, message: str) -> None:
    """Raise the caller's refusal, or refuse the caller's refusal."""
    if not callable(refuse):
        raise FrozenFactsError(
            f"{refuse!r} is not a refusal factory; every frozen fact primitive "
            "reports a contradiction in its caller's own error vocabulary and "
            "needs a callable to build one"
        )
    error = refuse(message)
    if not isinstance(error, BaseException):
        raise FrozenFactsError(
            f"the supplied refusal factory returned {error!r}, which cannot be "
            "raised"
        )
    raise error


# --------------------------------------------------------------------------
# the closed vocabularies
# --------------------------------------------------------------------------


class FrozenSegmentStatus(StrEnum):
    """What one frozen Phase 7A geography segment established. Closed."""

    #: The segment named exactly one country.
    RESOLVED = "RESOLVED"

    #: The segment found several places the text could mean. Not a failure to
    #: find one, and never read as "not in the target country".
    AMBIGUOUS = "AMBIGUOUS"

    #: The segment found no geographic signal at all. Likewise not "abroad".
    UNKNOWN = "UNKNOWN"


#: The status vocabulary as plain strings, for membership tests and for the
#: refusal messages. Deliberately not `sorted(FrozenSegmentStatus)`: a list of
#: enum members renders as `<FrozenSegmentStatus.RESOLVED: 'RESOLVED'>` inside
#: an f-string, and a message is read by a person.
FROZEN_SEGMENT_STATUS_NAMES: frozenset[str] = frozenset(
    item.value for item in FrozenSegmentStatus
)


class FrozenDataAiState(StrEnum):
    """How the Data/AI classifier disposed of one posting, or that it did not.

    Five states, and the fifth is why this is an enum rather than the four
    values of the production classifier: `UNCLASSIFIED` is the *absence* of a
    qualification block, which is a fact about the pipeline rather than a
    verdict about the posting.
    """

    CORE_TARGET = "CORE_TARGET"
    ADJACENT_TARGET = "ADJACENT_TARGET"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"
    UNCERTAIN = "UNCERTAIN"

    #: `record["qualification"] is None`: the classifier never read the posting.
    #: Never `OUT_OF_SCOPE`, never `UNCERTAIN`, never an empty object.
    UNCLASSIFIED = "UNCLASSIFIED"


#: The four states the frozen `qualification.qualification` column may state,
#: as plain strings. `UNCLASSIFIED` is not among them: no record states it, it
#: is what the absence of the block means.
FROZEN_DATA_AI_STATE_NAMES: frozenset[str] = frozenset(
    {
        FrozenDataAiState.CORE_TARGET.value,
        FrozenDataAiState.ADJACENT_TARGET.value,
        FrozenDataAiState.OUT_OF_SCOPE.value,
        FrozenDataAiState.UNCERTAIN.value,
    }
)


class FrozenTargetVerdict(StrEnum):
    """Where one posting sits relative to one bound target country.

    `UNKNOWN` is the wide one and `OUT_OF_TARGET` the narrow one, deliberately:
    a posting nobody could place might be in the target country, so calling it
    out of target would read uncertainty as a negative.
    """

    #: At least one segment resolved to the target country.
    MATCH = "MATCH"

    #: Segments exist, every one resolved, and none to the target country.
    OUT_OF_TARGET = "OUT_OF_TARGET"

    #: Everything else — no segment at all, or any segment not resolved.
    UNKNOWN = "UNKNOWN"


# --------------------------------------------------------------------------
# reading the primitives of one frozen record
# --------------------------------------------------------------------------
#
# A caller has already established that a record carries exactly the field
# *names* of the Phase 10.1 contract. That says nothing about what the fields
# hold, so every nested shape is checked here, and a value this reading cannot
# interpret is a hard error rather than a silently skipped record.


_COUNTRY_CODE_PATTERN = re.compile(r"^[A-Z]{2}$")


def frozen_text(value: Any, *, subject: str, refuse: Refusal) -> str:
    """A non-empty, unpadded string. Padding is a difference nobody sees."""
    if not isinstance(value, str) or not value.strip():
        _raise(
            refuse,
            f"{subject} must be a non-empty string, not {value!r} "
            f"({type(value).__name__})",
        )
    if value != value.strip():
        _raise(refuse, f"{subject} must not be padded with whitespace: {value!r}")
    return value


def frozen_country_code(value: Any, *, subject: str, refuse: Refusal) -> str:
    """An ISO-3166-1 alpha-2 code, uppercase. **No country is named here.**"""
    if not isinstance(value, str) or not _COUNTRY_CODE_PATTERN.match(value):
        _raise(
            refuse,
            f"{subject} is not an ISO-3166-1 alpha-2 country code: {value!r}",
        )
    return value


def frozen_optional_block(
    record: Mapping[str, Any], field: str, *, record_subject: str, refuse: Refusal
) -> Mapping[str, Any] | None:
    """One optional nested block of a frozen record, or `None` for its absence.

    `None` is returned, never replaced: each of the Phase 10.1 record's optional
    blocks is absent for its own distinct reason, and an empty object in place of
    one would state something the snapshot does not.
    """
    value = record[field]
    if value is None:
        return None
    if not isinstance(value, Mapping):
        _raise(
            refuse,
            f"{record_subject} states {field}={value!r} "
            f"({type(value).__name__}), which is not the optional block of the "
            "Phase 10.1 record contract",
        )
    return value


def frozen_block_sequence(
    record: Mapping[str, Any], field: str, *, record_subject: str, refuse: Refusal
) -> tuple[Mapping[str, Any], ...]:
    """One repeated nested block of a frozen record, as an immutable tuple."""
    value = record[field]
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        _raise(
            refuse,
            f"{record_subject} states {field}={value!r}, which is not a sequence",
        )
    for index, item in enumerate(value, start=1):
        if not isinstance(item, Mapping):
            _raise(
                refuse,
                f"{record_subject} states {field}[{index}]={item!r}, which is "
                "not a mapping",
            )
    return tuple(value)


def frozen_block_field(
    block: Mapping[str, Any], field: str, *, subject: str, refuse: Refusal
) -> Any:
    """One field of a nested block, refusing a block of another contract."""
    if field not in block:
        _raise(
            refuse,
            f"{subject} states no {field!r}; refusing to read a block of another "
            "contract",
        )
    return block[field]


# --------------------------------------------------------------------------
# geography
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FrozenRecordGeography:
    """One record's frozen geography, already checked for self-coherence.

    Two facts, and every question below is answered from them: which statuses
    the resolver produced, and which countries its RESOLVED segments named.
    """

    #: Empty when the resolver produced no segment at all for this posting.
    statuses: tuple[FrozenSegmentStatus, ...]
    #: The countries its RESOLVED segments named, in segment order.
    resolved_countries: tuple[str, ...]

    @property
    def has_segments(self) -> bool:
        return bool(self.statuses)

    @property
    def has_unknown_segment(self) -> bool:
        return FrozenSegmentStatus.UNKNOWN in self.statuses

    @property
    def has_ambiguous_segment(self) -> bool:
        return FrozenSegmentStatus.AMBIGUOUS in self.statuses

    @property
    def all_resolved(self) -> bool:
        """Segments exist and every one of them resolved to a country."""
        return bool(self.statuses) and all(
            status is FrozenSegmentStatus.RESOLVED for status in self.statuses
        )


def record_geography_of(
    record: Mapping[str, Any], *, record_subject: str, refuse: Refusal
) -> FrozenRecordGeography:
    """Read one record's frozen segments, refusing any that contradicts itself.

    The frozen Phase 7A segments and **nothing else**: no live geography
    resolver, and no fallback to `record["location"]` or `record["country"]`.
    Those fields are the raw text the resolver was given and the column the
    collector wrote; reading a country out of either would be resolving
    geography here, under a caller's name, with no rule and no version.

    The coherence rule is the resolver's own: a RESOLVED segment names a
    country, and an AMBIGUOUS or UNKNOWN one does not.
    """
    statuses: list[FrozenSegmentStatus] = []
    countries: list[str] = []
    segments = frozen_block_sequence(
        record, "geography_segments", record_subject=record_subject, refuse=refuse
    )
    for index, segment in enumerate(segments, start=1):
        subject = f"geography segment {index} of {record_subject}"
        status = frozen_text(
            frozen_block_field(
                segment, "status", subject=subject, refuse=refuse
            ),
            subject=f"the status of {subject}",
            refuse=refuse,
        )
        if status not in FROZEN_SEGMENT_STATUS_NAMES:
            _raise(
                refuse,
                f"the status of {subject} is {status!r}; this contract reads "
                f"{sorted(FROZEN_SEGMENT_STATUS_NAMES)}",
            )
        country = frozen_block_field(
            segment, "country_code", subject=subject, refuse=refuse
        )
        if status == FrozenSegmentStatus.RESOLVED:
            if country is None:
                _raise(
                    refuse,
                    f"{subject} is {status} and names no country; a resolved "
                    "segment resolved to somewhere",
                )
            countries.append(
                frozen_country_code(
                    country, subject=f"the country of {subject}", refuse=refuse
                )
            )
        elif country is not None:
            _raise(
                refuse,
                f"{subject} is {status} and names country {country!r}; only a "
                f"{FrozenSegmentStatus.RESOLVED} segment names one, and a "
                "country stated under either of the other two would be a "
                "placement the resolver did not make",
            )
        statuses.append(FrozenSegmentStatus(status))
    return FrozenRecordGeography(
        statuses=tuple(statuses), resolved_countries=tuple(countries)
    )


def target_verdict_of(
    record: Mapping[str, Any],
    target_country: str,
    *,
    record_subject: str,
    refuse: Refusal,
) -> FrozenTargetVerdict:
    """Where one posting sits relative to one bound target country.

    The production resolver's own rule, applied to the frozen segments:

        MATCH           at least one segment RESOLVED to the target country
        OUT_OF_TARGET   segments exist, every one RESOLVED, none to the target
        UNKNOWN         everything else

    `target_country` is validated as a *shape* and is otherwise whatever the
    caller bound. There is no default and no fallback: a caller with no target
    has no verdict to ask for, which is a different answer from `UNKNOWN` and
    belongs to the caller rather than here.
    """
    country = frozen_country_code(
        target_country, subject="the bound target country", refuse=refuse
    )
    geography = record_geography_of(
        record, record_subject=record_subject, refuse=refuse
    )
    if country in geography.resolved_countries:
        return FrozenTargetVerdict.MATCH
    if geography.all_resolved:
        return FrozenTargetVerdict.OUT_OF_TARGET
    return FrozenTargetVerdict.UNKNOWN


# --------------------------------------------------------------------------
# the Data/AI qualification
# --------------------------------------------------------------------------


def data_ai_state_of(
    record: Mapping[str, Any], *, record_subject: str, refuse: Refusal
) -> FrozenDataAiState:
    """How the Data/AI classifier disposed of one posting, or that it did not.

    `record["qualification"] is None` is `UNCLASSIFIED` — the classifier never
    read the posting. There is no fallback to the title, the description, the
    `primary_domain`, the top-level `opportunity_type` or anything downstream: a
    caller that guessed a classification would be reading its own guess.

    A value outside the four the frozen column may state is a **hard error**,
    never an "other" bucket and never an `UNCLASSIFIED`: folding a vocabulary
    this build has never heard of into the absence of the classifier would
    report an unknown classification as an unrun pipeline.
    """
    qualification = frozen_optional_block(
        record, "qualification", record_subject=record_subject, refuse=refuse
    )
    if qualification is None:
        return FrozenDataAiState.UNCLASSIFIED
    subject = f"the qualification of {record_subject}"
    value = frozen_text(
        frozen_block_field(
            qualification, "qualification", subject=subject, refuse=refuse
        ),
        subject=subject,
        refuse=refuse,
    )
    if value not in FROZEN_DATA_AI_STATE_NAMES:
        _raise(
            refuse,
            f"{subject} is {value!r}, which this contract does not define; the "
            f"values it reads are {sorted(FROZEN_DATA_AI_STATE_NAMES)} — "
            "refusing to project an unknown classification onto a count rather "
            "than report a vocabulary this build cannot interpret",
        )
    return FrozenDataAiState(value)
