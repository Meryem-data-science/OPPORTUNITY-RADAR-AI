import Link from "next/link";
import React from "react";
import ApplicationTracking from "@/components/application-tracking";
import Icon from "@/components/icon";
import { statusLabel, type Application } from "@/lib/application-contract";
import { loadApplications } from "@/lib/applications";
import { formatDate, formatDateTime, organizationInitials } from "@/lib/display";

function ApplicationCard({ application }: { application: Application }) {
  const { opportunity } = application;
  return (
    <article className="opportunity-card">
      <div>
        <div className="opportunity-card-top"><span className="organization-avatar" aria-hidden="true">{organizationInitials(opportunity.organization)}</span><div className="opportunity-card-body"><p className="organization">{opportunity.organization}</p><h3>{opportunity.canonical_title}</h3><p className="location"><Icon name="pin" size={15} />{opportunity.location ?? "Lieu non précisé"}</p></div></div>
        <p className="application-status">
          Statut&nbsp;: {statusLabel(application.status)}
        </p>
        <p>
          Candidature envoyée&nbsp;:{" "}
          {application.submitted_at === null ? "pas encore envoyée" : formatDateTime(application.submitted_at)}
        </p>
        <p>Dernier changement&nbsp;: {formatDateTime(application.last_status_change)}</p>
        <p>
          Prochaine action&nbsp;: {application.next_action ?? "aucune"}
          {" · "}
          Relance&nbsp;: {application.followup_date === null ? "aucune" : formatDate(application.followup_date)}
        </p>
        {application.notes !== null && (
          <details className="application-notes"><summary>Notes de candidature</summary><p>{application.notes}</p></details>
        )}
      </div>

      <details className="application-edit"><summary>Mettre à jour le suivi</summary><ApplicationTracking application={application} /></details>

      <div className="card-footer">
        <Link href={`/applications/${application.id}`}>Voir le détail<Icon name="arrow-right" size={16} /></Link>
        <a href={opportunity.original_url} target="_blank" rel="noopener noreferrer">Voir l’offre originale<Icon name="arrow-up-right" size={16} /></a>
      </div>
    </article>
  );
}

export default async function ApplicationsPage() {
  const applications = await loadApplications();

  return (
    <main className="page-shell">
      <header className="hero">
        <div>
          <p className="eyebrow">Suivi de candidatures</p>
          <h1>Mes candidatures</h1>
          <p className="subtitle">
            Vos offres sauvegardées et vos candidatures, avec chaque prochaine étape au même endroit.
          </p>
        </div>
        <nav className="hero-links" aria-label="Navigation">
          <Link className="health-link" href="/explorer">
            Trouver une offre<Icon name="arrow-right" size={16} />
          </Link>
        </nav>
      </header>

      {applications === null ? (
        <section className="status-panel" role="status">
          <h2>Candidatures temporairement indisponibles</h2>
          <p>Le suivi des candidatures ne peut pas être affiché pour le moment.</p>
        </section>
      ) : applications.items.length === 0 ? (
        <section className="status-panel" role="status">
          <h2>Aucune candidature suivie</h2>
          <p>
            Sauvegardez une opportunité, préparez-la ou indiquez que vous avez
            postulé pour la voir apparaître ici.
          </p>
          <Link className="health-link" href="/explorer">Explorer les offres<Icon name="arrow-right" size={16} /></Link>
        </section>
      ) : (
        <section aria-labelledby="applications-heading">
          <div className="section-heading">
            <h2 id="applications-heading">Candidatures suivies</h2>
            <p>{applications.total} candidatures suivies</p>
          </div>
          <div className="opportunity-grid">
            {applications.items.map((application) => (
              <ApplicationCard key={application.id} application={application} />
            ))}
          </div>
        </section>
      )}
    </main>
  );
}
