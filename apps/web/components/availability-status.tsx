import React from "react";
import { readAvailability, type OpportunityAvailability } from "@/lib/availability";
import { formatDateTime } from "@/lib/display";

/** The backend owns current availability and its freshness; no local inference. */
export default function AvailabilityStatus({ availability, showDate = true }: {
  availability?: OpportunityAvailability;
  showDate?: boolean;
}) {
  const current = readAvailability(availability);
  const confirmed = current.status === "OPEN_CONFIRMED";
  const date = confirmed ? current.verified_at : current.last_checked_at;
  return <div className={`availability-status availability-status--${confirmed ? "confirmed" : "unverified"}`}>
    <span className="availability-status__badge" title={confirmed
      ? "La source a confirmé l’ouverture lors du contrôle indiqué. Une collecte récente ne prouve pas l’ouverture."
      : "L’ouverture n’a pas été confirmée récemment. Une collecte récente ne prouve pas l’ouverture."}>
      {confirmed ? "Ouverture confirmée" : "À vérifier"}
    </span>
    {showDate && date !== null && <span className="availability-status__date">
      {confirmed ? "Ouverture confirmée le " : "Dernier contrôle : "}<time dateTime={date}>{formatDateTime(date)}</time>
    </span>}
  </div>;
}
