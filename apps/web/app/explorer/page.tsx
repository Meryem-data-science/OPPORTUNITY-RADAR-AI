import Link from "next/link";
import React from "react";
import ApplicationActions from "@/components/application-actions";
import Icon from "@/components/icon";
import type { ApplicationStatus } from "@/lib/application-contract";
import { formatDateTime, organizationInitials } from "@/lib/display";
import { loadApplications } from "@/lib/applications";
import { displaySource } from "@/lib/source-display";
import {
  EXPLORER_DEFAULT_LIMIT,
  EXPLORER_FINE_CATEGORIES,
  EXPLORER_FRESHNESS,
  EXPLORER_MAX_LIMIT,
  EXPLORER_OFFER_CATEGORIES,
  EXPLORER_OPPORTUNITY_TYPES,
  explorerSearchParams,
  loadExplorer,
  type ExplorerAvailableFilters,
  type ExplorerFineCategory,
  type ExplorerFreshness,
  type ExplorerItem,
  type ExplorerListingType,
  type ExplorerOfferCategory,
  type ExplorerOpportunityType,
  type ExplorerQuery,
  type ExplorerResponse,
} from "@/lib/explorer";

export const dynamic = "force-dynamic";

type SearchParams = Record<string, string | string[] | undefined>;

const domainLabels: Record<ExplorerFineCategory, string> = {
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

const typeLabels: Record<ExplorerOpportunityType, string> = {
  PFA: "PFA (projet de fin d’année)",
  PFE: "PFE (projet de fin d’études)",
  SUMMER_INTERNSHIP: "Stage d’été",
  PRE_HIRE_INTERNSHIP: "Stage de pré-embauche",
  ALTERNANCE: "Alternance",
  INTERNSHIP: "Stage",
  FIRST_JOB: "Premier emploi",
  JUNIOR_ROLE: "Poste junior",
};

const offerCategoryLabels: Record<ExplorerOfferCategory, string> = {
  STAGES: "Stages (PFA, PFE et autres stages)",
  PFE: "PFE",
  EMPLOIS: "Emplois — tous niveaux",
  ALTERNANCE: "Alternance",
};

const listingTypeLabels: Record<ExplorerListingType, string> = {
  PFE: "PFE (projet de fin d’études)",
  INTERNSHIP: "Stage",
  GRADUATE: "Programme diplômés",
  APPRENTICESHIP: "Alternance",
  JOB: "Emploi",
  UNKNOWN: "Type non renseigné",
};

function opportunityTypeLabel(item: ExplorerItem): string {
  if (item.opportunity_type !== null) return typeLabels[item.opportunity_type];
  return item.listing_type ? listingTypeLabels[item.listing_type] : "Type non renseigné";
}

const freshnessLabels: Record<ExplorerFreshness, string> = {
  "24h": "Vue dans les dernières 24 h",
  "7d": "Vue dans les 7 derniers jours",
  "30d": "Vue dans les 30 derniers jours",
};

function first(value: string | string[] | undefined): string | undefined {
  const raw = Array.isArray(value) ? value[0] : value;
  const trimmed = raw?.trim();
  return trimmed ? trimmed : undefined;
}

function oneOf<T extends string>(vocabulary: readonly T[]) {
  return (value: string): T | undefined => ((vocabulary as readonly string[]).includes(value) ? (value as T) : undefined);
}

function integer(min: number, max: number) {
  return (value: string): number | undefined => {
    if (!/^\d+$/.test(value)) return undefined;
    const parsed = Number(value);
    return parsed >= min && parsed <= max ? parsed : undefined;
  };
}

type ParsedExplorerQuery = { valid: true; query: ExplorerQuery } | { valid: false };

/**
 * The user-facing catalogue is restricted to Morocco. Older country choices
 * in saved URLs cannot broaden it; the remaining filters retain validation.
 */
function explorerQueryFromSearchParams(params: SearchParams): ParsedExplorerQuery {
  const query: ExplorerQuery = { country: "MA" };
  let valid = true;
  function read<K extends keyof ExplorerQuery>(key: K, parse: (value: string) => ExplorerQuery[K] | undefined) {
    const value = first(params[key]);
    if (value === undefined) return;
    const parsed = parse(value);
    if (parsed === undefined) valid = false;
    else query[key] = parsed;
  }
  read("city", (value) => value);
  read("offer_category", oneOf(EXPLORER_OFFER_CATEGORIES));
  read("opportunity_type", oneOf(EXPLORER_OPPORTUNITY_TYPES));
  read("domain", oneOf(EXPLORER_FINE_CATEGORIES));
  read("source", (value) => value);
  read("freshness", oneOf(EXPLORER_FRESHNESS));
  read("limit", integer(1, EXPLORER_MAX_LIMIT));
  read("offset", integer(0, Number.MAX_SAFE_INTEGER));
  return valid ? { valid: true, query } : { valid: false };
}

function explorerHref(query: ExplorerQuery, offset: number): string {
  const parameters = explorerSearchParams({ ...query, country: "MA", offset });
  return `/explorer?${parameters.toString()}`;
}

function Filters({ filters, query }: { filters: ExplorerAvailableFilters; query: ExplorerQuery }) {
  const queryWithoutPreciseType = { ...query };
  delete queryWithoutPreciseType.opportunity_type;
  const hasPreciseType = query.opportunity_type !== undefined;
  const cities = [...new Set(filters.cities.filter((city) => city.country_code === "MA").map((city) => city.city_key))];
  return <form className="explorer-filters" method="get" action="/explorer" aria-label="Filtres Explorer">
    <input type="hidden" name="country" value="MA" />
    <div className="explorer-filter-grid">
      <label>Ville
        <select name="city" defaultValue={query.city ?? ""}>
          <option value="">Toutes les villes du Maroc</option>
          {cities.map((cityKey) => <option key={cityKey} value={cityKey}>{cityKey}</option>)}
        </select>
      </label>
      <label>Type d’offre
        <select name="offer_category" defaultValue={query.offer_category ?? ""} disabled={hasPreciseType} aria-describedby={hasPreciseType ? "explorer-precise-type" : undefined}>
          <option value="">{hasPreciseType ? "Filtre précis actif" : "Toutes les offres"}</option>
          {EXPLORER_OFFER_CATEGORIES.map((category) => <option key={category} value={category}>{offerCategoryLabels[category]}</option>)}
        </select>
      </label>
      <label>Domaine Data/IA
        <select name="domain" defaultValue={query.domain ?? ""}>
          <option value="">Tous les domaines</option>
          {filters.domains.map((domain) => <option key={domain} value={domain}>{domainLabels[domain]}</option>)}
        </select>
      </label>
      <label>Source
        <select name="source" defaultValue={query.source ?? ""}>
          <option value="">Toutes les sources</option>
          {filters.sources.map((source) => <option key={source.source_id} value={source.source_id}>{displaySource(source.source_id)}</option>)}
        </select>
      </label>
      <label>Fraîcheur
        <select name="freshness" defaultValue={query.freshness ?? ""}>
          <option value="">Toute période</option>
          {EXPLORER_FRESHNESS.map((window) => <option key={window} value={window}>{freshnessLabels[window]}</option>)}
        </select>
      </label>
    </div>
    {query.opportunity_type !== undefined && <p id="explorer-precise-type" className="muted">
      <input type="hidden" name="opportunity_type" value={query.opportunity_type} />
      {query.offer_category !== undefined && <input type="hidden" name="offer_category" value={query.offer_category} />}
      Filtre précis : {typeLabels[query.opportunity_type]}. <Link className="health-link" href={explorerHref(queryWithoutPreciseType, 0)}>Supprimer le filtre précis</Link> pour choisir une autre catégorie.
    </p>}
    {query.limit !== undefined && query.limit !== EXPLORER_DEFAULT_LIMIT && <input type="hidden" name="limit" value={query.limit} />}
    <div className="explorer-filter-actions">
      <button type="submit">Appliquer les filtres</button>
      <Link className="health-link" href="/explorer">Réinitialiser · Maroc</Link>
    </div>
  </form>;
}

function Locations({ item }: { item: ExplorerItem }) {
  const resolved = item.resolved_locations;
  return <div className="explorer-meta-row">
    <strong>Localisation résolue</strong>
    {resolved.length > 0 && <ul className="explorer-chips">
      {resolved.map((location, index) => <li key={`${location.country_code}-${location.city_key ?? ""}-${index}`}>
        {location.country_code}{location.city_key !== null && <> · {location.city_key}</>}
      </li>)}
    </ul>}
    {item.has_unresolved_location && <span className="explorer-unresolved">
      {resolved.length === 0 ? "Localisation non résolue" : "Localisation partiellement résolue"}
    </span>}
  </div>;
}

function ExplorerCard({ item, trackedStatus }: { item: ExplorerItem; trackedStatus: ApplicationStatus | null }) {
  return <article className="explorer-card" data-opportunity-id={item.opportunity_id}>
    <div className="opportunity-card-top">
      <span className="organization-avatar" aria-hidden="true">{organizationInitials(item.organization)}</span>
      <div className="opportunity-card-body"><p className="organization">{item.organization}</p><h3>{item.canonical_title}</h3><p className="location"><Icon name="pin" size={15} />{item.raw_location ?? "Lieu non précisé"}</p></div>
    </div>
    <dl className="explorer-meta">
      <div><dt>Domaine Data/IA</dt><dd>{item.fine_primary_category === null ? "Domaine non renseigné" : domainLabels[item.fine_primary_category]}</dd></div>
      <div><dt>Type d’offre</dt><dd>{opportunityTypeLabel(item)}</dd></div>
      <div><dt>Dernière détection</dt><dd>{formatDateTime(item.last_seen_at)}</dd></div>
    </dl>
    <details className="offer-context"><summary>Localisation et sources</summary><Locations item={item} />
    <div className="explorer-meta-row">
      <strong>Sources</strong>
      {item.sources.length === 0 ? <span className="explorer-unresolved">Aucune source enregistrée</span>
        : <ul className="explorer-chips">{item.sources.map((source) => <li key={source.source_id}>{displaySource(source.source_id)}</li>)}</ul>}
    </div></details>
    <ApplicationActions opportunityId={item.opportunity_id} status={trackedStatus} />
    <div className="card-footer">
      <span>Data & IA</span>
      <a href={item.original_url} target="_blank" rel="noopener noreferrer">Voir l’offre originale<Icon name="arrow-up-right" size={16} /></a>
    </div>
  </article>;
}

function Results({ explorer, query, tracked }: { explorer: ExplorerResponse; query: ExplorerQuery; tracked: Map<number, ApplicationStatus> }) {
  const { items, total, returned, limit, offset } = explorer;
  const hasPrevious = offset > 0;
  const hasNext = offset + returned < total;
  return <section aria-labelledby="explorer-results-heading">
    <div className="section-heading">
      <h2 id="explorer-results-heading">{total} opportunité{total > 1 ? "s" : ""} au Maroc</h2>
      {returned > 0 && <p>Résultats {offset + 1}–{offset + returned} sur {total}</p>}
    </div>
    {items.length === 0 ? <div className="status-panel" role="status">
      <h2>Aucune opportunité ne correspond à ces filtres</h2>
      <p><Link href="/explorer">Réinitialiser les filtres · Maroc</Link></p>
    </div>
      // Rendered in exactly the order the API returned: navigation, not a ranking.
      : <div className="explorer-list">{items.map((item) => <ExplorerCard key={item.opportunity_id} item={item} trackedStatus={tracked.get(item.opportunity_id) ?? null} />)}</div>}
    {(hasPrevious || hasNext) && <nav className="explorer-pagination" aria-label="Pagination Explorer">
      {hasPrevious && <Link className="health-link" href={explorerHref(query, Math.max(offset - limit, 0))}>Page précédente</Link>}
      {hasNext && <Link className="health-link" href={explorerHref(query, offset + returned)}>Page suivante</Link>}
    </nav>}
  </section>;
}

function Header() {
  return <header className="hero">
    <div>
      <p className="eyebrow">Exploration Data & IA · Maroc</p>
      <h1>Votre prochaine opportunité au Maroc.</h1>
      <p className="subtitle">Stages, PFE et emplois Data & IA au Maroc, tous niveaux et secteurs. Le type de contrat est à vérifier dans l’annonce d’origine.</p>
    </div>
    <nav className="hero-links" aria-label="Navigation Explorer">
      <Link className="health-link" href="/recommendation">Mes recommandations<Icon name="arrow-right" size={16} /></Link>
    </nav>
  </header>;
}

export default async function ExplorerPage({ searchParams }: { searchParams: Promise<SearchParams> }) {
  const parsed = explorerQueryFromSearchParams(await searchParams);
  if (!parsed.valid) {
    // A malformed filter is never weakened into a broader query: nothing is loaded.
    return <main className="page-shell">
      <Header />
      <section className="status-panel" role="status">
        <h2>Filtres non reconnus</h2>
        <p>Un ou plusieurs paramètres de l’adresse ne sont pas reconnus. <Link href="/explorer">Revenir aux offres au Maroc</Link></p>
      </section>
    </main>;
  }
  const { query } = parsed;
  // Read independently: tracking being unavailable must not hide the Explorer.
  const [explorer, applications] = await Promise.all([loadExplorer(query), loadApplications()]);
  const tracked = new Map<number, ApplicationStatus>(
    (applications?.items ?? []).map((application) => [application.opportunity_id, application.status]),
  );
  return <main className="page-shell">
    <Header />
    {explorer === null ? <section className="status-panel" role="status">
      <h2>Offres temporairement indisponibles</h2>
      <p>Le radar ne peut pas afficher les offres pour le moment.</p><Link className="health-link" href="/source-health">Consulter l’état des sources</Link>
    </section> : <>
      <Filters filters={explorer.available_filters} query={query} />
      <Results explorer={explorer} query={query} tracked={tracked} />
    </>}
  </main>;
}
