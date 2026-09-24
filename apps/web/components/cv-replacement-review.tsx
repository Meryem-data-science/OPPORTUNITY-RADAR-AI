"use client";

import React from "react";
import {
  afterConflict,
  allowedDecisions,
  decisionExplanation,
  decisionLabel,
  differenceExplanation,
  differenceLabel,
  entryKey,
  isOpenState,
  type ActivationResult,
  type Decision,
  type ReviewEntry,
  type ReviewSnapshot,
} from "@/lib/cv-replacement-contract";

/**
 * The review itself: every reading, one answer each, and two separate
 * confirmations before anything changes.
 *
 * Nothing here decides for the person. No answer is preselected, an unanswered
 * entry stays unanswered — it never becomes an acceptance by omission — and the
 * page never infers a success from a click: every button waits for the answer
 * and reports what actually happened.
 *
 * Declaring the review ready and activating the new CV are two deliberate acts.
 * `ready` seals the review and hands back the token describing it; activating
 * sends that same token back. If the backend answers 409 the review has moved
 * under the person, so this refreshes it, says so, and asks for a fresh
 * confirmation rather than retrying with a newer token — retrying would be
 * exactly the way to slip an unreviewed change past them.
 *
 * The readings are visible because they are what the person is here to read.
 * They are never put in a URL, a title, a log or browser storage.
 */

const UNAVAILABLE = "L’action n’a pas pu être enregistrée.";
const RELOAD_FAILED =
  "La revue n’a pas pu être rechargée. Rien n’a été modifié.";
const CANCELLED_MESSAGE =
  "Revue annulée. Vos réponses restent consultables, et cette revue ne peut " +
  "plus être activée.";

type Busy = string | null;

function Progress({ snapshot }: { snapshot: ReviewSnapshot }) {
  const progress = snapshot.progress;
  if (progress === null) return null;
  return (
    <section className="status-panel" aria-labelledby="cv-progress-heading">
      <h2 id="cv-progress-heading">Progression</h2>
      <p role="status">
        {progress.answered} sur {progress.plan_entries} éléments répondus
        {progress.unanswered > 0
          ? ` · ${progress.unanswered} en attente de votre décision`
          : " · revue complète"}
      </p>
      {progress.stale_decisions > 0 && (
        <p role="alert">
          {progress.stale_decisions} décision(s) ne correspondent plus à l’état
          actuel de votre profil. Elles doivent être reprises avant d’aller plus
          loin.
        </p>
      )}
      {progress.decisions_outside_plan > 0 && (
        <p role="alert">
          {progress.decisions_outside_plan} décision(s) ne font plus partie de
          cette revue. Rechargez la revue avant de continuer.
        </p>
      )}
    </section>
  );
}

function EntryCard({
  entry,
  busy,
  onDecide,
}: {
  entry: ReviewEntry;
  busy: Busy;
  onDecide: (entry: ReviewEntry, decision: Decision, value?: string) => void;
}) {
  const key = entryKey(entry);
  const [correction, setCorrection] = React.useState("");
  const options = allowedDecisions(entry);
  const pending = busy === key;

  return (
    <article className="opportunity-card" aria-labelledby={`${key}-heading`}>
      <div>
        <p className="eyebrow">
          {entry.role === "INCOMING" ? "Nouveau CV" : "Profil actuel"} ·{" "}
          {entry.fact_type}
        </p>
        {/* The reading the person has to examine, shown in full. */}
        <h3 id={`${key}-heading`} className="cv-reading">
          {entry.display_value}
        </h3>
        <p className="cv-difference">{differenceLabel(entry.difference)}</p>
        <p className="cv-explanation">{differenceExplanation(entry.difference)}</p>
        {entry.decision === null ? (
          <p className="cv-undecided" role="status">
            Aucune décision enregistrée. Tant que vous n’en prenez pas, rien
            n’est appliqué à cette lecture.
          </p>
        ) : (
          <p className="cv-decided" role="status">
            Votre décision&nbsp;: {decisionLabel(entry.decision)}
            {entry.has_staged_value ? " (correction enregistrée)" : ""}
            {" — "}
            {decisionExplanation(entry.decision)}
          </p>
        )}
      </div>

      <div className="application-actions">
        <p className="application-buttons">
          {options.map((decision) => (
            <button
              key={decision}
              type="button"
              disabled={busy !== null}
              aria-busy={pending}
              onClick={() =>
                onDecide(
                  entry,
                  decision,
                  decision === "CORRECT" ? correction : undefined,
                )
              }
            >
              {decisionLabel(decision)}
            </button>
          ))}
        </p>
        <ul className="cv-decision-help">
          {options.map((decision) => (
            <li key={decision}>
              <strong>{decisionLabel(decision)}</strong>{" — "}
              {decisionExplanation(decision)}
            </li>
          ))}
        </ul>
        {options.includes("CORRECT") && (
          <p>
            <label htmlFor={`${key}-correction`}>
              Texte corrigé (obligatoire pour «&nbsp;Corriger&nbsp;»)
            </label>
            <input
              id={`${key}-correction`}
              name={`${key}-correction`}
              type="text"
              autoComplete="off"
              value={correction}
              disabled={busy !== null}
              onChange={(event) => setCorrection(event.target.value)}
            />
          </p>
        )}
      </div>
    </article>
  );
}

export default function CvReplacementReview({
  initial,
}: {
  initial: ReviewSnapshot;
}) {
  const [snapshot, setSnapshot] = React.useState(initial);
  const [busy, setBusy] = React.useState<Busy>(null);
  const [message, setMessage] = React.useState<string | null>(null);
  const [activation, setActivation] = React.useState<ActivationResult | null>(
    null,
  );
  const [confirming, setConfirming] = React.useState(false);
  // Set when the review is known to have moved and could not be re-read. The
  // snapshot on screen is then stale by definition, so nothing may be
  // activated from it until a reload succeeds.
  const [blocked, setBlocked] = React.useState(false);
  // The backend confirmed a cancellation. That is enough to stop presenting
  // the attempt as live, whether or not the refresh that follows succeeds.
  const [cancelled, setCancelled] = React.useState(false);
  const [extractionId, setExtractionId] = React.useState("");

  // `setSnapshot` does not update `snapshot` until the next render, and the
  // conflict branch needs the review it has just re-read. The ref follows it.
  const snapshotRef = React.useRef(snapshot);
  React.useEffect(() => {
    snapshotRef.current = snapshot;
  }, [snapshot]);

  const replacement = snapshot.replacement;

  /**
   * Re-read the review, and say whether it worked.
   *
   * A failed refresh leaves the last known review on screen — replacing it
   * with an invented one would be worse — but the caller has to know, because
   * "reloaded" and "could not reload" are two different things to tell
   * somebody who is about to activate.
   */
  async function refresh(): Promise<boolean> {
    try {
      const response = await fetch("/api/cv/replacements", { cache: "no-store" });
      if (!response.ok) return false;
      const fresh = (await response.json()) as ReviewSnapshot;
      snapshotRef.current = fresh;
      setSnapshot(fresh);
      setBlocked(false);
      return true;
    } catch {
      return false;
    }
  }

  async function reload(): Promise<void> {
    if (busy !== null) return;
    setBusy("reload");
    setMessage(null);
    try {
      setMessage((await refresh()) ? null : RELOAD_FAILED);
    } finally {
      setBusy(null);
    }
  }

  /** Run one action, and report what the backend actually answered. */
  async function run(
    token: string,
    request: () => Promise<Response>,
    onSuccess: (payload: unknown) => void,
  ): Promise<void> {
    if (busy !== null) return; // One action at a time: no double click, no race.
    setBusy(token);
    setMessage(null);
    try {
      const response = await fetch_guarded(request);
      if (response === null) {
        setMessage(UNAVAILABLE);
        return;
      }
      if (!response.ok) {
        if (response.status === 409) {
          // The review moved under the person. What follows is decided by one
          // pure function, so it is the same whatever action hit the conflict,
          // and it is never a retry: no fresher token is fetched to make the
          // activation succeed.
          const reloaded = await refresh();
          const outcome = afterConflict(reloaded ? snapshotRef.current : null);
          setConfirming(outcome.confirming);
          setBlocked(outcome.blocked);
          setMessage(outcome.message);
          return;
        }
        setMessage(UNAVAILABLE);
        return;
      }
      onSuccess(await response.json());
    } catch {
      setMessage(UNAVAILABLE);
    } finally {
      setBusy(null);
    }
  }

  async function fetch_guarded(
    request: () => Promise<Response>,
  ): Promise<Response | null> {
    try {
      return await request();
    } catch {
      return null;
    }
  }

  function decide(entry: ReviewEntry, decision: Decision, value?: string) {
    if (decision === "CORRECT" && (value ?? "").trim() === "") {
      setMessage("Saisissez le texte corrigé avant de choisir « Corriger ».");
      return;
    }
    const body =
      entry.role === "INCOMING"
        ? {
            candidate_id: entry.candidate_id,
            decision,
            // Sent exactly as typed: trimming it would change the correction
            // the person is about to see stored under their own name.
            ...(decision === "CORRECT" ? { staged_value: value } : {}),
          }
        : { fact_id: entry.fact_id, decision };
    void run(
      entryKey(entry),
      () =>
        fetch(`/api/cv/replacements/${replacement?.replacement_id}/decision`, {
          method: "PUT",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(body),
        }),
      (payload) => {
        const fresh = payload as ReviewSnapshot;
        snapshotRef.current = fresh;
        setSnapshot(fresh);
        setConfirming(false);
        setBlocked(false);
        setMessage("Décision enregistrée.");
      },
    );
  }

  function open() {
    const parsed = Number(extractionId);
    if (!/^[1-9][0-9]{0,15}$/.test(extractionId)) {
      setMessage("Indiquez l’identifiant numérique d’une extraction existante.");
      return;
    }
    void run(
      "open",
      () =>
        fetch("/api/cv/replacements", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ extraction_id: parsed }),
        }),
      (payload) => {
        setSnapshot(payload as ReviewSnapshot);
        setMessage("Revue ouverte.");
      },
    );
  }

  function markReady() {
    // No body at all: the backend takes none and refuses one.
    void run(
      "ready",
      () =>
        fetch(`/api/cv/replacements/${replacement?.replacement_id}/ready`, {
          method: "POST",
        }),
      (payload) => {
        setSnapshot(payload as ReviewSnapshot);
        // Declaring the review ready activates nothing. The second, separate
        // confirmation below is what does.
        setConfirming(true);
        setMessage(
          "Revue déclarée prête. Rien n’a encore été appliqué : confirmez " +
            "l’activation ci-dessous.",
        );
      },
    );
  }

  function activate(digest: string) {
    void run(
      "activate",
      () =>
        fetch(`/api/cv/replacements/${replacement?.replacement_id}/activate`, {
          method: "POST",
          headers: { "content-type": "application/json" },
          // The token of the snapshot the person is confirming, passed through
          // untouched — never re-read from a fresher snapshot.
          body: JSON.stringify({ review_digest: digest }),
        }),
      (payload) => {
        setActivation(payload as ActivationResult);
        setConfirming(false);
        setMessage(null);
        void refresh();
      },
    );
  }

  function cancel() {
    void run(
      "cancel",
      () =>
        fetch(`/api/cv/replacements/${replacement?.replacement_id}/cancel`, {
          method: "POST",
        }),
      () => {
        // The backend confirmed it. The attempt stops being presented as live
        // here and now, so a refresh that fails cannot leave a cancelled
        // review on screen with its buttons still offered.
        setConfirming(false);
        setBlocked(false);
        setCancelled(true);
        setMessage(CANCELLED_MESSAGE);
        void refresh();
      },
    );
  }

  if (activation !== null) {
    return (
      <section className="status-panel" aria-labelledby="cv-activated-heading">
        <h2 id="cv-activated-heading">Nouveau CV activé</h2>
        <p role="status">
          Révision {activation.activation_revision} ·{" "}
          {activation.activated
            ? "activation appliquée"
            : "activation déjà appliquée, rien n’a été réécrit"}
        </p>
        <ul>
          <li>{activation.facts_accepted} lecture(s) acceptée(s)</li>
          <li>{activation.facts_rejected} lecture(s) refusée(s) et mémorisée(s)</li>
          <li>{activation.facts_corrected} correction(s) appliquée(s)</li>
          <li>{activation.facts_retired} fait(s) retiré(s) du profil actif</li>
          <li>{activation.evidence_attached} preuve(s) rattachée(s)</li>
          <li>{activation.decisions_skipped} élément(s) laissé(s) tels quels</li>
        </ul>
        <p>
          Les résultats calculés avant ce changement — compétences, profil
          structuré, éligibilité, Matching, Recommandation, Priorités et
          Portfolio — décrivent encore l’ancien profil. Ils ne sont plus
          présentés comme actuels tant qu’ils n’ont pas été resynchronisés, dans
          cet ordre. Aucune synchronisation n’est lancée automatiquement.
        </p>
      </section>
    );
  }

  if (cancelled || (replacement !== null && !isOpenState(replacement.effective_state))) {
    return (
      <section className="status-panel" aria-labelledby="cv-closed-heading">
        <h2 id="cv-closed-heading">Revue close</h2>
        <p role="status">{message ?? CANCELLED_MESSAGE}</p>
        <p>
          Cette revue ne peut plus être activée. Vos réponses restent
          consultables dans l’historique.
        </p>
      </section>
    );
  }

  if (replacement === null) {
    return (
      <section className="status-panel" aria-labelledby="cv-none-heading">
        <h2 id="cv-none-heading">Aucune revue en cours</h2>
        <p>
          Aucun remplacement de CV n’est en cours de revue pour votre profil.
        </p>
        <p>
          Vous pouvez ouvrir une revue sur une extraction <strong>déjà
          enregistrée</strong>. C’est une opération technique&nbsp;: elle
          n’envoie aucun fichier et ne lit aucun CV. Elle demande l’identifiant
          d’une extraction existante, que cette page ne peut pas lister.
        </p>
        <p>
          <label htmlFor="cv-extraction-id">
            Identifiant d’extraction existante
          </label>
          <input
            id="cv-extraction-id"
            name="cv-extraction-id"
            type="text"
            inputMode="numeric"
            autoComplete="off"
            value={extractionId}
            disabled={busy !== null}
            onChange={(event) => setExtractionId(event.target.value)}
          />
        </p>
        <p className="application-buttons">
          <button type="button" disabled={busy !== null} onClick={open}>
            Ouvrir la revue
          </button>
        </p>
        {message !== null && <p role="status">{message}</p>}
      </section>
    );
  }

  const ready = replacement.effective_state === "READY_TO_ACTIVATE";
  const digest = replacement.ready_review_digest;
  const complete = (snapshot.progress?.unanswered ?? 1) === 0;

  return (
    <div className="cv-review">
      <Progress snapshot={snapshot} />

      {message !== null && (
        <p className="status-panel" role="status">
          {message}
        </p>
      )}

      <section aria-labelledby="cv-entries-heading">
        <div className="section-heading">
          <h2 id="cv-entries-heading">Lectures à examiner</h2>
          <p>
            Chaque élément attend votre décision. Rien n’est décidé à votre
            place, et une absence de réponse n’est pas une acceptation.
          </p>
        </div>
        <div className="opportunity-grid">
          {snapshot.entries.map((entry) => (
            <EntryCard
              key={entryKey(entry)}
              entry={entry}
              busy={busy}
              onDecide={decide}
            />
          ))}
        </div>
      </section>

      <section className="status-panel" aria-labelledby="cv-finish-heading">
        <h2 id="cv-finish-heading">Terminer la revue</h2>
        {!ready && blocked && (
          <>
            <p role="alert">
              La revue affichée n’est plus à jour et n’a pas pu être rechargée.
              Rechargez-la avant de continuer.
            </p>
            <p className="application-buttons">
              <button type="button" disabled={busy !== null} onClick={reload}>
                Recharger la revue
              </button>
            </p>
          </>
        )}
        {!ready && !blocked && (
          <>
            <p>
              Déclarer la revue prête n’applique rien. C’est une première étape
              qui fige vos réponses&nbsp;; l’activation vous sera demandée
              ensuite, séparément.
            </p>
            <p className="application-buttons">
              <button
                type="button"
                disabled={busy !== null || !complete}
                onClick={markReady}
              >
                Déclarer la revue prête
              </button>
              <button type="button" disabled={busy !== null} onClick={cancel}>
                Annuler la revue
              </button>
            </p>
            {!complete && (
              <p role="status">
                Répondez à tous les éléments avant de déclarer la revue prête.
              </p>
            )}
          </>
        )}
        {ready && digest !== null && blocked && (
          <>
            <p role="alert">
              L’activation est bloquée&nbsp;: la revue affichée n’est plus à
              jour et n’a pas pu être rechargée. Rechargez-la, vérifiez-la, puis
              confirmez à nouveau.
            </p>
            <p className="application-buttons">
              <button type="button" disabled={busy !== null} onClick={reload}>
                Recharger la revue
              </button>
            </p>
          </>
        )}
        {ready && digest !== null && !blocked && (
          <>
            <p role="status">
              Revue prête et figée. <strong>Rien n’a encore été appliqué.</strong>{" "}
              Confirmez ci-dessous pour activer le nouveau CV.
            </p>
            <ul>
              <li>{snapshot.progress?.plan_entries ?? 0} élément(s) examiné(s)</li>
              <li>
                Empreinte de la revue confirmée&nbsp;:{" "}
                <code>{digest.slice(0, 12)}…</code>
              </li>
            </ul>
            {confirming ? (
              <p className="application-buttons">
                <button
                  type="button"
                  disabled={busy !== null}
                  onClick={() => activate(digest)}
                >
                  Confirmer l’activation du nouveau CV
                </button>
                <button
                  type="button"
                  disabled={busy !== null}
                  onClick={() => setConfirming(false)}
                >
                  Revenir à la revue
                </button>
              </p>
            ) : (
              <p className="application-buttons">
                <button
                  type="button"
                  disabled={busy !== null}
                  onClick={() => setConfirming(true)}
                >
                  Passer à l’activation
                </button>
                <button type="button" disabled={busy !== null} onClick={cancel}>
                  Annuler la revue
                </button>
              </p>
            )}
          </>
        )}
      </section>
    </div>
  );
}
