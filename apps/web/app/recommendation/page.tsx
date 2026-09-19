import Link from "next/link";
import React from "react";
import ApplicationActions from "@/components/application-actions";
import type { ApplicationStatus } from "@/lib/application-contract";
import { loadApplications } from "@/lib/applications";
import {
  loadRecommendation,
  type FineCategory,
  type RecommendationDisposition,
  type RecommendationItem,
  type RecommendationOpportunity,
  type RecommendationResponse,
} from "@/lib/recommendation";

export const dynamic = "force-dynamic";

const dispositionLabels: Record<RecommendationDisposition, string> = {
  RECOMMENDED: "Recommandée",
  UNCERTAIN: "Des incertitudes restent à lever",
  OUTSIDE_PREFERENCES: "Contredit une préférence déclarée",
  KNOWN_BLOCKER: "Un obstacle connu existe",
};

const fineCategoryLabels: Record<FineCategory, string> = {
  DATA_SCIENCE: "Data science",
  DATA_ANALYTICS: "Analyse de données",
  DATA_ENGINEERING: "Data engineering",
  MACHINE_LEARNING: "Machine learning",
  ARTIFICIAL_INTELLIGENCE: "Intelligence artificielle",
  GENERATIVE_AI: "IA générative",
  NLP: "Traitement du langage (NLP)",
  COMPUTER_VISION: "Vision par ordinateur",
  BUSINESS_INTELLIGENCE: "Business intelligence",
  MLOPS: "MLOps",
  OTHER: "Data/IA — autre catégorie",
};

// One sentence per persisted code. The partition a code is shown in is always
// the one the backend put it in; this table only words it.
const reasonLabels: Record<string, string> = {
  REQUIRED_SKILLS_ALL_CONFIRMED: "Toutes les compétences requises sont confirmées par le profil.",
  FINE_DOMAIN_PREFERRED: "La catégorie fine correspond à un domaine préféré.",
  COARSE_DOMAIN_PREFERRED: "Le domaine général correspond à un domaine préféré.",
  OPPORTUNITY_TYPE_ALLOWED: "Le type d’opportunité fait partie des préférences.",
  WORK_MODE_ALLOWED: "Le mode de travail fait partie des préférences.",
  GEOGRAPHY_MATCHED: "La localisation correspond à la zone ciblée.",
  ELIGIBILITY_NO_KNOWN_BLOCKER: "Aucun obstacle d’éligibilité connu.",
  OPPORTUNITY_TYPE_OUTSIDE_PREFERENCES: "Le type d’opportunité est hors des préférences déclarées.",
  WORK_MODE_OUTSIDE_PREFERENCES: "Le mode de travail est hors des préférences déclarées.",
  GEOGRAPHY_OUT_OF_TARGET: "La localisation est hors de la zone ciblée.",
  DOMAIN_OUTSIDE_PREFERENCES: "Le domaine est hors des préférences déclarées.",
  ELIGIBILITY_KNOWN_BLOCKER: "Un obstacle d’éligibilité connu existe.",
  REQUIRED_SKILLS_NOT_ALL_CONFIRMED: "Certaines compétences requises restent à confirmer.",
  REQUIRED_SKILLS_UNAVAILABLE: "Les compétences requises de l’offre ne sont pas disponibles.",
  SEMANTIC_EVIDENCE_UNAVAILABLE: "La proximité sémantique n’est pas disponible.",
  DOMAIN_FIT_UNKNOWN: "L’adéquation au domaine n’est pas connue.",
  FINE_DOMAIN_UNAVAILABLE_USING_COARSE_FALLBACK: "Catégorie fine indisponible : le domaine général a été utilisé.",
  OPPORTUNITY_TYPE_UNKNOWN: "Le type d’opportunité n’est pas connu.",
  WORK_MODE_UNKNOWN: "Le mode de travail n’est pas connu.",
  GEOGRAPHY_UNKNOWN: "La compatibilité géographique n’est pas connue.",
  ELIGIBILITY_UNKNOWN: "L’éligibilité n’est pas connue.",
  ELIGIBILITY_SNAPSHOT_MISSING: "Aucune évaluation d’éligibilité n’est disponible.",
  USER_CONSTRAINTS_NOT_AUTOMATICALLY_EVALUATED: "Vos contraintes déclarées n’ont pas été vérifiées automatiquement.",
  NO_NUMERIC_EVIDENCE: "Aucune preuve chiffrée n’est disponible.",
};

const percent = (value: number) => `${Math.round(value * 100)} %`;

// The final product targets one geography and one family of opportunity types,
// and both are decided upstream: Phase 7A.1 against the profile's own mobility,
// Phase 7B.1 against its declared types. An item is shown only when Phase 9
// persisted *positive* evidence for both — the codes below, in the strengths
// partition the engine itself put them in. Nothing here reads a location
// string, a country or a type: an absent code is an unknown, never a match,
// and an unknown is not displayed as a confirmed opportunity.
const GEOGRAPHY_CONFIRMED = "GEOGRAPHY_MATCHED";
const OPPORTUNITY_TYPE_CONFIRMED = "OPPORTUNITY_TYPE_ALLOWED";

// The dispositions this surface shows. KNOWN_BLOCKER is among them on purpose:
// a blocker is established from the person's own Digital Twin facts — a
// diploma, a language, an authorisation — so hiding it would make the visible
// set depend on whose CV is loaded. Two people searching for the same thing
// must see the same opportunities; what differs is the score, the ranking and
// the explanation. The blocker is therefore *shown and named*, never silently
// dropped and never dressed up as a clean recommendation.
//
// OUTSIDE_PREFERENCES stays hidden: there it is the search itself that was
// contradicted — geography, opportunity type or work mode — and that is the
// person's stated criteria talking, not their CV.
const SHOWN_DISPOSITIONS: RecommendationDisposition[] = ["RECOMMENDED", "UNCERTAIN", "KNOWN_BLOCKER"];

// Phase 9 ranks KNOWN_BLOCKER above OUTSIDE_PREFERENCES, so one assessment can
// carry an eligibility blocker *and* a positively contradicted work mode at the
// same time, and only the blocker reaches the disposition. This code reads the
// persisted gap directly so that the stronger label cannot smuggle back in an
// opportunity the person's own work-mode preference already ruled out. An
// UNKNOWN work mode is not a contradiction and is never treated as one.
const WORK_MODE_CONTRADICTED = "WORK_MODE_OUTSIDE_PREFERENCES";

function isConfirmedForTarget(item: RecommendationItem): boolean {
  const { disposition, strengths, confirmed_gaps } = item.recommendation;
  return (
    SHOWN_DISPOSITIONS.includes(disposition) &&
    strengths.includes(GEOGRAPHY_CONFIRMED) &&
    strengths.includes(OPPORTUNITY_TYPE_CONFIRMED) &&
    !confirmed_gaps.includes(WORK_MODE_CONTRADICTED)
  );
}

function fineCategoryLabel(opportunity: RecommendationOpportunity): string {
  if (opportunity.fine_primary_category !== null) return fineCategoryLabels[opportunity.fine_primary_category];
  // Null is not OTHER: either the classifier never ran, or it retained nothing.
  return opportunity.fine_classifier_version === null ? "Non classée finement" : "Aucune catégorie fine retenue";
}

function ReasonGroup({ title, kind, codes, emptyMessage }: { title: string; kind: string; codes: string[]; emptyMessage: string }) {
  return <section className={`recommendation-reasons recommendation-reasons-${kind}`}>
    <h4>{title}</h4>
    {codes.length === 0 ? <p className="recommendation-reasons-empty">{emptyMessage}</p>
      : <ul>{codes.map((code, index) => <li key={`${code}-${index}`}>{reasonLabels[code] ?? <code>{code}</code>}</li>)}</ul>}
  </section>;
}

function RecommendationCard({ item, trackedStatus }: { item: RecommendationItem; trackedStatus: ApplicationStatus | null }) {
  const { opportunity, recommendation } = item;
  return <article className="recommendation-card" data-opportunity-id={item.opportunity_id}>
    <div className="recommendation-card-header">
      <span className="recommendation-rank">n° {item.rank_position}</span>
      <span className={`recommendation-badge recommendation-badge-${recommendation.disposition.toLowerCase()}`}>{dispositionLabels[recommendation.disposition]}</span>
    </div>
    <p className="organization">{opportunity.organization}</p>
    <h3>{opportunity.canonical_title}</h3>
    <p className="location">{opportunity.location ?? "Lieu non précisé"}</p>
    <div className="matching-scores">
      <p><strong>Score de recommandation</strong><span>{recommendation.recommendation_score === null ? "Score non disponible" : percent(recommendation.recommendation_score)}</span></p>
      <p><strong>Couverture des preuves</strong><span>{percent(recommendation.evidence_coverage)}</span></p>
    </div>
    <p className="recommendation-category"><strong>Catégorie Data/IA</strong><span>{fineCategoryLabel(opportunity)}</span></p>
    <div className="recommendation-reason-groups">
      <ReasonGroup title="Forces" kind="strengths" codes={recommendation.strengths} emptyMessage="Aucune force établie." />
      <ReasonGroup title="Écarts confirmés" kind="gaps" codes={recommendation.confirmed_gaps} emptyMessage="Aucun écart confirmé." />
      <ReasonGroup title="À confirmer" kind="unknowns" codes={recommendation.unknowns} emptyMessage="Aucun point à confirmer." />
    </div>
    <ApplicationActions opportunityId={item.opportunity_id} status={trackedStatus} />
    <div className="card-footer">
      <span>Classement persisté</span>
      <a href={opportunity.original_url} target="_blank" rel="noreferrer">Voir l’offre originale</a>
    </div>
  </article>;
}

function Ready({ recommendation, tracked }: { recommendation: RecommendationResponse; tracked: Map<number, ApplicationStatus> }) {
  const run = recommendation.current_run!;
  // `filter` keeps the received sequence, so the persisted Phase 9 ranking and
  // every `rank_position` survive untouched; nothing is re-scored or re-ordered.
  const shown = run.items.filter(isConfirmedForTarget);
  return <>
    <section aria-labelledby="recommendation-heading">
      <div className="section-heading">
        <div>
          <h2 id="recommendation-heading">Recommandations</h2>
          <p>{shown.length} opportunité{shown.length > 1 ? "s" : ""} confirmée{shown.length > 1 ? "s" : ""} sur {run.assessment_count} classée{run.assessment_count > 1 ? "s" : ""}</p>
          <p className="recommendation-target-note">Seules les opportunités dont la géographie ciblée et le type d’opportunité sont confirmés par la recommandation enregistrée sont affichées. Cette confirmation porte uniquement sur ces deux critères de recherche : elle ne dit rien de votre compatibilité globale ni de votre éligibilité, que chaque carte détaille séparément.</p>
        </div>
        <p>{recommendation.history_count} snapshot{recommendation.history_count > 1 ? "s" : ""} enregistré{recommendation.history_count > 1 ? "s" : ""}</p>
      </div>
      {shown.length === 0 ? <div className="status-panel" role="status"><p>Aucune opportunité confirmée pour la géographie et le type d’opportunité ciblés dans cette recommandation.</p></div>
        // Rendered in exactly the order the API returned: the ranking is Phase 9's.
        : <div className="recommendation-list">{shown.map((item) => <RecommendationCard key={item.opportunity_id} item={item} trackedStatus={tracked.get(item.opportunity_id) ?? null} />)}</div>}
    </section>
    <details className="technical-details"><summary>Détails techniques</summary><dl>
      <dt>run_id</dt><dd>{run.run_id}</dd><dt>created_at</dt><dd>{run.created_at}</dd>
      {(["persistence_version", "input_assembly_version", "recommendation_engine_version", "recommendation_rules_version", "source_matching_run_id", "source_matching_run_fingerprint", "batch_fingerprint", "run_fingerprint"] as const).map((key) => <React.Fragment key={key}><dt>{key}</dt><dd>{run[key]}</dd></React.Fragment>)}
      <dt>audit status</dt><dd>{recommendation.integrity.ok ? "OK" : "Non valide"}</dd><dt>audit_version</dt><dd>{recommendation.integrity.audit_version}</dd><dt>audit_fingerprint</dt><dd>{recommendation.integrity.audit_fingerprint}</dd>
    </dl></details>
  </>;
}

function Incomplete({ recommendation }: { recommendation: RecommendationResponse }) {
  const issues = recommendation.readiness_issues;
  return <section className="status-panel" role="status">
    <h2>Recommandation non disponible actuellement</h2>
    <p>Les données nécessaires ne sont pas toutes à jour : aucune recommandation n’est présentée comme actuelle.</p>
    {issues.length > 0 && <details className="recommendation-issues"><summary>Codes de préparation ({issues.length})</summary>
      <ul>{issues.map((issue, index) => <li key={index}><code>{issue.code}</code>{issue.opportunity_id !== null && <> — opportunité {issue.opportunity_id}</>}</li>)}</ul>
    </details>}
  </section>;
}

export default async function RecommendationPage() {
  // Read independently: tracking being unavailable must not hide the ranking.
  const [recommendation, applications] = await Promise.all([loadRecommendation(), loadApplications()]);
  const tracked = new Map<number, ApplicationStatus>(
    (applications?.items ?? []).map((application) => [application.opportunity_id, application.status]),
  );
  return <main className="page-shell">
    <header className="hero">
      <div><p className="eyebrow">Recommandation persistée</p><h1>Recommandé pour mon CV</h1><p className="subtitle">Ce classement provient de la dernière recommandation enregistrée pour votre profil.</p></div>
      <Link className="health-link" href="/">Retour aux opportunités</Link>
    </header>
    {recommendation === null ? <section className="status-panel" role="status"><h2>Recommandations temporairement indisponibles</h2><p>Le radar ne peut pas afficher les recommandations pour le moment.</p></section>
      : recommendation.status === "NOT_SYNCED" ? <section className="status-panel" role="status"><h2>Recommandation non synchronisée</h2><p>Aucune recommandation n’a encore été synchronisée.</p></section>
      : recommendation.status === "INCOMPLETE" ? <Incomplete recommendation={recommendation} />
      : <Ready recommendation={recommendation} tracked={tracked} />}
  </main>;
}
