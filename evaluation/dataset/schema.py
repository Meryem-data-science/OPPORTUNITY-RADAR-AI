"""The versioned evaluation dataset contract of Phase 10.1.

This module is the *contract* and nothing else: the vocabulary a frozen
evaluation snapshot is written in, and the one canonical payload function every
consumer of that snapshot reads it through. It executes no SQL, opens no file
and computes no digest.

Three rules shape everything below.

* **Nothing is invented.** Every field here is a column that already exists in
  the operational schema or a value an already-shipped read model already
  returns. Where the pipeline has not produced something — an opportunity the
  Matching run never assessed, a fine classification that never ran, a posting
  the Data/AI classifier has not read yet — the field is `None` and stays
  `None`. `None` means *not stated*; it is never rewritten into a `False`, a
  `0.0`, an empty list, an empty object or a negative verdict.
* **UNKNOWN is a value, not an absence.** `UNCERTAIN` qualification, `UNKNOWN`
  eligibility, the `UNCERTAIN` matching lane and the `UNCERTAIN` recommendation
  disposition are carried through verbatim. Phase 10 exists to measure how often
  the pipeline is right; folding its own hedges into refusals would delete the
  most interesting half of that question before it is asked.
* **The payload written is the payload fingerprinted.** `evaluation_record_payload`
  below is used both to serialize a record to JSONL and to compute its digest,
  so "what the file says" and "what the fingerprint covers" cannot drift apart.

Direction of dependency, which is the whole architectural point of this slice:

    operational SQLite / existing pipeline outputs
        -> evaluation snapshot (this package)
            -> later Phase 10 slices

and never the reverse. No production module imports anything from
`evaluation.dataset`, and nothing here reads a human label, a benchmark result
or any other evaluation artefact: none exists in this slice, by design.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


#: The dataset contract's own version. It moves whenever the *shape* of a
#: manifest or a record changes — a field added, removed or re-interpreted — so
#: that a reader can refuse a snapshot it does not understand instead of
#: silently reading a missing key as a missing fact.
EVALUATION_DATASET_SCHEMA_VERSION = "evaluation-dataset-v3"

#: The version of the cohort *rule* — which opportunities a snapshot contains.
#: Separate from the schema version on purpose: widening or narrowing the
#: universe changes what a measurement means without changing a single field
#: name, and two datasets taken under different cohort rules are not comparable
#: however identical their records look.
#:
#: `v2` widened `v1` from the Data/AI-qualified cohort to every active,
#: non-duplicate posting. `v1` filtered on `qualification`, which is a
#: *prediction* of the Data/AI classifier and not a human truth: a real Data/AI
#: opportunity misclassified `OUT_OF_SCOPE`, or one collected but not yet
#: classified at all, was absent from the snapshot, so that classifier error —
#: the earliest and most consequential false negative the pipeline can make —
#: was undetectable by every later Phase 10 slice. The universe is therefore
#: defined by what the *collector* found and deduplicated, not by what a
#: downstream model predicted about it.
EVALUATION_COHORT_VERSION = "evaluation-cohort-v2"

#: The canonical order of the records inside a dataset, stated once so the
#: manifest can carry it verbatim. `opportunities.id` is an INTEGER PRIMARY KEY,
#: so ordering by it is already a *total* order: there is no tie to break, and
#: no statement's position depends on how SQLite happened to return rows.
EVALUATION_CANONICAL_ORDER = "opportunity_id ASC"

#: Why this cohort and not the recommendation output — see `cohort.py` for the
#: SQL that implements it. Carried in the manifest so a dataset explains its own
#: universe without a reader having to find this file.
EVALUATION_COHORT_CRITERIA: tuple[str, ...] = (
    "opportunities.is_active = 1",
    "opportunities.status != 'merged_duplicate'",
)


class EvaluationDatasetError(RuntimeError):
    """Raised when an evaluation dataset cannot be built or read safely."""


def require_supported_schema_version(payload: Mapping[str, Any]) -> str:
    """Refuse a manifest this build cannot read, loudly and at the boundary.

    A future slice will point at a directory somebody else produced. A snapshot
    written under a different contract may well *parse* — JSON always parses —
    and then be read field by field as though its absent keys were absent facts.
    That is the failure this function exists to make impossible: an unsupported
    or missing `schema_version` is an error here, not a shrug three layers down.
    """
    version = payload.get("schema_version")
    if version != EVALUATION_DATASET_SCHEMA_VERSION:
        raise EvaluationDatasetError(
            "unsupported evaluation dataset schema version: "
            f"{version!r} (this build reads "
            f"{EVALUATION_DATASET_SCHEMA_VERSION!r})"
        )
    return version


# --------------------------------------------------------------------------
# record parts
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class EvaluationSourceRecord:
    """One `opportunity_sources` row: where this posting was seen, and when."""

    source_id: str
    source_type: str | None
    source_url: str
    application_url: str | None
    canonical_url: str | None
    discovered_at: str


@dataclass(frozen=True)
class EvaluationQualificationRecord:
    """The persisted Data & AI qualification, coarse and fine, as written.

    The five fine columns are nullable *and their nullability is meaningful*
    (see migration `0025`): `fine_classifier_version is None` means the fine
    classifier never ran, while a version with `fine_primary_category is None`
    means it ran and deliberately asserted no sub-domain. Those two states are
    kept apart here for the same reason the schema keeps them apart.
    """

    qualification: str
    primary_domain: str
    opportunity_type: str
    employment_type: str
    listing_quality: str
    classifier_version: str
    input_fingerprint: str
    classified_at: str
    fine_primary_category: str | None
    fine_secondary_categories: tuple[str, ...] | None
    fine_classifier_version: str | None


@dataclass(frozen=True)
class EvaluationGeographySegmentRecord:
    """One resolved segment of a posting's location string, Phase 7A.

    `country_code is None` is the resolver's UNKNOWN: the segment named no
    country any closed rule recognises. It is emphatically not "abroad".
    """

    segment_position: int
    raw_segment: str
    status: str
    rule_id: str
    country_code: str | None
    city_key: str | None
    resolver_version: str


@dataclass(frozen=True)
class EvaluationEligibilityRecord:
    """The stored Phase 3.6 decision for this person and this posting.

    `status` is one of ELIGIBLE, INELIGIBLE or UNKNOWN. UNKNOWN is a missing
    *fact*, never a soft refusal, and the counters say how the verdict was
    reached — which is what a later error analysis needs in order to separate
    "we knew it did not fit" from "we never found out".
    """

    status: str
    engine_version: str
    input_fingerprint: str
    satisfied_count: int
    violated_count: int
    unknown_count: int
    not_applicable_count: int
    not_evaluated_count: int
    blocking_unknown_count: int
    evaluated_at: str


@dataclass(frozen=True)
class EvaluationMatchingRecord:
    """This posting's assessment inside the profile's current Matching run.

    `match_quality is None` with `evidence_coverage == 0.0` is Phase 4's own
    contract: nothing was measured, so there is no score. A zero would read as
    "measured and bad", which is a different statement entirely.
    """

    lane: str
    match_quality: float | None
    evidence_coverage: float
    assessment_fingerprint: str


@dataclass(frozen=True)
class EvaluationRecommendationRecord:
    """This posting's assessment inside the profile's current Recommendation run.

    Absent on the record — `recommendation is None` — is the signal this whole
    slice is built to preserve: an opportunity that is in the cohort and that
    the Recommendation Engine never ranked. Those are the candidate false
    negatives, and they cannot be seen at all from the recommendation output.
    """

    rank_position: int
    disposition: str
    recommendation_score: float | None
    evidence_coverage: float
    assessment_fingerprint: str


@dataclass(frozen=True)
class EvaluationOpportunityRecord:
    """One real opportunity, frozen with the production signals it carries.

    `opportunity_id` is the operational primary key and is the stable identity a
    later slice attaches a human label to. Everything else is either a column of
    `opportunities` or an already-persisted downstream signal; nothing is
    derived, rescored or repaired here.

    Every downstream block is optional, and each absence is a different fact:
    no qualification row means the classifier never read the posting; no
    eligibility decision means none was stored for this person; no matching or
    recommendation assessment means the run never covered it. A reader that
    folds those four nulls together is wrong — but it will be wrong about a
    value the record states rather than about a key that quietly is not there.
    """

    opportunity_id: int
    canonical_title: str
    organization: str
    opportunity_type: str | None
    employment_type: str | None
    location: str | None
    country: str | None
    remote_type: str | None
    #: The posting's own text, exactly as `opportunities.description` holds it,
    #: and `None` when that column is NULL. It is carried so that a human
    #: judgement in a later slice can be made **from the frozen evidence** — not
    #: by re-reading the live database, and not by following a URL that may have
    #: changed or gone. Nothing here trims, normalises, cleans, summarises or
    #: truncates it: this record is evidence, and edited evidence is not.
    description: str | None
    source_url: str
    application_url: str | None
    canonical_url: str | None
    status: str
    is_active: bool
    published_at: str | None
    deadline: str | None
    discovered_at: str
    first_seen_at: str
    last_seen_at: str
    #: Postings this one absorbed as duplicates (`deduplication_merges`, APPLIED).
    #: Empty means it absorbed none — it does not mean deduplication never ran.
    absorbed_duplicate_ids: tuple[int, ...]
    sources: tuple[EvaluationSourceRecord, ...]
    #: `None` when the Data/AI classifier has never read this posting. That is
    #: an upstream fact worth measuring — a collector running ahead of
    #: qualification — and it is emphatically not `OUT_OF_SCOPE`, not
    #: `UNCERTAIN`, not `UNKNOWN` and not an empty object.
    qualification: EvaluationQualificationRecord | None
    geography_segments: tuple[EvaluationGeographySegmentRecord, ...]
    eligibility: EvaluationEligibilityRecord | None
    matching: EvaluationMatchingRecord | None
    recommendation: EvaluationRecommendationRecord | None


# --------------------------------------------------------------------------
# manifest parts
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class EvaluationCohortDefinition:
    """What the snapshot selected, in what order, and what it left behind.

    `excluded_counts` holds one count per exclusion rule this cohort applies,
    and it is **inside** the content fingerprint: it is a statement about the
    selection, so two datasets that excluded different numbers of postings did
    not select the same universe and must not share a `dataset_id`.
    """

    version: str
    criteria: tuple[str, ...]
    ordering: str
    excluded_counts: Mapping[str, int]


@dataclass(frozen=True)
class EvaluationProfileContext:
    """Whose evaluation this is, and what their profile said at the time.

    A dataset is not only a set of postings: three of its four downstream blocks
    — eligibility, matching, recommendation — are *personalised*, and the human
    labels a later slice attaches to it are judgements made for one specific
    person against one specific profile state. So the dataset is bound to that
    context and the binding is inside the content fingerprint.

    `profile_id` and `user_id` are in the digest domain even though row identity
    is excluded everywhere else in this contract, and the difference is real. A
    per-record digest excludes identity because two identical postings genuinely
    are the same input; a *dataset* is a frozen artefact that answers "what was
    evaluated, and for whom", and two people can hold identical profiles. Two
    profiles whose downstream runs are both absent would otherwise produce
    byte-identical datasets under one `dataset_id`, and a label attached to that
    id would name no one.

    `fingerprint` covers what the profile actually said — see
    `profile_context.py` for the exact domain — so editing a preference,
    gaining a skill or widening a declared mobility produces a new dataset id
    even when every posting in the cohort is unchanged.
    """

    profile_id: int
    user_id: int
    fingerprint: str


@dataclass(frozen=True)
class EvaluationUpstreamProvenance:
    """Which production runs the downstream signals in this dataset came from.

    The run *ids* are row identity and are recorded for traceability only. The
    run *fingerprints* and the engine versions are content, and are what a later
    slice compares when it asks whether two datasets describe the same pipeline.
    """

    matching_status: str
    matching_run_id: int | None
    matching_run_fingerprint: str | None
    matching_engine_version: str | None
    matching_rules_version: str | None
    matching_selection_version: str | None
    recommendation_status: str
    recommendation_run_id: int | None
    recommendation_run_fingerprint: str | None
    recommendation_engine_version: str | None
    recommendation_rules_version: str | None


@dataclass(frozen=True)
class EvaluationSourceIdentity:
    """Which database this snapshot was taken from, without copying a byte of it.

    The digest is of the main database file. `wal_present` is recorded beside it
    because a non-empty write-ahead log means committed state lives outside that
    file, so the digest alone would not identify what was read. Recording the
    fact is the honest answer; pretending the digest is complete is not.
    """

    database_path: str
    database_bytes: int
    database_sha256: str
    wal_present: bool
    wal_bytes: int | None
    shm_present: bool
    applied_migrations: tuple[str, ...]


@dataclass(frozen=True)
class EvaluationDatasetManifest:
    """Everything about a dataset except the records themselves.

    `generated_at` is execution metadata and **is not in `content_fingerprint`**.
    Two extractions a day apart over an unchanged database produce the same
    digest, which is precisely what makes the digest a statement about the data
    rather than about the run that read it — and `storage.py` then refuses to
    rewrite a dataset directory that already exists, so the first manifest
    written under a `dataset_id` keeps its `generated_at` for good. A frozen
    dataset whose metadata could still move would not be frozen.
    """

    schema_version: str
    dataset_id: str
    generated_at: str
    git_commit: str | None
    record_count: int
    profile_context: EvaluationProfileContext
    source: EvaluationSourceIdentity
    cohort: EvaluationCohortDefinition
    upstream: EvaluationUpstreamProvenance
    content_fingerprint: str


@dataclass(frozen=True)
class EvaluationDataset:
    """A manifest and its records, in the manifest's canonical order."""

    manifest: EvaluationDatasetManifest
    records: tuple[EvaluationOpportunityRecord, ...]


# --------------------------------------------------------------------------
# the one canonical serialization
# --------------------------------------------------------------------------


def _sources_payload(
    sources: Sequence[EvaluationSourceRecord],
) -> list[dict[str, Any]]:
    return [
        {
            "source_id": item.source_id,
            "source_type": item.source_type,
            "source_url": item.source_url,
            "application_url": item.application_url,
            "canonical_url": item.canonical_url,
            "discovered_at": item.discovered_at,
        }
        for item in sources
    ]


def _qualification_payload(
    qualification: EvaluationQualificationRecord,
) -> dict[str, Any]:
    return {
        "qualification": qualification.qualification,
        "primary_domain": qualification.primary_domain,
        "opportunity_type": qualification.opportunity_type,
        "employment_type": qualification.employment_type,
        "listing_quality": qualification.listing_quality,
        "classifier_version": qualification.classifier_version,
        "input_fingerprint": qualification.input_fingerprint,
        "classified_at": qualification.classified_at,
        "fine_primary_category": qualification.fine_primary_category,
        # `None` and `[]` are different statements here; `list(...)` is applied
        # only to the tuple, never to the absence.
        "fine_secondary_categories": (
            None
            if qualification.fine_secondary_categories is None
            else list(qualification.fine_secondary_categories)
        ),
        "fine_classifier_version": qualification.fine_classifier_version,
    }


def _geography_payload(
    segments: Sequence[EvaluationGeographySegmentRecord],
) -> list[dict[str, Any]]:
    return [
        {
            "segment_position": item.segment_position,
            "raw_segment": item.raw_segment,
            "status": item.status,
            "rule_id": item.rule_id,
            "country_code": item.country_code,
            "city_key": item.city_key,
            "resolver_version": item.resolver_version,
        }
        for item in segments
    ]


def evaluation_record_payload(
    record: EvaluationOpportunityRecord,
) -> dict[str, Any]:
    """The one JSON shape of a record: what is written *and* what is digested.

    Every optional block serializes to `null` when the pipeline produced
    nothing, and each of the four downstream blocks is optional for its own
    distinct reason — the Data/AI classifier never read the posting, no
    eligibility decision was stored for this person, no Matching assessment in
    the current run, no Recommendation assessment in the current run. A reader
    that treats those four nulls as the same thing is wrong, but it will at
    least be wrong about a value that is present in the file rather than about
    a key that quietly is not.
    """
    eligibility = record.eligibility
    matching = record.matching
    recommendation = record.recommendation
    return {
        "opportunity_id": record.opportunity_id,
        "canonical_title": record.canonical_title,
        "organization": record.organization,
        "opportunity_type": record.opportunity_type,
        "employment_type": record.employment_type,
        "location": record.location,
        "country": record.country,
        "remote_type": record.remote_type,
        "description": record.description,
        "source_url": record.source_url,
        "application_url": record.application_url,
        "canonical_url": record.canonical_url,
        "status": record.status,
        "is_active": record.is_active,
        "published_at": record.published_at,
        "deadline": record.deadline,
        "discovered_at": record.discovered_at,
        "first_seen_at": record.first_seen_at,
        "last_seen_at": record.last_seen_at,
        "absorbed_duplicate_ids": list(record.absorbed_duplicate_ids),
        "sources": _sources_payload(record.sources),
        # `null`, never `{}`: an unread posting states nothing about itself.
        "qualification": (
            None
            if record.qualification is None
            else _qualification_payload(record.qualification)
        ),
        "geography_segments": _geography_payload(record.geography_segments),
        "eligibility": (
            None
            if eligibility is None
            else {
                "status": eligibility.status,
                "engine_version": eligibility.engine_version,
                "input_fingerprint": eligibility.input_fingerprint,
                "satisfied_count": eligibility.satisfied_count,
                "violated_count": eligibility.violated_count,
                "unknown_count": eligibility.unknown_count,
                "not_applicable_count": eligibility.not_applicable_count,
                "not_evaluated_count": eligibility.not_evaluated_count,
                "blocking_unknown_count": eligibility.blocking_unknown_count,
                "evaluated_at": eligibility.evaluated_at,
            }
        ),
        "matching": (
            None
            if matching is None
            else {
                "lane": matching.lane,
                "match_quality": matching.match_quality,
                "evidence_coverage": matching.evidence_coverage,
                "assessment_fingerprint": matching.assessment_fingerprint,
            }
        ),
        "recommendation": (
            None
            if recommendation is None
            else {
                "rank_position": recommendation.rank_position,
                "disposition": recommendation.disposition,
                "recommendation_score": recommendation.recommendation_score,
                "evidence_coverage": recommendation.evidence_coverage,
                "assessment_fingerprint": recommendation.assessment_fingerprint,
            }
        ),
    }


def evaluation_cohort_payload(cohort: EvaluationCohortDefinition) -> dict[str, Any]:
    """The cohort block, in the one layout both the manifest and the digest use."""
    return {
        "version": cohort.version,
        # The criteria keep the order they are stated in: they are a written
        # rule, and a rule reads the way it was written.
        "criteria": list(cohort.criteria),
        "ordering": cohort.ordering,
        "excluded_counts": dict(cohort.excluded_counts),
    }


def evaluation_profile_context_payload(
    profile_context: EvaluationProfileContext,
) -> dict[str, Any]:
    """The profile binding block, in the one layout both uses share."""
    return {
        "profile_id": profile_context.profile_id,
        "user_id": profile_context.user_id,
        "fingerprint": profile_context.fingerprint,
    }


def evaluation_upstream_payload(
    upstream: EvaluationUpstreamProvenance,
) -> dict[str, Any]:
    """The upstream provenance block, run ids included.

    The digest keeps only part of this — `fingerprint.py` names which part — but
    the *layout* is defined once, here, so the manifest on disk and the digest
    domain cannot drift into two different spellings of the same field.
    """
    return {
        "matching_status": upstream.matching_status,
        "matching_run_id": upstream.matching_run_id,
        "matching_run_fingerprint": upstream.matching_run_fingerprint,
        "matching_engine_version": upstream.matching_engine_version,
        "matching_rules_version": upstream.matching_rules_version,
        "matching_selection_version": upstream.matching_selection_version,
        "recommendation_status": upstream.recommendation_status,
        "recommendation_run_id": upstream.recommendation_run_id,
        "recommendation_run_fingerprint": upstream.recommendation_run_fingerprint,
        "recommendation_engine_version": upstream.recommendation_engine_version,
        "recommendation_rules_version": upstream.recommendation_rules_version,
    }


def evaluation_manifest_payload(
    manifest: EvaluationDatasetManifest,
) -> dict[str, Any]:
    """The manifest as it is written to `manifest.json`, volatile fields included.

    This is *not* the fingerprint domain — see `fingerprint.py`, which projects
    this structure onto a deliberately smaller one.
    """
    source = manifest.source
    return {
        "schema_version": manifest.schema_version,
        "dataset_id": manifest.dataset_id,
        "generated_at": manifest.generated_at,
        "git_commit": manifest.git_commit,
        "record_count": manifest.record_count,
        "profile_context": evaluation_profile_context_payload(
            manifest.profile_context
        ),
        "source": {
            "database_path": source.database_path,
            "database_bytes": source.database_bytes,
            "database_sha256": source.database_sha256,
            "wal_present": source.wal_present,
            "wal_bytes": source.wal_bytes,
            "shm_present": source.shm_present,
            "applied_migrations": list(source.applied_migrations),
        },
        "cohort": evaluation_cohort_payload(manifest.cohort),
        "upstream": evaluation_upstream_payload(manifest.upstream),
        "content_fingerprint": manifest.content_fingerprint,
    }
