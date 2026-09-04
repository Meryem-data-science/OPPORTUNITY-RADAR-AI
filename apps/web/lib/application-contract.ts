/**
 * The shape of a tracked candidature, shared by both sides of the app.
 *
 * This module is deliberately free of `server-only`: the status vocabulary and
 * the response types are as true in a browser control as they are in a server
 * loader, and a client component that had to reach into a server module to
 * learn them would drag the backend base URL into the bundle with it. Nothing
 * here talks to anything — the fetching lives in `lib/applications.ts`.
 */
export const APPLICATION_STATUSES = [
  "DISCOVERED",
  "SAVED",
  "PREPARING",
  "READY",
  "SUBMITTED",
  "CONFIRMED",
  "ASSESSMENT",
  "INTERVIEW",
  "REJECTED",
  "OFFER",
  "WITHDRAWN",
] as const;

/**
 * The statuses a person may set by hand in Phase 6.1.
 *
 * READY and DISCOVERED are deliberately absent: the backend refuses both, and
 * offering a control the backend would reject is how a UI starts lying. READY
 * arrives when 6.2–6.4 can decide that an application really is ready.
 */
export const SELECTABLE_STATUSES = [
  "SAVED",
  "PREPARING",
  "SUBMITTED",
  "CONFIRMED",
  "ASSESSMENT",
  "INTERVIEW",
  "REJECTED",
  "OFFER",
  "WITHDRAWN",
] as const;

export const APPLICATION_ACTIONS = ["SAVE", "PREPARE", "MARK_SUBMITTED"] as const;

export type ApplicationStatus = (typeof APPLICATION_STATUSES)[number];
export type SelectableStatus = (typeof SELECTABLE_STATUSES)[number];
export type ApplicationAction = (typeof APPLICATION_ACTIONS)[number];

export type ApplicationOpportunity = {
  id: number;
  canonical_title: string;
  organization: string;
  location: string | null;
  original_url: string;
};

export type ApplicationEvent = {
  id: number;
  event_type: "APPLICATION_CREATED" | "STATUS_CHANGED" | "TRACKING_UPDATED";
  from_status: string | null;
  to_status: string | null;
  actor_type: "USER" | "SYSTEM";
  occurred_at: string;
};

export type Application = {
  id: number;
  opportunity_id: number;
  status: ApplicationStatus;
  submitted_at: string | null;
  last_status_change: string;
  next_action: string | null;
  followup_date: string | null;
  notes: string | null;
  created_at: string;
  updated_at: string;
  opportunity: ApplicationOpportunity;
};

export type ApplicationDetail = Application & { events: ApplicationEvent[] };

export type ApplicationsResponse = {
  profile_id: number;
  items: Application[];
  total: number;
};

export type ApplicationWriteResult = {
  created: boolean;
  changed: boolean;
  application: ApplicationDetail;
};

const STATUS_LABELS: Record<ApplicationStatus, string> = {
  DISCOVERED: "Détectée",
  SAVED: "Sauvegardée",
  PREPARING: "En préparation",
  READY: "Prête",
  SUBMITTED: "Envoyée",
  CONFIRMED: "Accusé de réception",
  ASSESSMENT: "Test technique",
  INTERVIEW: "Entretien",
  REJECTED: "Refus",
  OFFER: "Offre",
  WITHDRAWN: "Abandonnée",
};

/** A status in French, or the status itself if the backend ever adds one. */
export function statusLabel(status: string): string {
  return STATUS_LABELS[status as ApplicationStatus] ?? status;
}
