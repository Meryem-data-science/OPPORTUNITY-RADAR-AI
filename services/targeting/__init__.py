"""Targeting: is this posting one of the ones this person is actually looking for?

A namespace, not a service. Each slice underneath answers exactly one closed
question about one posting, for one profile, with the same three answers —
`MATCH`, `OUT_OF_TARGET`, `UNKNOWN` — and none of them stores its answer: the
left-hand side of every comparison here is a declaration a person can change
this afternoon, and a stored verdict would go on answering for a profile that
no longer exists.

    opportunity_type/   Phase 7B.1 — is its structured type one of the types
                        this profile explicitly said it is looking for?

The geographic slice of the same family, Phase 7A.1, lives in
`services/geography/` because it owns a stored projection of its own
(`opportunity_location_resolutions`) as well as a verdict. It is closed and
untouched here; whether the two verdict vocabularies should become one is a
question for the slice that first has to compose them, not for this one.

Nothing in this package classifies, scores, ranks, or decides eligibility, and
no slice under it reads a CV.
"""
