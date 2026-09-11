"""Phase 10.2: the human label protocol, and the blind instrument that applies it.

This package turns a frozen Phase 10.1 dataset into human judgements that a
later slice can measure the pipeline against. The pipeline it runs is short and
one-directional:

    frozen Phase 10.1 dataset
        -> integrity verification          (frozen.py)
        -> deterministic calibration draw  (selection.py)
        -> blind evidence view             (blind.py)
        -> a 0-3 human relevance grade     (schema.py)
        -> local label artefact + digest   (storage.py, fingerprint.py)

and never the reverse. Nothing in `services/` imports this package; nothing
here writes to the operational database, opens SQLite, or reads a production
table. Recommendation, Matching, Qualification, Eligibility and Geography do not
know that human labels exist, and the arrow that keeps it that way is asserted
by a test rather than described in a comment.

**The one idea worth remembering.** A human judgement is evidence about the
pipeline only if it was formed without seeing the pipeline's answer. So the
*selection* of what to judge reads every verdict the system produced — that is
how a twelve-item lot ends up containing a top-ranked posting, an unranked one,
an unclassified one and one whose location never resolved — while the *view*
handed to the annotator contains none of them. Two modules, one asymmetry,
enforced by an allowlist and a runtime guard rather than by discipline.

**What is deliberately not here.** No Precision@K, no NDCG, no recall, no
business metric, no URL health check, no baseline, no experiment runner, no
error analysis, no dashboard, no migration, no evaluation table in the database,
and no change to any production module. Those belong to Phase 10.3 and after,
and each of them starts from a labelset produced here.

**Where the two protocol versions stand.**

* `human-relevance-calibration-v0` (`schema.HUMAN_LABEL_PROTOCOL_VERSION`) is
  what the writer records and the reader interprets, and every label that exists
  was made under it. Its calibration round has been **run for real**, on the
  operator's machine: twelve judgements, AI-assisted with final human validation
  of every grade, one explicit relabel. `rubric.CALIBRATION_V0_PROVENANCE`
  records its digests and counts, and says out loud what that round is not — it
  is not an independent human benchmark, an inter-annotator study or a
  gold-standard holdout;
* `human-relevance-v1` (`rubric.FROZEN_HUMAN_RELEVANCE_PROTOCOL_VERSION`) is the
  **semantic contract frozen** by that round: relevance judged on what is
  actually actionable for the profile and preferences the dataset is bound to,
  with hard constraints declared by that profile, and with UNKNOWN never
  collapsing into a contradiction.

**No real v1 label exists, and none can yet.** The writer still stamps `v0`,
`SUPPORTED_PROTOCOL_VERSIONS` still holds only `v0`, and a row claiming v1 is
refused on read — so a frozen definition can never quietly become a rewritten
history. A real v1 benchmark needs a labelset stored separately from the
calibration history; building that separation is future work, not this slice's.
"""

from .blind import (
    BLIND_VIEW_VERSION,
    FORBIDDEN_VIEW_KEY_TOKENS,
    VISIBLE_RECORD_FIELDS,
    VISIBLE_SOURCE_FIELDS,
    WITHHELD_RECORD_FIELDS,
    BlindnessViolation,
    blind_evidence_payload,
    unclassified_record_fields,
)
from .fingerprint import (
    calibration_selection_fingerprint,
    canonical_calibration_selection_payload,
    canonical_labelset_payload,
    labelset_fingerprint,
)
from .frozen import (
    EVALUATION_RECORD_CONTRACT_FIELDS,
    FrozenDatasetIntegrityError,
    FrozenEvaluationDataset,
    read_frozen_dataset,
)
from .rubric import (
    CALIBRATION_V0_PROVENANCE,
    FROZEN_HUMAN_RELEVANCE_PROTOCOL_VERSION,
    FROZEN_RELEVANCE_RUBRIC,
    UNKNOWN_IS_NOT_FALSE,
    CalibrationProvenance,
    ConstraintEvidence,
    FrozenRubricGrade,
    HardConstraint,
    HardConstraintKind,
    frozen_rubric_grade,
    hard_contradictions,
    out_of_target_is_established,
)
from .schema import (
    HUMAN_LABEL_PROTOCOL_VERSION,
    HUMAN_LABEL_SCHEMA_VERSION,
    RELEVANCE_GRADE_NAMES,
    RELEVANCE_GRADES,
    RELEVANCE_RUBRIC,
    SUPPORTED_PROTOCOL_VERSIONS,
    DataAiJudgment,
    GeoJudgment,
    HumanLabelError,
    HumanRelevanceLabel,
    LabelDiagnostics,
    OpportunityTypeJudgment,
    canonical_label_order,
    human_label_payload,
    human_label_semantic_payload,
    label_diagnostics_payload,
    normalize_note,
    normalize_reason_tags,
    require_supported_protocol_version,
    validate_relevance_grade,
)
from .selection import (
    CALIBRATION_SELECTION_SCHEMA_VERSION,
    CALIBRATION_SELECTOR_VERSION,
    SUPPORTED_SELECTOR_VERSIONS,
    CalibrationSelection,
    CalibrationSelectionItem,
    assert_selection_bindings,
    select_calibration_sample,
    stratum_of,
)
from .storage import (
    DEFAULT_LABEL_ROOT,
    LABELS_FILENAME,
    WRITE_STATUS_CREATED,
    WRITE_STATUS_UNCHANGED,
    WRITE_STATUS_UPDATED,
    LabelsetReport,
    LabelWriteResult,
    append_human_label,
    build_labelset_report,
    calibration_selection_payload,
    dataset_label_directory,
    labelset_manifest_payload,
    list_calibration_selections,
    load_calibration_selection,
    read_label_history,
    read_validated_label_history,
    resolve_effective_labels,
    validate_label_history,
    write_calibration_selection,
    write_labelset_manifest,
)

__all__ = [
    "BLIND_VIEW_VERSION",
    "CALIBRATION_SELECTION_SCHEMA_VERSION",
    "CALIBRATION_SELECTOR_VERSION",
    "DEFAULT_LABEL_ROOT",
    "CALIBRATION_V0_PROVENANCE",
    "EVALUATION_RECORD_CONTRACT_FIELDS",
    "FROZEN_HUMAN_RELEVANCE_PROTOCOL_VERSION",
    "FROZEN_RELEVANCE_RUBRIC",
    "FORBIDDEN_VIEW_KEY_TOKENS",
    "HUMAN_LABEL_PROTOCOL_VERSION",
    "HUMAN_LABEL_SCHEMA_VERSION",
    "LABELS_FILENAME",
    "RELEVANCE_GRADES",
    "RELEVANCE_GRADE_NAMES",
    "RELEVANCE_RUBRIC",
    "SUPPORTED_PROTOCOL_VERSIONS",
    "SUPPORTED_SELECTOR_VERSIONS",
    "UNKNOWN_IS_NOT_FALSE",
    "VISIBLE_RECORD_FIELDS",
    "VISIBLE_SOURCE_FIELDS",
    "WITHHELD_RECORD_FIELDS",
    "WRITE_STATUS_CREATED",
    "WRITE_STATUS_UNCHANGED",
    "WRITE_STATUS_UPDATED",
    "BlindnessViolation",
    "CalibrationProvenance",
    "CalibrationSelection",
    "CalibrationSelectionItem",
    "ConstraintEvidence",
    "DataAiJudgment",
    "FrozenDatasetIntegrityError",
    "FrozenEvaluationDataset",
    "FrozenRubricGrade",
    "GeoJudgment",
    "HardConstraint",
    "HardConstraintKind",
    "HumanLabelError",
    "HumanRelevanceLabel",
    "LabelDiagnostics",
    "LabelWriteResult",
    "LabelsetReport",
    "OpportunityTypeJudgment",
    "append_human_label",
    "assert_selection_bindings",
    "blind_evidence_payload",
    "build_labelset_report",
    "calibration_selection_fingerprint",
    "calibration_selection_payload",
    "canonical_calibration_selection_payload",
    "canonical_label_order",
    "canonical_labelset_payload",
    "dataset_label_directory",
    "frozen_rubric_grade",
    "hard_contradictions",
    "human_label_payload",
    "human_label_semantic_payload",
    "label_diagnostics_payload",
    "labelset_fingerprint",
    "labelset_manifest_payload",
    "list_calibration_selections",
    "load_calibration_selection",
    "normalize_note",
    "out_of_target_is_established",
    "normalize_reason_tags",
    "read_frozen_dataset",
    "read_label_history",
    "read_validated_label_history",
    "resolve_effective_labels",
    "require_supported_protocol_version",
    "select_calibration_sample",
    "stratum_of",
    "unclassified_record_fields",
    "validate_label_history",
    "validate_relevance_grade",
    "write_calibration_selection",
    "write_labelset_manifest",
]
