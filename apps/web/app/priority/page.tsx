import Link from "next/link";
import React from "react";
import Icon from "@/components/icon";
import { formatDate, organizationInitials } from "@/lib/display";
import { loadPriority, type PriorityItem, type PriorityMatchingLane, type PriorityResponse } from "@/lib/priority";

export const dynamic = "force-dynamic";

const keys = ["URGENT", "HIGH", "MEDIUM", "LOW", "IGNORE", "NONE"] as const;
const labels = { URGENT: "Urgente", HIGH: "Élevée", MEDIUM: "Moyenne", LOW: "Faible", IGNORE: "À ignorer", NONE: "Non classée" };
const lane: Record<PriorityMatchingLane, string> = { PRIMARY: "Dans vos préférences de type", UNCERTAIN: "Compatibilité de type incertaine", OUTSIDE_PREFERENCES: "Hors préférences de type" };
const eligible = { ELIGIBLE: "Éligible", INELIGIBLE: "Non éligible", UNKNOWN: "Inconnue / incertaine" };
const messages: Record<string, string> = {
  DEADLINE_IMMINENT: "Échéance imminente",
  URGENT_PROMOTION: "Promotion en priorité urgente appliquée",
  OUTSIDE_PREFERENCES_CAP: "Priorité plafonnée car l’opportunité est hors préférences",
  KNOWN_ELIGIBILITY_BLOCKER: "Obstacle d’éligibilité identifié",
  COVERAGE_HIGH_GUARDRAIL: "Couverture insuffisante pour une priorité élevée",
  DEADLINE_EXPIRED_BLOCKER: "Échéance dépassée",
};
const deadlineLabels: Record<string, string> = { IMMINENT: "Imminente", EXPIRED: "Dépassée", OPEN: "À venir", FUTURE: "À venir", UNKNOWN: "Non connue", MISSING: "Non renseignée", NOT_PROVIDED: "Non renseignée" };
const percent = (value: number | null) => value === null ? "Indisponible" : `${Math.round(value * 100)} %`;
const record = (value: unknown): Record<string, unknown> | null => typeof value === "object" && value !== null && !Array.isArray(value) ? value as Record<string, unknown> : null;

function Card({ item }: { item: PriorityItem }) {
  const priority = item.priority;
  const explanation = priority.explanation;
  const components = [
    ["Correspondance", record(explanation.match)?.score],
    ["Fraîcheur", record(explanation.freshness)?.score],
    ["Qualité de publication", record(explanation.opportunity_quality)?.score],
    ["Éligibilité", eligible[priority.eligibility_status]],
    ["Couverture des preuves", priority.priority_evidence_coverage],
  ];
  return <article className="matching-card">
    <div className="opportunity-card-top">
      <span className="organization-avatar" aria-hidden="true">{organizationInitials(item.opportunity.organization)}</span>
      <div className="opportunity-card-body"><p className="organization">{item.opportunity.organization}</p><h3>{item.opportunity.canonical_title}</h3><p className="location"><Icon name="pin" size={15} />{item.opportunity.location ?? "Lieu non précisé"}</p></div>
    </div>
    <span className="matching-badge">{labels[priority.category ?? "NONE"]}</span>
    <div className="matching-scores">
      <p><strong>Score de priorité</strong><span>{percent(priority.priority_score)}</span></p>
      <p><strong>Couverture des preuves</strong><span>{percent(priority.priority_evidence_coverage)}</span></p>
      <p><strong>Éligibilité</strong><span>{eligible[priority.eligibility_status]}</span></p>
      <p><strong>Type d’opportunité</strong><span>{lane[priority.matching_lane]}</span></p>
    </div>
    {priority.deadline && <p>Échéance : {formatDate(priority.deadline)}</p>}
    <p>Statut de l’échéance : {deadlineLabels[priority.deadline_status] ?? priority.deadline_status}</p>
    <details className="matching-details"><summary>Pourquoi cette priorité ?</summary>
      {components.map(([name, value]) => <p key={name as string}><strong>{name as string}</strong> : {typeof value === "number" ? percent(value) : String(value ?? "Indisponible")}</p>)}
      {priority.reason_codes.filter((code) => messages[code]).map((code) => <p key={code}>{messages[code]}</p>)}
    </details>
    <div className="card-footer"><span>Priorité enregistrée</span><a href={item.opportunity.original_url} target="_blank" rel="noopener noreferrer">Voir l’offre originale<Icon name="arrow-up-right" size={16} /></a></div>
  </article>;
}

function Ready({ data }: { data: PriorityResponse }) {
  const run = data.current_run!;
  return <>
    <section className="matching-summary">
      <div className="section-heading"><div><h2>Vos priorités</h2><p>{run.assessment_count} opportunités évaluées</p></div><p>Évaluation : {formatDate(run.evaluation_date)} · {data.history_count} analyse{data.history_count > 1 ? "s" : ""}</p></div>
      <dl>{keys.map((key) => <div key={key}><dt>{labels[key]}</dt><dd>{run.category_counts[key]}</dd></div>)}</dl>
    </section>
    <div>{keys.map((key) => {
      const items = run.items.filter((item) => (item.priority.category ?? "NONE") === key);
      const section = <section aria-labelledby={`priority-${key}`}><div className="section-heading"><h2 id={`priority-${key}`}>{labels[key]}</h2><p>{items.length}</p></div>{items.length ? <div className="opportunity-grid">{items.map((item) => <Card key={item.opportunity_id} item={item} />)}</div> : <div className="status-panel"><p>Aucune opportunité dans cette section.</p></div>}</section>;
      return ["LOW", "IGNORE", "NONE"].includes(key) ? <details className="matching-lane-collapsible" key={key}><summary>{labels[key]} ({items.length})</summary>{section}</details> : <React.Fragment key={key}>{section}</React.Fragment>;
    })}</div>
    <details className="technical-details"><summary>Détails techniques</summary><dl>
      {(["run_id", "matching_run_id", "evaluation_date", "created_at", "persistence_version", "input_assembly_version", "priority_engine_version", "priority_rules_version", "freshness_version", "quality_version", "run_fingerprint", "matching_run_fingerprint"] as const).map((key) => <React.Fragment key={key}><dt>{key}</dt><dd>{run[key]}</dd></React.Fragment>)}
      <dt>audit status</dt><dd>OK</dd>
    </dl></details>
  </>;
}

export default async function PriorityPage() {
  const data = await loadPriority();
  return <main className="page-shell">
    <header className="hero"><div><p className="eyebrow">Choisir sa prochaine action</p><h1>L’essentiel, par priorité.</h1><p className="subtitle">Adéquation au profil, fraîcheur et échéances : les priorités de la dernière analyse enregistrée.</p></div><Link className="health-link" href="/explorer">Explorer les offres<Icon name="arrow-right" size={16} /></Link></header>
    {data === null ? <section className="status-panel" role="status"><h2>Priorités temporairement indisponibles</h2></section>
      : data.status === "NOT_SYNCED" ? <section className="status-panel" role="status"><h2>Priorités non encore calculées</h2><p>Aucune analyse de priorité n’a encore été enregistrée.</p></section>
      : <Ready data={data} />}
  </main>;
}
