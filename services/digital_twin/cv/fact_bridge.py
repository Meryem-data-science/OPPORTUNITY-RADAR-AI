"""Phase 3.3B: the one place a CV candidate becomes a *proposed* profile fact.

This module is the boundary, and it exists so that neither side has to cross
it. `services/digital_twin/cv/candidates/` still knows nothing about
`profile_facts` — no module there imports the fact package, and a test asserts
it — and `services/digital_twin/facts/` still knows nothing about CVs. The
translation between the two vocabularies happens here, once, in the open.

What it translates, and nothing more:

* a `CandidateType` becomes a `ProfileFactType` through one closed, total
  mapping written out by hand below;
* `raw_text` becomes `value` and `normalized_value` becomes `normalized_value`,
  both carried over unchanged;
* the candidate's own provenance becomes a `ProvenanceInput`, field for field.

What it refuses to do: interpret. No skill alias, no skill level, no employer,
institution, date or country parsed out of a value, no canonical role, no
rewording and no invented locator — that business normalization is Phase 3.4
and has not started. A field the candidate did not carry stays `None` rather
than being filled with something plausible.

Every fact it creates is `PROPOSED`. There is no threshold, no confidence, no
score and no automatic acceptance anywhere in this module: an extraction is a
reading, and only a human deciding makes it true. The review that asks that
human is `services/digital_twin/cv/review_cli.py`.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from services.digital_twin.cv.candidates.models import (
    CandidateType,
    ExtractedCandidate,
    StructuredCvExtraction,
)
from services.digital_twin.facts.models import (
    FactSourceType,
    FactStatus,
    ProfileFact,
    ProfileFactType,
    ProvenanceInput,
)
from services.digital_twin.facts.repository import ensure_profile_fact_proposal

#: What kind of fact each kind of candidate proposes. The mapping is written
#: out entry by entry rather than derived from the two names, because the two
#: taxonomies are allowed to disagree and one of them already does:
#: `NAME_CANDIDATE` is named for what it is — a proposal read off a header —
#: and the fact it proposes is a plain `NAME`.
#:
#: It is **total** over `CandidateType` and `_verify_mapping_is_total` proves it
#: at import time, so adding a candidate type without deciding what it means
#: here breaks loudly, on the first import, instead of silently dropping the
#: candidates it produces.
#:
#: Nothing maps to `PREFERENCE`, `AVAILABILITY`, `MOBILITY` or
#: `CAREER_OBJECTIVE`: Phase 3.2B produces no such candidate, and a mapping to
#: a category nothing feeds would be a promise the extractor does not keep.
CANDIDATE_TYPE_TO_FACT_TYPE: dict[CandidateType, ProfileFactType] = {
    CandidateType.NAME_CANDIDATE: ProfileFactType.NAME,
    CandidateType.PROFESSIONAL_TITLE: ProfileFactType.PROFESSIONAL_TITLE,
    CandidateType.EMAIL: ProfileFactType.EMAIL,
    CandidateType.PHONE: ProfileFactType.PHONE,
    CandidateType.GITHUB_URL: ProfileFactType.GITHUB_URL,
    CandidateType.LINKEDIN_URL: ProfileFactType.LINKEDIN_URL,
    CandidateType.PORTFOLIO_URL: ProfileFactType.PORTFOLIO_URL,
    CandidateType.PROFESSIONAL_URL: ProfileFactType.PROFESSIONAL_URL,
    CandidateType.EDUCATION_ENTRY: ProfileFactType.EDUCATION,
    CandidateType.EXPERIENCE_ENTRY: ProfileFactType.EXPERIENCE,
    CandidateType.PROJECT_ENTRY: ProfileFactType.PROJECT,
    CandidateType.CERTIFICATION_ENTRY: ProfileFactType.CERTIFICATION,
    CandidateType.LANGUAGE_ENTRY: ProfileFactType.LANGUAGE,
    CandidateType.SKILL: ProfileFactType.SKILL,
}

#: Fact types this bridge never proposes, because no rule produces them.
UNMAPPED_FACT_TYPES: frozenset[ProfileFactType] = frozenset(
    {
        ProfileFactType.PREFERENCE,
        ProfileFactType.AVAILABILITY,
        ProfileFactType.MOBILITY,
        ProfileFactType.CAREER_OBJECTIVE,
    }
)

__all__ = [
    "CANDIDATE_TYPE_TO_FACT_TYPE",
    "UNMAPPED_FACT_TYPES",
    "CvImportResult",
    "ImportedCandidate",
    "UnmappedCandidateTypeError",
    "fact_type_for_candidate_type",
    "import_cv_candidates",
    "provenance_for_candidate",
]


class UnmappedCandidateTypeError(LookupError):
    """Raised when a candidate type has no decided meaning as a fact.

    Guessing one would be the whole failure mode this project is built against:
    a value would enter the profile under a category nobody chose for it.
    """


def _verify_mapping_is_total() -> None:
    """Refuse to load with a candidate type nobody decided the meaning of."""
    missing = sorted(
        candidate_type.value
        for candidate_type in CandidateType
        if candidate_type not in CANDIDATE_TYPE_TO_FACT_TYPE
    )
    if missing:
        raise UnmappedCandidateTypeError(
            "CANDIDATE_TYPE_TO_FACT_TYPE does not cover: " + ", ".join(missing)
        )


_verify_mapping_is_total()


def fact_type_for_candidate_type(candidate_type: CandidateType) -> ProfileFactType:
    """Return what this kind of candidate proposes. Never a default."""
    try:
        return CANDIDATE_TYPE_TO_FACT_TYPE[candidate_type]
    except KeyError as error:
        raise UnmappedCandidateTypeError(
            f"no profile fact type is mapped to {candidate_type}"
        ) from error


def provenance_for_candidate(candidate: ExtractedCandidate) -> ProvenanceInput:
    """Carry the candidate's own provenance over, field for field.

    Every value comes from the candidate. Nothing is derived, defaulted or
    completed, so the evidence recorded says exactly what the extraction said
    and no more.

    `source_locator` stays `None` on purpose. The only locator this side could
    supply is the local path of the PDF, and a CV filename usually carries the
    person's name: the digest identifies the document without naming anybody.
    """
    return ProvenanceInput(
        source_type=FactSourceType.CV,
        source_locator=None,
        cv_sha256=candidate.cv_sha256,
        parser_version=candidate.parser_version,
        extractor_version=candidate.extractor_version,
        candidate_fingerprint=candidate.fingerprint,
        rule_id=candidate.rule_id.value,
        page_numbers=candidate.page_numbers,
        section_type=(
            None if candidate.section_type is None else candidate.section_type.value
        ),
        section_index=candidate.section_index,
    )


@dataclass(frozen=True)
class ImportedCandidate:
    """One candidate and the fact its evidence justifies, new or already there."""

    candidate: ExtractedCandidate
    fact: ProfileFact
    #: True only when this run created the fact. False means the same proof was
    #: already imported — whatever the human has since decided about it.
    created: bool

    @property
    def fact_type(self) -> str:
        return self.fact.fact_type

    @property
    def needs_review(self) -> bool:
        """True while nobody has decided. A decided fact is never asked again."""
        return self.fact.status is FactStatus.PROPOSED

    def summary(self) -> dict[str, object]:
        """The privacy-safe view: what kind, from which rule, where it stands.

        No `value` and no `raw_text`, so no name, address, number, URL,
        employer, school or skill mention. Safe to print and to log.
        """
        return {
            "candidate_type": self.candidate.candidate_type.value,
            "fact_type": self.fact.fact_type,
            "fact_id": self.fact.id,
            "status": self.fact.status.value,
            "created": self.created,
            "rule_id": self.candidate.rule_id.value,
            "page_numbers": list(self.candidate.page_numbers),
            "section_type": (
                None
                if self.candidate.section_type is None
                else self.candidate.section_type.value
            ),
            "section_index": self.candidate.section_index,
        }


@dataclass(frozen=True)
class CvImportResult:
    """What one import of one extraction did, for one profile."""

    profile_id: int
    cv_sha256: str
    parser_version: str
    extractor_version: str
    #: One entry per candidate, in the extraction's own deterministic order, so
    #: a review walks the document the way it was read.
    imported: tuple[ImportedCandidate, ...]

    @property
    def candidate_count(self) -> int:
        return len(self.imported)

    @property
    def newly_proposed(self) -> int:
        return sum(1 for entry in self.imported if entry.created)

    @property
    def already_imported(self) -> int:
        return sum(1 for entry in self.imported if not entry.created)

    @property
    def pending_review(self) -> tuple[ImportedCandidate, ...]:
        """The entries a human has not decided yet, in document order."""
        return tuple(entry for entry in self.imported if entry.needs_review)

    def counts_by_fact_type(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for entry in self.imported:
            counts[entry.fact_type] = counts.get(entry.fact_type, 0) + 1
        return {key: counts[key] for key in sorted(counts)}

    def summary(self) -> dict[str, object]:
        """Counters, versions and canonical types. Never a value from the CV."""
        return {
            "profile_id": self.profile_id,
            "sha256": self.cv_sha256,
            "parser_version": self.parser_version,
            "extractor_version": self.extractor_version,
            "candidates": self.candidate_count,
            "newly_proposed": self.newly_proposed,
            "already_imported": self.already_imported,
            "pending_review": len(self.pending_review),
            "counts_by_fact_type": self.counts_by_fact_type(),
        }


def import_cv_candidates(
    connection: sqlite3.Connection,
    *,
    profile_id: int,
    extraction: StructuredCvExtraction,
) -> CvImportResult:
    """Propose one profile fact per candidate, and never more than one.

    Candidates are imported in the extraction's own order, each in its own
    transaction, so the operation is restartable rather than all-or-nothing: if
    a run stops after N candidates, those N facts exist complete with their
    evidence, and re-running imports the rest without duplicating any of them.
    Re-running a finished import creates nothing at all.

    Identity is the evidence, not the text — see `ensure_profile_fact_proposal`
    — so a value read from a second, different CV is a second proof and gets
    its own proposal. Consolidating several CV versions into one reading is not
    attempted here and is not implied by anything this returns.

    Every fact created is `PROPOSED`. This function accepts nothing, and there
    is no argument that would let it.
    """
    imported: list[ImportedCandidate] = []
    for candidate in extraction.candidates:
        fact_type = fact_type_for_candidate_type(candidate.candidate_type)
        proposal = ensure_profile_fact_proposal(
            connection,
            profile_id=profile_id,
            fact_type=fact_type,
            # Verbatim, both of them: the CV said this, and a human validating
            # it later must read what the CV said, not a tidied-up version.
            value=candidate.raw_text,
            normalized_value=candidate.normalized_value,
            provenance=provenance_for_candidate(candidate),
        )
        imported.append(
            ImportedCandidate(
                candidate=candidate,
                fact=proposal.fact,
                created=proposal.created,
            )
        )
    return CvImportResult(
        profile_id=profile_id,
        cv_sha256=extraction.cv_sha256,
        parser_version=extraction.parser_version,
        extractor_version=extraction.extractor_version,
        imported=tuple(imported),
    )
