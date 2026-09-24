import type {
  ActivationResult,
  ReviewEntry,
  ReviewSnapshot,
} from "./cv-replacement-contract";

/**
 * Synthetic review data for the tests. No real CV, no real profile.
 *
 * The readings are invented and marked TEST ONLY. `SENTINEL_READING` is the one
 * the privacy tests hunt for: it must reach the rendered page and nothing else
 * — no URL, no log, no error body, no browser storage.
 */
export const SENTINEL_READING = "QpReadingSentinelTestOnly";
export const SENTINEL_CORRECTION = "ZqCorrectionSentinelTestOnly";

export const READY_DIGEST = "ab".repeat(32);

export const incomingNew: ReviewEntry = {
  role: "INCOMING",
  difference: "NEW",
  candidate_id: 11,
  fact_id: null,
  fact_type: "SKILL",
  fact_status: null,
  display_value: SENTINEL_READING,
  decision: null,
  has_staged_value: false,
};

export const incomingBlocked: ReviewEntry = {
  role: "INCOMING",
  difference: "BLOCKED_TERMINAL_REJECTED",
  candidate_id: 12,
  fact_id: null,
  fact_type: "SKILL",
  fact_status: "REJECTED",
  display_value: "BlockedTestOnlyToolkit",
  decision: null,
  has_staged_value: false,
};

export const existingAbsent: ReviewEntry = {
  role: "EXISTING",
  difference: "ABSENT_FROM_NEW_CV",
  candidate_id: null,
  fact_id: 21,
  fact_type: "SKILL",
  fact_status: "ACCEPTED",
  display_value: "AbsentTestOnlyToolkit",
  decision: null,
  has_staged_value: false,
};

export const existingProtected: ReviewEntry = {
  role: "EXISTING",
  difference: "USER_INPUT_REPLACEMENT",
  candidate_id: null,
  fact_id: 22,
  fact_type: "SKILL",
  fact_status: "ACCEPTED",
  display_value: "ProtectedTestOnlyToolkit",
  decision: null,
  has_staged_value: false,
};

const entries: readonly ReviewEntry[] = [
  incomingNew,
  incomingBlocked,
  existingAbsent,
  existingProtected,
];

export const noReview: ReviewSnapshot = {
  replacement: null,
  progress: null,
  entries: [],
};

export const openReview: ReviewSnapshot = {
  replacement: {
    replacement_id: 7,
    extraction_id: 3,
    baseline_document_id: 2,
    lifecycle: "PREPARED",
    effective_state: "PREPARED",
    ready_review_digest: null,
    activation_revision: null,
    activated_at: null,
  },
  progress: {
    plan_entries: entries.length,
    answered: 0,
    unanswered: entries.length,
    decisions_outside_plan: 0,
    stale_decisions: 0,
    counts_by_difference: {
      NEW: 1,
      BLOCKED_TERMINAL_REJECTED: 1,
      ABSENT_FROM_NEW_CV: 1,
      USER_INPUT_REPLACEMENT: 1,
    },
  },
  entries,
};

const answered: readonly ReviewEntry[] = [
  { ...incomingNew, decision: "CORRECT", has_staged_value: true },
  { ...incomingBlocked, decision: "SKIP_BLOCKED" },
  { ...existingAbsent, decision: "KEEP" },
  { ...existingProtected, decision: "KEEP" },
];

export const readyReview: ReviewSnapshot = {
  replacement: {
    ...openReview.replacement!,
    lifecycle: "READY_TO_ACTIVATE",
    effective_state: "READY_TO_ACTIVATE",
    ready_review_digest: READY_DIGEST,
  },
  progress: {
    ...openReview.progress!,
    answered: answered.length,
    unanswered: 0,
  },
  entries: answered,
};

export const staleReview: ReviewSnapshot = {
  ...openReview,
  progress: { ...openReview.progress!, stale_decisions: 2 },
};

export const activation: ActivationResult = {
  replacement_id: 7,
  activated: true,
  activation_revision: 1,
  previous_document_id: 2,
  document_id: 5,
  facts_accepted: 1,
  facts_rejected: 1,
  facts_corrected: 1,
  facts_retired: 1,
  evidence_attached: 2,
  decisions_skipped: 1,
  effective_state: "ACTIVATED",
};
