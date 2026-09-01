import Link from "next/link";
import React from "react";
import { loadMatching, type MatchingItem, type MatchingLane, type MatchingResponse } from "@/lib/matching";

export const dynamic = "force-dynamic";

const laneLabels: Record<MatchingLane, string> = {
  PRIMARY: "Dans vos préférences de type",
  UNCERTAIN: "Compatibilité de type incertaine",
  OUTSIDE_PREFERENCES: "Hors préférences de type",
};
const lanes = Object.keys(laneLabels) as MatchingLane[];
const percent = (value: number | null) => value === null ? "Indisponible" : `${Math.round(value * 100)} %`;

function Component({ label, value }: { label: string; value: unknown }) {
  if (typeof value !== "object" || value === null) return null;
  const data = value as Record<string, unknown>;
  const score = typeof data.normalized_score === "number" ? data.normalized_score : null;
  return <div><strong>{label}</strong><span>{percent(score)}</span><pre>{JSON.stringify(data, null, 2)}</pre></div>;
}

function MatchingCard({ item }: { item: MatchingItem }) {
  const explanation = item.matching.explanation;
  return <article className="matching-card">
    <p className="organization">{item.opportunity.organization}</p>
    <h3>{item.opportunity.canonical_title}</h3>
    <p className="location">{item.opportunity.location ?? "Lieu non précisé"}</p>
    <span className="matching-badge">{laneLabels[item.matching.lane]}</span>
    <div className="matching-scores">
      <p><strong>Qualité du matching</strong><span>{percent(item.matching.match_quality)}</span></p>
      <p><strong>Couverture des preuves</strong><span>{percent(item.matching.evidence_coverage)}</span></p>
    </div>
    <details className="matching-details"><summary>Pourquoi ce résultat ?</summary>
      <Component label="Type d’opportunité" value={explanation.opportunity_type} />
      <Component label="Compétences requises" value={explanation.required_skill} />
      <Component label="Sémantique" value={explanation.semantic} />
      <Component label="Domaine" value={explanation.domain} />
      <Component label="Compétences préférées" value={(explanation.supporting as Record<string, unknown> | undefined)?.preferred} />
      <Component label="Compétences de contexte" value={(explanation.supporting as Record<string, unknown> | undefined)?.context} />
    </details>
    <a href={item.opportunity.original_url} target="_blank" rel="noreferrer">Voir l’offre originale</a>
  </article>;
}

function Ready({ matching }: { matching: MatchingResponse }) {
  const run = matching.current_run!;
  return <>
    <section className="matching-summary" aria-labelledby="matching-summary-heading">
      <div className="section-heading"><div><h2 id="matching-summary-heading">Matching prêt</h2><p>{run.assessment_count} opportunités évaluées</p></div><p>{matching.history_count} snapshot{matching.history_count > 1 ? "s" : ""} enregistré{matching.history_count > 1 ? "s" : ""}</p></div>
      <dl>{lanes.map((lane) => <div key={lane}><dt>{laneLabels[lane]}</dt><dd>{run.lane_counts[lane]}</dd></div>)}</dl>
    </section>
    <div className="matching-lanes">{lanes.map((lane) => {
      const items = run.items.filter((item) => item.matching.lane === lane);
      const content = <section aria-labelledby={`lane-${lane}`}><div className="section-heading"><h2 id={`lane-${lane}`}>{laneLabels[lane]}</h2><p>{items.length}</p></div>{items.length ? <div className="opportunity-grid">{items.map((item) => <MatchingCard key={item.opportunity_id} item={item} />)}</div> : <div className="status-panel"><p>Aucune opportunité dans cette section.</p></div>}</section>;
      return lane === "OUTSIDE_PREFERENCES" ? <details className="matching-lane-collapsible" key={lane}><summary>{laneLabels[lane]} ({items.length})</summary>{content}</details> : <React.Fragment key={lane}>{content}</React.Fragment>;
    })}</div>
    <details className="technical-details"><summary>Détails techniques</summary><dl>
      <dt>run_id</dt><dd>{run.run_id}</dd><dt>created_at</dt><dd>{run.created_at}</dd>
      {(["persistence_version", "selection_version", "matching_engine_version", "matching_rules_version", "semantic_percentile_version", "run_fingerprint", "batch_fingerprint"] as const).map((key) => <React.Fragment key={key}><dt>{key}</dt><dd>{run[key]}</dd></React.Fragment>)}
      <dt>audit status</dt><dd>OK</dd><dt>audit_version</dt><dd>{matching.integrity.audit_version}</dd><dt>audit_fingerprint</dt><dd>{matching.integrity.audit_fingerprint}</dd>
    </dl></details>
  </>;
}

export default async function MatchingPage() {
  const matching = await loadMatching();
  return <main className="page-shell"><header className="hero"><div><p className="eyebrow">Correspondance avec le profil</p><h1>Matching</h1><p className="subtitle">Ces résultats proviennent du dernier snapshot de matching persisté.</p></div><Link className="health-link" href="/">Retour aux opportunités</Link></header>
    {matching === null ? <section className="status-panel" role="status"><h2>Matching temporairement indisponible</h2></section>
      : matching.status === "NOT_SYNCED" ? <section className="status-panel" role="status"><h2>Matching non encore calculé</h2><p>Aucun snapshot de matching n’a encore été enregistré.</p></section>
      : matching.status === "EMPTY" ? <section className="status-panel" role="status"><h2>Aucune opportunité dans le périmètre de matching actuel.</h2></section>
      : <Ready matching={matching} />}
  </main>;
}
