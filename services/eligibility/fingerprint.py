"""The digest that makes a second synchronization write nothing.

    input_fingerprint = SHA256(canonical_json({
        engine_version, opportunity: {...}, profile: {...}
    }))

Same pattern as `qualification-rules-v1`, `opportunity-constraints-v1` and
`opportunity-requirements-v3` before it: canonical JSON, sorted keys, no
insignificant whitespace, UTF-8, SHA-256. What is new here is that the digest
spans **two** sides, so it answers one question those three could not: did
anything either the posting or the person stated, that any rule of this engine
actually reads, change since the last verdict?

**The domain is the two input dataclasses, and nothing else.** They are the
same objects the rules are handed, so "in the digest" and "read by a rule" are
the same set by construction rather than by discipline. That is the property
worth having:

* a corrected telephone number, a new GitHub URL, a rewritten portfolio link, a
  changed availability date, a widened mobility, a reordered list of preferred
  domains — none of them is a field of `ProfileEligibilityInput`, so none of
  them is in the digest and none of them recomputes anything;
* a posting's retitled advertisement, its city, its work mode, its duration and
  its start date — none of them is a field of `OpportunityEligibilityInput`,
  for the same reason;
* a changed education requirement, a changed required language, a newly stated
  sponsorship need, a skill gained: every one of them **is** read, so every one
  of them changes the digest and the next run re-evaluates.

**Nothing volatile is in it.** No clock, no `evaluated_at`, no `updated_at`, no
row id, no database ordering, no random value. Two runs a week apart over
unchanged data produce the same sixty-four characters, which is exactly what
makes `unchanged=N, changed=false` a fact about the data rather than about the
run.

**Order is normalized, not trusted.** Every collection is sorted here by its own
values before serialization, so two readings that produce the same requirements
in a different order produce the same digest. The rules keep the order they were
given, because a decision's rule list should read the way the posting reads; the
digest does not care, because a set of demands is the same set however it is
listed.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from services.eligibility.models import (
    EligibilityInput,
    ExperienceRequirementInput,
    LanguageRequirementInput,
    OpportunityEligibilityInput,
    ProfileEligibilityInput,
    ProfileLanguageInput,
)

__all__ = [
    "canonical_eligibility_payload",
    "canonical_json",
    "eligibility_fingerprint",
]


def canonical_json(payload: Any) -> str:
    """The one serialization the digest is taken over."""
    return json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )


def _optional_int_sort_key(value: int | None) -> tuple[bool, int]:
    """Order a nullable integer without replacing its serialized value."""
    return (value is not None, 0 if value is None else value)


def _optional_str_sort_key(value: str | None) -> tuple[bool, str]:
    """Order a nullable string without replacing its serialized value."""
    return (value is not None, "" if value is None else value)


def _experience_sort_key(
    item: ExperienceRequirementInput,
) -> tuple[tuple[bool, int], tuple[bool, int], tuple[bool, str]]:
    return (
        _optional_int_sort_key(item.min_months),
        _optional_int_sort_key(item.max_months),
        _optional_str_sort_key(None if item.kind is None else item.kind.value),
    )


def _opportunity_language_sort_key(
    item: LanguageRequirementInput,
) -> tuple[str, str, tuple[bool, str]]:
    return (
        item.language_key,
        item.kind.value,
        _optional_str_sort_key(item.proficiency_text),
    )


def _profile_language_sort_key(
    item: ProfileLanguageInput,
) -> tuple[str, tuple[bool, str]]:
    return (item.language_key, _optional_str_sort_key(item.proficiency_text))


def _opportunity_payload(
    opportunity: OpportunityEligibilityInput,
) -> dict[str, Any]:
    """Exactly what the rules read from the offer side.

    `opportunity_id` is deliberately absent, as it is from 3.5A's and 3.5B's
    digests: the identity of the row is not part of what was read, and including
    it would give two identical postings two digests for no reason. The two
    extractor versions **are** present: a re-reading of the same advertisement
    under new extraction rules is a new statement of what it demands, even when
    the values happen to land the same, and the verdict should be recomputed
    under the version that produced them.
    """
    return {
        "constraints_extractor_version": opportunity.constraints_extractor_version,
        "requirements_extractor_version": opportunity.requirements_extractor_version,
        "education": sorted(
            [item.level, item.mode] for item in opportunity.education
        ),
        "enrollment_required": opportunity.enrollment_required,
        "experience": [
            [
                item.min_months,
                item.max_months,
                None if item.kind is None else item.kind.value,
            ]
            for item in sorted(opportunity.experience, key=_experience_sort_key)
        ],
        "languages": [
            [item.language_key, item.kind.value, item.proficiency_text]
            for item in sorted(
                opportunity.languages, key=_opportunity_language_sort_key
            )
        ],
        "skills": sorted(
            [item.canonical_key, item.kind.value] for item in opportunity.skills
        ),
        # The refused demands are in the digest because they are in the output:
        # each produces a NOT_EVALUATED row, so a posting gaining or losing one
        # has a different decision to store even though its verdict cannot move.
        "ambiguities": sorted(
            [item.kind, item.reason] for item in opportunity.ambiguities
        ),
        "visa_sponsorship": opportunity.visa_sponsorship,
        "work_authorization": opportunity.work_authorization,
        "convention": opportunity.convention,
    }


def _profile_payload(profile: ProfileEligibilityInput) -> dict[str, Any]:
    """Exactly what the rules read from the person's side.

    `profile_id` is absent for the same reason `opportunity_id` is: it names the
    row, not what the row said. The decision is still per person — the stored
    row carries `user_id` and `UNIQUE (user_id, opportunity_id)` — but two people
    whose relevant facts are identical genuinely have the same eligibility input,
    and a digest claiming otherwise would be describing identity rather than
    content.
    """
    return {
        "education_levels": sorted(profile.education_levels),
        "currently_enrolled": profile.currently_enrolled,
        "comparable_experience_months": profile.comparable_experience_months,
        "languages": [
            [item.language_key, item.proficiency_text]
            for item in sorted(profile.languages, key=_profile_language_sort_key)
        ],
        "skills": sorted(profile.skills),
        "sponsorship_need": profile.sponsorship_need.value,
        "convention_capability": profile.convention_capability.value,
    }


def canonical_eligibility_payload(inputs: EligibilityInput) -> dict[str, Any]:
    """The whole digest domain, as a plain structure a test can read."""
    return {
        "engine_version": inputs.engine_version,
        "opportunity": _opportunity_payload(inputs.opportunity),
        "profile": _profile_payload(inputs.profile),
    }


def eligibility_fingerprint(inputs: EligibilityInput) -> str:
    """The SHA-256 of the canonical JSON of everything this engine reads."""
    serialized = canonical_json(canonical_eligibility_payload(inputs)).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()
