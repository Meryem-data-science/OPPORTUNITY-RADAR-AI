import Link from "next/link";
import React from "react";
import Icon from "@/components/icon";
import { organizationInitials } from "@/lib/display";
import { loadPortfolio, type PortfolioBucket, type PortfolioItem, type PortfolioResponse } from "@/lib/portfolio";

export const dynamic = "force-dynamic";

const buckets: [PortfolioBucket, string][] = [["SAFE", "Prudentes"], ["TARGET", "Cibles"], ["AMBITIOUS", "Ambitieuses"]];
const bucketLabels: Record<PortfolioBucket, string> = { SAFE: "Prudente", TARGET: "Cible", AMBITIOUS: "Ambitieuse" };
const eligibility = { ELIGIBLE: "Éligible", UNKNOWN: "Inconnue / incertaine", INELIGIBLE: "Non éligible" };
const priorityLabels = { URGENT: "Urgente", HIGH: "Élevée", MEDIUM: "Moyenne", LOW: "Faible", IGNORE: "À ignorer" };
const laneLabels = { PRIMARY: "Dans vos préférences de type", UNCERTAIN: "Compatibilité de type incertaine", OUTSIDE_PREFERENCES: "Hors préférences de type" };
const reasonLabels: Record<string, string> = {
  PRIORITY_NOT_PORTFOLIO_CANDIDATE: "La priorité de cette offre ne permet pas son inclusion dans le portefeuille actuel.",
  ELIGIBILITY_INELIGIBLE: "Un obstacle d’éligibilité a été établi.",
  MATCHING_OUTSIDE_PREFERENCES: "Le type d’opportunité est hors de vos préférences.",
  REQUIRED_SKILLS_FULL_COVERAGE: "Toutes les compétences requises ont été reconnues.",
  REQUIRED_SKILLS_TARGET_COVERAGE: "La couverture des compétences requises correspond à une opportunité cible.",
  REQUIRED_SKILLS_AMBITIOUS_GAP: "Les écarts de compétences placent cette opportunité dans le groupe ambitieux.",
  REQUIRED_SKILL_EVIDENCE_MISSING: "Les preuves concernant les compétences requises sont insuffisantes.",
  SAFE_CAP_ELIGIBILITY_UNKNOWN: "L’éligibilité restant incertaine, l’opportunité n’est pas classée comme prudente.",
  SAFE_CAP_MATCHING_UNCERTAIN: "La compatibilité de type restant incertaine, l’opportunité n’est pas classée comme prudente.",
};

function Reasons({ item }: { item: PortfolioItem }) {
  return item.portfolio.reason_codes.length ? <details className="matching-details"><summary>Pourquoi ce classement ?</summary>{item.portfolio.reason_codes.map((code) => <p key={code}>{reasonLabels[code] ?? <code>{code}</code>}</p>)}</details> : null;
}

function Card({ item, excluded = false }: { item: PortfolioItem; excluded?: boolean }) {
  const portfolio = item.portfolio;
  return <article className="matching-card">
    <div className="opportunity-card-top"><span className="organization-avatar" aria-hidden="true">{organizationInitials(item.opportunity.organization)}</span><div className="opportunity-card-body"><p className="organization">{item.opportunity.organization}</p><h3>{item.opportunity.canonical_title}</h3>{item.opportunity.location && <p className="location"><Icon name="pin" size={15} />{item.opportunity.location}</p>}</div></div>
    {!excluded && portfolio.bucket !== null && <span className="matching-badge">{bucketLabels[portfolio.bucket]}</span>}
    <div className="matching-scores">
      <p><strong>Priorité</strong><span>{portfolio.priority_category === null ? "Non classée" : priorityLabels[portfolio.priority_category]}</span></p>
      <p><strong>Éligibilité</strong><span>{eligibility[portfolio.eligibility_status]}</span></p>
      <p><strong>Type d’opportunité</strong><span>{laneLabels[portfolio.matching_lane]}</span></p>
      {!excluded && <p><strong>Compétences requises</strong><span>{portfolio.required_skill_total_count > 0 ? `${portfolio.required_skill_matched_count} / ${portfolio.required_skill_total_count} compétences requises (${Math.round(portfolio.required_skill_score! * 100)} %)` : "Preuve de couverture non disponible"}</span></p>}
    </div>
    <Reasons item={item} />
    <div className="card-footer"><span>Classement enregistré</span><a href={item.opportunity.original_url} target="_blank" rel="noopener noreferrer">Voir l’offre originale<Icon name="arrow-up-right" size={16} /></a></div>
  </article>;
}

function Ready({ data }: { data: PortfolioResponse }) {
  const run = data.current_run!;
  const byId = (a: PortfolioItem, b: PortfolioItem) => a.opportunity_id - b.opportunity_id;
  const included = run.items.filter((item) => item.portfolio.disposition === "INCLUDED").sort(byId);
  const excluded = run.items.filter((item) => item.portfolio.disposition === "EXCLUDED").sort(byId);
  return <>
    <section className="matching-summary"><div className="section-heading"><div><h2>Votre portefeuille actuel</h2><p>{run.assessment_count} opportunités évaluées · {run.included_count} retenues · {run.excluded_count} hors portefeuille</p></div></div><dl>{buckets.map(([key, label]) => <div key={key}><dt>{label}</dt><dd>{run.bucket_counts[key]}</dd></div>)}</dl></section>
    {buckets.map(([key, label]) => {
      const items = included.filter((item) => item.portfolio.bucket === key);
      return <section key={key} aria-labelledby={`portfolio-${key}`}><div className="section-heading"><h2 id={`portfolio-${key}`}>{label}</h2><p>{items.length}</p></div>{items.length ? <div className="opportunity-grid">{items.map((item) => <Card key={item.opportunity_id} item={item} />)}</div> : <div className="status-panel"><p>Aucune opportunité dans ce groupe.</p></div>}</section>;
    })}
    <details className="matching-lane-collapsible"><summary>Opportunités hors portefeuille actuel ({excluded.length})</summary><div className="section-heading"><h2>Opportunités hors portefeuille actuel</h2><p>{excluded.length}</p></div><div className="opportunity-grid">{excluded.map((item) => <Card key={item.opportunity_id} item={item} excluded />)}</div></details>
  </>;
}

export default async function PortfolioPage() {
  const data = await loadPortfolio();
  return <main className="page-shell">
    <header className="hero"><div><p className="eyebrow">Équilibrer ses opportunités</p><h1>Votre portefeuille d’opportunités.</h1><p className="subtitle">Une répartition entre opportunités prudentes, cibles et ambitieuses selon les compétences et les évaluations enregistrées.</p></div><Link className="health-link" href="/explorer">Explorer les offres<Icon name="arrow-right" size={16} /></Link></header>
    {data === null ? <section className="status-panel" role="status"><h2>Portefeuille temporairement indisponible</h2></section>
      : data.status === "NOT_SYNCED" ? <section className="status-panel" role="status"><h2>Portefeuille non synchronisé</h2><p>Aucun classement de portefeuille n’a encore été enregistré.</p></section>
      : <Ready data={data} />}
  </main>;
}
