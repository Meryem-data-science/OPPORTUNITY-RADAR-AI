import Link from "next/link";
import React from "react";
import ApplicationActions from "@/components/application-actions";
import PushNotifications from "@/components/push-notifications";
import type { ApplicationStatus } from "@/lib/application-contract";
import { loadApplications } from "@/lib/applications";
import { loadOpportunities, type Opportunity } from "@/lib/opportunities";

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
      <ApplicationActions
        opportunityId={opportunity.id}
        status={trackedStatus}
      />
      <div className="card-footer">
        <span>Vue récemment par le radar</span>
        <a href={opportunity.original_url} target="_blank" rel="noreferrer">
          Voir l’offre originale
        </a>
      </div>
    </article>
  );
}

export default async function Home() {
  // Both surfaces are read independently: the tracking one being unavailable
  // must not stop the radar from showing what it found.
  const [opportunities, applications] = await Promise.all([
    loadOpportunities(),
    loadApplications(),
  ]);
  const tracked = new Map<number, ApplicationStatus>(
    (applications?.items ?? []).map((application) => [
      application.opportunity_id,
      application.status,
    ]),
  );

  return (
    <main className="page-shell">
      <header className="hero">
        <div>
          <p className="eyebrow">Veille d’opportunités</p>
          <h1>Opportunity Radar AI</h1>
          <p className="subtitle">
            Les opportunités affichées proviennent des sources collectées par le radar.
          </p>
        </div>
        <nav className="hero-links" aria-label="Pages de supervision">
          <Link className="health-link" href="/applications">
            Mes candidatures
          </Link>
          <Link className="health-link" href="/portfolio">
            Voir mon portfolio
          </Link>
          <Link className="health-link" href="/priority">
            Voir mes priorités
          </Link>
          <Link className="health-link" href="/matching">
            Voir mon matching
          </Link>
          <Link className="health-link" href="/source-health">
            État des sources
          </Link>
          <Link className="health-link" href="/health">
            État du système
          </Link>
        </nav>
      </header>

      <PushNotifications />

      {opportunities === null ? (
        <section className="status-panel" role="status">
          <h2>Opportunités temporairement indisponibles</h2>
          <p>Le radar ne peut pas afficher les opportunités pour le moment.</p>
        </section>
      ) : (
        <section aria-labelledby="opportunities-heading">
          <div className="section-heading">
            <h2 id="opportunities-heading">Opportunités détectées</h2>
            <p>{opportunities.total} opportunités détectées</p>
          </div>

          {opportunities.items.length === 0 ? (
            <div className="status-panel" role="status">
              <p>Aucune opportunité disponible pour le moment.</p>
            </div>
          ) : (
            <div className="opportunity-grid">
              {opportunities.items.map((opportunity) => (
                <OpportunityCard
                  key={opportunity.id}
                  opportunity={opportunity}
                  trackedStatus={tracked.get(opportunity.id) ?? null}
                />
              ))}
            </div>
          )}
        </section>
      )}
    </main>
  );
}
