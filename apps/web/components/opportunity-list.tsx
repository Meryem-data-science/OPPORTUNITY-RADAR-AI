"use client";

import React from "react";
import ApplicationActions from "@/components/application-actions";
import type { ApplicationStatus } from "@/lib/application-contract";
import {
  OPPORTUNITIES_PAGE_SIZE,
  applyOpportunityPage,
  hasMoreOpportunities,
  initialPageState,
  type Opportunity,
} from "@/lib/opportunity-contract";

/**
 * The opportunity listing, one page at a time.
 *
 * The first page arrives rendered from the server. Pressing "Afficher plus"
 * asks this app's own route handler for the next one and appends it, so the
 * list grows without the page reloading and without the browser ever knowing
 * the backend's address.
 *
 * Four things it is careful about:
 *
 * a page is appended only once. Opportunities already on screen are skipped by
 * id, so a row that appears in two answers — which the stable ordering is meant
 * to prevent, but which a collection landing mid-walk could still cause — is
 * rendered once;
 *
 * what is asked for next is counted in rows *received*, not in cards displayed.
 * Those two numbers differ the moment a duplicate arrives, and asking from the
 * card count would request rows the API had already handed over, get the same
 * page back, and never reach the end;
 *
 * the button reports what happened rather than what was clicked. It is disabled
 * while a request is in flight, which is also what stops a double click from
 * fetching the same page twice;
 *
 * the end of the list is stated, not implied by a button that stops working.
 * A page that comes back empty ends the walk even if the `total` read on the
 * first render has since gone stale.
 *
 * The bookkeeping itself lives in `applyOpportunityPage`, in the contract
 * module, so it can be tested against real answers rather than through a
 * simulated click.
 */

const LOAD_FAILED =
  "Les offres suivantes n’ont pas pu être chargées. Réessayez.";

export type TrackedStatuses = Readonly<Record<string, ApplicationStatus>>;

function OpportunityCard({
  opportunity,
  trackedStatus,
}: {
  opportunity: Opportunity;
  trackedStatus: ApplicationStatus | null;
}) {
  return (
    <article className="opportunity-card">
      <div>
        <p className="organization">{opportunity.organization}</p>
        <h2>{opportunity.canonical_title}</h2>
        <p className="location">{opportunity.location ?? "Lieu non précisé"}</p>
      </div>
      <ApplicationActions opportunityId={opportunity.id} status={trackedStatus} />
      <div className="card-footer">
        <span>Vue récemment par le radar</span>
        <a href={opportunity.original_url} target="_blank" rel="noreferrer">
          Voir l’offre originale
        </a>
      </div>
    </article>
  );
}

export default function OpportunityList({
  initial,
  total,
  tracked,
  pageSize = OPPORTUNITIES_PAGE_SIZE,
}: {
  initial: readonly Opportunity[];
  total: number;
  tracked: TrackedStatuses;
  pageSize?: number;
}) {
  const [page, setPage] = React.useState(() => initialPageState(initial));
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  const items = page.items;
  const shown = items.length;
  const more = hasMoreOpportunities(page, total);

  async function loadMore(): Promise<void> {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      const response = await fetch(
        `/api/opportunities?limit=${pageSize}&offset=${page.nextOffset}`,
        { cache: "no-store" },
      );
      if (!response.ok) {
        setError(LOAD_FAILED);
        return;
      }
      const outcome = applyOpportunityPage(page, await response.json());
      if (outcome.kind === "malformed") {
        // An unreadable answer is a failure to report, not an empty page:
        // calling it the end would close the listing on a guess.
        setError(LOAD_FAILED);
        return;
      }
      setPage(outcome.state);
    } catch {
      setError(LOAD_FAILED);
    } finally {
      setBusy(false);
    }
  }

  if (items.length === 0) {
    return (
      <div className="status-panel" role="status">
        <p>Aucune opportunité disponible pour le moment.</p>
      </div>
    );
  }

  return (
    <>
      <div className="opportunity-grid">
        {items.map((opportunity) => (
          <OpportunityCard
            key={opportunity.id}
            opportunity={opportunity}
            trackedStatus={tracked[String(opportunity.id)] ?? null}
          />
        ))}
      </div>

      <div className="opportunity-pagination">
        <p role="status">
          {shown} opportunité{shown > 1 ? "s" : ""} affichée
          {shown > 1 ? "s" : ""} sur {total}
        </p>
        {error !== null && <p role="alert">{error}</p>}
        {!more ? (
          <p role="status">
            Fin de la liste. Toutes les opportunités enregistrées sont
            affichées.
          </p>
        ) : (
          <p className="application-buttons">
            <button
              type="button"
              disabled={busy}
              aria-busy={busy}
              onClick={loadMore}
            >
              {busy ? "Chargement…" : "Afficher plus"}
            </button>
          </p>
        )}
      </div>
    </>
  );
}
