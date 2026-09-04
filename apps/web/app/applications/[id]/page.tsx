import Link from "next/link";
import React from "react";
import ApplicationTracking from "@/components/application-tracking";
import { statusLabel, type ApplicationEvent } from "@/lib/application-contract";
import { loadApplication } from "@/lib/applications";

const EVENT_LABELS: Record<ApplicationEvent["event_type"], string> = {
  APPLICATION_CREATED: "Candidature créée",
  STATUS_CHANGED: "Changement de statut",
  TRACKING_UPDATED: "Suivi mis à jour",
};

function describe(event: ApplicationEvent): string {
  if (event.event_type === "STATUS_CHANGED" && event.from_status && event.to_status) {
    return `${statusLabel(event.from_status)} → ${statusLabel(event.to_status)}`;
  }
  if (event.event_type === "APPLICATION_CREATED" && event.to_status) {
    return statusLabel(event.to_status);
  }
  return "";
}

export default async function ApplicationDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  const application = /^[1-9][0-9]{0,15}$/.test(id)
    ? await loadApplication(Number(id))
    : null;

  if (application === null) {
    return (
      <main className="page-shell">
        <section className="status-panel" role="status">
          <h1>Candidature introuvable</h1>
          <p>Cette candidature n’existe pas ou ne peut pas être affichée.</p>
          <Link className="health-link" href="/applications">
            Retour aux candidatures
          </Link>
        </section>
      </main>
    );
  }

  const { opportunity } = application;

  return (
    <main className="page-shell">
      <header className="hero">
        <div>
          <p className="eyebrow">{opportunity.organization}</p>
          <h1>{opportunity.canonical_title}</h1>
          <p className="subtitle">{opportunity.location ?? "Lieu non précisé"}</p>
          <p className="application-status">
            Statut&nbsp;: {statusLabel(application.status)}
          </p>
        </div>
        <nav className="hero-links" aria-label="Navigation">
          <Link className="health-link" href="/applications">
            Retour aux candidatures
          </Link>
          <a
            className="health-link"
            href={opportunity.original_url}
            target="_blank"
            rel="noreferrer"
          >
            Voir l’offre originale
          </a>
        </nav>
      </header>

      <section aria-labelledby="application-tracking-heading">
        <div className="section-heading">
          <h2 id="application-tracking-heading">Suivi</h2>
          <p>
            Envoyée&nbsp;: {application.submitted_at ?? "pas encore envoyée"} ·
            Dernier changement&nbsp;: {application.last_status_change}
          </p>
        </div>
        <ApplicationTracking application={application} />
      </section>

      <section aria-labelledby="application-timeline-heading">
        <div className="section-heading">
          <h2 id="application-timeline-heading">Historique</h2>
          <p>{application.events.length} événements</p>
        </div>
        <ol className="application-timeline">
          {application.events.map((event) => (
            <li key={event.id}>
              <span>{event.occurred_at}</span> — {EVENT_LABELS[event.event_type]}
              {describe(event) === "" ? "" : ` (${describe(event)})`}
            </li>
          ))}
        </ol>
      </section>
    </main>
  );
}
