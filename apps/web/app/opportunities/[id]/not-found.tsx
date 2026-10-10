import Link from "next/link";

export default function OpportunityNotFound() {
  return <main className="page-shell">
    <section className="status-panel">
      <p className="eyebrow">Fiche d’offre</p>
      <h1>Offre introuvable</h1>
      <p>Cette offre n’existe pas dans le catalogue ou n’est plus affichée par Radar.</p>
      <Link className="health-link" href="/explorer?country=MA">Retour aux offres au Maroc</Link>
    </section>
  </main>;
}
