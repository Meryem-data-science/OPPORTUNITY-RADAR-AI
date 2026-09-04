import type {
  Application,
  ApplicationDetail,
  ApplicationsResponse,
} from "./application-contract";

/**
 * A candidature shaped exactly like one the API returns.
 *
 * It is test data and lives only in tests: nothing in the running application
 * ever renders an invented candidature or an invented opportunity.
 */
export const trackedApplication: Application = {
  id: 7,
  opportunity_id: 42,
  status: "SUBMITTED",
  submitted_at: "2026-03-02 10:15:00",
  last_status_change: "2026-03-02 10:15:00",
  next_action: "Relancer le recruteur",
  followup_date: "2026-03-16",
  notes: "Candidature envoyée via le site carrière",
  created_at: "2026-03-01 09:00:00",
  updated_at: "2026-03-02 10:15:00",
  opportunity: {
    id: 42,
    canonical_title: "Data Engineer",
    organization: "Example Org",
    location: "Paris",
    original_url: "https://careers.example.invalid/apply/42",
  },
};

export const trackedApplications: ApplicationsResponse = {
  profile_id: 1,
  items: [trackedApplication],
  total: 1,
};

export const trackedDetail: ApplicationDetail = {
  ...trackedApplication,
  events: [
    {
      id: 1,
      event_type: "APPLICATION_CREATED",
      from_status: null,
      to_status: "SAVED",
      actor_type: "USER",
      occurred_at: "2026-03-01 09:00:00",
    },
    {
      id: 2,
      event_type: "STATUS_CHANGED",
      from_status: "SAVED",
      to_status: "SUBMITTED",
      actor_type: "USER",
      occurred_at: "2026-03-02 10:15:00",
    },
    {
      id: 3,
      event_type: "TRACKING_UPDATED",
      from_status: null,
      to_status: null,
      actor_type: "USER",
      occurred_at: "2026-03-02 10:20:00",
    },
  ],
};
