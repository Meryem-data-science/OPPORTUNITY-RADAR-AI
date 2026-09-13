"""The cryptographic identities of a business metric — Phase 10.4a.

Same primitive as every phase before this one — the project's single
`canonical_json` (sorted keys, no insignificant whitespace, UTF-8), digested
with SHA-256. Nothing is invented here; only the domains are new, and each is
stated in full, because a digest whose domain is implicit is a digest nobody can
reason about.

**Three identities per result, three different questions**, and each is a
projection of *one metric's* needs:

    scope_fingerprint     which exact question did we ask?
    evidence_fingerprint  on which exact evidence does the answer rest?
    result_fingerprint    which exact answer did we produce?

**Two further identities exist**, and neither is one of those three: the
frozen cohort binding's own digest fixes the snapshot's derived facts — its
size, its `generated_at`, the as-of date that follows and the complete set of
action URLs it implies — and the run context's digest says what one assembly of
evidence had available. On the latter: `run_context_fingerprint` says what one assembly of evidence had
available. An earlier version of this contract digested the run's artefacts and
called the result the scope fingerprint of every metric in it, which made a
`DATA_AI_RATE` change identity whenever an unrelated URL audit was re-run. A run
context answers "what did we have"; a scope answers "what did we ask". They are
not the same question and they no longer share a digest.

**The scope domain** — the question, and only the bindings this metric uses:

    scope_schema_version          the shape of a scope
    contract_version              the rules that decide what may be reported
    key                           metric, dimension kind, dimension value
    universe_kind                 the closed set this metric is defined over
    dataset_id / dataset_fingerprint       which snapshot
    profile_id / profile_fingerprint       whose, and in which state
    the six member digests, each or None — and a member the metric does not use
    is *structurally* absent, refused by `validate_business_metric_scope_structure`
    rather than merely left out by convention

**The evidence domain** — input artefacts only, and only this metric's:

    evidence_view_schema_version
    evidence_class                what this metric's number may claim
    dataset_id / dataset_fingerprint / profile_fingerprint
    the six member digests, each or None

It contains **no output**: no value, no status, no numerator, no denominator, no
support block. It contains no metric key either, so two metrics that genuinely
rest on the same artefacts share one evidence identity — which they should; what
differs between them is the question, and that is the scope's job.

**The result domain** — the answer, and the other two identities by reference:

    result_schema_version / contract_version
    key / status / value / support / universe_kind / reason / evidence_class
    scope_fingerprint / evidence_fingerprint

and `result_fingerprint` itself is outside it, because it is derived from that
domain and including it would be circular.

**Nested artefacts enter every domain by their own digest** rather than by their
contents. That is safe only because `bindings.py` recomputes each nested digest
before assembling a context, a scope or a view, which it does.

Two exclusions are worth naming, because they are the ones a reader will look
for:

* **No clock, no directory, no commit.** `provenance_path`, `git_commit` and the
  dataset directory are carried on the artefacts and digested nowhere. They say
  where a file was read and by which checkout, not what it says.
* **Except `audited_at`**, which *is* inside the URL audit domain. That is not an
  inconsistency: an HTTP audit is external, temporal evidence, and when a URL was
  looked at is part of what was observed about it.

Following Phase 10.1, a stored object's self-declared fingerprint is never
trusted: each `verify_*` below **re-establishes the structure first** and then
recomputes the digest. Both halves, in that order, because either alone is
insufficient — a digest check catches an artefact that changed after somebody
digested it, and a structural check catches one that was already wrong when they
did. A URL audit with two observations of one URL, re-digested, has a perfectly
valid fingerprint over an invalid artefact.

This module opens no file, opens no database connection and makes no request.
"""

from __future__ import annotations

import hashlib
from typing import Any

from services.collector.matching.fingerprint import canonical_json

from .schema import (
    BenchmarkBinding,
    BusinessMetricBindingError,
    BusinessMetricEvidenceView,
    BusinessMetricResult,
    BusinessMetricRun,
    BusinessMetricRunContext,
    BusinessMetricScope,
    DeclaredSourceUniverseEvidence,
    DedupEvidence,
    FreshnessBinding,
    FrozenCohortBinding,
    ProfileTargetBindingEvidence,
    UrlAuditBinding,
    benchmark_binding_payload,
    business_metric_evidence_payload,
    business_metric_result_payload,
    business_metric_run_context_payload,
    business_metric_run_fingerprint_payload,
    business_metric_scope_payload,
    declared_source_universe_payload,
    dedup_evidence_payload,
    freshness_binding_payload,
    frozen_cohort_binding_payload,
    profile_target_binding_payload,
    url_audit_binding_payload,
    validate_benchmark_binding_structure,
    validate_business_metric_evidence_structure,
    validate_business_metric_result_structure,
    validate_business_metric_run_context_structure,
    validate_business_metric_run_structure,
    validate_business_metric_scope_structure,
    validate_declared_source_universe_structure,
    validate_dedup_evidence_structure,
    validate_freshness_binding_structure,
    validate_frozen_cohort_binding_structure,
    validate_profile_target_binding_structure,
    validate_url_audit_binding_structure,
)

__all__ = [
    "PLACEHOLDER_FINGERPRINT",
    "benchmark_binding_fingerprint",
    "business_metric_evidence_fingerprint",
    "business_metric_result_fingerprint",
    "business_metric_run_context_fingerprint",
    "business_metric_run_fingerprint",
    "business_metric_scope_fingerprint",
    "canonical_benchmark_binding_payload",
    "canonical_business_metric_evidence_payload",
    "canonical_business_metric_result_payload",
    "canonical_business_metric_run_context_payload",
    "canonical_business_metric_run_payload",
    "canonical_business_metric_scope_payload",
    "canonical_declared_source_universe_payload",
    "canonical_dedup_evidence_payload",
    "canonical_freshness_binding_payload",
    "canonical_frozen_cohort_binding_payload",
    "canonical_profile_target_binding_payload",
    "canonical_url_audit_binding_payload",
    "declared_source_universe_fingerprint",
    "dedup_evidence_fingerprint",
    "freshness_binding_fingerprint",
    "frozen_cohort_binding_fingerprint",
    "profile_target_binding_fingerprint",
    "url_audit_binding_fingerprint",
    "verify_benchmark_binding_fingerprint",
    "verify_business_metric_evidence_fingerprint",
    "verify_business_metric_result_fingerprint",
    "verify_business_metric_run_context_fingerprint",
    "verify_business_metric_run_fingerprint",
    "verify_business_metric_scope_fingerprint",
    "verify_declared_source_universe_fingerprint",
    "verify_dedup_evidence_fingerprint",
    "verify_freshness_binding_fingerprint",
    "verify_frozen_cohort_binding_fingerprint",
    "verify_profile_target_binding_fingerprint",
    "verify_url_audit_binding_fingerprint",
]

#: The value a draft artefact carries while its structure is validated and
#: before its real digest is computed. Never written to an artefact a caller
#: receives: every builder in `bindings.py` replaces it in the same expression
#: that validated the draft.
PLACEHOLDER_FINGERPRINT = "0" * 64


def _digest(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _refuse(subject: str, claimed: str, recomputed: str) -> None:
    raise BusinessMetricBindingError(
        f"{subject} claims fingerprint {claimed} but its content digests to "
        f"{recomputed}; refusing an artefact that is not what it says it is"
    )


# --------------------------------------------------------------------------
# the frozen cohort binding
# --------------------------------------------------------------------------


def canonical_frozen_cohort_binding_payload(
    binding: FrozenCohortBinding,
) -> dict[str, Any]:
    """The cohort binding's digest domain: the snapshot's derived facts.

    The expected action URL set is digested **in full**, not by count: two
    cohorts of the same size implying different URLs are different cohorts, and
    an audit is verified against the set rather than against its length. The
    `generated_at` is in as well — it is the snapshot's own timestamp, from which
    the only acceptable freshness as-of date follows.
    """
    return frozen_cohort_binding_payload(binding)


def frozen_cohort_binding_fingerprint(binding: FrozenCohortBinding) -> str:
    """SHA-256 of the canonical JSON of everything a cohort binding asserts."""
    return _digest(canonical_frozen_cohort_binding_payload(binding))


def verify_frozen_cohort_binding_fingerprint(binding: FrozenCohortBinding) -> str:
    """Re-establish the structure, then recompute the digest, or refuse.

    The structural half re-derives the as-of date from the stated `generated_at`,
    so a binding somebody rebuilt with a hand-chosen date is refused here rather
    than silently parameterising every freshness number in the run.
    """
    validate_frozen_cohort_binding_structure(binding)
    recomputed = frozen_cohort_binding_fingerprint(binding)
    if recomputed != binding.binding_fingerprint:
        _refuse("the frozen cohort binding", binding.binding_fingerprint, recomputed)
    return recomputed


# --------------------------------------------------------------------------
# the profile target binding
# --------------------------------------------------------------------------


def canonical_profile_target_binding_payload(
    binding: ProfileTargetBindingEvidence,
) -> dict[str, Any]:
    """The target binding's digest domain, as a structure a test can assert on."""
    return profile_target_binding_payload(binding)


def profile_target_binding_fingerprint(
    binding: ProfileTargetBindingEvidence,
) -> str:
    """SHA-256 of the canonical JSON of everything a target binding asserts."""
    return _digest(canonical_profile_target_binding_payload(binding))


def verify_profile_target_binding_fingerprint(
    binding: ProfileTargetBindingEvidence,
) -> str:
    """Re-establish the structure, then recompute the digest, or refuse."""
    validate_profile_target_binding_structure(binding)
    recomputed = profile_target_binding_fingerprint(binding)
    if recomputed != binding.binding_fingerprint:
        _refuse("the profile target binding", binding.binding_fingerprint, recomputed)
    return recomputed


# --------------------------------------------------------------------------
# the declared source universe
# --------------------------------------------------------------------------


def canonical_declared_source_universe_payload(
    evidence: DeclaredSourceUniverseEvidence,
) -> dict[str, Any]:
    """The declared universe's digest domain: its content, not its path.

    Covers every entry's `source_id`, integration status, coverage role,
    priority, country and production id, in canonical id order. So an edit to the
    YAML that leaves `map_name` and `version` untouched — a status corrected from
    `NEEDS_VERIFICATION` to `ACTIVE`, an entry added — produces a different
    universe, which it is.
    """
    return declared_source_universe_payload(evidence)


def declared_source_universe_fingerprint(
    evidence: DeclaredSourceUniverseEvidence,
) -> str:
    """SHA-256 of the canonical JSON of a declared source universe's content."""
    return _digest(canonical_declared_source_universe_payload(evidence))


def verify_declared_source_universe_fingerprint(
    evidence: DeclaredSourceUniverseEvidence,
) -> str:
    """Re-establish the structure, then recompute the digest, or refuse."""
    validate_declared_source_universe_structure(evidence)
    recomputed = declared_source_universe_fingerprint(evidence)
    if recomputed != evidence.content_fingerprint:
        _refuse(
            "the declared source universe", evidence.content_fingerprint, recomputed
        )
    return recomputed


# --------------------------------------------------------------------------
# the URL audit
# --------------------------------------------------------------------------


def canonical_url_audit_binding_payload(
    binding: UrlAuditBinding,
) -> dict[str, Any]:
    """The URL audit's digest domain, `audited_at` included."""
    return url_audit_binding_payload(binding)


def url_audit_binding_fingerprint(binding: UrlAuditBinding) -> str:
    """SHA-256 of the canonical JSON of everything a URL audit asserts."""
    return _digest(canonical_url_audit_binding_payload(binding))


def verify_url_audit_binding_fingerprint(binding: UrlAuditBinding) -> str:
    """Re-establish the structure, then recompute the digest, or refuse.

    The structural half matters here more than anywhere else in this module: an
    audit with two observations of one URL, a declared size that does not equal
    the observations, or a `VALID` verdict on a 404 carries a perfectly valid
    digest over an artefact every later rate would be computed wrongly from.
    """
    validate_url_audit_binding_structure(binding)
    recomputed = url_audit_binding_fingerprint(binding)
    if recomputed != binding.binding_fingerprint:
        _refuse("the URL audit binding", binding.binding_fingerprint, recomputed)
    return recomputed


# --------------------------------------------------------------------------
# deduplication evidence
# --------------------------------------------------------------------------


def canonical_dedup_evidence_payload(evidence: DedupEvidence) -> dict[str, Any]:
    """The dedup evidence digest domain: both statements, and the dataset."""
    return dedup_evidence_payload(evidence)


def dedup_evidence_fingerprint(evidence: DedupEvidence) -> str:
    """SHA-256 of the canonical JSON of what deduplication did."""
    return _digest(canonical_dedup_evidence_payload(evidence))


def verify_dedup_evidence_fingerprint(evidence: DedupEvidence) -> str:
    """Re-establish the structure — the manifest agreement included — then digest."""
    validate_dedup_evidence_structure(evidence)
    recomputed = dedup_evidence_fingerprint(evidence)
    if recomputed != evidence.evidence_fingerprint:
        _refuse(
            "the deduplication evidence", evidence.evidence_fingerprint, recomputed
        )
    return recomputed


# --------------------------------------------------------------------------
# the freshness binding
# --------------------------------------------------------------------------


def canonical_freshness_binding_payload(
    binding: FreshnessBinding,
) -> dict[str, Any]:
    """The freshness digest domain: the as-of date and the policy, nothing else."""
    return freshness_binding_payload(binding)


def freshness_binding_fingerprint(binding: FreshnessBinding) -> str:
    """SHA-256 of the canonical JSON of a freshness binding."""
    return _digest(canonical_freshness_binding_payload(binding))


def verify_freshness_binding_fingerprint(binding: FreshnessBinding) -> str:
    """Re-establish the structure, then recompute the digest, or refuse."""
    validate_freshness_binding_structure(binding)
    recomputed = freshness_binding_fingerprint(binding)
    if recomputed != binding.binding_fingerprint:
        _refuse("the freshness binding", binding.binding_fingerprint, recomputed)
    return recomputed


# --------------------------------------------------------------------------
# the benchmark binding
# --------------------------------------------------------------------------


def canonical_benchmark_binding_payload(
    binding: BenchmarkBinding,
) -> dict[str, Any]:
    """The benchmark digest domain: the manifest's semantics and the rows' digest."""
    return benchmark_binding_payload(binding)


def benchmark_binding_fingerprint(binding: BenchmarkBinding) -> str:
    """SHA-256 of the canonical JSON of a benchmark binding."""
    return _digest(canonical_benchmark_binding_payload(binding))


def verify_benchmark_binding_fingerprint(binding: BenchmarkBinding) -> str:
    """Re-establish the structure, then recompute the digest, or refuse."""
    validate_benchmark_binding_structure(binding)
    recomputed = benchmark_binding_fingerprint(binding)
    if recomputed != binding.content_fingerprint:
        _refuse("the benchmark binding", binding.content_fingerprint, recomputed)
    return recomputed


# --------------------------------------------------------------------------
# the run context — what one run had available
# --------------------------------------------------------------------------


def canonical_business_metric_run_context_payload(
    context: BusinessMetricRunContext,
) -> dict[str, Any]:
    """The run context digest domain. **Not** any metric's scope domain."""
    return business_metric_run_context_payload(context)


def business_metric_run_context_fingerprint(
    context: BusinessMetricRunContext,
) -> str:
    """SHA-256 of the canonical JSON of one run's assembled evidence.

    Useful for saying "these numbers came from one assembly", and used for
    nothing else: no result's identity depends on it, which is the whole point
    of separating it from the per-metric scope.
    """
    return _digest(canonical_business_metric_run_context_payload(context))


def verify_business_metric_run_context_fingerprint(
    context: BusinessMetricRunContext,
) -> str:
    """Re-establish the structure, then recompute the digest, or refuse."""
    validate_business_metric_run_context_structure(context)
    recomputed = business_metric_run_context_fingerprint(context)
    if recomputed != context.run_context_fingerprint:
        _refuse(
            "the business metric run context",
            context.run_context_fingerprint,
            recomputed,
        )
    return recomputed


# --------------------------------------------------------------------------
# the per-metric scope
# --------------------------------------------------------------------------


def canonical_business_metric_scope_payload(
    scope: BusinessMetricScope,
) -> dict[str, Any]:
    """The scope digest domain: which question, and nothing about the answer."""
    return business_metric_scope_payload(scope)


def business_metric_scope_fingerprint(scope: BusinessMetricScope) -> str:
    """SHA-256 of the canonical JSON of the question one metric is asking."""
    return _digest(canonical_business_metric_scope_payload(scope))


def verify_business_metric_scope_fingerprint(scope: BusinessMetricScope) -> str:
    """Re-establish the structure, then recompute the digest, or refuse."""
    validate_business_metric_scope_structure(scope)
    recomputed = business_metric_scope_fingerprint(scope)
    if recomputed != scope.scope_fingerprint:
        _refuse("the business metric scope", scope.scope_fingerprint, recomputed)
    return recomputed


# --------------------------------------------------------------------------
# the per-metric evidence view
# --------------------------------------------------------------------------


def canonical_business_metric_evidence_payload(
    evidence: BusinessMetricEvidenceView,
) -> dict[str, Any]:
    """The evidence digest domain: inputs only.

    A test asserts that this payload's keys contain no output of a computation —
    no value, no status, no numerator, no denominator, no key — because that
    property is the contract rather than an accident of the current field list.
    """
    return business_metric_evidence_payload(evidence)


def business_metric_evidence_fingerprint(
    evidence: BusinessMetricEvidenceView,
) -> str:
    """SHA-256 of the canonical JSON of the evidence one metric rests on."""
    return _digest(canonical_business_metric_evidence_payload(evidence))


def verify_business_metric_evidence_fingerprint(
    evidence: BusinessMetricEvidenceView,
) -> str:
    """Re-establish the structure, then recompute the digest, or refuse."""
    validate_business_metric_evidence_structure(evidence)
    recomputed = business_metric_evidence_fingerprint(evidence)
    if recomputed != evidence.evidence_fingerprint:
        _refuse(
            "the business metric evidence view",
            evidence.evidence_fingerprint,
            recomputed,
        )
    return recomputed


# --------------------------------------------------------------------------
# the result
# --------------------------------------------------------------------------


def canonical_business_metric_result_payload(
    result: BusinessMetricResult,
) -> dict[str, Any]:
    """The result digest domain: the result minus its own digest."""
    return business_metric_result_payload(result, include_result_fingerprint=False)


def business_metric_result_fingerprint(result: BusinessMetricResult) -> str:
    """SHA-256 of the canonical JSON of the answer a run produced.

    Moves when the value moves, when the status moves, when the support moves,
    when the universe or the reason or the evidence class moves — and when either
    of the other two identities moves. That last property is what makes a result
    unforgeable in the way that matters: the same 0.83 computed from other
    evidence is a different result.
    """
    return _digest(canonical_business_metric_result_payload(result))


def verify_business_metric_result_fingerprint(
    result: BusinessMetricResult,
) -> str:
    """Re-establish the structure, then recompute the digest, or refuse."""
    validate_business_metric_result_structure(result)
    recomputed = business_metric_result_fingerprint(result)
    if recomputed != result.result_fingerprint:
        _refuse("the business metric result", result.result_fingerprint, recomputed)
    return recomputed


# --------------------------------------------------------------------------
# the whole run — Phase 10.4b
# --------------------------------------------------------------------------


def canonical_business_metric_run_payload(
    run: BusinessMetricRun,
) -> dict[str, Any]:
    """The run's digest domain: its identity, and never its document.

    Four things — the layout version, the contract version, the run context's
    own digest, and the canonical sequence of `(key, result_fingerprint)` pairs.
    Each result enters by digest, as every nested artefact does throughout this
    package, and each digest is recomputed before a run is sealed.

    Deliberately outside it: the provenance block entire, so a run generated at
    another moment, read from another directory, on another machine, under
    another Python, is the **same run**. That is what makes the content-addressed
    store in `storage.py` able to say UNCHANGED and mean it.

    And this is emphatically not the SHA-256 of `run.json`: the file carries
    the provenance and its own formatting, and two files differing in either
    describe one measurement.
    """
    return business_metric_run_fingerprint_payload(run)


def business_metric_run_fingerprint(run: BusinessMetricRun) -> str:
    """SHA-256 of the canonical JSON of everything a run *is*.

    Moves when any result's digest moves — which is to say when any value,
    status, reason, support block, universe or evidence identity moves — when
    the key set moves, when the assembled evidence moves, and when either
    version moves. It does not move when the clock does.
    """
    return _digest(canonical_business_metric_run_payload(run))


def verify_business_metric_run_fingerprint(run: BusinessMetricRun) -> str:
    """Re-establish the structure, then recompute the digest, or refuse.

    Both halves and in that order, as everywhere else here: a run whose results
    were edited after somebody digested them is caught by the recomputation, and
    a run that was already mis-assembled when they did — two results under one
    key, a result in the wrong order — is caught by the structure, whose digest
    would otherwise be perfectly valid over an artefact nobody should read.
    """
    validate_business_metric_run_structure(run)
    recomputed = business_metric_run_fingerprint(run)
    if recomputed != run.run_fingerprint:
        _refuse("the business metric run", run.run_fingerprint, recomputed)
    return recomputed
