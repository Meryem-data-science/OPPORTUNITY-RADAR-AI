"""Phase 3.3C: lining a newer CV extraction up against an older one's facts.

A CV is read by a *campaign*: one document digest, one parser version, one
extractor version. When the parser or the extractor is fixed, the same PDF is
read again and most of what the new campaign produces is, word for word, what
the old one already produced and a human already decided about. Importing the
new campaign the ordinary way would propose all of it again, because
`ensure_profile_fact_proposal` keys idempotence on the *evidence*, and the
evidence carries the versions: a new campaign is a new proof by construction.
That is the right answer for an import and the wrong one for a re-read.

This module is the re-read. It compares the two campaigns and splits the new
one in three:

* **unchanged** — the new campaign read exactly the same `fact_type` and
  exactly the same text as a fact the old campaign already produced. That is
  the same reading, so no fact is created: the new campaign's provenance is
  attached to the fact that already exists, and the human decision on it —
  `ACCEPTED`, `CORRECTED`, whatever it is — is left exactly as it stands;
* **changed** — the new campaign read something the old campaign did not
  produce under that type: the same text under another type, or a block cut on
  different boundaries. That is a *new reading of the person's CV* and it goes
  through the ordinary bridge as a `PROPOSED` fact, for a human to decide;
* **superseded** — an old fact the new campaign no longer produces under its
  type. Nothing happens to it during `prepare`, and `finalize` may later mark
  it `REJECTED`, never delete it.

Identity, and what it deliberately is not
-----------------------------------------

Two readings are the same reading when, and only when, all of this holds:

    same profile, CV provenance, same `cv_sha256`, the old campaign's
    `parser_version` and `extractor_version`, the same `fact_type`, and a
    `value` **byte for byte equal** to the candidate's `raw_text`.

There is no normalization, no case folding, no whitespace collapsing, no
substring rule, no edit distance, no similarity, no fingerprint comparison, no
embedding and no model anywhere in this module. A text that differs by one
character is a different reading and becomes a proposal a human answers — which
is the whole safety property: the failure mode worth engineering against is a
changed reading slipping in under an old acceptance, and only exact equality
rules that out. Where the old campaign holds more than one fact for one
reading, this module raises rather than choosing: picking one would silently
decide which of two accepted readings counts.

Status takes no part in identity. A fact the person already corrected is still
the same reading, and the correction stays: `prepare` attaches evidence and
never re-opens a decision, `finalize` never touches a terminal `CORRECTED` fact
and never rewrites a `value`.

What it never does
------------------

It accepts nothing. There is no threshold, no confidence, no score, no
auto-accept and no argument that would enable one: every changed reading
reaches a human through the ordinary review, and `finalize` refuses to run at
all until each of them has been answered. It deletes nothing — no fact, no
provenance row — so both segmentations of the document stay auditable side by
side. It writes to `profile_facts` and `profile_fact_provenance` and to nothing
else: no skill, no opportunity, no migration and no structured
experience/project table is read or written here.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Callable

from services.digital_twin.cv.candidates.models import (
    CANDIDATE_EXTRACTOR_VERSION,
    ExtractedCandidate,
    StructuredCvExtraction,
)
from services.digital_twin.cv.fact_bridge import (
    fact_type_for_candidate_type,
    provenance_for_candidate,
)
from services.digital_twin.cv.models import PARSER_VERSION
from services.digital_twin.facts.models import FactStatus, ProfileFact
from services.digital_twin.facts.repository import (
    ensure_profile_fact_proposal,
    ensure_profile_fact_provenance,
    get_profile_fact,
    list_profile_facts_by_cv_evidence,
    list_profile_facts_by_evidence,
    reject_profile_fact,
)

#: The campaign this project is reconciling *away from*. They are the documented
#: defaults of the CLI and nothing more: both operations take the old versions
#: as arguments, and neither reads these constants. Version strings are
#: technical identifiers — no CV content and no personal data is hardcoded
#: anywhere in this module.
#:
#: They move with `PARSER_VERSION` and `CANDIDATE_EXTRACTOR_VERSION`, one step
#: behind: the default reconciliation is always the one from the campaign a
#: profile most likely already holds facts from to the campaign this checkout
#: produces. An operator reconciling from an older campaign than that names it
#: explicitly, which is the only thing that changes here — nothing about *how*
#: two campaigns are compared depends on which versions they are, and no fact
#: is re-decided by a bump: a reading the new campaign produces identically is
#: still the same reading and keeps the human decision already made on it,
#: while a reading that changed is still a `PROPOSED` fact a human answers.
PREVIOUS_PARSER_VERSION = "cv-parser-v2"
PREVIOUS_EXTRACTOR_VERSION = "cv-candidates-v2"

__all__ = [
    "ALREADY_REJECTED",
    "AMBIGUOUS_REASON",
    "MISSING_REASON",
    "PREVIOUS_EXTRACTOR_VERSION",
    "PREVIOUS_PARSER_VERSION",
    "REFUSED_REASON",
    "REPLACEMENT_NOT_ACCEPTED_REASON",
    "TERMINAL_CORRECTED",
    "UNDECIDED",
    "UNDECIDED_REASON",
    "AmbiguousHistoricalReadingError",
    "AttachedProvenance",
    "ChangedReading",
    "CvReconciliationError",
    "CvReconciliationFinalization",
    "CvReconciliationPlan",
    "CvReconciliationPreparation",
    "CvReconciliationVersionError",
    "ProposedReading",
    "ResolvedChangedReading",
    "SupersededOutcome",
    "SupersededReading",
    "UnchangedReading",
    "UnresolvedChangedReadingError",
    "compute_cv_reconciliation_plan",
    "extraction_is_current",
    "finalize_cv_fact_reconciliation",
    "prepare_cv_fact_reconciliation",
]


class CvReconciliationError(RuntimeError):
    """Raised when two extraction campaigns cannot be reconciled safely."""


class CvReconciliationVersionError(CvReconciliationError):
    """Raised when the two campaigns named are not two different campaigns.

    Reconciling a campaign with itself would report every candidate as
    unchanged and every fact as reused, which is true and useless, and it would
    hide a mistyped version behind a clean-looking report.
    """


class AmbiguousHistoricalReadingError(CvReconciliationError):
    """Raised when the old campaign holds several facts for one exact reading.

    Same profile, same document, same campaign, same `fact_type`, same `value`:
    two rows that should have been one. Attaching the new evidence to either of
    them would decide which of two human decisions counts, so this module says
    so instead, before writing anything.
    """


class UnresolvedChangedReadingError(CvReconciliationError):
    """Raised when `finalize` is asked to run over an unanswered new reading.

    Finalizing means declaring the old segmentation of the document no longer
    true. That is only ever justified by the new one having been confirmed, so
    a changed reading still `PROPOSED`, one that was `REJECTED`, one whose fact
    cannot be found and one whose evidence justifies several facts each stop
    the whole operation — before a single old fact is rejected.

    `unresolved` carries the privacy-safe reason for each of them: canonical
    types, statuses and ids, never a value.
    """

    def __init__(self, message: str, unresolved: tuple[dict[str, object], ...]) -> None:
        super().__init__(message)
        self.unresolved = unresolved


#: Why one changed reading is not usable as ground truth yet. Each value names
#: a state a human, not this module, has to move.
MISSING_REASON = "MISSING"
AMBIGUOUS_REASON = "AMBIGUOUS"
UNDECIDED_REASON = "PROPOSED"
REFUSED_REASON = "REJECTED"
REPLACEMENT_NOT_ACCEPTED_REASON = "CORRECTED_REPLACEMENT_NOT_ACCEPTED"


def _reading_key(fact_type: str, value: str) -> tuple[str, str]:
    """The identity of one reading: a canonical type and the text, verbatim.

    Both halves are used exactly as they are. Trimming, folding or collapsing
    the text here would make two different readings compare equal, and the fact
    on the other side of that comparison may already be accepted.
    """
    return (fact_type, value)


def _pages(candidate: ExtractedCandidate) -> list[int]:
    return list(candidate.page_numbers)


def _section(candidate: ExtractedCandidate) -> str | None:
    return None if candidate.section_type is None else candidate.section_type.value


@dataclass(frozen=True)
class UnchangedReading:
    """One new candidate that reads exactly what an old fact already says."""

    candidate: ExtractedCandidate
    #: The old campaign's fact, as it stands. Its status is carried, never
    #: acted on: this module reads a decision, it does not revisit one.
    fact: ProfileFact

    @property
    def fact_type(self) -> str:
        return self.fact.fact_type

    def summary(self) -> dict[str, object]:
        """Canonical types, ids, a status and a place in the document. No value."""
        return {
            "candidate_type": self.candidate.candidate_type.value,
            "fact_type": self.fact.fact_type,
            "fact_id": self.fact.id,
            "status": self.fact.status.value,
            "rule_id": self.candidate.rule_id.value,
            "page_numbers": _pages(self.candidate),
            "section_type": _section(self.candidate),
            "section_index": self.candidate.section_index,
        }


@dataclass(frozen=True)
class ChangedReading:
    """One new candidate the old campaign did not produce under that type."""

    candidate: ExtractedCandidate
    #: What it proposes now. It may be a type the old campaign used for the
    #: same text, and that is precisely the case worth a human's attention.
    fact_type: str

    def summary(self) -> dict[str, object]:
        return {
            "candidate_type": self.candidate.candidate_type.value,
            "fact_type": self.fact_type,
            "rule_id": self.candidate.rule_id.value,
            "page_numbers": _pages(self.candidate),
            "section_type": _section(self.candidate),
            "section_index": self.candidate.section_index,
        }


@dataclass(frozen=True)
class SupersededReading:
    """One old fact the new campaign no longer reads under its own type.

    "Superseded" is a statement about the *reading*, not about the person: the
    text may well still be in the CV, cut differently or classified as
    something else. Nothing is done to it here, and `finalize` only ever moves
    it from `ACCEPTED` to `REJECTED`.
    """

    fact: ProfileFact

    def summary(self) -> dict[str, object]:
        return {
            "fact_type": self.fact.fact_type,
            "fact_id": self.fact.id,
            "status": self.fact.status.value,
        }


def _tally(labels: tuple[str, ...]) -> dict[str, int]:
    """How many of each label, by ascending label. Labels are canonical names.

    Only ever fed canonical vocabulary — fact types, statuses, refusal reasons
    — so a tally is safe to print and to log wherever it appears.
    """
    counts: dict[str, int] = {}
    for label in labels:
        counts[label] = counts.get(label, 0) + 1
    return {key: counts[key] for key in sorted(counts)}


@dataclass(frozen=True)
class CvReconciliationPlan:
    """What comparing the two campaigns says, before anything is written.

    Computing a plan is a pure read. `prepare` and `finalize` each recompute it
    from the same three inputs — the profile, the document with its old
    campaign, and the new extraction — so the two operations cannot drift apart
    and neither depends on a plan the other one stored.
    """

    profile_id: int
    cv_sha256: str
    old_parser_version: str
    old_extractor_version: str
    new_parser_version: str
    new_extractor_version: str
    #: How many facts the old campaign holds for this document, whatever their
    #: status. `unchanged + superseded` accounts for all of them.
    historical_facts: int
    unchanged: tuple[UnchangedReading, ...]
    changed: tuple[ChangedReading, ...]
    superseded: tuple[SupersededReading, ...]

    @property
    def new_candidates(self) -> int:
        return len(self.unchanged) + len(self.changed)

    def changed_by_fact_type(self) -> dict[str, int]:
        return _tally(tuple(entry.fact_type for entry in self.changed))

    def unchanged_by_fact_type(self) -> dict[str, int]:
        return _tally(tuple(entry.fact_type for entry in self.unchanged))

    def superseded_by_fact_type(self) -> dict[str, int]:
        return _tally(
            tuple(entry.fact.fact_type for entry in self.superseded)
        )

    def summary(self) -> dict[str, object]:
        """Counters, versions, canonical types and a digest. Never a value."""
        return {
            "profile_id": self.profile_id,
            "sha256": self.cv_sha256,
            "old_parser_version": self.old_parser_version,
            "old_extractor_version": self.old_extractor_version,
            "new_parser_version": self.new_parser_version,
            "new_extractor_version": self.new_extractor_version,
            "historical_facts": self.historical_facts,
            "new_candidates": self.new_candidates,
            "unchanged_candidates": len(self.unchanged),
            "changed_candidates": len(self.changed),
            "superseded_old_facts": len(self.superseded),
            "unchanged_by_fact_type": self.unchanged_by_fact_type(),
            "changed_by_fact_type": self.changed_by_fact_type(),
            "superseded_by_fact_type": self.superseded_by_fact_type(),
        }


def _require_text(value: str, label: str) -> str:
    if not isinstance(value, str) or value.strip() == "" or value != value.strip():
        raise CvReconciliationVersionError(f"{label} is empty or untrimmed")
    return value


def _require_two_campaigns(
    extraction: StructuredCvExtraction,
    old_parser_version: str,
    old_extractor_version: str,
) -> None:
    """Refuse to reconcile a campaign with itself, or with a blank one."""
    if not isinstance(extraction, StructuredCvExtraction):
        raise CvReconciliationError("extraction must be a StructuredCvExtraction")
    _require_text(old_parser_version, "old_parser_version")
    _require_text(old_extractor_version, "old_extractor_version")
    if (old_parser_version, old_extractor_version) == (
        extraction.parser_version,
        extraction.extractor_version,
    ):
        raise CvReconciliationVersionError(
            "the old campaign and the extraction name the same parser and "
            "extractor versions; there is nothing to reconcile"
        )


def compute_cv_reconciliation_plan(
    connection: sqlite3.Connection,
    *,
    profile_id: int,
    extraction: StructuredCvExtraction,
    old_parser_version: str,
    old_extractor_version: str,
) -> CvReconciliationPlan:
    """Compare the two campaigns. Reads rows, writes none, decides nothing.

    Every candidate of the new extraction is looked up by its exact
    `(fact_type, raw_text)` among the facts the old campaign produced for this
    document. A hit is an unchanged reading, a miss is a changed one, and an
    old fact no candidate hit is superseded. Several hits is
    `AmbiguousHistoricalReadingError` — raised here, during the read, so a
    caller that goes on to write has already been stopped.
    """
    _require_two_campaigns(extraction, old_parser_version, old_extractor_version)
    historical = list_profile_facts_by_cv_evidence(
        connection,
        profile_id,
        cv_sha256=extraction.cv_sha256,
        parser_version=old_parser_version,
        extractor_version=old_extractor_version,
    )
    by_reading: dict[tuple[str, str], list[ProfileFact]] = {}
    for fact in historical:
        by_reading.setdefault(_reading_key(fact.fact_type, fact.value), []).append(fact)

    unchanged: list[UnchangedReading] = []
    changed: list[ChangedReading] = []
    matched: set[tuple[str, str]] = set()
    for candidate in extraction.candidates:
        fact_type = fact_type_for_candidate_type(candidate.candidate_type).value
        key = _reading_key(fact_type, candidate.raw_text)
        found = by_reading.get(key, ())
        if len(found) > 1:
            # The message names the type and the count; the reading itself is
            # CV content and stays out of it.
            raise AmbiguousHistoricalReadingError(
                f"{len(found)} facts of profile {profile_id} hold the same "
                f"{fact_type} reading of this document under "
                f"{old_parser_version}/{old_extractor_version}"
            )
        if found:
            matched.add(key)
            unchanged.append(UnchangedReading(candidate=candidate, fact=found[0]))
        else:
            changed.append(ChangedReading(candidate=candidate, fact_type=fact_type))

    superseded = tuple(
        SupersededReading(fact=fact)
        for fact in historical
        if _reading_key(fact.fact_type, fact.value) not in matched
    )
    return CvReconciliationPlan(
        profile_id=profile_id,
        cv_sha256=extraction.cv_sha256,
        old_parser_version=old_parser_version,
        old_extractor_version=old_extractor_version,
        new_parser_version=extraction.parser_version,
        new_extractor_version=extraction.extractor_version,
        historical_facts=len(historical),
        unchanged=tuple(unchanged),
        changed=tuple(changed),
        superseded=superseded,
    )


@dataclass(frozen=True)
class AttachedProvenance:
    """One unchanged reading, and what attaching its new evidence did."""

    reading: UnchangedReading
    #: True only when this run wrote the provenance row. False means the new
    #: campaign's proof was already recorded on that fact.
    created: bool

    def summary(self) -> dict[str, object]:
        return {**self.reading.summary(), "provenance_created": self.created}


@dataclass(frozen=True)
class ProposedReading:
    """One changed reading, and the fact the ordinary bridge gave it."""

    reading: ChangedReading
    fact: ProfileFact
    #: True only when this run created the fact.
    created: bool

    @property
    def needs_review(self) -> bool:
        """True while nobody has decided. Nothing here ever decides for them."""
        return self.fact.status is FactStatus.PROPOSED

    def summary(self) -> dict[str, object]:
        return {
            **self.reading.summary(),
            "fact_id": self.fact.id,
            "status": self.fact.status.value,
            "created": self.created,
        }


@dataclass(frozen=True)
class CvReconciliationPreparation:
    """What one `prepare` did: evidence attached, and proposals raised."""

    plan: CvReconciliationPlan
    attached: tuple[AttachedProvenance, ...]
    proposed: tuple[ProposedReading, ...]

    @property
    def provenance_attached(self) -> int:
        return sum(1 for entry in self.attached if entry.created)

    @property
    def provenance_already_present(self) -> int:
        return sum(1 for entry in self.attached if not entry.created)

    @property
    def newly_proposed(self) -> int:
        return sum(1 for entry in self.proposed if entry.created)

    @property
    def already_proposed(self) -> int:
        return sum(1 for entry in self.proposed if not entry.created)

    @property
    def pending_review(self) -> tuple[ProposedReading, ...]:
        """The changed readings a human still has to answer, in document order."""
        return tuple(entry for entry in self.proposed if entry.needs_review)

    def summary(self) -> dict[str, object]:
        """The whole report as counters. No value, no `raw_text`, no name.

        Safe to print and to log: every entry is a counter, a canonical type, a
        version or the document digest.
        """
        return {
            **self.plan.summary(),
            "provenance_attached": self.provenance_attached,
            "provenance_already_present": self.provenance_already_present,
            "newly_proposed": self.newly_proposed,
            "already_proposed": self.already_proposed,
            "pending_review": len(self.pending_review),
        }


def prepare_cv_fact_reconciliation(
    connection: sqlite3.Connection,
    *,
    profile_id: int,
    extraction: StructuredCvExtraction,
    old_parser_version: str,
    old_extractor_version: str,
) -> CvReconciliationPreparation:
    """Attach the new campaign to what it re-reads; propose what it reads anew.

    For every unchanged reading, the new campaign's provenance is attached to
    the fact that already exists — `ensure_profile_fact_provenance`, so a
    second `prepare` writes nothing and raises nothing. The fact keeps its id,
    its `value`, its `normalized_value` and its status: an `ACCEPTED` reading
    stays accepted, a `CORRECTED` one stays corrected and its `USER_INPUT`
    replacement is never touched. No fact is created for it, so re-reading a CV
    cannot double the profile.

    For every changed reading, the ordinary Phase 3.3B path runs unchanged:
    `ensure_profile_fact_proposal` with the candidate's own provenance, which
    creates a `PROPOSED` fact or returns the one this exact proof already
    justifies. Nothing is accepted, nothing is corrected, and no old fact is
    rejected here — `prepare` never ends a reading's life, it only opens the
    question.

    Each candidate is handled in its own transaction, so an interrupted run
    leaves every candidate it did handle complete, with its evidence, and
    re-running resumes without duplicating any of them.
    """
    plan = compute_cv_reconciliation_plan(
        connection,
        profile_id=profile_id,
        extraction=extraction,
        old_parser_version=old_parser_version,
        old_extractor_version=old_extractor_version,
    )
    attached: list[AttachedProvenance] = []
    for reading in plan.unchanged:
        attachment = ensure_profile_fact_provenance(
            connection,
            profile_id=profile_id,
            fact_id=reading.fact.id,
            provenance=provenance_for_candidate(reading.candidate),
        )
        attached.append(
            AttachedProvenance(reading=reading, created=attachment.created)
        )

    proposed: list[ProposedReading] = []
    for reading in plan.changed:
        candidate = reading.candidate
        proposal = ensure_profile_fact_proposal(
            connection,
            profile_id=profile_id,
            fact_type=reading.fact_type,
            # Verbatim, both of them, exactly as the bridge carries them: a
            # human validating this later must read what the CV said.
            value=candidate.raw_text,
            normalized_value=candidate.normalized_value,
            provenance=provenance_for_candidate(candidate),
        )
        proposed.append(
            ProposedReading(
                reading=reading, fact=proposal.fact, created=proposal.created
            )
        )
    return CvReconciliationPreparation(
        plan=plan, attached=tuple(attached), proposed=tuple(proposed)
    )


@dataclass(frozen=True)
class ResolvedChangedReading:
    """One changed reading a human has answered, and what they answered with."""

    reading: ChangedReading
    #: The fact the new campaign's evidence justifies.
    fact: ProfileFact
    #: The `ACCEPTED` fact that carries the truth: `fact` itself when it was
    #: accepted, its replacement when the person corrected it instead.
    truth: ProfileFact

    def summary(self) -> dict[str, object]:
        return {
            **self.reading.summary(),
            "fact_id": self.fact.id,
            "status": self.fact.status.value,
            "truth_fact_id": self.truth.id,
        }


@dataclass(frozen=True)
class SupersededOutcome:
    """One old fact, and what `finalize` did — or refused to do — to it."""

    #: The fact as it stood when the run read it.
    fact: ProfileFact
    #: True only when this run moved it from `ACCEPTED` to `REJECTED`.
    rejected: bool
    #: Why it was left alone, when it was. `None` when this run rejected it.
    #: `ALREADY_REJECTED` on a second run, `TERMINAL_CORRECTED` for a reading
    #: the person replaced themselves, `UNDECIDED` for one nobody ever answered
    #: — rejecting that last one would be this module taking a decision.
    left_alone: str | None

    def summary(self) -> dict[str, object]:
        return {
            "fact_type": self.fact.fact_type,
            "fact_id": self.fact.id,
            "status": self.fact.status.value,
            "rejected": self.rejected,
            "left_alone": self.left_alone,
        }


ALREADY_REJECTED = "ALREADY_REJECTED"
TERMINAL_CORRECTED = "TERMINAL_CORRECTED"
UNDECIDED = "UNDECIDED"


@dataclass(frozen=True)
class CvReconciliationFinalization:
    """What one `finalize` did: which old readings stopped being active truth."""

    plan: CvReconciliationPlan
    resolved: tuple[ResolvedChangedReading, ...]
    outcomes: tuple[SupersededOutcome, ...]

    @property
    def newly_rejected(self) -> int:
        return sum(1 for entry in self.outcomes if entry.rejected)

    @property
    def already_rejected(self) -> int:
        return sum(1 for entry in self.outcomes if entry.left_alone == ALREADY_REJECTED)

    @property
    def left_terminal_corrected(self) -> int:
        return sum(
            1 for entry in self.outcomes if entry.left_alone == TERMINAL_CORRECTED
        )

    @property
    def left_undecided(self) -> int:
        return sum(1 for entry in self.outcomes if entry.left_alone == UNDECIDED)

    @property
    def changed_anything(self) -> bool:
        """False on a re-run, which is how idempotence is read off the result."""
        return bool(self.newly_rejected)

    def summary(self) -> dict[str, object]:
        """Counters, versions and canonical types. Never a value."""
        return {
            **self.plan.summary(),
            "resolved_changed": len(self.resolved),
            "newly_rejected": self.newly_rejected,
            "already_rejected": self.already_rejected,
            "left_terminal_corrected": self.left_terminal_corrected,
            "left_undecided": self.left_undecided,
            "changed_anything": self.changed_anything,
        }


def _resolve_changed_reading(
    connection: sqlite3.Connection, profile_id: int, reading: ChangedReading
) -> tuple[ResolvedChangedReading | None, dict[str, object] | None]:
    """Find the fact this changed reading became, and whether a human blessed it.

    The lookup is by the new campaign's exact provenance key, never by text: the
    fact this reading became is the one its own evidence justifies, and a fact
    that merely happens to carry the same value is a different row.
    """
    proof = provenance_for_candidate(reading.candidate)
    facts = list_profile_facts_by_evidence(
        connection, profile_id, proof.resolved_provenance_key()
    )
    if not facts:
        return None, {**reading.summary(), "reason": MISSING_REASON}
    if len(facts) > 1:
        return None, {**reading.summary(), "reason": AMBIGUOUS_REASON}
    fact = facts[0]
    if fact.status is FactStatus.ACCEPTED:
        return (
            ResolvedChangedReading(reading=reading, fact=fact, truth=fact),
            None,
        )
    if fact.status is FactStatus.CORRECTED:
        replacement = (
            None
            if fact.replaced_by_fact_id is None
            else get_profile_fact(connection, profile_id, fact.replaced_by_fact_id)
        )
        if replacement is not None and replacement.status is FactStatus.ACCEPTED:
            return (
                ResolvedChangedReading(reading=reading, fact=fact, truth=replacement),
                None,
            )
        return None, {
            **reading.summary(),
            "fact_id": fact.id,
            "reason": REPLACEMENT_NOT_ACCEPTED_REASON,
        }
    reason = (
        REFUSED_REASON if fact.status is FactStatus.REJECTED else UNDECIDED_REASON
    )
    return None, {**reading.summary(), "fact_id": fact.id, "reason": reason}


def finalize_cv_fact_reconciliation(
    connection: sqlite3.Connection,
    *,
    profile_id: int,
    extraction: StructuredCvExtraction,
    old_parser_version: str,
    old_extractor_version: str,
    after_precheck: Callable[[], None] | None = None,
) -> CvReconciliationFinalization:
    """Retire the old readings the new, human-confirmed campaign has replaced.

    The plan is recomputed from the same three inputs `prepare` used, so
    nothing is carried between the two calls and neither trusts a stored
    decision. Then, **before any mutation**, every changed reading is resolved
    through its own evidence and has to be ground truth: `ACCEPTED`, or
    `CORRECTED` with an `ACCEPTED` replacement. One that is still `PROPOSED`,
    one that was `REJECTED`, one whose fact is missing and one whose evidence
    justifies several facts each raise `UnresolvedChangedReadingError` and stop
    the run with not a single old fact touched.

    Once they are all answered, each superseded old fact that is still
    `ACCEPTED` becomes `REJECTED`, meaning "this whole reading — that type,
    that text, under that segmentation — is no longer active truth". Nothing
    else happens to it: the row stays, its `value` stays, its `normalized_value`
    stays, its own campaign's provenance stays, and there is no `DELETE`
    anywhere in this module. An already `REJECTED` fact is a no-op, a terminal
    `CORRECTED` one is left exactly as the person left it, and one nobody ever
    decided is left `PROPOSED` — turning an unanswered proposal into a refusal
    would be this module deciding.

    Each rejection is its own transaction and each is a no-op the second time,
    so an interrupted run is resumed by running it again, and a second complete
    run rejects nothing.

    `after_precheck` is a test seam invoked once every changed reading has been
    resolved and before the first old fact is touched; production callers leave
    it unset.
    """
    plan = compute_cv_reconciliation_plan(
        connection,
        profile_id=profile_id,
        extraction=extraction,
        old_parser_version=old_parser_version,
        old_extractor_version=old_extractor_version,
    )
    resolved: list[ResolvedChangedReading] = []
    unresolved: list[dict[str, object]] = []
    for reading in plan.changed:
        entry, problem = _resolve_changed_reading(connection, profile_id, reading)
        if entry is not None:
            resolved.append(entry)
        else:
            assert problem is not None
            unresolved.append(problem)
    if unresolved:
        reasons = _tally(
            tuple(str(problem["reason"]) for problem in unresolved)
        )
        raise UnresolvedChangedReadingError(
            f"{len(unresolved)} of {len(plan.changed)} new readings are not "
            "confirmed yet ("
            + ", ".join(f"{reason}={count}" for reason, count in reasons.items())
            + "); no old fact was rejected",
            tuple(unresolved),
        )

    if after_precheck is not None:
        after_precheck()

    outcomes: list[SupersededOutcome] = []
    for entry in plan.superseded:
        # Re-read rather than trusting the plan's snapshot: a previous run of
        # this same finalize may already have rejected it, and a human may have
        # decided it in the meantime.
        current = get_profile_fact(connection, profile_id, entry.fact.id)
        if current is None:
            # It cannot vanish — nothing in this project deletes a fact — so
            # saying so is better than silently counting it as handled.
            raise CvReconciliationError(
                f"fact {entry.fact.id} of profile {profile_id} disappeared "
                "while the reconciliation was running"
            )
        if current.status is FactStatus.ACCEPTED:
            rejected = reject_profile_fact(connection, profile_id, current.id)
            outcomes.append(
                SupersededOutcome(fact=rejected, rejected=True, left_alone=None)
            )
            continue
        if current.status is FactStatus.REJECTED:
            left_alone = ALREADY_REJECTED
        elif current.status is FactStatus.CORRECTED:
            left_alone = TERMINAL_CORRECTED
        else:
            left_alone = UNDECIDED
        outcomes.append(
            SupersededOutcome(fact=current, rejected=False, left_alone=left_alone)
        )
    return CvReconciliationFinalization(
        plan=plan, resolved=tuple(resolved), outcomes=tuple(outcomes)
    )


def extraction_is_current(extraction: StructuredCvExtraction) -> bool:
    """True when the extraction was produced by the versions this code ships.

    The CLI refuses to reconcile *towards* anything else: a reconciliation is
    only meaningful when the newer side is the reading this checkout actually
    produces, and an extraction announcing older versions is a stale artefact.
    """
    return (
        extraction.parser_version == PARSER_VERSION
        and extraction.extractor_version == CANDIDATE_EXTRACTOR_VERSION
    )
