"""Building business metric evidence, and refusing evidence that does not add up.

This module is where the contract in `schema.py` is *established*. Every
invariant those frozen dataclasses claim is created here and nowhere else.

What is built, and in which direction:

    the frozen cohort binding    derived from the verified Phase 10.1 records and
                                 manifest: the cohort's size, when it was taken,
                                 the as-of date that follows, the complete set of
                                 action URLs it implies, and what its records say
                                 about deduplication
    the evidence artefacts       a target binding, a declared source universe, a
                                 URL audit campaign, dedup counts, the freshness
                                 binding, a benchmark — each sealed with its own
                                 digest and each held against that cohort
    the run context              every artefact one run assembled, with an
                                 identity of its own
    a per-metric scope           projected: which exact question this metric asked
    a per-metric evidence view   projected: on which exact artefacts it rests
    the result                   one answer, sealed, bound to both projections

**Nothing is chosen that can be derived.** No caller states a cohort size, and no
caller chooses a freshness as-of date: both come from the snapshot, through
`build_frozen_cohort_binding`, and every later check is against those facts
rather than against numbers somebody passed in.

**The universe is checked, not merely named.** A `FROZEN_COHORT` result states
the cohort's real size; a `PRE_DEDUP_REPRESENTED_UNIVERSE` result states `S + D`;
a `URL_AUDITED_UNIVERSE` result states the audit's size **and** the audit is held
complete against the cohort's expected URL set, on every build and every
verification, with no argument that can skip it; a `DECLARED_SOURCE_UNIVERSE`
result states the number of declared entries. A wrong size is a binding failure.

**Each metric may only refuse in ways it is entitled to.**
`BUSINESS_METRIC_DEFINITIONS` names the permitted reasons and this module checks
the *condition* each one asserts: an absent member must be reported as its own
absence, a target with no country must be `TARGET_COUNTRY_UNKNOWN`, a
broken-URL rate over an audit that concluded nothing must be
`NO_CONCLUSIVE_URL_AUDIT`.

**No metric is computed here, and none can be.** There is no formula in this
package. `_build_business_metric_result` assembles a result from a value
somebody else produced, and its whole job is to refuse to seal one the bindings
do not support — which is why it is **private**. A public sealer taking a
`value`, a numerator and a denominator would be an API for publishing an
arbitrary number as a measured one, months before any formula exists to produce
it honestly. Phase 10.4b owns that surface, following the shape Phase 10.3
already uses: a public metric function gates availability, does the arithmetic,
and then assembles the result privately. What stays public here is
`verify_business_metric_result`, which re-establishes a result somebody else
sealed.

**What this module reads, and what it will never write.** One function —
`build_profile_target_binding` — accepts a database connection, because Phase
10.1's manifest records the *fingerprint* of the profile context without
republishing the payload, so the target country a future metric needs exists only
in the operational profile. That read goes through production's own read-only
loaders and writes nothing. Every other function here is pure, and the cohort
binding is built from records and a manifest a caller already holds — no file is
opened, and Phase 10.1 is not modified.

**No human label, anywhere.** This package does not import `evaluation.labeling`,
not even for a type, which is why the frozen-record helpers take a plain sequence
of record mappings rather than a `FrozenEvaluationDataset`.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import fields as dataclass_fields, replace
from typing import Any

from services.collector.matching.fingerprint import canonical_json

from services.geography.profile_target import (
    RESTRICTED_COUNTRY_RULE,
    TARGET_RULE_IDS,
    resolve_profile_target,
)

from evaluation.dataset import (
    EVALUATION_CANONICAL_ORDER,
    EVALUATION_COHORT_CRITERIA,
    EVALUATION_COHORT_VERSION,
    EvaluationDatasetError,
    EvaluationOpportunityRecord,
    require_supported_schema_version,
)
from evaluation.dataset.fingerprint import manifest_fingerprint_domain
from evaluation.dataset.profile_context import profile_context_fingerprint

from .fingerprint import (
    PLACEHOLDER_FINGERPRINT,
    benchmark_binding_fingerprint,
    business_metric_evidence_fingerprint,
    business_metric_result_fingerprint,
    business_metric_run_context_fingerprint,
    business_metric_scope_fingerprint,
    declared_source_universe_fingerprint,
    dedup_evidence_fingerprint,
    freshness_binding_fingerprint,
    frozen_cohort_binding_fingerprint,
    profile_target_binding_fingerprint,
    url_audit_binding_fingerprint,
    verify_benchmark_binding_fingerprint,
    verify_business_metric_evidence_fingerprint,
    verify_business_metric_result_fingerprint,
    verify_business_metric_run_context_fingerprint,
    verify_business_metric_scope_fingerprint,
    verify_declared_source_universe_fingerprint,
    verify_dedup_evidence_fingerprint,
    verify_freshness_binding_fingerprint,
    verify_frozen_cohort_binding_fingerprint,
    verify_profile_target_binding_fingerprint,
    verify_url_audit_binding_fingerprint,
)
from .schema import (
    ALWAYS_UNAVAILABLE_METRICS,
    BENCHMARK_BINDING_VERSION,
    BUSINESS_EVIDENCE_VIEW_SCHEMA_VERSION,
    BUSINESS_METRIC_CONTRACT_VERSION,
    BUSINESS_METRIC_RUN_CONTEXT_SCHEMA_VERSION,
    BUSINESS_METRIC_SCOPE_SCHEMA_VERSION,
    DECLARED_SOURCE_UNIVERSE_VERSION,
    DEDUP_EVIDENCE_VERSION,
    EVIDENCE_MEMBER_ATTRIBUTES,
    EXPECTED_FRESHNESS_POLICY_VERSION,
    FRESHNESS_BINDING_VERSION,
    FROZEN_ACTION_URL_PROTOCOL_VERSION,
    FROZEN_COHORT_BINDING_VERSION,
    MEMBER_ABSENCE_REASONS,
    MERGED_DUPLICATE_EXCLUSION_KEY,
    PROFILE_TARGET_BINDING_VERSION,
    URL_AUDIT_BINDING_VERSION,
    URL_AUDIT_POLICY_VERSION,
    BenchmarkBinding,
    BusinessEvidenceClass,
    BusinessEvidenceClassError,
    BusinessEvidenceMember,
    BusinessMetricBindingError,
    BusinessMetricContractError,
    BusinessMetricEvidenceView,
    BusinessMetricKey,
    BusinessMetricName,
    BusinessMetricResult,
    BusinessMetricRunContext,
    BusinessMetricScope,
    BusinessMetricStatus,
    BusinessMetricSupport,
    BusinessMetricUnavailableReason,
    BusinessMetricUniverse,
    DeclaredSourceEntry,
    DeclaredSourceUniverseEvidence,
    DedupEvidence,
    FreshnessBinding,
    FrozenCohortBinding,
    ProfileTargetBindingEvidence,
    UrlAuditBinding,
    UrlAuditObservation,
    business_metric_key_sort_key,
    calendar_date_of,
    frozen_action_url,
    member_fingerprint,
    member_value,
    metric_definition,
    require_metric_universe,
    require_permitted_reason,
    require_supported_business_metric_contract_version,
    validate_benchmark_binding_structure,
    validate_business_metric_evidence_structure,
    validate_business_metric_result_structure,
    validate_business_metric_run_context_structure,
    validate_business_metric_scope_structure,
    validate_count,
    validate_declared_source_universe_structure,
    validate_dedup_evidence_structure,
    validate_fingerprint,
    validate_freshness_binding_structure,
    validate_frozen_cohort_binding_structure,
    validate_git_commit,
    validate_profile_id,
    validate_profile_target_binding_structure,
    validate_text,
    validate_timestamp,
    validate_url_audit_binding_structure,
)

__all__ = [
    "assert_unique_business_metric_keys",
    "assert_url_audit_covers",
    "benchmark_records_fingerprint",
    "build_benchmark_binding",
    "build_business_metric_run_context",
    "build_dedup_evidence",
    "build_freshness_binding",
    "build_frozen_cohort_binding",
    "build_profile_target_binding",
    "build_url_audit_binding",
    "declared_source_universe_from_source_map",
    "expected_frozen_action_urls",
    "project_metric_evidence",
    "project_metric_scope",
    "require_business_evidence_class",
    "source_map_payload",
    "validate_target_rule_binding",
    "verify_business_metric_result",
    "verify_business_metric_results",
    "verify_business_metric_run_context",
    "verify_metric_scope_and_evidence_agree",
]


# --------------------------------------------------------------------------
# the frozen cohort binding
# --------------------------------------------------------------------------


def expected_frozen_action_urls(
    records: Sequence[Mapping[str, Any]],
) -> tuple[str, ...]:
    """The complete set of unique frozen action URLs a cohort implies, **in
    snapshot order**.

    Derived by `FROZEN_ACTION_URL_PROTOCOL_VERSION` and nothing else: the frozen
    records are walked in their canonical order, the protocol selects one URL
    per record, records that select nothing contribute nothing, and each distinct
    URL is kept at its **first occurrence**. Two postings republished at one
    address are one URL to audit, and auditing it twice would double-count a
    single fact.

    An earlier version collected the URLs into a `set` and returned them
    `sorted()`. That produced a *lexical* order, which is an order over the
    strings and not a fact about the cohort: it would have been identical for a
    snapshot whose records were shuffled, so the derived universe could not tell
    two different snapshots apart. First occurrence can, and it is reproducible
    precisely because `build_frozen_cohort_binding` has already established that
    the records ascend by `opportunity_id` — an ordered derivation over an
    unordered snapshot would be arbitrary rather than canonical.

    A selected value that is not fetchable — a `mailto:`, a relative path — is
    **in** this universe. It was selected by the protocol, so it is part of the
    universe, and the audit must carry an observation for it. Dropping it here
    would let the audited universe silently exclude the postings whose URLs are
    worst, which is precisely the population a data-quality metric exists to
    surface.

    Records with no usable action URL at all contribute nothing: they are what
    `MISSING_ACTION_URL_RATE` counts over the cohort instead.
    """
    ordered: list[str] = []
    seen: set[str] = set()
    for position, record in enumerate(records, start=1):
        if not isinstance(record, Mapping):
            raise BusinessMetricBindingError(
                f"frozen record {position} is not a mapping: {record!r}"
            )
        url = frozen_action_url(record)
        if url is not None and url not in seen:
            seen.add(url)
            ordered.append(url)
    return tuple(ordered)


def _manifest_value(manifest: Mapping[str, Any], key: str) -> Any:
    if not isinstance(manifest, Mapping):
        raise BusinessMetricBindingError(
            f"the evaluation manifest is not a mapping: {manifest!r}"
        )
    if key not in manifest:
        raise BusinessMetricBindingError(
            f"the evaluation manifest states no {key!r}; refusing to derive a "
            "cohort binding from a manifest of another contract"
        )
    return manifest[key]


def _manifest_section(manifest: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    section = _manifest_value(manifest, key)
    if not isinstance(section, Mapping):
        raise BusinessMetricBindingError(
            f"the evaluation manifest's {key!r} is not an object: {section!r}"
        )
    return section


#: The contract of one frozen record, taken from the Phase 10.1 dataclass rather
#: than written out again. A second hand-kept list here would be free to drift
#: from the snapshot it claims to verify, and a field added upstream would then
#: pass unnoticed through a check whose whole purpose is to notice it.
FROZEN_RECORD_FIELDS: frozenset[str] = frozenset(
    field.name for field in dataclass_fields(EvaluationOpportunityRecord)
)


def _require_cohort_rule(cohort_section: Mapping[str, Any]) -> None:
    """The selection rule itself, not merely its version string.

    A version is a label a writer chooses; the criteria and the ordering are the
    rule. A snapshot that kept `evaluation-cohort-v2` on the tin while selecting
    on different predicates, or while ordering its records some other way, is
    not the cohort this build measures against — and its `dataset_id` would
    still verify, because the criteria are inside the content fingerprint and
    both sides of that comparison moved together.
    """
    cohort_version = cohort_section.get("version")
    if cohort_version != EVALUATION_COHORT_VERSION:
        raise BusinessMetricBindingError(
            f"unsupported evaluation cohort version: {cohort_version!r} (this "
            f"build measures against {EVALUATION_COHORT_VERSION!r}); two "
            "snapshots taken under different cohort rules are not comparable"
        )
    ordering = cohort_section.get("ordering")
    if ordering != EVALUATION_CANONICAL_ORDER:
        raise BusinessMetricBindingError(
            f"the manifest states cohort.ordering {ordering!r}; this build "
            f"derives an ordered universe from {EVALUATION_CANONICAL_ORDER!r}, "
            "and a snapshot in another order does not have a first occurrence "
            "of anything"
        )
    criteria = cohort_section.get("criteria")
    if isinstance(criteria, (str, bytes)) or not isinstance(criteria, Sequence):
        raise BusinessMetricBindingError(
            f"the manifest's cohort.criteria is not a sequence: {criteria!r}"
        )
    stated = tuple(criteria)
    if stated != EVALUATION_COHORT_CRITERIA:
        raise BusinessMetricBindingError(
            f"the manifest states cohort.criteria {stated!r} and this build "
            f"measures against {EVALUATION_COHORT_CRITERIA!r}; the criteria are "
            "the rule and their order is part of it, so a snapshot selected by "
            "different predicates is a different universe however its version "
            "string reads"
        )


def _require_frozen_record_structure(
    records: Sequence[Mapping[str, Any]],
) -> None:
    """The records must be the contract's records, in the contract's order.

    A valid SHA-256 over an invalid structure is not a valid frozen dataset. The
    digest says "these bytes are the bytes the manifest describes"; it says
    nothing about whether those bytes are records of *this* contract, whether
    each posting appears once, or whether they arrive in the canonical order the
    manifest itself declares. Those are separate facts and each is checked here:

    * every record carries exactly the fields of `EvaluationOpportunityRecord` —
      neither a missing one, which would make an absent fact indistinguishable
      from a field this build has never heard of, nor an extra one, which would
      be a statement no later slice can interpret;
    * `opportunity_id` is an `int` and explicitly not a `bool`: `True` is an
      instance of `int` in Python, and an identity of `True` would compare equal
      to the identity `1` and silently merge two postings;
    * no `opportunity_id` repeats, so the cohort size is a count of distinct
      postings rather than of lines;
    * the ids ascend strictly, which is `EVALUATION_CANONICAL_ORDER` — the
      property that makes "the first occurrence of a URL" a fact about the
      snapshot rather than about the order somebody happened to read it in.
    """
    seen: set[int] = set()
    previous: int | None = None
    for position, record in enumerate(records, start=1):
        stated = set(record)
        if stated != FROZEN_RECORD_FIELDS:
            missing = sorted(FROZEN_RECORD_FIELDS - stated)
            unexpected = sorted(stated - FROZEN_RECORD_FIELDS)
            raise BusinessMetricBindingError(
                f"frozen record {position} does not carry the fields of the "
                f"evaluation record contract (missing {missing!r}, unexpected "
                f"{unexpected!r}); refusing to derive a cohort binding from "
                "records of another contract"
            )
        identifier = record["opportunity_id"]
        if isinstance(identifier, bool) or not isinstance(identifier, int):
            raise BusinessMetricBindingError(
                f"frozen record {position} states opportunity_id "
                f"{identifier!r}, which is not an integer identity"
            )
        if identifier in seen:
            raise BusinessMetricBindingError(
                f"frozen record {position} repeats opportunity_id {identifier}; "
                "a cohort counts distinct postings, not lines"
            )
        seen.add(identifier)
        if previous is not None and identifier <= previous:
            raise BusinessMetricBindingError(
                f"frozen record {position} states opportunity_id {identifier} "
                f"after {previous}; the records are not in "
                f"{EVALUATION_CANONICAL_ORDER!r}, so nothing derived from their "
                "order is reproducible"
            )
        previous = identifier


def _recomputed_content_fingerprint(
    manifest: Mapping[str, Any], records: Sequence[Mapping[str, Any]]
) -> str:
    """Phase 10.1's digest, recomputed from what the manifest and records say.

    `manifest_fingerprint_domain` is **imported** rather than reimplemented: it
    is Phase 10.1's single definition of which manifest fields are semantic, and
    a second copy here would be free to drift from the snapshot it claims to
    verify. The record digests are taken over the canonical form of the parsed
    lines, which is the same domain `evaluation_record_fingerprint` covers
    because a line *is* `evaluation_record_payload`.
    """
    try:
        domain = manifest_fingerprint_domain(manifest)
    except EvaluationDatasetError as error:
        raise BusinessMetricBindingError(
            f"the evaluation manifest cannot be projected onto Phase 10.1's "
            f"fingerprint domain: {error}"
        ) from error
    domain["records"] = [
        hashlib.sha256(canonical_json(record).encode("utf-8")).hexdigest()
        for record in records
    ]
    return hashlib.sha256(canonical_json(domain).encode("utf-8")).hexdigest()


def build_frozen_cohort_binding(
    records: Sequence[Mapping[str, Any]],
    manifest: Mapping[str, Any],
) -> FrozenCohortBinding:
    """Verify a Phase 10.1 snapshot and derive the facts every later check needs.

    **The single frontier of this package**, and it does not take a manifest's
    word for anything. An earlier version read `content_fingerprint` as a fact;
    a manifest that declares its own digest declares nothing, so the digest is
    now recomputed from the records and the manifest through Phase 10.1's own
    `manifest_fingerprint_domain` and required to match. In order:

    1. the **schema version** must be one Phase 10.1 supports, through that
       package's own `require_supported_schema_version`;
    2. the **cohort rule** must be the one this build measures against — not
       only its version string, but the exact `criteria` in their contractual
       order and the canonical `ordering`. Two snapshots taken under different
       cohort rules are not comparable however identical their fields look, and
       the version is a label a writer chooses while the criteria are the rule;
    3. the **records must be records of this contract**: each carries exactly
       the fields of `EvaluationOpportunityRecord`, each `opportunity_id` is an
       `int` and not a `bool`, none repeats, and they ascend strictly — which is
       `EVALUATION_CANONICAL_ORDER`. This runs *before* the digest, because a
       valid SHA-256 over an invalid structure is not a valid frozen dataset:
       the digest establishes that the bytes are the bytes the manifest
       describes, and nothing whatever about whether they are this contract's
       records in this contract's order;
    4. the **content fingerprint** is recomputed from the records and the
       manifest and must equal what the manifest states;
    5. the **dataset id** must be the identifier Phase 10.1 derives from that
       digest (`<schema version>-<first 16 hex>`), so a manifest whose id and
       content were edited apart is refused;
    6. `record_count` must equal the records supplied;
    7. the **deduplication invariant** is established here, against the
       manifest's own `cohort.excluded_counts`, before any evidence exists:
       `D == excluded_counts["merged_duplicate"]`.

    The Phase 10.1 constants and the Phase 10.1 dataclass are the source of
    truth for (2) and (3): `EVALUATION_COHORT_CRITERIA`,
    `EVALUATION_CANONICAL_ORDER` and the fields of
    `EvaluationOpportunityRecord` are imported, never restated. A second copy
    here would be free to drift from the snapshot it claims to verify.

    Everything the binding carries is then *derived*: the cohort size, the
    profile identity from `profile_context`, the snapshot's timestamp and the
    as-of date that follows from it, the complete set of frozen action URLs, and
    the deduplication counts. No caller states any of them.

    There is no `verified=True` flag and no proof token. The verification is
    unavoidable because this function performs it, not because its result is
    hard to construct — and `validate_frozen_cohort_binding_structure` re-checks
    every derivation that can be re-checked without the records.

    It opens no file and no database, and it changes nothing in Phase 10.1: this
    is a derivation of that slice's output, not an addition to it.
    """
    if isinstance(records, (str, bytes)) or not isinstance(records, Sequence):
        raise BusinessMetricBindingError(
            f"the frozen records are not a sequence: {records!r}"
        )
    for position, record in enumerate(records, start=1):
        if not isinstance(record, Mapping):
            raise BusinessMetricBindingError(
                f"frozen record {position} is not a mapping: {record!r}"
            )
    if not records:
        raise BusinessMetricBindingError(
            "a frozen cohort holds at least one record; an empty snapshot cannot "
            "be the denominator of anything"
        )

    try:
        schema_version = require_supported_schema_version(manifest)
    except EvaluationDatasetError as error:
        raise BusinessMetricBindingError(str(error)) from error
    except AttributeError as error:
        raise BusinessMetricBindingError(
            f"the evaluation manifest is not a mapping: {manifest!r}"
        ) from error

    cohort_section = _manifest_section(manifest, "cohort")
    _require_cohort_rule(cohort_section)
    _require_frozen_record_structure(records)

    declared_fingerprint = validate_fingerprint(
        _manifest_value(manifest, "content_fingerprint"),
        subject="the manifest content_fingerprint",
    )
    recomputed = _recomputed_content_fingerprint(manifest, records)
    if recomputed != declared_fingerprint:
        raise BusinessMetricBindingError(
            f"the manifest claims content fingerprint {declared_fingerprint} and "
            f"its own fields and records digest to {recomputed}; refusing a "
            "snapshot that is not what it says it is"
        )

    dataset_id = validate_text(
        _manifest_value(manifest, "dataset_id"), subject="the manifest dataset_id"
    )
    expected_id = f"{schema_version}-{recomputed[:16]}"
    if dataset_id != expected_id:
        raise BusinessMetricBindingError(
            f"dataset_id {dataset_id!r} is not the identifier its content implies "
            f"({expected_id!r}); Phase 10.1 names a dataset after the digest of "
            "what it contains"
        )

    declared_count = validate_count(
        _manifest_value(manifest, "record_count"),
        subject="the manifest record_count",
        minimum=1,
    )
    cohort_size = len(records)
    if cohort_size != declared_count:
        raise BusinessMetricBindingError(
            f"the manifest states record_count {declared_count} and {cohort_size} "
            "records were supplied; refusing to derive a cohort binding from a "
            "snapshot whose two statements about its own size disagree"
        )

    profile_section = _manifest_section(manifest, "profile_context")
    profile_id = validate_profile_id(
        profile_section.get("profile_id"),
        subject="the manifest's profile_context.profile_id",
    )
    profile_fingerprint = validate_fingerprint(
        profile_section.get("fingerprint"),
        subject="the manifest's profile_context.fingerprint",
    )

    excluded_counts = cohort_section.get("excluded_counts")
    if not isinstance(excluded_counts, Mapping):
        raise BusinessMetricBindingError(
            f"the manifest's cohort.excluded_counts is not an object: "
            f"{excluded_counts!r}"
        )
    if MERGED_DUPLICATE_EXCLUSION_KEY not in excluded_counts:
        raise BusinessMetricBindingError(
            f"the manifest's cohort.excluded_counts states no "
            f"{MERGED_DUPLICATE_EXCLUSION_KEY!r}; refusing to assume it was zero, "
            "which is exactly the silent repair this contract forbids"
        )
    declared_merged = validate_count(
        excluded_counts[MERGED_DUPLICATE_EXCLUSION_KEY],
        subject=f"the manifest's {MERGED_DUPLICATE_EXCLUSION_KEY!r} exclusion count",
    )

    generated_at = validate_timestamp(
        _manifest_value(manifest, "generated_at"),
        subject="the manifest generated_at",
    )

    # The field is guaranteed present by `_require_frozen_record_structure`
    # above; what it holds is not, so its shape is still checked here.
    absorbed = 0
    with_duplicates = 0
    for position, record in enumerate(records, start=1):
        ids = record["absorbed_duplicate_ids"]
        if isinstance(ids, (str, bytes)) or not isinstance(ids, Sequence):
            raise BusinessMetricBindingError(
                f"frozen record {position} states absorbed_duplicate_ids {ids!r}, "
                "which is not a sequence of ids"
            )
        count = len(ids)
        absorbed += count
        if count:
            with_duplicates += 1

    draft = FrozenCohortBinding(
        binding_version=FROZEN_COHORT_BINDING_VERSION,
        dataset_id=dataset_id,
        dataset_fingerprint=recomputed,
        cohort_size=cohort_size,
        profile_id=profile_id,
        profile_fingerprint=profile_fingerprint,
        dataset_generated_at=generated_at,
        freshness_as_of_date=calendar_date_of(
            generated_at, subject="the manifest generated_at"
        ),
        expected_action_urls=expected_frozen_action_urls(records),
        absorbed_duplicate_total=absorbed,
        records_with_absorbed_duplicates=with_duplicates,
        declared_merged_duplicate_count=declared_merged,
        binding_fingerprint=PLACEHOLDER_FINGERPRINT,
    )
    # The dedup invariant is inside this validator, so it is established here —
    # against the manifest the cohort came from — rather than later, against a
    # mapping somebody passed beside it.
    validate_frozen_cohort_binding_structure(draft)
    return replace(
        draft, binding_fingerprint=frozen_cohort_binding_fingerprint(draft)
    )


# --------------------------------------------------------------------------
# the profile target binding
# --------------------------------------------------------------------------


def validate_target_rule_binding(
    binding: ProfileTargetBindingEvidence,
) -> ProfileTargetBindingEvidence:
    """The half of the target contract that needs production's vocabulary.

    * `rule_id` is one of `services.geography.profile_target.TARGET_RULE_IDS`;
    * a country is stated **iff** the rule is the restricted-country rule. That
      is the production resolver's own invariant, and a binding that stated `MA`
      under `mobility-open-v1` would have manufactured a restriction the person
      never wrote.
    """
    validate_profile_target_binding_structure(binding)
    if binding.rule_id not in TARGET_RULE_IDS:
        raise BusinessMetricBindingError(
            f"{binding.rule_id!r} is not a profile target rule of this build "
            f"({list(TARGET_RULE_IDS)})"
        )
    restricted = binding.rule_id == RESTRICTED_COUNTRY_RULE
    if restricted and binding.country_code is None:
        raise BusinessMetricBindingError(
            f"the target binding states rule {binding.rule_id} and no country; the "
            "restricted-country rule exists precisely to name one"
        )
    if not restricted and binding.country_code is not None:
        raise BusinessMetricBindingError(
            f"the target binding states rule {binding.rule_id} and country "
            f"{binding.country_code!r}; every rule but {RESTRICTED_COUNTRY_RULE} "
            "withholds the target, and a country stated under one of them would be "
            "a restriction nobody declared"
        )
    return binding


def build_profile_target_binding(
    connection, cohort_binding: FrozenCohortBinding
) -> ProfileTargetBindingEvidence:
    """Re-derive the profile's target country and bind it to a frozen snapshot.

    The one function in this package that touches the operational database, and
    it does so read-only, through production's own loaders, for the single value
    the frozen dataset cannot supply: Phase 10.1's manifest records the
    *fingerprint* of the profile context and deliberately does not republish the
    payload, so the target country a future metric needs exists only in the
    operational profile.

    **The profile is the cohort's, not the caller's.** An earlier signature took
    `profile_id` and `dataset_profile_fingerprint` as arguments, which meant a
    caller could ask for the target of a person the snapshot was not built for
    and get an artefact that named them. Both now come from the verified cohort
    binding, and there is nothing left to pass.

    In order: recompute the whole Phase 10.1 profile context fingerprint through
    `evaluation.dataset.profile_context`; require exact equality with the one the
    manifest froze; ask the production resolver `resolve_profile_target`;
    validate the rule/country pair against that resolver's own contract; seal.

    A profile edited since the snapshot **raises**. It does not produce an `N_A`:
    a target derived from a profile state the dataset was not built for is not
    missing evidence, it is the wrong question answered confidently.

    A coherent binding whose `country_code` is `None` is returned normally, and is
    what licenses `N_A / TARGET_COUNTRY_UNKNOWN` while making `COMPUTED`
    impossible.
    """
    verify_frozen_cohort_binding_fingerprint(cohort_binding)
    profile_id = cohort_binding.profile_id
    recomputed = profile_context_fingerprint(connection, profile_id)
    if recomputed != cohort_binding.profile_fingerprint:
        raise BusinessMetricBindingError(
            f"profile {profile_id} now canonicalizes to profile context "
            f"{recomputed} and the frozen dataset was built against "
            f"{cohort_binding.profile_fingerprint}; the profile has changed since "
            "the snapshot, so no target binding for that snapshot can be derived "
            "from it — this is an integrity failure and not an unavailable metric"
        )
    target = resolve_profile_target(connection, profile_id)
    country_code = getattr(target, "country_code", None)
    rule_id = getattr(target, "rule_id", None)
    if rule_id is None:
        raise BusinessMetricBindingError(
            f"the production target resolver returned {target!r}, which states no "
            "rule_id"
        )
    draft = ProfileTargetBindingEvidence(
        binding_version=PROFILE_TARGET_BINDING_VERSION,
        profile_id=profile_id,
        profile_fingerprint=cohort_binding.profile_fingerprint,
        country_code=country_code,
        rule_id=rule_id,
        binding_fingerprint=PLACEHOLDER_FINGERPRINT,
    )
    validate_target_rule_binding(draft)
    return replace(
        draft, binding_fingerprint=profile_target_binding_fingerprint(draft)
    )


# --------------------------------------------------------------------------
# the declared source universe
# --------------------------------------------------------------------------


def _jsonable(value: Any, *, path: str) -> Any:
    """A validated source map object as plain JSON-serializable values.

    Walks whatever the map's own validator produced — dataclasses, tuples, enums,
    scalars — so the digest covers **every** field it holds, not the six a
    coverage rate happens to read. A value this function cannot represent is
    refused rather than coerced with `str()`, which could otherwise put a memory
    address in a fingerprint and make it non-deterministic.
    """
    from dataclasses import fields, is_dataclass
    from enum import Enum

    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _jsonable(
                getattr(value, field.name), path=f"{path}.{field.name}"
            )
            for field in fields(value)
        }
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {
            str(key): _jsonable(item, path=f"{path}[{key!r}]")
            for key, item in value.items()
        }
    if isinstance(value, (tuple, list)):
        return [
            _jsonable(item, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise BusinessMetricBindingError(
        f"cannot canonicalize {path}: a source map value of type "
        f"{type(value).__name__} has no deterministic JSON form"
    )


def source_map_payload(source_map: Any) -> dict[str, Any]:
    """The **whole** validated source map, as the structure its digest covers.

    This is the answer to "`version: v1` can be amended in place". The typed
    `DeclaredSourceEntry` projection carries the six fields a coverage rate
    computes over; this carries everything the map actually says — every entry's
    homepage, its verification status, its notes, its live-canary flag, and the
    map's own header. An edit that leaves the version string alone still moves
    this digest, which is the only reason the binding can claim to identify an
    exact map.
    """
    return _jsonable(source_map, path="source_map")


def _build_declared_source_universe_evidence(
    *,
    map_name: str,
    map_version: str,
    scope_country: str,
    entries: Sequence[DeclaredSourceEntry],
    source_map_fingerprint: str,
    git_commit: str,
    provenance_path: str | None = None,
) -> DeclaredSourceUniverseEvidence:
    """Seal a declared source universe. **Private, and deliberately so.**

    `source_map_fingerprint` is the digest of the whole validated map. If this
    builder were public, a caller could state one — `"a" * 64` — and produce an
    artefact claiming to identify an exact map while identifying nothing. The
    only public path is `declared_source_universe_from_source_map`, which
    computes the digest from the map object itself; there is no way in from
    outside this module with a digest already in hand.

    Entries are validated *before* they are ordered: sorting a list that holds
    something without a `source_id` would raise an `AttributeError` or a
    `TypeError` out of a public function.
    """
    validated: list[DeclaredSourceEntry] = []
    for position, entry in enumerate(entries, start=1):
        if not isinstance(entry, DeclaredSourceEntry):
            raise BusinessMetricBindingError(
                f"declared source entry {position} is not a declared source entry: "
                f"{entry!r} ({type(entry).__name__})"
            )
        validate_text(
            entry.source_id, subject=f"the id of declared source entry {position}"
        )
        validated.append(entry)
    ordered = tuple(sorted(validated, key=lambda item: item.source_id))
    draft = DeclaredSourceUniverseEvidence(
        binding_version=DECLARED_SOURCE_UNIVERSE_VERSION,
        map_name=map_name,
        map_version=map_version,
        scope_country=scope_country,
        entries=ordered,
        source_map_fingerprint=source_map_fingerprint,
        git_commit=validate_git_commit(
            git_commit, subject="the declared source map git commit"
        ),
        content_fingerprint=PLACEHOLDER_FINGERPRINT,
        provenance_path=provenance_path,
    )
    validate_declared_source_universe_structure(draft)
    return replace(
        draft, content_fingerprint=declared_source_universe_fingerprint(draft)
    )


def declared_source_universe_from_source_map(
    source_map: Any,
    *,
    git_commit: str | None = None,
    provenance_path: str | None = None,
) -> DeclaredSourceUniverseEvidence:
    """The **only** public way to bind a declared source universe.

    Takes the *validated object*, never a path: loading and validating that YAML
    is `evaluation.morocco_pfe.validator`'s job — it owns that file's schema, its
    closed vocabularies and the check that an entry claiming to be an active
    collector names a real row of `config/sources.yaml`.

    The exact identity is computed here, from the whole object, by
    `source_map_payload`; a caller cannot state one. `git_commit` is required in
    v1 and validated as a full Git object name: `version: v1` names a file, not a
    revision of it, and this artefact is a file people edit in place.

    Nothing about Morocco is hardcoded: the scope country, the map's name and its
    version all come from the artefact.
    """
    if git_commit is None:
        raise BusinessMetricBindingError(
            "a declared source universe must name the git commit it was read at; "
            "the map is an authored file that is amended in place, so a binding "
            "without a revision identifies a filename rather than a state of it"
        )
    sources = getattr(source_map, "sources", None)
    if (
        sources is None
        or isinstance(sources, (str, bytes))
        or not isinstance(sources, Sequence)
    ):
        raise BusinessMetricBindingError(
            f"{source_map!r} is not a declared source map: it states no sequence of "
            "sources"
        )
    for name in ("map_name", "version", "scope_country"):
        if not hasattr(source_map, name):
            raise BusinessMetricBindingError(
                f"{source_map!r} is not a declared source map: it states no {name}"
            )
    entries: list[DeclaredSourceEntry] = []
    for position, entry in enumerate(sources, start=1):
        missing = [
            name
            for name in (
                "id",
                "integration_status",
                "coverage_role",
                "priority",
                "country",
                "production_source_id",
            )
            if not hasattr(entry, name)
        ]
        if missing:
            raise BusinessMetricBindingError(
                f"source map entry {position} is missing {', '.join(missing)}; it "
                "is not a declared source entry"
            )
        entries.append(
            DeclaredSourceEntry(
                source_id=entry.id,
                integration_status=entry.integration_status,
                coverage_role=entry.coverage_role,
                priority=entry.priority,
                country=entry.country,
                production_source_id=entry.production_source_id,
            )
        )
    exact = hashlib.sha256(
        canonical_json(source_map_payload(source_map)).encode("utf-8")
    ).hexdigest()
    return _build_declared_source_universe_evidence(
        map_name=source_map.map_name,
        map_version=source_map.version,
        scope_country=source_map.scope_country,
        entries=entries,
        source_map_fingerprint=exact,
        git_commit=git_commit,
        provenance_path=provenance_path,
    )


# --------------------------------------------------------------------------
# the URL audit
# --------------------------------------------------------------------------


def build_url_audit_binding(
    *,
    cohort_binding: FrozenCohortBinding,
    audit_started_at: str,
    observations: Sequence[UrlAuditObservation],
    audit_policy_version: str = URL_AUDIT_POLICY_VERSION,
) -> UrlAuditBinding:
    """Seal a URL audit campaign, or refuse one that is not well formed.

    Bound to the cohort rather than to two strings, and **checked complete
    against it here**: the declared universe size is the cohort's expected URL
    count and every eligible URL has an observation. There is no way to seal a
    partial audit and no argument that skips the check.

    `audit_started_at` is required: an audit in which every URL was withheld by
    robots has no per-observation timestamp anywhere and is still a temporal
    claim about a moment.

    Observations are type-checked before they are ordered, and are **not**
    deduplicated: two observations of one URL are a contradiction about that URL.

    This function performs no request. Phase 10.4a contains no HTTP client at
    all: it defines the artefact a later, separate slice will produce, under the
    policy `URL_AUDIT_POLICY_VERSION` names.
    """
    verify_frozen_cohort_binding_fingerprint(cohort_binding)
    validated: list[UrlAuditObservation] = []
    for position, observation in enumerate(observations, start=1):
        if not isinstance(observation, UrlAuditObservation):
            raise BusinessMetricBindingError(
                f"URL audit observation {position} is not a URL audit observation: "
                f"{observation!r} ({type(observation).__name__})"
            )
        validated.append(observation)
    ordered = tuple(sorted(validated, key=lambda item: item.requested_url))
    draft = UrlAuditBinding(
        binding_version=URL_AUDIT_BINDING_VERSION,
        protocol_version=FROZEN_ACTION_URL_PROTOCOL_VERSION,
        audit_policy_version=audit_policy_version,
        dataset_id=cohort_binding.dataset_id,
        dataset_fingerprint=cohort_binding.dataset_fingerprint,
        audit_started_at=audit_started_at,
        observations=ordered,
        declared_universe_size=len(ordered),
        binding_fingerprint=PLACEHOLDER_FINGERPRINT,
    )
    validate_url_audit_binding_structure(draft)
    assert_url_audit_covers(draft, cohort_binding)
    return replace(draft, binding_fingerprint=url_audit_binding_fingerprint(draft))


def assert_url_audit_covers(
    binding: UrlAuditBinding, cohort_binding: FrozenCohortBinding
) -> UrlAuditBinding:
    """Refuse a URL audit that is not exactly complete over the cohort.

    The check `URL_AUDITED_UNIVERSE` depends on, and it now takes the cohort
    binding rather than a caller-supplied URL list — there is no argument left
    through which the expected set could be narrowed to whatever the audit
    happened to hold.

    An audit that simply omits the URLs it could not reach is not a smaller
    audit, it is a wrong denominator, and every rate over it would be computed
    against the URLs that happened to answer. An unreached URL is represented
    rather than dropped: `attempted=False`, `INCONCLUSIVE`, and a reason specific
    enough to act on.

    Both directions are refused. An observation of a URL the cohort does not
    imply means the audit was run over some other snapshot or some other
    protocol, and its verdicts are about postings nobody asked about.
    """
    validate_url_audit_binding_structure(binding)
    validate_frozen_cohort_binding_structure(cohort_binding)
    if binding.dataset_id != cohort_binding.dataset_id:
        raise BusinessMetricBindingError(
            f"the URL audit was built over dataset {binding.dataset_id} and this "
            f"cohort is {cohort_binding.dataset_id}"
        )
    if binding.dataset_fingerprint != cohort_binding.dataset_fingerprint:
        raise BusinessMetricBindingError(
            f"the URL audit was built over content fingerprint "
            f"{binding.dataset_fingerprint} and this cohort is "
            f"{cohort_binding.dataset_fingerprint}"
        )
    expected = set(cohort_binding.expected_action_urls)
    observed = set(binding.requested_urls)
    missing = sorted(expected - observed)
    if missing:
        raise BusinessMetricBindingError(
            f"the URL audit omits {len(missing)} eligible URL(s) — "
            f"{missing[:5]}{'...' if len(missing) > 5 else ''}; a sealed audit "
            "holds one observation per eligible URL, and a URL nobody attempted is "
            "recorded as attempted=False / INCONCLUSIVE rather than left out"
        )
    unexpected = sorted(observed - expected)
    if unexpected:
        raise BusinessMetricBindingError(
            f"the URL audit observes {len(unexpected)} URL(s) the cohort does not "
            f"imply — {unexpected[:5]}{'...' if len(unexpected) > 5 else ''}; it "
            "was run over another snapshot or another protocol"
        )
    if binding.declared_universe_size != len(expected):
        raise BusinessMetricBindingError(
            f"the URL audit declares a universe of {binding.declared_universe_size} "
            f"and this cohort implies {len(expected)} unique action URLs"
        )
    return binding


# --------------------------------------------------------------------------
# deduplication evidence
# --------------------------------------------------------------------------


def build_dedup_evidence(cohort_binding: FrozenCohortBinding) -> DedupEvidence:
    """Seal deduplication evidence from the cohort, and from nothing else.

    **There is no `excluded_counts` argument.** An earlier signature took one,
    which meant a caller could satisfy the invariant `D == excluded_counts
    ["merged_duplicate"]` by passing `{"merged_duplicate": D}` — two
    "independent" statements that were one statement made twice. The invariant is
    now established in `build_frozen_cohort_binding`, against the manifest the
    cohort was derived from, before this artefact can exist; here the counts are
    copied from the verified binding.

    What remains a decision is whether to attach this evidence at all: a run
    without it licenses `DEDUP_EVIDENCE_MISSING`, and the two dedup metrics are
    `N_A` rather than quietly computed from the cohort binding everything else
    already uses.
    """
    verify_frozen_cohort_binding_fingerprint(cohort_binding)
    draft = DedupEvidence(
        evidence_version=DEDUP_EVIDENCE_VERSION,
        dataset_id=cohort_binding.dataset_id,
        dataset_fingerprint=cohort_binding.dataset_fingerprint,
        cohort_size=cohort_binding.cohort_size,
        absorbed_duplicate_total=cohort_binding.absorbed_duplicate_total,
        declared_merged_duplicate_count=(
            cohort_binding.declared_merged_duplicate_count
        ),
        records_with_absorbed_duplicates=(
            cohort_binding.records_with_absorbed_duplicates
        ),
        evidence_fingerprint=PLACEHOLDER_FINGERPRINT,
    )
    validate_dedup_evidence_structure(draft)
    return replace(draft, evidence_fingerprint=dedup_evidence_fingerprint(draft))


# --------------------------------------------------------------------------
# the freshness binding
# --------------------------------------------------------------------------


def build_freshness_binding(
    cohort_binding: FrozenCohortBinding,
    *,
    policy_version: str = EXPECTED_FRESHNESS_POLICY_VERSION,
) -> FreshnessBinding:
    """Derive the as-of date from the snapshot. **There is no date parameter.**

    An earlier version of this function took `as_of_date` from the caller, which
    meant the one value every freshness number depends on was whatever somebody
    typed. It is now `cohort_binding.freshness_as_of_date` — the calendar date of
    the snapshot's own `generated_at` — and the binding carries the cohort's
    digest so that a stored freshness binding can be held against its origin.

    No clock is read anywhere in this package.
    """
    verify_frozen_cohort_binding_fingerprint(cohort_binding)
    draft = FreshnessBinding(
        binding_version=FRESHNESS_BINDING_VERSION,
        as_of_date=cohort_binding.freshness_as_of_date,
        policy_version=policy_version,
        cohort_binding_fingerprint=cohort_binding.binding_fingerprint,
        binding_fingerprint=PLACEHOLDER_FINGERPRINT,
    )
    validate_freshness_binding_structure(draft)
    return replace(draft, binding_fingerprint=freshness_binding_fingerprint(draft))


# --------------------------------------------------------------------------
# the benchmark binding
# --------------------------------------------------------------------------


#: The fields a benchmark manifest states about itself, named once. These are
#: the *manifest contract's* names and carry nothing about any particular
#: benchmark: no country, no benchmark id, no phase. `current_rows` is the
#: manifest's own claim about how many rows it has, and it is the claim this
#: builder checks rather than the number it uses.
BENCHMARK_MANIFEST_NAME_KEY = "benchmark_name"
BENCHMARK_MANIFEST_VERSION_KEY = "version"
BENCHMARK_MANIFEST_COUNTRY_KEY = "scope_country"
BENCHMARK_MANIFEST_STATUS_KEY = "status"
BENCHMARK_MANIFEST_TARGET_ROWS_KEY = "target_minimum_rows"
BENCHMARK_MANIFEST_ROW_COUNT_KEY = "current_rows"
BENCHMARK_MANIFEST_READY_KEY = "evaluation_ready"


def _benchmark_manifest_value(manifest: Mapping[str, Any], key: str) -> Any:
    if not isinstance(manifest, Mapping):
        raise BusinessMetricBindingError(
            f"the benchmark manifest is not a mapping: {manifest!r}"
        )
    if key not in manifest:
        raise BusinessMetricBindingError(
            f"the benchmark manifest states no {key!r}; refusing to derive a "
            "benchmark binding from a manifest of another contract"
        )
    return manifest[key]


def benchmark_records_fingerprint(records: Sequence[Mapping[str, Any]]) -> str:
    """SHA-256 over the benchmark's real rows, in the order the file holds them.

    Order-sensitive on purpose: this digest identifies one exact benchmark file,
    which is what an evaluation-ready benchmark must be identified by. A recall
    is a fraction of a *specific* set of rows, and two files with the same rows
    in a different order are two files — whichever of them a later run read, the
    digest says which.

    Each row is digested on its own and the digests are then digested together,
    the same shape `_recomputed_content_fingerprint` uses for frozen records, so
    the domain is a stated list of row identities rather than one opaque blob.
    """
    digests: list[str] = []
    for position, row in enumerate(records, start=1):
        if not isinstance(row, Mapping):
            raise BusinessMetricBindingError(
                f"benchmark row {position} is not a mapping: {row!r}"
            )
        digests.append(hashlib.sha256(canonical_json(row).encode("utf-8")).hexdigest())
    return hashlib.sha256(canonical_json(digests).encode("utf-8")).hexdigest()


def build_benchmark_binding(
    manifest: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
    *,
    provenance_path: str | None = None,
) -> BenchmarkBinding:
    """Derive a benchmark binding from a real manifest and its real rows.

    **The rows are the evidence, and the count is derived from them.** An earlier
    version took `record_count`, `evaluation_ready` and `records_fingerprint` as
    separate keyword arguments, which meant a caller could seal a binding
    claiming sixty rows and a digest of nothing, with no rows anywhere near it.
    Everything a benchmark says about itself now comes from the manifest, and
    everything it *is* comes from the rows:

    * `record_count` is `len(records)` — never the manifest's `current_rows`,
      which is instead **compared** to it, so a manifest that has drifted from
      its own file is an integrity failure rather than the number used;
    * `records_fingerprint` is computed here from the rows themselves. There is
      no parameter for it: a caller-supplied digest would let a binding claim to
      identify an exact set of rows while identifying nothing;
    * `evaluation_ready`, `status`, `target_minimum_rows`, the name, the version
      and the scope country are read from the manifest, and readiness below the
      manifest's own declared target is refused by
      `validate_benchmark_binding_structure`.

    Nothing here knows which benchmark it is reading. There is no country, no
    benchmark id and no phase in this function: it is given a manifest and a set
    of rows and it derives what follows. With today's artefacts that is
    `morocco_pfe_gold_v1`, two rows, `DRAFT`, `evaluation_ready=False` — which is
    an ordinary state of the world and exactly what licenses
    `DECLARED_SOURCE_DISCOVERY_RECALL -> N_A /
    SOURCE_BENCHMARK_NOT_EVALUATION_READY`. No recall is computed here; there is
    no formula in this package to compute one with.
    """
    if isinstance(records, (str, bytes)) or not isinstance(records, Sequence):
        raise BusinessMetricBindingError(
            f"the benchmark rows are not a sequence: {records!r}"
        )
    record_count = len(records)
    declared_rows = validate_count(
        _benchmark_manifest_value(manifest, BENCHMARK_MANIFEST_ROW_COUNT_KEY),
        subject=f"the benchmark manifest's {BENCHMARK_MANIFEST_ROW_COUNT_KEY!r}",
    )
    if declared_rows != record_count:
        raise BusinessMetricBindingError(
            f"the benchmark manifest states {BENCHMARK_MANIFEST_ROW_COUNT_KEY} "
            f"{declared_rows} and {record_count} row(s) were supplied; refusing a "
            "benchmark whose manifest and whose rows disagree about what it "
            "contains"
        )
    evaluation_ready = _benchmark_manifest_value(
        manifest, BENCHMARK_MANIFEST_READY_KEY
    )
    if not isinstance(evaluation_ready, bool):
        raise BusinessMetricBindingError(
            f"the benchmark manifest's {BENCHMARK_MANIFEST_READY_KEY!r} is not a "
            f"boolean: {evaluation_ready!r}"
        )
    draft = BenchmarkBinding(
        binding_version=BENCHMARK_BINDING_VERSION,
        benchmark_name=validate_text(
            _benchmark_manifest_value(manifest, BENCHMARK_MANIFEST_NAME_KEY),
            subject="the benchmark name",
        ),
        benchmark_version=validate_text(
            _benchmark_manifest_value(manifest, BENCHMARK_MANIFEST_VERSION_KEY),
            subject="the benchmark version",
        ),
        scope_country=_benchmark_manifest_value(
            manifest, BENCHMARK_MANIFEST_COUNTRY_KEY
        ),
        status=validate_text(
            _benchmark_manifest_value(manifest, BENCHMARK_MANIFEST_STATUS_KEY),
            subject="the benchmark status",
        ),
        record_count=record_count,
        target_minimum_rows=validate_count(
            _benchmark_manifest_value(manifest, BENCHMARK_MANIFEST_TARGET_ROWS_KEY),
            subject=f"the benchmark {BENCHMARK_MANIFEST_TARGET_ROWS_KEY}",
            minimum=1,
        ),
        evaluation_ready=evaluation_ready,
        content_fingerprint=PLACEHOLDER_FINGERPRINT,
        records_fingerprint=benchmark_records_fingerprint(records),
        provenance_path=provenance_path,
    )
    validate_benchmark_binding_structure(draft)
    return replace(draft, content_fingerprint=benchmark_binding_fingerprint(draft))


# --------------------------------------------------------------------------
# the run context, and the two per-metric projections
# --------------------------------------------------------------------------


def build_business_metric_run_context(
    *,
    cohort_binding: FrozenCohortBinding,
    profile_target_binding: ProfileTargetBindingEvidence | None = None,
    declared_source_universe: DeclaredSourceUniverseEvidence | None = None,
    url_audit_binding: UrlAuditBinding | None = None,
    dedup_evidence: DedupEvidence | None = None,
    benchmark_binding: BenchmarkBinding | None = None,
    freshness_binding: FreshnessBinding | None = None,
    contract_version: str = BUSINESS_METRIC_CONTRACT_VERSION,
) -> BusinessMetricRunContext:
    """Assemble everything one run has to work with, and seal its own identity.

    The cohort binding is required, not optional: every universe check, every
    freshness date **and the profile identity** are derived from it. There is no
    `profile_id` or `profile_fingerprint` parameter — a run cannot be assembled
    for a person the snapshot was not frozen for, because there is nothing to
    pass.

    Every member's fingerprint is **recomputed** before the context is digested,
    and every member is re-established against the cohort — a URL audit is held
    complete against the expected URL set here as well as at every result, a
    freshness binding must carry the snapshot's own as-of date, and dedup
    evidence must count this cohort.

    The result is not any metric's scope, and the returned object's digest field
    is not called one.
    """
    require_supported_business_metric_contract_version(contract_version)
    verify_frozen_cohort_binding_fingerprint(cohort_binding)
    if profile_target_binding is not None:
        verify_profile_target_binding_fingerprint(profile_target_binding)
        validate_target_rule_binding(profile_target_binding)
    if declared_source_universe is not None:
        verify_declared_source_universe_fingerprint(declared_source_universe)
    if url_audit_binding is not None:
        verify_url_audit_binding_fingerprint(url_audit_binding)
        assert_url_audit_covers(url_audit_binding, cohort_binding)
    if dedup_evidence is not None:
        verify_dedup_evidence_fingerprint(dedup_evidence)
    if benchmark_binding is not None:
        verify_benchmark_binding_fingerprint(benchmark_binding)
    if freshness_binding is not None:
        verify_freshness_binding_fingerprint(freshness_binding)
    draft = BusinessMetricRunContext(
        run_context_schema_version=BUSINESS_METRIC_RUN_CONTEXT_SCHEMA_VERSION,
        contract_version=contract_version,
        frozen_cohort_binding=cohort_binding,
        run_context_fingerprint=PLACEHOLDER_FINGERPRINT,
        profile_target_binding=profile_target_binding,
        declared_source_universe=declared_source_universe,
        url_audit_binding=url_audit_binding,
        dedup_evidence=dedup_evidence,
        benchmark_binding=benchmark_binding,
        freshness_binding=freshness_binding,
    )
    validate_business_metric_run_context_structure(draft)
    return replace(
        draft, run_context_fingerprint=business_metric_run_context_fingerprint(draft)
    )


def verify_business_metric_run_context(context: BusinessMetricRunContext) -> str:
    """Re-establish a run context from itself, cohort completeness included.

    **There is no optional argument.** An earlier version took
    `expected_action_urls=None` and skipped the audit's completeness check when
    it was not supplied, which made the strongest guarantee in the contract
    opt-in. The expected set now comes from the context's own cohort binding, so
    the check runs whenever an audit is present and there is nothing to omit.
    """
    validate_business_metric_run_context_structure(context)
    cohort = context.frozen_cohort_binding
    verify_frozen_cohort_binding_fingerprint(cohort)
    if context.profile_target_binding is not None:
        verify_profile_target_binding_fingerprint(context.profile_target_binding)
        validate_target_rule_binding(context.profile_target_binding)
    if context.declared_source_universe is not None:
        verify_declared_source_universe_fingerprint(context.declared_source_universe)
    if context.url_audit_binding is not None:
        verify_url_audit_binding_fingerprint(context.url_audit_binding)
        assert_url_audit_covers(context.url_audit_binding, cohort)
    if context.dedup_evidence is not None:
        verify_dedup_evidence_fingerprint(context.dedup_evidence)
    if context.benchmark_binding is not None:
        verify_benchmark_binding_fingerprint(context.benchmark_binding)
    if context.freshness_binding is not None:
        verify_freshness_binding_fingerprint(context.freshness_binding)
    return verify_business_metric_run_context_fingerprint(context)


def _projected_members(
    context: BusinessMetricRunContext, key: BusinessMetricKey
) -> dict[str, Any]:
    """Exactly the optional members this metric's definition permits.

    The projection. Anything the metric does not use is left out — not merely
    unread, but absent from the artefact and therefore absent from its digest,
    which is what makes "an unused URL audit cannot move this number's identity"
    true by construction.
    """
    permitted = key.definition.permitted_members
    return {
        EVIDENCE_MEMBER_ATTRIBUTES[member]: member_value(context, member)
        for member in permitted
    }


def project_metric_scope(
    context: BusinessMetricRunContext,
    key: BusinessMetricKey,
    universe_kind: Any,
) -> BusinessMetricScope:
    """Which exact question this one metric asked, sealed with its own digest.

    `universe_kind` is passed explicitly rather than derived silently, and then
    held against `BUSINESS_METRIC_DEFINITIONS`: a result should *say* which closed
    set it is about, and a mismatch is a contract failure rather than a quietly
    corrected argument.
    """
    verify_business_metric_run_context_fingerprint(context)
    if not isinstance(key, BusinessMetricKey):
        raise BusinessMetricContractError(f"{key!r} is not a business metric key")
    require_metric_universe(key.metric, universe_kind)
    draft = BusinessMetricScope(
        scope_schema_version=BUSINESS_METRIC_SCOPE_SCHEMA_VERSION,
        contract_version=context.contract_version,
        key=key,
        universe_kind=universe_kind,
        frozen_cohort_binding=context.frozen_cohort_binding,
        scope_fingerprint=PLACEHOLDER_FINGERPRINT,
        **_projected_members(context, key),
    )
    validate_business_metric_scope_structure(draft)
    return replace(draft, scope_fingerprint=business_metric_scope_fingerprint(draft))


def project_metric_evidence(
    context: BusinessMetricRunContext, key: BusinessMetricKey
) -> BusinessMetricEvidenceView:
    """On which exact artefacts this one metric's answer rests.

    The evidence class comes from the metric's definition, never from a caller
    and never from the run: that is the fix for the failure where a
    `DATA_AI_RATE` computed beside a source-map coverage inherited
    `DECLARED_UNIVERSE_DESCRIPTIVE`.

    The view carries no value, no status and no fraction. It cannot: there is no
    parameter for one.
    """
    verify_business_metric_run_context_fingerprint(context)
    if not isinstance(key, BusinessMetricKey):
        raise BusinessMetricContractError(f"{key!r} is not a business metric key")
    definition = metric_definition(key.metric)
    draft = BusinessMetricEvidenceView(
        evidence_view_schema_version=BUSINESS_EVIDENCE_VIEW_SCHEMA_VERSION,
        evidence_class=definition.evidence_class,
        frozen_cohort_binding=context.frozen_cohort_binding,
        evidence_fingerprint=PLACEHOLDER_FINGERPRINT,
        **_projected_members(context, key),
    )
    validate_business_metric_evidence_structure(draft)
    return replace(
        draft, evidence_fingerprint=business_metric_evidence_fingerprint(draft)
    )


# --------------------------------------------------------------------------
# the evidence class gate
# --------------------------------------------------------------------------


def require_business_evidence_class(
    key: BusinessMetricKey,
    evidence: BusinessMetricEvidenceView,
    *,
    status: BusinessMetricStatus,
) -> BusinessEvidenceClass:
    """Refuse an evidence class the evidence in hand cannot support.

    The class is the metric's, from `BUSINESS_METRIC_DEFINITIONS`, so there is
    nothing for a caller to claim; what this decides is whether the artefacts
    actually projected *support* it.

    The gate is **status-aware**, and that is not a loophole:

    * a `COMPUTED` result must carry every member its metric requires, and an
      `EVALUATION_READY_BENCHMARK` claim additionally needs a benchmark that says
      it is ready, identifies its rows by digest, and is scoped to the same
      country as the declared universe it is a recall over;
    * an `N_A` result may be missing a required member. That absence is the
      reason it is `N_A`, and refusing to represent it would leave the metric
      with no honest state at all. Whatever *is* present is still checked.
    """
    if not isinstance(key, BusinessMetricKey):
        raise BusinessMetricContractError(f"{key!r} is not a business metric key")
    validate_business_metric_evidence_structure(evidence)
    if not isinstance(status, BusinessMetricStatus):
        raise BusinessMetricContractError(f"{status!r} is not a business metric status")
    definition = metric_definition(key.metric)
    if evidence.evidence_class is not definition.evidence_class:
        raise BusinessEvidenceClassError(
            f"the evidence view claims class {evidence.evidence_class} and "
            f"{key.metric} is defined as {definition.evidence_class}; an evidence "
            "class is a property of the metric, not of the report it appears in"
        )
    for member in BusinessEvidenceMember:
        if (
            member_value(evidence, member) is not None
            and member not in definition.permitted_members
        ):
            raise BusinessEvidenceClassError(
                f"the evidence view of {key.metric} carries {member}, which that "
                "metric does not use; a number partly identified by evidence it "
                "never reads is not the number it claims to be"
            )
    _require_present_members_are_sound(evidence)
    if status is BusinessMetricStatus.N_A:
        return definition.evidence_class
    missing = sorted(
        str(member)
        for member in definition.required_members
        if member_value(evidence, member) is None
    )
    if missing:
        raise BusinessEvidenceClassError(
            f"{key.metric} is COMPUTED and its evidence carries no "
            f"{', '.join(missing)}; the contract requires "
            f"{sorted(str(item) for item in definition.required_members)} for a "
            "number to exist at all"
        )
    if definition.evidence_class is BusinessEvidenceClass.EVALUATION_READY_BENCHMARK:
        benchmark = evidence.benchmark_binding
        if not benchmark.evaluation_ready:
            raise BusinessEvidenceClassError(
                f"benchmark {benchmark.benchmark_name} "
                f"{benchmark.benchmark_version} records evaluation_ready=False "
                f"({benchmark.record_count} rows against its declared target of "
                f"{benchmark.target_minimum_rows}), so it cannot support a "
                f"{definition.evidence_class} claim; {key.metric} is "
                f"{BusinessMetricStatus.N_A} / "
                f"{BusinessMetricUnavailableReason.SOURCE_BENCHMARK_NOT_EVALUATION_READY}"
            )
        if benchmark.records_fingerprint is None:
            raise BusinessEvidenceClassError(
                f"benchmark {benchmark.benchmark_name} is evaluation-ready and "
                "identifies no rows; a recall is a fraction of a specific set of "
                "rows, so the class needs their digest"
            )
    return definition.evidence_class


def _require_present_members_are_sound(
    evidence: BusinessMetricEvidenceView,
) -> None:
    """A member that *is* attached must be valid, whatever the status.

    Plus the one cross-member agreement this contract requires: a recall over a
    declared universe, measured against a benchmark, needs both to be about the
    same country. A Moroccan source map beside a benchmark of French postings
    would produce a recall whose numerator and denominator describe two different
    markets — arithmetically fine, and about nothing.
    """
    if evidence.profile_target_binding is not None:
        validate_target_rule_binding(evidence.profile_target_binding)
    if evidence.benchmark_binding is not None:
        validate_benchmark_binding_structure(evidence.benchmark_binding)
    if evidence.url_audit_binding is not None:
        validate_url_audit_binding_structure(evidence.url_audit_binding)
    if evidence.declared_source_universe is not None:
        validate_declared_source_universe_structure(evidence.declared_source_universe)
    if evidence.dedup_evidence is not None:
        validate_dedup_evidence_structure(evidence.dedup_evidence)
    if evidence.freshness_binding is not None:
        validate_freshness_binding_structure(evidence.freshness_binding)
    universe = evidence.declared_source_universe
    benchmark = evidence.benchmark_binding
    if universe is not None and benchmark is not None:
        if universe.scope_country != benchmark.scope_country:
            raise BusinessMetricBindingError(
                f"the declared source universe is scoped to "
                f"{universe.scope_country} and the benchmark to "
                f"{benchmark.scope_country}; a recall whose numerator and "
                "denominator describe two different markets is a number about "
                "nothing"
            )


# --------------------------------------------------------------------------
# scope / evidence agreement
# --------------------------------------------------------------------------


def verify_metric_scope_and_evidence_agree(
    scope: BusinessMetricScope, evidence: BusinessMetricEvidenceView
) -> None:
    """Require the question and the proof to name the **same** artefacts.

    Member by member, by fingerprint, and the cohort binding first. A scope
    parameterised by source map A beside an evidence view holding source map B is
    refused, and so are audit A/B and as-of-date A/B.

    Projected artefacts agree by construction. This exists for the artefact
    somebody reconstructed, stored and re-read — which is every artefact, the
    second time anybody looks at it.
    """
    validate_business_metric_scope_structure(scope)
    validate_business_metric_evidence_structure(evidence)
    if (
        scope.frozen_cohort_binding.binding_fingerprint
        != evidence.frozen_cohort_binding.binding_fingerprint
    ):
        raise BusinessMetricBindingError(
            f"the scope asks about cohort "
            f"{scope.frozen_cohort_binding.binding_fingerprint} and the evidence "
            f"rests on {evidence.frozen_cohort_binding.binding_fingerprint}"
        )
    definition = scope.key.definition
    if evidence.evidence_class is not definition.evidence_class:
        raise BusinessMetricBindingError(
            f"the evidence view claims class {evidence.evidence_class} and "
            f"{scope.key.metric} is defined as {definition.evidence_class}"
        )
    for member in BusinessEvidenceMember:
        in_scope = member_fingerprint(member_value(scope, member), member)
        in_evidence = member_fingerprint(member_value(evidence, member), member)
        if in_scope == in_evidence:
            continue
        raise BusinessMetricBindingError(
            f"the scope of {scope.key.metric} is bound to {member} "
            f"{in_scope or 'nothing'} and its evidence rests on "
            f"{in_evidence or 'nothing'}; a question and a proof that name "
            "different artefacts describe two different worlds"
        )


# --------------------------------------------------------------------------
# universe sizes, and the conditions each refusal asserts
# --------------------------------------------------------------------------


def _universe_size_of(
    universe: BusinessMetricUniverse, evidence: BusinessMetricEvidenceView
) -> int | None:
    """The real size of this universe, or `None` when its artefact is absent."""
    if universe is BusinessMetricUniverse.FROZEN_COHORT:
        return evidence.frozen_cohort_binding.cohort_size
    if universe is BusinessMetricUniverse.PRE_DEDUP_REPRESENTED_UNIVERSE:
        dedup = evidence.dedup_evidence
        return None if dedup is None else dedup.represented_universe_size
    if universe is BusinessMetricUniverse.URL_AUDITED_UNIVERSE:
        audit = evidence.url_audit_binding
        return None if audit is None else audit.declared_universe_size
    declared = evidence.declared_source_universe
    return None if declared is None else len(declared.entries)


def _require_universe_size(
    key: BusinessMetricKey,
    universe: BusinessMetricUniverse,
    status: BusinessMetricStatus,
    support: BusinessMetricSupport,
    evidence: BusinessMetricEvidenceView,
) -> None:
    """Hold `support.universe_size` against the artefact that defines the universe.

    The check an earlier version of this contract left out: it required the field
    to *exist* and never asked whether it was true. A `FROZEN_COHORT` rate over
    "394" when the cohort holds 380 is a number whose denominator nobody
    recognises, and it would have sealed cleanly.

    Determinability is the one exception, and it is narrow: when the artefact that
    defines the universe is absent — which only an `N_A` may be — there is no size
    to state, and `None` is the honest value. A zero would be the exact failure
    this contract exists to prevent. The frozen cohort is always determinable,
    because the cohort binding is mandatory.
    """
    actual = _universe_size_of(universe, evidence)
    if actual is None:
        if status is BusinessMetricStatus.COMPUTED:
            raise BusinessMetricBindingError(
                f"{key.metric} is COMPUTED over {universe} and the artefact that "
                "defines that universe is not in its evidence"
            )
        if support.universe_size is not None:
            raise BusinessMetricBindingError(
                f"{key.metric} is N_A over {universe}, whose defining artefact is "
                f"absent, and states universe_size {support.universe_size!r}; a "
                "size nothing could have counted is not a fact"
            )
        return
    if support.universe_size is None:
        raise BusinessMetricContractError(
            f"{key.metric} states no universe_size and {universe} holds {actual} "
            "member(s) in this evidence; an unavailable metric still says how big "
            "the question was"
        )
    if support.universe_size != actual:
        raise BusinessMetricBindingError(
            f"{key.metric} states a universe of {support.universe_size} and "
            f"{universe} holds {actual} member(s) in this evidence; a rate whose "
            "denominator is not the size of its own universe is a number about "
            "nothing in particular"
        )


def _legitimate_reasons(
    key: BusinessMetricKey, evidence: BusinessMetricEvidenceView
) -> frozenset[BusinessMetricUnavailableReason]:
    """Exactly the refusals this metric may state **given this evidence**.

    The condition half of the availability contract. `permitted_reasons` says
    which codes the metric owns; this says which of them are true right now:

    * a metric that cannot be computed under the current dataset contract at all
      may state only that;
    * a required member that is absent licenses its own absence reason, and
      nothing else — "the audit is missing" is not "the audit concluded nothing";
    * with every member present, only a *condition* can license a refusal: a
      target binding that names no country, an audit in which nothing was
      conclusive, a benchmark that is not evaluation-ready, and — the one this
      contract cannot check because it is a fact about records it does not hold —
      a freshness mean with no valid publication date.
    """
    definition = metric_definition(key.metric)
    if key.metric in ALWAYS_UNAVAILABLE_METRICS:
        return frozenset({ALWAYS_UNAVAILABLE_METRICS[key.metric]})
    absent = {
        member
        for member in definition.required_members
        if member_value(evidence, member) is None
    }
    if absent:
        return frozenset(MEMBER_ABSENCE_REASONS[member] for member in absent)
    legitimate: set[BusinessMetricUnavailableReason] = set()
    if key.metric is BusinessMetricName.TARGET_COUNTRY_MATCH_RATE:
        if evidence.profile_target_binding.country_code is None:
            legitimate.add(BusinessMetricUnavailableReason.TARGET_COUNTRY_UNKNOWN)
    if key.metric is BusinessMetricName.BROKEN_URL_RATE:
        if evidence.url_audit_binding.conclusive_count == 0:
            legitimate.add(BusinessMetricUnavailableReason.NO_CONCLUSIVE_URL_AUDIT)
    if key.metric is BusinessMetricName.DECLARED_SOURCE_DISCOVERY_RECALL:
        if not evidence.benchmark_binding.evaluation_ready:
            legitimate.add(
                BusinessMetricUnavailableReason.SOURCE_BENCHMARK_NOT_EVALUATION_READY
            )
    if key.metric is BusinessMetricName.MEAN_KNOWN_FRESHNESS_SCORE:
        # The one condition no binding can verify — whether any record carries a
        # usable `published_at` is a fact about the records, which this slice
        # does not hold — so it is the one that must be *witnessed*. The reason
        # is legitimate here only when the result states a
        # `FreshnessAvailabilityWitness` of zero, which
        # `BusinessMetricResult._check_freshness_witness` requires; this set
        # merely stops the reason being unavailable for the wrong metric.
        legitimate.add(BusinessMetricUnavailableReason.NO_VALID_PUBLICATION_DATES)
    return frozenset(legitimate)


def _require_reason_conditions(
    key: BusinessMetricKey,
    status: BusinessMetricStatus,
    reason: BusinessMetricUnavailableReason | None,
    support: BusinessMetricSupport,
    evidence: BusinessMetricEvidenceView,
) -> None:
    """Refuse a refusal whose condition does not hold, and a number that cannot exist.

    Every condition here is checked against artefacts this package holds, with
    one exception that is checked against a witness instead:
    `NO_VALID_PUBLICATION_DATES` asserts something about the records, so the
    result must carry a `FreshnessAvailabilityWitness` of zero — enforced in
    `BusinessMetricResult`, and required again here so the builder refuses before
    the dataclass is even constructed.
    """
    definition = metric_definition(key.metric)
    legitimate = _legitimate_reasons(key, evidence)
    if (
        status is BusinessMetricStatus.N_A
        and reason is BusinessMetricUnavailableReason.NO_VALID_PUBLICATION_DATES
    ):
        witness = support.freshness_availability_witness
        if witness is None or witness.known_date_count != 0:
            raise BusinessMetricContractError(
                f"{key.metric} states {reason} and offers no witness of zero "
                "usable publication dates; this is the one refusal no binding can "
                "verify, so the contract requires the count it asserts"
            )
    if status is BusinessMetricStatus.N_A:
        if reason is None:
            raise BusinessMetricContractError(
                f"{key.metric} is N_A without a stated reason code"
            )
        require_permitted_reason(key.metric, reason)
        if reason not in legitimate:
            honest = sorted(str(item) for item in legitimate)
            available = (
                str(honest)
                if honest
                else "none — with this evidence the metric is computable"
            )
            raise BusinessMetricContractError(
                f"{key.metric} states {reason} and that condition does not hold "
                f"for this evidence; the refusals it could honestly state here "
                f"are {available}"
            )
        return
    if key.metric in ALWAYS_UNAVAILABLE_METRICS:
        raise BusinessMetricContractError(
            f"{key.metric} cannot be COMPUTED under this dataset contract; it is "
            f"{BusinessMetricStatus.N_A} / {ALWAYS_UNAVAILABLE_METRICS[key.metric]}"
        )
    if key.metric is BusinessMetricName.TARGET_COUNTRY_MATCH_RATE:
        if evidence.profile_target_binding.country_code is None:
            raise BusinessMetricContractError(
                f"{key.metric} is COMPUTED and its target binding names no country "
                f"(rule {evidence.profile_target_binding.rule_id}); a match rate "
                "against an unknown target would read as 'none of these postings "
                f"is where they want to work'. It is {BusinessMetricStatus.N_A} / "
                f"{BusinessMetricUnavailableReason.TARGET_COUNTRY_UNKNOWN}"
            )
    if key.metric is BusinessMetricName.BROKEN_URL_RATE:
        if evidence.url_audit_binding.conclusive_count == 0:
            raise BusinessMetricContractError(
                f"{key.metric} is COMPUTED over an audit in which no observation "
                "was conclusive; the rate would be 0.0 by construction and would "
                "report 'nothing is broken' on the strength of having established "
                f"nothing. It is {BusinessMetricStatus.N_A} / "
                f"{BusinessMetricUnavailableReason.NO_CONCLUSIVE_URL_AUDIT}"
            )
    # A required member absent under COMPUTED is refused by the evidence class
    # gate; this states the same rule in this function's own terms so a reader of
    # either sees the whole contract.
    for member in definition.required_members:
        if member_value(evidence, member) is None:
            raise BusinessMetricContractError(
                f"{key.metric} is COMPUTED without {member}"
            )


# --------------------------------------------------------------------------
# results, and the keys of a run
# --------------------------------------------------------------------------


def assert_unique_business_metric_keys(
    keys: Sequence[BusinessMetricKey],
) -> tuple[BusinessMetricKey, ...]:
    """Refuse a run that states one metric key twice, in canonical order.

    Two results for one key are not two measurements: one of them is wrong, and
    nothing downstream can tell which.
    """
    ordered: list[BusinessMetricKey] = []
    seen: set[tuple[str, str, str]] = set()
    repeated: set[tuple[str, str, str]] = set()
    for key in keys:
        if not isinstance(key, BusinessMetricKey):
            raise BusinessMetricContractError(f"{key!r} is not a business metric key")
        identity = business_metric_key_sort_key(key)
        if identity in seen:
            repeated.add(identity)
        seen.add(identity)
        ordered.append(key)
    if repeated:
        raise BusinessMetricBindingError(
            f"the run states these metric keys more than once: {sorted(repeated)}; "
            "one key carries one result"
        )
    return tuple(sorted(ordered, key=business_metric_key_sort_key))


def _establish_result(
    key: BusinessMetricKey,
    status: BusinessMetricStatus,
    universe_kind: BusinessMetricUniverse,
    support: BusinessMetricSupport,
    reason: BusinessMetricUnavailableReason | None,
    scope: BusinessMetricScope,
    evidence: BusinessMetricEvidenceView,
) -> BusinessEvidenceClass:
    """Everything a result must satisfy beyond its own dataclass invariants.

    Applied identically by `_build_business_metric_result` and
    `verify_business_metric_result`, so an artefact this package sealed and one
    somebody reconstructed are held to one contract:

    * the scope and the evidence name the same artefacts;
    * the evidence class is the metric's, and the evidence supports it;
    * a `URL_AUDITED_UNIVERSE` metric's audit is **complete** against the cohort;
    * the universe size is the real size of that universe;
    * the refusal's condition holds, or the number is one that may exist.
    """
    verify_metric_scope_and_evidence_agree(scope, evidence)
    evidence_class = require_business_evidence_class(key, evidence, status=status)
    if (
        universe_kind is BusinessMetricUniverse.URL_AUDITED_UNIVERSE
        and evidence.url_audit_binding is not None
    ):
        # Not optional, and not conditional on a caller remembering to ask: a
        # rate over the audited universe is a rate over *every* eligible URL.
        assert_url_audit_covers(
            evidence.url_audit_binding, evidence.frozen_cohort_binding
        )
    _require_universe_size(key, universe_kind, status, support, evidence)
    _require_reason_conditions(key, status, reason, support, evidence)
    return evidence_class


def _build_business_metric_result(
    *,
    key: BusinessMetricKey,
    status: BusinessMetricStatus,
    universe_kind: Any,
    context: BusinessMetricRunContext,
    support: BusinessMetricSupport | None = None,
    value: float | None = None,
    reason: BusinessMetricUnavailableReason | None = None,
) -> BusinessMetricResult:
    """Assemble one answer, or refuse to — and compute nothing. **Private.**

    **This function does no arithmetic**, and it does not check any. `value` is
    a number produced elsewhere; the job here is to refuse a result the bindings
    do not support: the metric/universe pair, the recomputed context, the two
    projections and their agreement, the evidence class, the audit's
    completeness, the universe's real size, the refusal's condition, and the
    status invariants of `BusinessMetricResult`. Whether the number is the right
    answer to the metric's question is a property of a formula, and Phase 10.4a
    contains no formula to have that property.

    It is private for exactly that reason. Exported, it would be a public way to
    seal an arbitrary float as a measured rate with a valid digest and a
    complete evidence trail behind it — every structural guarantee this package
    offers, wrapped around a number nobody derived. Phase 10.4b will own the
    public surface that produces COMPUTED results, and will reach the assembly
    through this module.

    The result's own fingerprint is computed last, from the canonical semantic
    projection, so it can only ever describe a scope and an evidence view that
    already agree.
    """
    if not isinstance(key, BusinessMetricKey):
        raise BusinessMetricContractError(f"{key!r} is not a business metric key")
    if not isinstance(status, BusinessMetricStatus):
        raise BusinessMetricContractError(f"{status!r} is not a business metric status")
    require_metric_universe(key.metric, universe_kind)
    verify_business_metric_run_context(context)
    resolved_support = BusinessMetricSupport() if support is None else support
    if not isinstance(resolved_support, BusinessMetricSupport):
        raise BusinessMetricContractError(
            f"{support!r} is not a business metric support block"
        )
    scope = project_metric_scope(context, key, universe_kind)
    evidence = project_metric_evidence(context, key)
    evidence_class = _establish_result(
        key, status, universe_kind, resolved_support, reason, scope, evidence
    )
    draft = BusinessMetricResult(
        key=key,
        status=status,
        universe_kind=universe_kind,
        evidence_class=evidence_class,
        support=resolved_support,
        scope_fingerprint=scope.scope_fingerprint,
        evidence_fingerprint=evidence.evidence_fingerprint,
        result_fingerprint=PLACEHOLDER_FINGERPRINT,
        contract_version=context.contract_version,
        value=value,
        reason=reason,
    )
    validate_business_metric_result_structure(draft)
    return replace(draft, result_fingerprint=business_metric_result_fingerprint(draft))


def verify_business_metric_result(
    result: BusinessMetricResult,
    *,
    context: BusinessMetricRunContext,
) -> BusinessMetricResult:
    """Re-establish one result against the context it claims to come from.

    Re-projects the scope and the evidence view this metric *should* have had
    from that context, requires the result's two fingerprints to be exactly
    those, and then applies every check the assembly applied — the audit's
    completeness and the universe's real size included. A stored result over a
    partial audit, or with a denominator nobody counted, is refused here however
    valid its own digest is.

    **What this does not verify is the arithmetic.** It re-establishes that the
    number is *structurally* supportable — stated over the right universe, on
    evidence of the right class, with a support block that adds up against
    itself — and not that it is the value the metric's formula yields over those
    frozen records. There is no formula in this package to check it against;
    that belongs to Phase 10.4b, which will recompute rather than re-read.
    """
    validate_business_metric_result_structure(result)
    verify_business_metric_result_fingerprint(result)
    verify_business_metric_run_context(context)
    scope = project_metric_scope(context, result.key, result.universe_kind)
    evidence = project_metric_evidence(context, result.key)
    verify_business_metric_scope_fingerprint(scope)
    verify_business_metric_evidence_fingerprint(evidence)
    if result.scope_fingerprint != scope.scope_fingerprint:
        raise BusinessMetricBindingError(
            f"{result.key.metric} states scope {result.scope_fingerprint} and this "
            f"context asks {scope.scope_fingerprint}"
        )
    if result.evidence_fingerprint != evidence.evidence_fingerprint:
        raise BusinessMetricBindingError(
            f"{result.key.metric} rests on evidence {result.evidence_fingerprint} "
            f"and this context offers {evidence.evidence_fingerprint}"
        )
    _establish_result(
        result.key,
        result.status,
        result.universe_kind,
        result.support,
        result.reason,
        scope,
        evidence,
    )
    return result


def verify_business_metric_results(
    results: Sequence[BusinessMetricResult],
    *,
    context: BusinessMetricRunContext,
) -> tuple[BusinessMetricResult, ...]:
    """Re-establish a whole run of results, and return them in canonical order.

    A run is a set of answers drawn from **one** body of assembled evidence, so
    this checks what no single result can: the context, no duplicate key, and
    every result re-established against that context.

    Results are deliberately **not** required to share a scope fingerprint, an
    evidence fingerprint or an evidence class: a run legitimately holds a
    `SNAPSHOT_DESCRIPTIVE` `DATA_AI_RATE` beside a `DECLARED_UNIVERSE_DESCRIPTIVE`
    source coverage and an `EXTERNAL_AUDIT_DESCRIPTIVE` broken-URL rate, and each
    keeps its own identity. What binds them together is this context.
    """
    verify_business_metric_run_context(context)
    ordered: list[BusinessMetricResult] = []
    for result in results:
        if not isinstance(result, BusinessMetricResult):
            raise BusinessMetricContractError(
                f"{result!r} is not a business metric result"
            )
        ordered.append(result)
    assert_unique_business_metric_keys([item.key for item in ordered])
    for result in ordered:
        verify_business_metric_result(result, context=context)
    return tuple(
        sorted(ordered, key=lambda item: business_metric_key_sort_key(item.key))
    )
