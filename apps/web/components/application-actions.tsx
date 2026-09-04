"use client";

import React from "react";
import { useRouter } from "next/navigation";
import {
  APPLICATION_ACTIONS,
  statusLabel,
  type ApplicationAction,
  type ApplicationStatus,
} from "@/lib/application-contract";

const LABELS: Record<ApplicationAction, string> = {
  SAVE: "Sauvegarder",
  PREPARE: "Préparer la candidature",
  MARK_SUBMITTED: "J’ai postulé",
};

const ORDER: readonly ApplicationAction[] = APPLICATION_ACTIONS;

/**
 * The three real actions on a real opportunity.
 *
 * Each one posts to this app's own route handler, which forwards to FastAPI.
 * Nothing here decides anything: the backend creates the candidature, refuses
 * an action that would walk a concluded one backwards, and answers a repeated
 * click with the candidature unchanged. The button reports whichever of those
 * happened rather than assuming it worked.
 */
export default function ApplicationActions({
  opportunityId,
  status,
}: {
  opportunityId: number;
  status: ApplicationStatus | null;
}) {
  const router = useRouter();
  const [busy, setBusy] = React.useState(false);
  const [message, setMessage] = React.useState<string | null>(null);

  async function run(action: ApplicationAction) {
    setBusy(true);
    setMessage(null);
    try {
      const response = await fetch("/api/applications", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ opportunity_id: opportunityId, action }),
      });
      if (!response.ok) {
        setMessage(
          response.status === 409
            ? "Cette candidature est terminée."
            : "L’action n’a pas pu être enregistrée.",
        );
        return;
      }
      const payload: { changed?: unknown } = await response.json();
      setMessage(
        payload.changed === true
          ? "Candidature mise à jour."
          : "Candidature déjà à ce stade.",
      );
      router.refresh();
    } catch {
      setMessage("L’action n’a pas pu être enregistrée.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="application-actions">
      {status !== null && (
        <p className="application-status" role="status">
          Suivi&nbsp;: {statusLabel(status)}
        </p>
      )}
      <p className="application-buttons">
        {ORDER.map((action) => (
          <button
            key={action}
            type="button"
            disabled={busy}
            onClick={() => run(action)}
          >
            {LABELS[action]}
          </button>
        ))}
      </p>
      {message !== null && <p role="status">{message}</p>}
    </div>
  );
}
