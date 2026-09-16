import Link from "next/link";
import React from "react";
import ApplicationActions from "@/components/application-actions";
import type { ApplicationStatus } from "@/lib/application-contract";
import { loadApplications } from "@/lib/applications";
import {
  EXPLORER_DEFAULT_LIMIT,
  EXPLORER_FINE_CATEGORIES,
  EXPLORER_FRESHNESS,
  EXPLORER_MAX_LIMIT,
  EXPLORER_OPPORTUNITY_TYPES,
  explorerSearchParams,
  loadExplorer,
  type ExplorerAvailableFilters,
  type ExplorerFineCategory,
  type ExplorerFreshness,
  type ExplorerItem,
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
 * The Explorer query named by the URL. An absent or empty supported parameter
 * is no filter; a present but malformed one invalidates the whole request, so
 * it is never silently read as "filter absent". Unrelated parameters are ignored.
 */
function explorerQueryFromSearchParams(params: SearchParams): ParsedExplorerQuery {
  const query: ExplorerQuery = {};
  let valid = true;
  function read<K extends keyof ExplorerQuery>(key: K, parse: (value: string) => ExplorerQuery[K] | undefined) {
    const value = first(params[key]);
    if (value === undefined) return;
    const parsed = parse(value);
    if (parsed === undefined) valid = false;
    else query[key] = parsed;
  }
  read("country", (value) => (/^[A-Z]{2}$/.test(value) ? value : undefined));
  read("city", (value) => value);
  read("opportunity_type", oneOf(EXPLORER_OPPORTUNITY_TYPES));
  read("domain", oneOf(EXPLORER_FINE_CATEGORIES));
  read("source", (value) => value);
  read("freshness", oneOf(EXPLORER_FRESHNESS));
  read("limit", integer(1, EXPLORER_MAX_LIMIT));
  read("offset", integer(0, Number.MAX_SAFE_INTEGER));
  return valid ? { valid: true, query } : { valid: false };
}

function explorerHref(query: ExplorerQuery, offset: number): string {
  return `/explorer?${explorerSearchParams({ ...query, offset }).toString()}`;
}

function countryLabel(code: string): string {
  try {
    const name = new Intl.DisplayNames(["fr"], { type: "region" }).of(code);
    return name && name !== code ? `${name} (${code})` : code;
  } catch {
    return code;
  }
}

function Filters({ filters, query }: { filters: ExplorerAvailableFilters; query: ExplorerQuery }) {
  // Presentation only: one option per persisted city_key, labelled with its countries.
  const cities = new Map<string, string[]>();
  for (const city of filters.cities) {
    const countries = cities.get(city.city_key) ?? [];
    if (!countries.includes(city.country_code)) countries.push(city.country_code);
    cities.set(city.city_key, countries);
  }
  return <form className="explorer-filters" method="get" action="/explorer" aria-label="Filtres Explorer">
    <div className="explorer-filter-grid">
      <label>Pays
        <select name="country" defaultValue={query.country ?? ""}>
          <option value="">Tous les pays</option>
          {filters.countries.map((code) => <option key={code} value={code}>{countryLabel(code)}</option>)}
        </select>
      </label>
      <label>Ville
        <select name="city" defaultValue={query.city ?? ""}>
          <option value="">Toutes les villes</option>
          {[...cities].map(([cityKey, countries]) => <option key={cityKey} value={cityKey}>{cityKey} ({countries.join(", ")})</option>)}
        </select>
      </label>
      <label>Type d’opportunité
        <select name="opportunity_type" defaultValue={query.opportunity_type ?? ""}>
          <option value="">Tous les types</option>
          {filters.opportunity_types.map((type) => <option key={type} value={type}>{typeLabels[type]}</option>)}
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
          {filters.sources.map((source) => <option key={source.source_id} value={source.source_id}>{source.source_id} ({source.source_type})</option>)}
        </select>
      </label>
      <label>Fraîcheur
        <select name="freshness" defaultValue={query.freshness ?? ""}>
          <option value="">Toute période</option>
          {EXPLORER_FRESHNESS.map((window) => <option key={window} value={window}>{freshnessLabels[window]}</option>)}
        </select>
      </label>
    </div>
    {query.limit !== undefined && query.limit !== EXPLORER_DEFAULT_LIMIT && <input type="hidden" name="limit" value={query.limit} />}
    <div className="explorer-filter-actions">
      <button type="submit">Appliquer les filtres</button>
      <Link className="health-link" href="/explorer">Réinitialiser</Link>
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
    <p className="organization">{item.organization}</p>
    <h3>{item.canonical_title}</h3>
    <p className="location">{item.raw_location ?? "Lieu non précisé"}</p>
    <dl className="explorer-meta">
      <div><dt>Domaine Data/IA</dt><dd>{item.fine_primary_category === null ? "Domaine non renseigné" : domainLabels[item.fine_primary_category]}</dd></div>
      <div><dt>Type d’opportunité</dt><dd>{item.opportunity_type === null ? "Type non renseigné" : typeLabels[item.opportunity_type]}</dd></div>
      <div><dt>Vue pour la dernière fois</dt><dd>{item.last_seen_at}</dd></div>
    </dl>
    <Locations item={item} />
    <div className="explorer-meta-row">
      <strong>Sources</strong>
      {item.sources.length === 0 ? <span className="explorer-unresolved">Aucune source enregistrée</span>
        : <ul className="explorer-chips">{item.sources.map((source) => <li key={source.source_id}>{source.source_id} ({source.source_type})</li>)}</ul>}
    </div>
    <ApplicationActions opportunityId={item.opportunity_id} status={trackedStatus} />
    <div className="card-footer">
      <span>Opportunité Data/IA persistée</span>
      <a href={item.original_url} target="_blank" rel="noreferrer">Voir l’offre originale</a>
    </div>
  </article>;
}

function Results({ explorer, query, tracked }: { explorer: ExplorerResponse; query: ExplorerQuery; tracked: Map<number, ApplicationStatus> }) {
  const { items, total, returned, limit, offset } = explorer;
  const hasPrevious = offset > 0;
  const hasNext = offset + returned < total;
  return <section aria-labelledby="explorer-results-heading">
    <div className="section-heading">
      <h2 id="explorer-results-heading">{total} opportunité{total > 1 ? "s" : ""} correspondante{total > 1 ? "s" : ""}</h2>
      {returned > 0 && <p>Résultats {offset + 1}–{offset + returned} sur {total}</p>}
    </div>
    {items.length === 0 ? <div className="status-panel" role="status">
      <h2>Aucune opportunité ne correspond à ces filtres</h2>
      <p><Link href="/explorer">Réinitialiser les filtres</Link></p>
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
      <p className="eyebrow">Exploration Data & IA</p>
      <h1>Explorer Data & AI</h1>
      <p className="subtitle">Parcourez les opportunités Data/IA enregistrées par le radar, indépendamment du matching et de la recommandation pour votre CV.</p>
    </div>
    <nav className="hero-links" aria-label="Navigation Explorer">
      <Link className="health-link" href="/recommendation">Recommandé pour mon CV</Link>
      <Link className="health-link" href="/">Retour aux opportunités</Link>
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
        <h2>Paramètres Explorer invalides</h2>
        <p>Un ou plusieurs paramètres de l’adresse ne sont pas reconnus. <Link href="/explorer">Revenir à l’Explorer sans filtre</Link></p>
      </section>
    </main>;
  }
  const query = parsed.query;
  // Read independently: tracking being unavailable must not hide the Explorer.
  const [explorer, applications] = await Promise.all([loadExplorer(query), loadApplications()]);
  const tracked = new Map<number, ApplicationStatus>(
    (applications?.items ?? []).map((application) => [application.opportunity_id, application.status]),
  );
  return <main className="page-shell">
    <Header />
    {explorer === null ? <section className="status-panel" role="status">
      <h2>Explorer temporairement indisponible</h2>
      <p>Le radar ne peut pas afficher l’exploration pour le moment.</p>
    </section> : <>
      <Filters filters={explorer.available_filters} query={query} />
      <Results explorer={explorer} query={query} tracked={tracked} />
    </>}
  </main>;
}
