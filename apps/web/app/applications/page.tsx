import Link from "next/link";
import React from "react";
import ApplicationTracking from "@/components/application-tracking";
import { statusLabel, type Application } from "@/lib/application-contract";
import { loadApplications } from "@/lib/applications";

function ApplicationCard({ application }: { application: Application }) {
  const { opportunity } = application;
  return (
    <article className="opportunity-card">
      <div>
        <p className="organization">{opportunity.organization}</p>
        <h2>{opportunity.canonical_title}</h2>
        <p className="location">{opportunity.location ?? "Lieu non précisé"}</p>
        <p className="application-status">
          Statut&nbsp;: {statusLabel(application.status)}
        </p>
        <p>
          Candidature envoyée&nbsp;:{" "}
          {application.submitted_at ?? "pas encore envoyée"}
        </p>
        <p>Dernier changement de statut&nbsp;: {application.last_status_change}</p>
        <p>
          Prochaine action&nbsp;: {application.next_action ?? "aucune"}
          {" · "}
          Relance&nbsp;: {application.followup_date ?? "aucune"}
        </p>
        {application.notes !== null && (
          <p className="application-notes">Notes&nbsp;: {application.notes}</p>
        )}
      </div>

      <ApplicationTracking application={application} />

      <div className="card-footer">
        <Link href={`/applications/${application.id}`}>Voir le détail</Link>
        <a href={opportunity.original_url} target="_blank" rel="noreferrer">
          Voir l’offre originale
        </a>
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
            Seules les opportunités sur lesquelles vous avez agi apparaissent ici.
          </p>
        </div>
        <nav className="hero-links" aria-label="Navigation">
          <Link className="health-link" href="/">
            Retour aux opportunités
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
