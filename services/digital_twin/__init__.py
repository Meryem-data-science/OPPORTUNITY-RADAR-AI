"""Digital Twin domain package: the persistent user and profile root.

Phase 3.1A holds only the identity/ownership root (`users`) and the stable
Digital Twin root (`profiles`), one profile per user. Factual profile content
and its provenance — CV parsing, `cv_versions`, `profile_facts`, skills,
education, experiences, projects, certifications, languages, preferences,
eligibility, matching and scoring — are not implemented here and belong to the
following slices.
"""
