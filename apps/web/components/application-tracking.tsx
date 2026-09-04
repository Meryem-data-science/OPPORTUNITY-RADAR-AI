"use client";

import React from "react";
import { useRouter } from "next/navigation";
import {
  SELECTABLE_STATUSES,
  statusLabel,
  type Application,
  type SelectableStatus,
} from "@/lib/application-contract";

/**
 * The manual controls of one tracked candidature.
 *
 * The status list is the one Phase 6.1 actually supports: READY and
 * DISCOVERED are absent because the backend refuses them, and a control the
 * backend would reject has no business existing. The notes, the next action
 * and the follow-up date are written through the tracking route, which cannot
 * change a status, an owner or the submission instant even if asked.
 */
export default function ApplicationTracking({
  application,
}: {
  application: Application;
}) {
  const router = useRouter();
  const [busy, setBusy] = React.useState(false);
  const [message, setMessage] = React.useState<string | null>(null);
  const [nextAction, setNextAction] = React.useState(application.next_action ?? "");
  const [followup, setFollowup] = React.useState(application.followup_date ?? "");
  const [notes, setNotes] = React.useState(application.notes ?? "");

  async function send(path: string, body: unknown, refusal: string) {
    setBusy(true);
    setMessage(null);
    try {
      const response = await fetch(path, {
        method: "PATCH",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!response.ok) {
        setMessage(response.status === 409 ? refusal : "Modification refusée.");
        return;
      }
      const payload: { changed?: unknown } = await response.json();
      setMessage(
        payload.changed === true ? "Modification enregistrée." : "Aucun changement.",
      );
      router.refresh();
    } catch {
      setMessage("Modification refusée.");
    } finally {
      setBusy(false);
    }
  }

  function changeStatus(status: SelectableStatus) {
    return send(
      `/api/applications/${application.id}/status`,
      { status },
      "Ce statut n’est pas cohérent avec cette candidature.",
    );
  }

  function saveTracking(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    // An empty field clears the stored value; the backend reads "" and null
    // as the same intention.
    return send(
      `/api/applications/${application.id}`,
      {
        next_action: nextAction.trim() === "" ? null : nextAction,
        followup_date: followup.trim() === "" ? null : followup,
        notes: notes.trim() === "" ? null : notes,
      },
      "Modification refusée.",
    );
  }

  const statusField = `application-${application.id}-status`;

  return (
    <div className="application-tracking">
      <p>
        <label htmlFor={statusField}>Statut</label>{" "}
        <select
          id={statusField}
          value={application.status}
          disabled={busy}
          onChange={(event) => changeStatus(event.target.value as SelectableStatus)}
        >
          {SELECTABLE_STATUSES.map((status) => (
            <option key={status} value={status}>
              {statusLabel(status)}
            </option>
          ))}
          {!(SELECTABLE_STATUSES as readonly string[]).includes(application.status) && (
            <option value={application.status} disabled>
              {statusLabel(application.status)}
            </option>
          )}
        </select>
      </p>

      <form onSubmit={saveTracking}>
        <p>
          <label htmlFor={`application-${application.id}-next-action`}>
            Prochaine action
          </label>{" "}
          <input
            id={`application-${application.id}-next-action`}
            type="text"
            maxLength={500}
            value={nextAction}
            disabled={busy}
            onChange={(event) => setNextAction(event.target.value)}
          />
        </p>
        <p>
          <label htmlFor={`application-${application.id}-followup`}>
            Date de relance
          </label>{" "}
          <input
            id={`application-${application.id}-followup`}
            type="date"
            value={followup}
            disabled={busy}
            onChange={(event) => setFollowup(event.target.value)}
          />
        </p>
        <p>
          <label htmlFor={`application-${application.id}-notes`}>Notes</label>{" "}
          <textarea
            id={`application-${application.id}-notes`}
            maxLength={4000}
            rows={3}
            value={notes}
            disabled={busy}
            onChange={(event) => setNotes(event.target.value)}
          />
        </p>
        <p>
          <button type="submit" disabled={busy}>
            Enregistrer le suivi
          </button>
        </p>
      </form>

      {message !== null && <p role="status">{message}</p>}
    </div>
  );
}
