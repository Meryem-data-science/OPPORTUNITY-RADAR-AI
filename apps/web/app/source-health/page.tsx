import Link from "next/link";
import React from "react";
import { loadSourceHealth, type SourceHealthEntry } from "@/lib/source-health";

export const dynamic = "force-dynamic";

const UNKNOWN = "Inconnu";
const NEVER_RUN = "Jamais exécutée";

function count(value: number | null) {
  return value === null ? <span className="unknown-value">{UNKNOWN}</span> : value;
}

/**
 * Render the backend's own verdict for one source.
 *
 * The anomaly is displayed when the backend reported one; the page never
 * inspects `zero_result_streak` to decide whether a source is anomalous.
 */
function Errors({ entry }: { entry: SourceHealthEntry }) {
  const hasAnomaly = entry.anomaly_message !== null;
  const hasError = entry.error_type !== null || entry.error_message !== null;

  if (!hasAnomaly && !hasError) {
    return <span className="no-error">Aucune</span>;
  }

  return (
    <div className="error-cell">
      {hasAnomaly ? (
        <p className="anomaly">
          <span className="anomaly-badge">Anomalie</span>
          {entry.anomaly_message}
        </p>
      ) : null}
      {hasError ? (
        <p className="run-error">
          {entry.error_type !== null ? (
            <span className="error-type">{entry.error_type}</span>
          ) : null}
          {entry.error_message ?? UNKNOWN}
        </p>
      ) : null}
    </div>
  );
}

function SourceRow({ entry }: { entry: SourceHealthEntry }) {
  return (
    <tr data-anomaly={entry.anomaly_code !== null ? "true" : "false"}>
      <th scope="row">{entry.source_id}</th>
      <td>{entry.enabled ? "Oui" : "Non"}</td>
      <td>{entry.last_run_at ?? <span className="unknown-value">{NEVER_RUN}</span>}</td>
      <td>
        {entry.status ?? <span className="unknown-value">{NEVER_RUN}</span>}
      </td>
      <td>{count(entry.items_found)}</td>
      <td>{count(entry.new_items)}</td>
      <td>{count(entry.relevant_items)}</td>
      <td>
        <Errors entry={entry} />
      </td>
    </tr>
  );
}

export default async function SourceHealthPage() {
  const health = await loadSourceHealth();

  return (
    <main className="page-shell">
      <header className="hero">
        <div>
          <p className="eyebrow">Surveillance des sources</p>
          <h1>Source Health</h1>
          <p className="subtitle">
            État de chaque source, calculé côté backend à partir de l’historique des
            exécutions réellement enregistrées.
          </p>
        </div>
        <Link className="health-link" href="/">
          Retour aux opportunités
        </Link>
      </header>

      {health === null ? (
        <section className="status-panel" role="status">
          <h2>État des sources temporairement indisponible</h2>
          <p>Le radar ne peut pas afficher l’état des sources pour le moment.</p>
        </section>
      ) : (
        <section aria-labelledby="source-health-heading">
          <div className="section-heading">
            <h2 id="source-health-heading">Sources surveillées</h2>
            <p>{health.returned} sources connues</p>
          </div>

          {health.items.length === 0 ? (
            <div className="status-panel" role="status">
              <p>Aucune source connue pour le moment.</p>
            </div>
          ) : (
            <div className="table-scroll">
              <table className="source-health-table">
                <thead>
                  <tr>
                    <th scope="col">Source</th>
                    <th scope="col">Activée</th>
                    <th scope="col">Dernière exécution</th>
                    <th scope="col">Statut</th>
                    <th scope="col">Éléments trouvés</th>
                    <th scope="col">Nouveaux éléments</th>
                    <th scope="col">Éléments pertinents</th>
                    <th scope="col">Erreurs</th>
                  </tr>
                </thead>
                <tbody>
                  {health.items.map((entry) => (
                    <SourceRow key={entry.source_id} entry={entry} />
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
      )}
    </main>
  );
}
