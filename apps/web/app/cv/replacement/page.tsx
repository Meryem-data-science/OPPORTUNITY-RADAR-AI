import Link from "next/link";
import React from "react";
import CvReplacementReview from "@/components/cv-replacement-review";
import { loadCurrentReview } from "@/lib/cv-replacement";

/**
 * The CV replacement review.
 *
 * Dynamic, never cached: the payload carries the person's own CV readings, and
 * a cached copy is a copy nobody asked for.
 *
 * When the backend cannot be read — it is down, misconfigured, or running
 * against a database this workflow's migrations have not reached — the page
 * says exactly that. It does not invent a review, and it does not offer to
 * migrate anything: applying a migration is an operator's deliberate act, not
 * a side effect of opening a page.
 */
export const dynamic = "force-dynamic";

export default async function CvReplacementPage() {
  const review = await loadCurrentReview();

  return (
    <main className="page-shell">
      <header className="hero">
        <div>
          <p className="eyebrow">Jumeau numérique</p>
          <h1>Remplacement du CV</h1>
          <p className="subtitle">
            Examinez chaque lecture du nouveau CV et chaque fait de votre profil
            actuel, puis décidez vous-même de ce qui entre dans votre profil.
          </p>
        </div>
        <nav className="hero-links" aria-label="Navigation">
          <Link className="health-link" href="/">
            Retour aux opportunités
          </Link>
        </nav>
      </header>

      {review === null ? (
        <section className="status-panel" role="status">
          <h2>Revue de remplacement indisponible</h2>
          <p>
            La revue de remplacement CV ne peut pas être affichée pour le
            moment. Le service peut être arrêté, ou la base de données peut ne
            pas être préparée pour cette fonctionnalité.
          </p>
          <p>
            Aucune revue n’est créée et aucune donnée n’est modifiée tant que le
            service n’est pas disponible.
          </p>
        </section>
      ) : (
        <CvReplacementReview initial={review} />
      )}
    </main>
  );
}
