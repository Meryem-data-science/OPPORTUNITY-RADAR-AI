import Link from "next/link";
import React from "react";
import OpportunityList, {
  type TrackedStatuses,
} from "@/components/opportunity-list";
import PushNotifications from "@/components/push-notifications";
import type { ApplicationStatus } from "@/lib/application-contract";
import { loadApplications } from "@/lib/applications";
import { loadOpportunities } from "@/lib/opportunities";

export default async function Home() {
  // Both surfaces are read independently: the tracking one being unavailable
  // must not stop the radar from showing what it found.
  const [opportunities, applications] = await Promise.all([
    loadOpportunities(),
    loadApplications(),
  ]);
  // A plain object rather than a Map: this crosses into a client component,
  // and only serializable props do.
  const tracked: TrackedStatuses = Object.fromEntries(
    (applications?.items ?? []).map((application) => [
      String(application.opportunity_id),
      application.status as ApplicationStatus,
    ]),
  );

  return (
    <main className="page-shell">
      <header className="hero">
        <div>
          <p className="eyebrow">Veille d’opportunités</p>
          <h1>Opportunity Radar AI</h1>
          <p className="subtitle">
            Les opportunités affichées proviennent des sources collectées par le
            radar. Leur date de validité n’est pas vérifiée&nbsp;: certaines
            annonces peuvent déjà être closes.
          </p>
        </div>
        <nav className="hero-links" aria-label="Pages de supervision">
          <Link className="health-link" href="/recommendation">
            Recommandé pour mon CV
          </Link>
          <Link className="health-link" href="/explorer">
            Explorer Data & AI
          </Link>
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
            <p>{opportunities.total} opportunités enregistrées par le radar</p>
          </div>

          <OpportunityList
            initial={opportunities.items}
            total={opportunities.total}
            tracked={tracked}
          />
        </section>
      )}
    </main>
  );
}
