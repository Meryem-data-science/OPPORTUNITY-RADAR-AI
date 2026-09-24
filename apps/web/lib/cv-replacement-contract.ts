/**
 * The shape of a CV replacement review, shared by both sides of the app.
 *
 * Deliberately free of `server-only`: the vocabulary and the response types are
 * as true in a browser control as in a server loader, and a client component
 * that had to reach into a server module to learn them would drag the backend
 * base URL into the bundle with it. Nothing here talks to anything — the
 * fetching lives in `lib/cv-replacement.ts`.
 *
 * Every word below mirrors the Python domain. None of it is re-decided here: a
 * control this file describes as unavailable is one the backend would refuse
 * anyway, and offering it would only be how a UI starts lying.
 */

/** What the plan says about one reading, spelled as the domain spells it. */
export const DIFFERENCES = [
  "NEW",
  "UNCHANGED_STILL_SUPPORTED",
  "ABSENT_FROM_NEW_CV",
  "INDEPENDENTLY_SUPPORTED",
  "USER_INPUT_REPLACEMENT",
  "ALREADY_PROPOSED",
  "ALREADY_ACCEPTED",
  "BLOCKED_TERMINAL_REJECTED",
  "BLOCKED_TERMINAL_CORRECTED",
] as const;

export type Difference = (typeof DIFFERENCES)[number];

/** Answers about a reading the new document proposes. */
export const INCOMING_DECISIONS = [
  "ACCEPT",
  "REJECT",
  "CORRECT",
  "SKIP_BLOCKED",
] as const;

/** Answers about a fact the profile already holds. */
export const EXISTING_DECISIONS = ["KEEP", "RETIRE"] as const;

export type IncomingDecision = (typeof INCOMING_DECISIONS)[number];
export type ExistingDecision = (typeof EXISTING_DECISIONS)[number];
export type Decision = IncomingDecision | ExistingDecision;

export const EFFECTIVE_STATES = [
  "PREPARED",
  "REVIEWING",
  "READY_TO_ACTIVATE",
  "CANCELLED",
  "ACTIVATED",
] as const;

export type EffectiveState = (typeof EFFECTIVE_STATES)[number];

/**
 * A reading whose proof already justifies a decided fact. Reopening one is a
 * workflow nobody has designed, so the only answer is to skip it — and
 * skipping is not accepting.
 */
export const BLOCKED_DIFFERENCES: readonly Difference[] = [
  "BLOCKED_TERMINAL_REJECTED",
  "BLOCKED_TERMINAL_CORRECTED",
];

/**
 * Facts a replacement may never retire: one the person stated themselves, and
 * one another source also supports. The backend refuses it; the control is not
 * offered.
 */
export const PROTECTED_DIFFERENCES: readonly Difference[] = [
  "INDEPENDENTLY_SUPPORTED",
  "USER_INPUT_REPLACEMENT",
];

export type ReplacementSummary = {
  replacement_id: number;
  extraction_id: number;
  baseline_document_id: number | null;
  lifecycle: string;
  effective_state: EffectiveState;
  ready_review_digest: string | null;
  activation_revision: number | null;
  activated_at: string | null;
};

export type ReviewProgress = {
  plan_entries: number;
  answered: number;
  unanswered: number;
  decisions_outside_plan: number;
  stale_decisions: number;
  counts_by_difference: Record<string, number>;
};

export type ReviewEntry = {
  role: "INCOMING" | "EXISTING";
  difference: Difference;
  candidate_id: number | null;
  fact_id: number | null;
  fact_type: string;
  fact_status: string | null;
  /** The reading the person has to examine. Private: see `lib/cv-replacement.ts`. */
  display_value: string;
  decision: Decision | null;
  has_staged_value: boolean;
};

export type ReviewSnapshot = {
  replacement: ReplacementSummary | null;
  progress: ReviewProgress | null;
  entries: readonly ReviewEntry[];
};

export type ActivationResult = {
  replacement_id: number;
  activated: boolean;
  activation_revision: number;
  previous_document_id: number | null;
  document_id: number;
  facts_accepted: number;
  facts_rejected: number;
  facts_corrected: number;
  facts_retired: number;
  evidence_attached: number;
  decisions_skipped: number;
  effective_state: EffectiveState;
};

export type CancelResult = {
  replacement_id: number;
  effective_state: EffectiveState;
};

/** French wording for each classification, as the review must explain it. */
const DIFFERENCE_LABELS: Record<Difference, string> = {
  NEW: "Nouvelle lecture",
  UNCHANGED_STILL_SUPPORTED: "Lecture identique, toujours présente",
  // Absence is UNKNOWN. It is never evidence that the person lost anything,
  // and the wording has to say so rather than imply a loss.
  ABSENT_FROM_NEW_CV: "Absente du nouveau CV",
  INDEPENDENTLY_SUPPORTED: "Appuyée par une autre source",
  USER_INPUT_REPLACEMENT: "Issue d’une correction que vous avez saisie",
  ALREADY_PROPOSED: "Déjà proposée, en attente de décision",
  ALREADY_ACCEPTED: "Déjà acceptée",
  BLOCKED_TERMINAL_REJECTED: "Déjà refusée définitivement",
  BLOCKED_TERMINAL_CORRECTED: "Déjà remplacée par une correction",
};

const DIFFERENCE_EXPLANATIONS: Record<Difference, string> = {
  NEW: "Le nouveau CV propose cette lecture, que le CV actif ne produisait pas.",
  UNCHANGED_STILL_SUPPORTED:
    "Le nouveau CV et le CV actif disent exactement la même chose.",
  ABSENT_FROM_NEW_CV:
    "Le nouveau CV ne mentionne pas cette information. Cela ne prouve pas que " +
    "vous l’avez perdue : c’est une absence, pas une contradiction. Conservez-la " +
    "si elle est toujours vraie.",
  INDEPENDENTLY_SUPPORTED:
    "Une source autre qu’un CV appuie ce fait. Ce remplacement ne peut pas le retirer.",
  USER_INPUT_REPLACEMENT:
    "Ce fait vient d’une correction que vous avez saisie. Ce remplacement ne peut pas le retirer.",
  ALREADY_PROPOSED:
    "Cette preuve justifie déjà un fait qui attend votre décision ailleurs.",
  ALREADY_ACCEPTED: "Cette preuve justifie déjà un fait que vous avez accepté.",
  BLOCKED_TERMINAL_REJECTED:
    "Vous aviez déjà refusé cette lecture. Elle ne peut pas être rouverte ici : " +
    "elle peut seulement être passée, ce qui ne l’accepte pas.",
  BLOCKED_TERMINAL_CORRECTED:
    "Une correction a déjà remplacé cette lecture. Elle ne peut pas être rouverte " +
    "ici : elle peut seulement être passée, ce qui ne l’accepte pas.",
};

const DECISION_LABELS: Record<Decision, string> = {
  ACCEPT: "Accepter",
  REJECT: "Refuser",
  CORRECT: "Corriger",
  SKIP_BLOCKED: "Passer",
  KEEP: "Conserver",
  RETIRE: "Retirer du profil",
};

/**
 * What each answer actually does, in the person's own terms.
 *
 * REJECT and SKIP_BLOCKED are the two that a careless wording would turn into
 * an acceptance, so both say plainly what they are not.
 */
const DECISION_EXPLANATIONS: Record<Decision, string> = {
  ACCEPT: "Cette lecture entre dans votre profil.",
  REJECT:
    "Le refus est mémorisé : la lecture est enregistrée comme refusée, elle " +
    "n’entre pas dans votre profil et n’est pas considérée comme acceptée.",
  CORRECT: "Votre texte remplace la lecture du CV.",
  SKIP_BLOCKED:
    "Aucune décision n’est prise : la lecture reste dans l’état où vous l’aviez " +
    "laissée. Passer n’est pas accepter.",
  KEEP: "Le fait reste dans votre profil, inchangé.",
  RETIRE:
    "Le fait sort du profil actif. Il reste consultable dans l’historique et " +
    "reste marqué comme validé à l’époque : rien n’est supprimé.",
};

export const differenceLabel = (value: Difference): string =>
  DIFFERENCE_LABELS[value];

export const differenceExplanation = (value: Difference): string =>
  DIFFERENCE_EXPLANATIONS[value];

export const decisionLabel = (value: Decision): string => DECISION_LABELS[value];

export const decisionExplanation = (value: Decision): string =>
  DECISION_EXPLANATIONS[value];

/**
 * Which answers this entry may actually take.
 *
 * A blocked reading takes only SKIP_BLOCKED; a protected fact cannot be
 * retired. Both rules are the backend's, repeated here only so that the page
 * does not offer a button whose click is already known to fail.
 */
export function allowedDecisions(entry: ReviewEntry): readonly Decision[] {
  if (entry.role === "INCOMING") {
    return BLOCKED_DIFFERENCES.includes(entry.difference)
      ? ["SKIP_BLOCKED"]
      : ["ACCEPT", "REJECT", "CORRECT"];
  }
  return PROTECTED_DIFFERENCES.includes(entry.difference)
    ? ["KEEP"]
    : ["KEEP", "RETIRE"];
}

/**
 * What the review looks like after the backend answered 409.
 *
 * A conflict means the review moved under the person: the token they were
 * about to confirm no longer describes what is stored. Two things follow, and
 * they are separate.
 *
 * The confirmation in progress is always dropped — whether or not the reload
 * worked, the thing they were confirming is gone.
 *
 * Then it depends on whether the review could be re-read. If it could, the new
 * one is shown and they are asked to look again and confirm again. If it could
 * not, the page must not claim it reloaded anything: it says so, and it blocks
 * activation, because the only snapshot left on screen is the one already known
 * to be stale. Activating from it is exactly what the conflict was warning
 * about, and no fresher token is ever fetched to make it succeed.
 */
export type ConflictOutcome = {
  /** Always false: what they were confirming no longer exists. */
  confirming: false;
  /** True when activation must not proceed from what is on screen. */
  blocked: boolean;
  /** The review as it now stands, or null when it could not be re-read. */
  snapshot: ReviewSnapshot | null;
  message: string;
};

export const CONFLICT_RELOADED =
  "La revue a changé depuis votre dernier affichage. Elle vient d’être " +
  "rechargée : vérifiez-la, puis confirmez à nouveau.";

export const CONFLICT_NOT_RELOADED =
  "La revue a changé depuis votre dernier affichage, et n’a pas pu être " +
  "rechargée. Ce qui est affiché ci-dessous n’est plus à jour : l’activation " +
  "est bloquée tant que la revue n’a pas été rechargée.";

export function afterConflict(refreshed: ReviewSnapshot | null): ConflictOutcome {
  return refreshed === null
    ? {
        confirming: false,
        blocked: true,
        snapshot: null,
        message: CONFLICT_NOT_RELOADED,
      }
    : {
        confirming: false,
        blocked: false,
        snapshot: refreshed,
        message: CONFLICT_RELOADED,
      };
}

/**
 * Whether a review is still open, according to the snapshot in hand.
 *
 * A cancelled or activated attempt is not something to keep offering buttons
 * for, and the answer does not depend on a refresh having succeeded: the
 * backend confirming a cancellation is enough to know.
 */
export function isOpenState(state: EffectiveState): boolean {
  return state === "PREPARED" || state === "REVIEWING" || state === "READY_TO_ACTIVATE";
}

/** A stable key for one plan entry, used for React lists and form names. */
export const entryKey = (entry: ReviewEntry): string =>
  entry.role === "INCOMING"
    ? `candidate-${entry.candidate_id}`
    : `fact-${entry.fact_id}`;
