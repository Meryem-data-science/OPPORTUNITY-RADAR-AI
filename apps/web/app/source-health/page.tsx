import Link from "next/link";
import React from "react";
import Icon from "@/components/icon";
import { formatDateTime, organizationInitials } from "@/lib/display";
import { loadSourceHealth, type SourceHealthEntry } from "@/lib/source-health";
import { displaySource } from "@/lib/source-display";

export const dynamic = "force-dynamic";

const NEVER_RUN = "Jamais exécutée";
const statusLabels: Record<string, string> = { SUCCESS: "Collecte réussie", FAILED: "Collecte en échec", RUNNING: "Collecte en cours" };
const ALERT_GUIDES: Record<string, { label: string; url: string }> = {
  linkedin_job_alert_email: { label: "Créer des alertes LinkedIn", url: "https://www.linkedin.com/help/linkedin/answer/a508396" },
  indeed_job_alert_email: { label: "Créer des alertes Indeed", url: "https://www.indeed.com/help/job-seekers/articles/204488890-starting-stopping-and-managing-job-alerts?co=US&hl=en" },
};
const GMAIL_AUTH_ERRORS = new Set(["GmailAuthenticationError", "GmailConfigurationError", "RefreshError"]);
function count(value: number | null) {
  return value === null ? <span className="unknown-value" title="Non renseigné">—<span className="sr-only"> Non renseigné</span></span> : value.toLocaleString("fr-FR");
}

function SourceCard({ entry }: { entry: SourceHealthEntry }) {
  const name = displaySource(entry.source_id);
  const hasAnomaly = entry.anomaly_code !== null || entry.anomaly_message !== null;
  const hasError = entry.error_type !== null || entry.error_message !== null;
  const partialCollection = entry.error_type === "IndeedJobAlertPartialError" && (entry.items_found ?? 0) > 0;
  const alertGuide = ALERT_GUIDES[entry.source_id];
  const needsGmailConnection = alertGuide && entry.error_type !== null && GMAIL_AUTH_ERRORS.has(entry.error_type);
  return <article className="source-card" data-anomaly={hasAnomaly ? "true" : "false"}>
    <div className="source-card-header">
      <span className="organization-avatar" aria-hidden="true">{organizationInitials(name)}</span>
      <div className="source-card-title"><h3>{name}</h3><span className="source-enabled">Source activée</span></div>
      <span className="source-status" data-status={entry.status?.toLowerCase() ?? "unknown"}>{partialCollection ? "Collecte partielle" : entry.status === null ? "Aucune collecte" : statusLabels[entry.status] ?? entry.status}</span>
    </div>
    <p className="source-last-run">Dernière collecte enregistrée <strong>{entry.last_run_at === null ? NEVER_RUN : <time dateTime={entry.last_run_at}>{formatDateTime(entry.last_run_at)}</time>}</strong></p>
    {entry.source_id === "stage_ma" && <p className="source-summary-note">Offres publiques de la spécialité Informatique, jusqu’à 25 fiches par collecte. <a href="https://www.stage.ma/specialites/computer-science" target="_blank" rel="noopener noreferrer">Consulter Stage.ma</a>. La couverture reste partielle ; une collecte peut ne trouver aucune offre admissible.</p>}
    {entry.source_id === "stagiaires_ma" && <p className="source-summary-note">Offres publiques de Stagiaires.ma, jusqu’à 25 fiches par collecte à partir du sitemap officiel.</p>}
    {alertGuide && <p className="source-summary-note">Annonces contenues dans les alertes reçues sur votre compte Gmail autorisé, jusqu’à 50 emails des sept derniers jours. <a href={alertGuide.url} target="_blank" rel="noopener noreferrer">{alertGuide.label}</a> pour les métiers Data/IA au Maroc. Cette collecte couvre les alertes reçues ; elle ne couvre pas tout le catalogue.</p>}
    {alertGuide && <p className="source-summary-note">La localisation doit confirmer le Maroc pour apparaître dans les offres. Un email ancien ne prouve pas que le recrutement est encore ouvert.</p>}
    {partialCollection && <p className="source-problem" role="status">Les annonces lisibles ont été importées. Certaines alertes utilisent un format ou des liens de suivi que Radar ne peut pas lire ; le détail indique combien d’emails restent concernés.</p>}
    {needsGmailConnection && <p className="source-problem" role="status">Dans le dossier du projet, lancer <strong>Connecter-Alertes-Emploi.cmd</strong> pour autoriser Gmail en lecture seule. La même connexion sert à LinkedIn et Indeed ; la prochaine collecte indiquera si les alertes sont lisibles.</p>}
    <dl className="source-metrics">
      <div><dt>Détectées</dt><dd>{count(entry.items_found)}</dd></div>
      <div><dt>Nouvelles</dt><dd>{count(entry.new_items)}</dd></div>
      <div><dt>Data & IA</dt><dd>{count(entry.relevant_items)}</dd></div>
    </dl>
    <details className="source-card-details"><summary>{hasAnomaly || hasError ? "Voir le problème signalé" : "Détails de la collecte"}</summary>
      {hasAnomaly && <div className="source-problem"><strong>Anomalie signalée par le radar</strong>{entry.anomaly_message !== null && <p>{entry.anomaly_message}</p>}{entry.anomaly_code !== null && <code>{entry.anomaly_code}</code>}</div>}
      {hasError && <div className="source-problem"><strong>Erreur enregistrée</strong>{entry.error_message !== null && <p>{entry.error_message}</p>}{entry.error_type !== null && <code>{entry.error_type}</code>}</div>}
      {alertGuide && hasError && !needsGmailConnection && <p>Un format d’alerte non reconnu ou un accès Gmail refusé peut interrompre cette source. Le diagnostic ci-dessus permet d’identifier le problème avant la prochaine collecte.</p>}
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
  const activeSources = health?.items.filter((entry) => entry.enabled) ?? [];
  const successful = activeSources.filter((entry) => entry.status === "SUCCESS").length;
  const failed = activeSources.filter((entry) => entry.status === "FAILED").length;
  return <main className="page-shell">
    <header className="hero">
      <div><p className="eyebrow">Vos collectes enregistrées</p><h1>Les sources de votre radar.</h1><p className="subtitle">La dernière collecte de chaque source, ses résultats et les éventuels problèmes signalés.</p></div>
      <Link className="health-link" href="/explorer">Explorer les offres<Icon name="arrow-right" size={16} /></Link>
    </header>
    {health === null ? <section className="status-panel" role="status"><h2>État des sources temporairement indisponible</h2><p>Le radar ne peut pas afficher l’état des sources pour le moment.</p></section>
      : activeSources.length === 0 ? <section className="status-panel" role="status"><h2>Aucune source activée</h2><p>Les collectes apparaîtront lorsqu’une source sera activée.</p></section>
      : <>
        <section className="matching-summary source-summary" aria-label="Synthèse des dernières collectes">
          <dl><div><dt>Sources activées</dt><dd>{activeSources.length}</dd></div><div><dt>Dernières collectes réussies</dt><dd>{successful}</dd></div><div><dt>Dernières collectes en échec</dt><dd>{failed}</dd></div></dl>
          <p className="source-summary-note">Les statuts et compteurs correspondent à la dernière collecte enregistrée pour chaque source.</p>
        </section>
        <section aria-labelledby="source-health-heading">
          <div className="section-heading"><h2 id="source-health-heading">Sources surveillées</h2><p>{activeSources.length} source{activeSources.length > 1 ? "s" : ""} activée{activeSources.length > 1 ? "s" : ""}</p></div>
          <div className="source-grid">{activeSources.map((entry) => <SourceCard key={entry.source_id} entry={entry} />)}</div>
        </section>
      </>}
  </main>;
}
