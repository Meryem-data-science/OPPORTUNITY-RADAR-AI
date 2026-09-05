"""Phase 7C.1 — the Morocco PFE source map and gold benchmark foundation.

Three artefacts, deliberately kept apart from each other and from production:

    config/sources.yaml                     what the radar actually collects
    evaluation/source_coverage/...yaml      what we declare we want to cover
    evaluation/benchmarks/...jsonl          real opportunities we know existed

The first is operational and this slice does not touch it. The second is a
declaration of intent and activates nothing. The third is historical evidence
used to score the radar, and is never re-injected as an active opportunity.

    validator.py  the offline, deterministic checks both artefacts must pass
    README.md     the scope, the metric definitions, and what stays out

Phase 7C.2A adds one measurement beside them, and no artefact:

    linkedin_gmail_audit.py  aggregate Gmail intake quality of LinkedIn alerts

It reads mail read-only through the existing Gmail client, reuses the existing
LinkedIn parser, writes nothing anywhere, and reports counts — parser yield
over a bounded Gmail window, which is not LinkedIn recall.
"""

from evaluation.morocco_pfe.linkedin_gmail_audit import (
    DEFAULT_AUDIT_MESSAGE_LIMIT,
    DEFAULT_AUDIT_QUERY,
    GmailIntakeAuditError,
    GmailIntakeAuditReport,
    audit_gmail_intake,
)
from evaluation.morocco_pfe.validator import (
    BENCHMARK_COUNTRY_CODE,
    BenchmarkReport,
    BenchmarkValidationError,
    COLLECTION_STRATEGIES,
    COVERAGE_ROLES,
    INTEGRATION_STATUSES,
    OBSERVATION_HORIZONS,
    PRIORITIES,
    SOURCE_AUTHORITIES,
    SOURCE_CLASSES,
    SourceMap,
    SourceMapEntry,
    SourceMapValidationError,
    check_source_map_against_production_registry,
    load_benchmark_records,
    load_manifest,
    load_source_map,
    validate_benchmark,
)

__all__ = [
    "BENCHMARK_COUNTRY_CODE",
    "BenchmarkReport",
    "BenchmarkValidationError",
    "COLLECTION_STRATEGIES",
    "COVERAGE_ROLES",
    "DEFAULT_AUDIT_MESSAGE_LIMIT",
    "DEFAULT_AUDIT_QUERY",
    "GmailIntakeAuditError",
    "GmailIntakeAuditReport",
    "INTEGRATION_STATUSES",
    "OBSERVATION_HORIZONS",
    "PRIORITIES",
    "SOURCE_AUTHORITIES",
    "SOURCE_CLASSES",
    "SourceMap",
    "SourceMapEntry",
    "SourceMapValidationError",
    "audit_gmail_intake",
    "check_source_map_against_production_registry",
    "load_benchmark_records",
    "load_manifest",
    "load_source_map",
    "validate_benchmark",
]
