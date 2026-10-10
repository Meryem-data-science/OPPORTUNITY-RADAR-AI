import Link from "next/link";
import { notFound } from "next/navigation";
import ApplicationActions from "@/components/application-actions";
import Icon from "@/components/icon";
import { loadApplications } from "@/lib/applications";
import { formatDateTime, organizationInitials } from "@/lib/display";
import { loadOpportunityDetail, type OpportunityListingType } from "@/lib/opportunity-detail";

export const dynamic = "force-dynamic";

const listingLabels: Record<OpportunityListingType, string> = {
  PFE: "PFE",
  INTERNSHIP: "Stage",
  GRADUATE: "Programme diplômés",
  APPRENTICESHIP: "Alternance",
  JOB: "Emploi",
  UNKNOWN: "Type d’offre non renseigné",
};

const employmentLabels: Record<string, string> = {
  FULL_TIME: "Temps plein",
  PART_TIME: "Temps partiel",
  CONTRACT: "Mission / contrat",
  TEMPORARY: "Temporaire",
  UNKNOWN: "Non renseigné",
};

function externalUrl(value: string): string | null {
  try {
    const url = new URL(value);
    return url.protocol === "https:" || url.protocol === "http:" ? url.href : null;
  } catch {
    return null;
  }
}

export default async function OpportunityDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  if (!/^[1-9][0-9]{0,15}$/.test(id) || !Number.isSafeInteger(Number(id))) notFound();
  const [result, applications] = await Promise.all([
    loadOpportunityDetail(Number(id)),
    loadApplications(),
  ]);
  if (result.kind === "not-found") notFound();
  if (result.kind === "unavailable") {
    return <main className="page-shell">
      <section className="status-panel" role="status">
        <p className="eyebrow">Fiche d’offre</p>
        <h1>Offre temporairement indisponible</h1>
        <p>La description ne peut pas être affichée pour le moment. Réessayez dans quelques instants.</p>
        <Link className="health-link" href="/explorer?country=MA">Retour aux offres au Maroc</Link>
      </section>
    </main>;
  }

  const { opportunity } = result;
  const trackedStatus = applications?.items.find((application) => application.opportunity_id === opportunity.id)?.status ?? null;
  const applicationUrl = externalUrl(opportunity.original_url);
  const description = opportunity.description?.trim() ?? "";
  const paragraphs = description.split(/\n+/).filter((paragraph) => paragraph.trim().length > 0);
  const employmentType = opportunity.employment_type?.trim() || "UNKNOWN";

  return <main className="page-shell opportunity-detail-page">
    <nav className="offer-detail-breadcrumb" aria-label="Navigation de la fiche">
      <Link className="text-link" href="/explorer?country=MA"><Icon name="search" size={15} />Retour aux offres au Maroc</Link>
    </nav>

    <header className="offer-detail-heading">
      <div className="offer-detail-organization">
        <span className="organization-avatar" aria-hidden="true">{organizationInitials(opportunity.organization)}</span>
        <p className="organization">{opportunity.organization}</p>
      </div>
      <p className="eyebrow">Opportunité Data & IA</p>
      <h1>{opportunity.canonical_title}</h1>
      <p className="offer-detail-location"><Icon name="pin" size={17} />{opportunity.location ?? "Lieu non précisé"}</p>
      {opportunity.listing_type !== null && opportunity.listing_type !== "UNKNOWN" &&
        <span className="opportunity-kind">{listingLabels[opportunity.listing_type]}</span>}
    </header>

    <div className="offer-detail-layout">
      <section className="offer-detail-description" aria-labelledby="offer-description-heading">
        <p className="eyebrow">L’annonce</p>
        <h2 id="offer-description-heading">Présentation du poste</h2>
        {description === "" ? <p className="offer-detail-empty">La source n’a pas fourni de description pour cette offre. Consultez l’annonce de l’entreprise pour connaître les missions et les compétences demandées.</p>
          : <div className="offer-description-copy">{paragraphs.map((paragraph, index) => <p key={index}>{paragraph}</p>)}</div>}
      </section>

      <aside className="offer-detail-aside" aria-label="Informations et candidature">
        <section className="offer-detail-panel" aria-labelledby="offer-apply-heading">
          <p className="eyebrow">Votre prochaine étape</p>
          <h2 id="offer-apply-heading">Cette offre vous intéresse ?</h2>
          <p>Vous pouvez lire cette fiche sans vous inscrire. Pour envoyer une candidature, le formulaire est hébergé sur le site de l’entreprise.</p>
          {applicationUrl !== null ? <a className="offer-apply-link" href={applicationUrl} target="_blank" rel="noopener noreferrer">
            Postuler sur le site de l’entreprise<Icon name="arrow-up-right" size={18} />
          </a> : <p className="offer-detail-empty">Le lien de candidature n’est pas disponible.</p>}
          <p className="offer-detail-note">L’ouverture du formulaire n’envoie aucune candidature.</p>
          <div className="offer-detail-tracking">
            <h3>Mon suivi dans Radar</h3>
            <ApplicationActions opportunityId={opportunity.id} status={trackedStatus} />
            {applications === null && <p className="offer-detail-note">Le statut de votre suivi ne peut pas être chargé pour le moment.</p>}
          </div>
        </section>

        <section className="offer-detail-panel" aria-labelledby="offer-information-heading">
          <h2 id="offer-information-heading">Repères</h2>
          <dl className="offer-detail-facts">
            <div><dt>Type d’offre</dt><dd>{opportunity.listing_type === null ? "Non renseigné" : listingLabels[opportunity.listing_type]}</dd></div>
            <div><dt>Contrat / rythme indiqué</dt><dd>{employmentLabels[employmentType] ?? employmentType}</dd></div>
            {opportunity.published_at !== null && <div><dt>Publication indiquée par la source</dt><dd>{formatDateTime(opportunity.published_at)}</dd></div>}
            <div><dt>Première collecte par Radar</dt><dd>{formatDateTime(opportunity.discovered_at)}</dd></div>
            <div><dt>Dernière détection par Radar</dt><dd>{formatDateTime(opportunity.last_seen_at)}</dd></div>
          </dl>
          <p className="offer-detail-note">La disponibilité de l’offre et le contrat exact (CDI, CDD…) sont à vérifier dans l’annonce d’origine. Les dates de collecte correspondent aux passages de Radar.</p>
        </section>
      </aside>
    </div>
  </main>;
}
