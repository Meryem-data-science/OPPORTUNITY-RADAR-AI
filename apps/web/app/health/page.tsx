import { getSystemHealth } from "@/lib/health";

export const dynamic = "force-dynamic";

function label(value: string): string {
  return value.replace("_", " ").replace(/^./, (character) => character.toUpperCase());
}

export default async function HealthPage() {
  const health = await getSystemHealth();

  return (
    <main>
      <h1>Opportunity Radar Health</h1>
      <dl>
        <dt>Web Application</dt>
        <dd>OK</dd>
        <dt>Database</dt>
        <dd>{label(health.database)}</dd>
        <dt>Foundation Schema</dt>
        <dd>{label(health.schema)}</dd>
        <dt>Migration Version</dt>
        <dd>{health.migrationVersion ?? "Unavailable"}</dd>
      </dl>
    </main>
  );
}
