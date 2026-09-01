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
const record = (value: unknown): Record<string, unknown> | null =>
  typeof value === "object" && value !== null && !Array.isArray(value) ? value as Record<string, unknown> : null;

function TechnicalPayload({ data }: { data: Record<string, unknown> }) {
  return <details><summary>Données techniques</summary><pre>{JSON.stringify(data, null, 2)}</pre></details>;
}

function OpportunityTypeExplanation({ value }: { value: unknown }) {
  const data = record(value);
  if (data === null) return null;
  const labels: Record<string, string> = { MATCH: "Dans vos préférences", MISMATCH: "Hors préférences", UNKNOWN: "Compatibilité incertaine" };
  const status = typeof data.status === "string" ? labels[data.status] ?? data.status : "Indisponible";
  const reason = data.reason === "OPPORTUNITY_TYPE_INCOMPATIBLE_WITH_PREFERENCES"
    ? "Le type de cette opportunité ne correspond pas aux préférences du profil."
    : null;
  return <div><strong>Type d’opportunité</strong><span>{status}</span>{reason && <p>{reason}</p>}<TechnicalPayload data={data} /></div>;
}

function RequiredSkillExplanation({ value }: { value: unknown }) {
  const data = record(value);
  if (data === null) return null;
  const score = typeof data.normalized_score === "number" ? data.normalized_score : null;
  const hasCounts = typeof data.matched_count === "number" && typeof data.total_count === "number";
  return <div><strong>Compétences requises</strong><span>{percent(score)}</span>{hasCounts && <p>{data.matched_count as number} compétences reconnues sur {data.total_count as number}</p>}<TechnicalPayload data={data} /></div>;
}

function SemanticExplanation({ value }: { value: unknown }) {
  const data = record(value);
  if (data === null) return null;
  const available = data.status === "available" && typeof data.percentile === "number";
  const rawSimilarity = typeof data.raw_similarity === "number" ? data.raw_similarity : null;
  return <div><strong>Sémantique</strong><span>{percent(available ? data.percentile as number : null)}</span>{available && rawSimilarity !== null && <p>Similarité brute : {percent(rawSimilarity)}</p>}<TechnicalPayload data={data} /></div>;
}

function DomainExplanation({ value }: { value: unknown }) {
  const data = record(value);
  if (data === null) return null;
  const score = typeof data.normalized_score === "number" ? data.normalized_score : null;
  const rank = data.status === "MATCH" && typeof data.preferred_rank === "number" ? data.preferred_rank : null;
  return <div><strong>Domaine</strong><span>{percent(score)}</span>{rank !== null && <p>Préférence de domaine n°{rank}</p>}{data.status === "MISMATCH" && <p>Hors préférences de domaine</p>}<TechnicalPayload data={data} /></div>;
}

function SupportingSkillExplanation({ label, value, emptyMessage }: { label: string; value: unknown; emptyMessage: string }) {
  const data = record(value);
  if (data === null) return null;
  const ratio = typeof data.ratio === "number" ? data.ratio : null;
  const hasCounts = typeof data.matched_count === "number" && typeof data.total_count === "number";
  const empty = data.total_count === 0 && data.ratio === null;
  return <div><strong>{label}</strong><span>{empty ? `Non évaluable — ${emptyMessage}` : percent(ratio)}</span>{!empty && hasCounts && <p>{data.matched_count as number} sur {data.total_count as number}</p>}<TechnicalPayload data={data} /></div>;
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
      <OpportunityTypeExplanation value={explanation.opportunity_type} />
      <RequiredSkillExplanation value={explanation.required_skill} />
      <SemanticExplanation value={explanation.semantic} />
      <DomainExplanation value={explanation.domain} />
      <SupportingSkillExplanation label="Compétences préférées" value={record(explanation.supporting)?.preferred} emptyMessage="aucune compétence préférée requise" />
      <SupportingSkillExplanation label="Compétences de contexte" value={record(explanation.supporting)?.context} emptyMessage="aucun signal de contexte disponible" />
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
