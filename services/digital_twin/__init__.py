"""Digital Twin domain package: the persistent profile root and its facts.

Phase 3.1A added the identity/ownership root (`users`) and the stable Digital
Twin root (`profiles`), one profile per user. Phase 3.3A adds
`services/digital_twin/facts/`: the persistent `profile_facts`, their
provenance, and the PROPOSED / ACCEPTED / CORRECTED / REJECTED validation cycle
in which only `ACCEPTED` means verified.

Reading a CV (`services/digital_twin/cv/`) still stores nothing: the Phase 3.2B
candidates are not imported into the fact store, which is Phase 3.3B. Skill
normalization and levels, employers, institutions, dates, structured
preferences, eligibility, matching and scoring belong to later slices and do
not exist.
"""
