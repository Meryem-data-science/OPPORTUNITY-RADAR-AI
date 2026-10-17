/** Public display names shared by the source strip, Explorer and health page. */
const SOURCE_NAMES: Record<string, string> = {
  scale_ai_greenhouse: "Scale AI",
  artefact_greenhouse: "Artefact",
  linkedin_job_alert_email: "Alertes LinkedIn",
  linkedin_gmail_alerts: "Alertes LinkedIn",
  indeed_job_alert_email: "Alertes Indeed",
  stagiaires_ma: "Stagiaires.ma",
  stage_ma: "Stage.ma",
  sqli_careers: "SQLI · carrières",
  yassir_lever: "Yassir · carrières",
  welodata_lever: "Welo Data · carrières",
};

export function displaySource(id: string): string {
  const key = id.trim().toLowerCase().replace(/-/g, "_");
  return SOURCE_NAMES[key] ?? id;
}
