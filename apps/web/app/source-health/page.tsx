import Link from "next/link";
import React from "react";
import Icon from "@/components/icon";
import { formatDateTime, organizationInitials } from "@/lib/display";
import { loadSourceHealth, type SourceHealthEntry } from "@/lib/source-health";

export const dynamic = "force-dynamic";

const NEVER_RUN = "Jamais exécutée";
const statusLabels: Record<string, string> = { SUCCESS: "Collecte réussie", FAILED: "Collecte en échec", RUNNING: "Collecte en cours" };
const sourceNames: Record<string, string> = {
  scale_ai_greenhouse: "Scale AI",
  artefact_greenhouse: "Artefact",
  linkedin_job_alert_email: "Alertes LinkedIn",
  stagiaires_ma: "Stagiaires.ma",
  stage_ma: "Stage.ma",
};

function displaySource(id: string): string {
  if (sourceNames[id]) return sourceNames[id];
  const key = id.toLowerCase();
  if (key.includes("scale")) return "Scale AI";
  if (key.includes("artefact")) return "Artefact";
  if (key.includes("linkedin") || key.includes("gmail")) return "Alertes LinkedIn";
  if (key.includes("stagiaires")) return "Stagiaires.ma";
  if (key.includes("stage")) return "Stage.ma";
  return id;
}

function count(value: number | null) {
  return value === null ? <span className="unknown-value" title="Non renseigné">—<span className="sr-only"> Non renseigné</span></span> : value.toLocaleString("fr-FR");
}

function SourceCard({ entry }: { entry: SourceHealthEntry }) {
  const name = displaySource(entry.source_id);
  const hasAnomaly = entry.anomaly_code !== null || entry.anomaly_message !== null;
  const hasError = entry.error_type !== null || entry.error_message !== null;
  return <article className="source-card" data-anomaly={hasAnomaly ? "true" : "false"}>
    <div className="source-card-header">
      <span className="organization-avatar" aria-hidden="true">{organizationInitials(name)}</span>
      <div className="source-card-title"><h3>{name}</h3><span className="source-enabled">{entry.enabled ? "Source activée" : "Source désactivée"}</span></div>
      <span className="source-status" data-status={entry.status?.toLowerCase() ?? "unknown"}>{entry.status === null ? "Aucune collecte" : statusLabels[entry.status] ?? entry.status}</span>
    </div>
    <p className="source-last-run">Dernière collecte enregistrée <strong>{entry.last_run_at === null ? NEVER_RUN : <time dateTime={entry.last_run_at}>{formatDateTime(entry.last_run_at)}</time>}</strong></p>
    <dl className="source-metrics">
      <div><dt>Détectées</dt><dd>{count(entry.items_found)}</dd></div>
      <div><dt>Nouvelles</dt><dd>{count(entry.new_items)}</dd></div>
      <div><dt>Data & IA</dt><dd>{count(entry.relevant_items)}</dd></div>
    </dl>
    <details className="source-card-details"><summary>{hasAnomaly || hasError ? "Voir le problème signalé" : "Détails de la collecte"}</summary>
      {hasAnomaly && <div className="source-problem"><strong>Anomalie signalée par le radar</strong>{entry.anomaly_message !== null && <p>{entry.anomaly_message}</p>}{entry.anomaly_code !== null && <code>{entry.anomaly_code}</code>}</div>}
      {hasError && <div className="source-problem"><strong>Erreur enregistrée</strong>{entry.error_message !== null && <p>{entry.error_message}</p>}{entry.error_type !== null && <code>{entry.error_type}</code>}</div>}
      <dl className="source-diagnostic">
        <dt>Identifiant de la source</dt><dd><code>{entry.source_id}</code></dd>
        <dt>Statut enregistré</dt><dd><code>{entry.status ?? "Non renseigné"}</code></dd>
        <dt>Collectes successives sans résultat</dt><dd>{entry.zero_result_streak}</dd>
      </dl>
    </details>
  </article>;
}

export default async function SourceHealthPage() {
  const health = await loadSourceHealth();
  const enabled = health?.items.filter((entry) => entry.enabled).length ?? 0;
  const successful = health?.items.filter((entry) => entry.status === "SUCCESS").length ?? 0;
  const failed = health?.items.filter((entry) => entry.status === "FAILED").length ?? 0;
  return <main className="page-shell">
    <header className="hero">
      <div><p className="eyebrow">Vos collectes enregistrées</p><h1>Les sources de votre radar.</h1><p className="subtitle">La dernière collecte de chaque source, ses résultats et les éventuels problèmes signalés.</p></div>
      <Link className="health-link" href="/explorer">Explorer les offres<Icon name="arrow-right" size={16} /></Link>
    </header>
    {health === null ? <section className="status-panel" role="status"><h2>État des sources temporairement indisponible</h2><p>Le radar ne peut pas afficher l’état des sources pour le moment.</p></section>
      : health.items.length === 0 ? <section className="status-panel" role="status"><h2>Aucune source enregistrée</h2><p>L’état des sources apparaîtra après leur configuration.</p></section>
      : <>
        <section className="matching-summary source-summary" aria-label="Synthèse des dernières collectes">
          <dl><div><dt>Sources activées</dt><dd>{enabled}</dd></div><div><dt>Dernières collectes réussies</dt><dd>{successful}</dd></div><div><dt>Dernières collectes en échec</dt><dd>{failed}</dd></div></dl>
          <p className="source-summary-note">Les statuts et compteurs correspondent à la dernière collecte enregistrée pour chaque source.</p>
        </section>
        <section aria-labelledby="source-health-heading">
          <div className="section-heading"><h2 id="source-health-heading">Sources surveillées</h2><p>{health.returned} source{health.returned > 1 ? "s" : ""} connue{health.returned > 1 ? "s" : ""}</p></div>
          <div className="source-grid">{health.items.map((entry) => <SourceCard key={entry.source_id} entry={entry} />)}</div>
        </section>
      </>}
  </main>;
}
