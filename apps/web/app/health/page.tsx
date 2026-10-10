import Link from "next/link";
import { getSystemHealth, HEALTH_MESSAGES } from "@/lib/health";

export const dynamic = "force-dynamic";

export default async function HealthPage() {
  const health = await getSystemHealth();
  const title = health.status === "ok" ? "Services prêts"
    : health.status === "degraded" ? "Services partiellement disponibles"
      : "Service indisponible";

  return (
    <main className="page-shell">
      <header className="hero">
        <div>
          <p className="eyebrow">État du système</p>
          <h1>{title}</h1>
          <p className="subtitle">
            {health.reason ? HEALTH_MESSAGES[health.reason]
              : "L’API, la base opérationnelle et le profil configuré sont disponibles."}
          </p>
        </div>
        <nav className="hero-links" aria-label="Navigation">
          <Link className="health-link" href="/">Retour aux offres</Link>
          <Link className="health-link" href="/source-health">État des sources</Link>
        </nav>
      </header>
      <section className="matching-summary" aria-label="Disponibilité des services">
        <dl>
          <div><dt>Application web</dt><dd>Disponible</dd></div>
          <div><dt>Service de données</dt><dd>{health.api === "available" ? "Disponible" : "Inaccessible"}</dd></div>
          <div><dt>Base opérationnelle</dt><dd>{health.database === "connected" ? "Connectée" : "Inaccessible"}</dd></div>
          <div><dt>Schéma produit</dt><dd>{health.schema === "ready" ? "Prêt" : health.schema === "not_ready" ? "Incomplet" : "Non vérifié"}</dd></div>
          <div><dt>Profil serveur</dt><dd>{health.profile === "ready" ? "Prêt" : health.profile === "not_configured" ? "À configurer" : health.profile === "not_found" ? "Introuvable" : "Non vérifié"}</dd></div>
        </dl>
      </section>
      <details className="technical-details">
        <summary>Diagnostic technique</summary>
        <dl>
          <dt>Backend configuré</dt><dd>{health.databaseBackend === "unknown" ? "Non vérifié" : health.databaseBackend}</dd>
          <dt>Dernière migration appliquée</dt><dd>{health.migrationVersion ?? "Non vérifiée"}</dd>
          <dt>Migration minimale du produit</dt><dd>{health.minimumMigrationVersion}</dd>
          {health.reason && <><dt>Code du diagnostic</dt><dd>{health.reason}</dd></>}
        </dl>
        <p>Ce diagnostic vérifie la disponibilité technique. La fraîcheur des collectes se consulte dans l’état des sources.</p>
      </details>
    </main>
  );
}
