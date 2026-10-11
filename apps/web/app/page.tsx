import Image from "next/image";
import Link from "next/link";
import React from "react";
import Icon from "@/components/icon";
import { OpportunityCard, type TrackedStatuses } from "@/components/opportunity-list";
import type { ApplicationStatus } from "@/lib/application-contract";
import { loadApplications } from "@/lib/applications";
import { loadExplorer } from "@/lib/explorer";
import { loadSourceHealth } from "@/lib/source-health";
import { displaySource } from "@/lib/source-display";

export const dynamic = "force-dynamic";

const listingTypeLabels = {
  PFE: "PFE", INTERNSHIP: "Stage", GRADUATE: "Programme diplômés",
  APPRENTICESHIP: "Alternance", JOB: "Emploi", UNKNOWN: "Type non précisé",
};

function dateLabel(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "Date non disponible" : new Intl.DateTimeFormat("fr", { day: "numeric", month: "long", year: "numeric" }).format(date);
}

export default async function Home() {
  const [explorer, applications, sources] = await Promise.all([
    loadExplorer({ country: "MA", limit: 6 }), loadApplications(), loadSourceHealth(),
  ]);
  const moroccoCities = explorer ? new Set(explorer.available_filters.cities.filter((city) => city.country_code === "MA").map((city) => city.city_key)).size : undefined;
  const tracked: TrackedStatuses = Object.fromEntries((applications?.items ?? []).map((application) => [String(application.opportunity_id), application.status as ApplicationStatus]));
  const enabledSources = sources?.items.filter((source) => source.enabled) ?? [];
  const lastCollection = enabledSources.map((source) => source.last_run_at).filter((date): date is string => date !== null && !Number.isNaN(new Date(date).getTime())).sort((a, b) => new Date(b).getTime() - new Date(a).getTime())[0];
  const number = (value: number | undefined) => value === undefined ? "—" : value.toLocaleString("fr");

  return <main className="page-shell home-page">
    <header className="home-masthead">
      <p>VOTRE CARRIÈRE, EN PERSPECTIVE</p>
      <Link href="/source-health" className="masthead-source"><span className="status-dot" />{lastCollection ? `Dernière collecte · ${dateLabel(lastCollection)}` : "Consulter l’état de la collecte"}<Icon name="arrow-up-right" size={14} /></Link>
    </header>

    <section className="editorial-hero" aria-labelledby="home-title">
      <div className="hero-photo"><Image src="/images/radar-editorial-hero.png" alt="Une jeune professionnelle portant un ordinateur, dans une architecture baignée de lumière." fill priority sizes="(max-width: 700px) 100vw, 60vw" /></div>
      <div className="editorial-copy">
        <p className="eyebrow"><span className="eyebrow-line" /> DATA · IA · TECH · MAROC</p>
        <h1 id="home-title">Votre prochaine<br />opportunité<br /><em>commence ici.</em></h1>
        <p className="editorial-intro">Stages, PFE et emplois Data & IA au Maroc, réunis au même endroit. Tous les niveaux d’expérience, pour construire la suite de votre parcours.</p>
        <form className="hero-search" action="/explorer" method="get" aria-label="Rechercher des opportunités Data et IA">
          <label className="hero-search-domain"><Icon name="search" size={19} /><span className="sr-only">Domaine recherché</span><select name="domain" defaultValue=""><option value="">Quel domaine vous intéresse ?</option><option value="DATA_SCIENCE">Data science</option><option value="DATA_ANALYTICS">Analyse de données</option><option value="DATA_ENGINEERING">Data engineering</option><option value="ARTIFICIAL_INTELLIGENCE">Intelligence artificielle</option><option value="MACHINE_LEARNING">Machine learning</option><option value="GENERATIVE_AI">IA générative</option></select></label>
          <div className="hero-search-country hero-search-scope"><Icon name="pin" size={17} /><span>Maroc</span><input type="hidden" name="country" value="MA" /></div>
          <button type="submit">Explorer <Icon name="arrow-right" size={17} /></button>
        </form>
        <div className="hero-shortcuts" aria-label="Accès rapides aux offres au Maroc"><Link href="/explorer?country=MA"><Icon name="globe" size={15} />Toutes les offres</Link><Link href="/explorer?country=MA&offer_category=STAGES"><Icon name="briefcase" size={15} />Stages</Link><Link href="/explorer?country=MA&offer_category=PFE">PFE</Link><Link href="/explorer?country=MA&offer_category=EMPLOIS"><Icon name="briefcase" size={15} />Emplois</Link></div>
      </div>
      <div className="hero-side-note" aria-hidden="true"><span>LE PROCHAIN CHAPITRE</span><p>Apprendre.<br />Oser.<br /><em>Avancer.</em></p></div>
      <span className="hero-photo-caption">Une ambition. De nouvelles perspectives.</span>
    </section>

    <section className="radar-stats" aria-label="Votre radar en chiffres">
      <div><span className="stat-value">{number(explorer?.total)}</span><span>opportunités Data & IA au Maroc</span></div>
      <div><span className="stat-value">{number(moroccoCities)}</span><span>villes marocaines dans le catalogue</span></div>
      <div><span className="stat-value">{number(applications?.total)}</span><span>candidature{applications?.total === 1 ? "" : "s"} suivie{applications?.total === 1 ? "" : "s"}</span></div>
      <div><span className="stat-value">{number(sources ? enabledSources.length : undefined)}</span><span>sources activées</span></div>
    </section>

    {enabledSources.length > 0 && <div className="source-strip"><span className="source-strip-label">VOTRE VEILLE<br />MULTISOURCE</span><div>{enabledSources.map((source) => <Link href="/source-health" key={source.source_id}>{displaySource(source.source_id)}</Link>)}</div><Link className="text-link" href="/source-health">État des sources <Icon name="arrow-right" size={15} /></Link></div>}

    <section className="journey-section" aria-labelledby="journey-heading"><div className="journey-intro"><p className="eyebrow">VOTRE PARCOURS</p><h2 id="journey-heading">Trouver.<br />Comprendre.<br /><em>Passer à l’action.</em></h2><p>Une veille lisible, pour vous concentrer sur la prochaine étape.</p></div><div className="journey-cards">
      <Link href="/explorer" className="journey-card"><span className="journey-top"><Icon name="search" size={23} /><span>01</span></span><h3>Explorez vos options.</h3><p>Stages, PFE et emplois : filtrez par domaine, ville et catégorie d’offre.</p><span className="journey-link">Parcourir les offres <Icon name="arrow-right" size={18} /></span></Link>
      <Link href="/recommendation" className="journey-card"><span className="journey-top"><Icon name="sparkles" size={23} /><span>02</span></span><h3>Repérez les correspondances.</h3><p>Comprenez les points forts, les écarts et les informations à vérifier.</p><span className="journey-link">Voir les recommandations <Icon name="arrow-right" size={18} /></span></Link>
      <Link href="/applications" className="journey-card"><span className="journey-top"><Icon name="folder" size={23} /><span>03</span></span><h3>Gardez le fil.</h3><p>Enregistrez une offre et suivez vos candidatures, étape par étape.</p><span className="journey-link">Mes candidatures <Icon name="arrow-right" size={18} /></span></Link>
    </div></section>

    <section className="home-opportunities" aria-labelledby="opportunities-heading"><div className="section-heading"><div><p className="eyebrow">OPPORTUNITÉS AU MAROC</p><h2 id="opportunities-heading">Votre prochaine étape, au Maroc.</h2></div><Link href="/explorer?country=MA" className="text-link">Toutes les offres au Maroc <Icon name="arrow-right" size={17} /></Link></div><p className="collection-note">Stages, PFE et emplois Data & IA, tous niveaux, localisés au Maroc. Vérifiez le contrat (CDI, CDD…), la disponibilité et les conditions sur l’annonce d’origine. Les dates indiquées correspondent à la collecte.</p>
      {explorer === null ? <div className="status-panel" role="status"><h3>Le catalogue est momentanément indisponible.</h3><p>Consultez <Link href="/health">l’état du système</Link> pour retrouver les offres.</p></div> : explorer.items.length === 0 ? <div className="status-panel" role="status"><h3>Aucune offre Data & IA localisée au Maroc pour le moment.</h3><p>Les prochaines collectes actualiseront cette sélection.</p></div> : <><div className="opportunity-grid">{explorer.items.map((item) => <OpportunityCard key={item.opportunity_id} opportunity={{ id: item.opportunity_id, canonical_title: item.canonical_title, organization: item.organization, location: item.raw_location, original_url: item.original_url, last_seen_at: item.last_seen_at }} trackedStatus={tracked[String(item.opportunity_id)] ?? null} offerLabel={item.opportunity_type === "PFE" ? "PFE" : item.opportunity_type === "PFA" ? "PFA" : item.listing_type ? listingTypeLabels[item.listing_type] : undefined} />)}</div><div className="opportunity-pagination"><p>{explorer.returned} opportunités affichées sur {explorer.total} au Maroc</p><Link className="health-link" href="/explorer?country=MA">Voir toutes les offres au Maroc <Icon name="arrow-right" size={16} /></Link></div></>}
    </section>

    <section className="mission-banner"><div><p className="eyebrow">OPPORTUNITY RADAR AI</p><h2>Moins de dispersion.<br /><em>Plus de perspectives.</em></h2></div><div><p>Un espace pour réunir votre veille, comprendre les opportunités et organiser la suite de votre parcours.</p><Link href="/recommendation">Découvrir mes recommandations <Icon name="arrow-right" size={18} /></Link></div></section>
    <footer className="home-footer"><p>Opportunity Radar AI <span>· Veille Data & IA au Maroc</span></p><Link href="/health">État du système <Icon name="arrow-up-right" size={14} /></Link></footer>
  </main>;
}
