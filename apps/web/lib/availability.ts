/** Current source availability is separate from collection and profile scoring. */
export type OpportunityAvailability = {
  status: "OPEN_CONFIRMED" | "TO_VERIFY";
  last_checked_at: string | null;
  verified_at: string | null;
};

const object = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

function awareDate(value: unknown): value is string {
  return typeof value === "string" &&
    /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(value) &&
    Number.isFinite(Date.parse(value));
}

export function isAvailability(value: unknown): value is OpportunityAvailability {
  if (!object(value)) return false;
  return (value.status === "OPEN_CONFIRMED" || value.status === "TO_VERIFY") &&
    (value.last_checked_at === null || awareDate(value.last_checked_at)) &&
    (value.verified_at === null || awareDate(value.verified_at)) &&
    (value.status !== "OPEN_CONFIRMED" ||
      (awareDate(value.verified_at) && awareDate(value.last_checked_at)));
}

/** Older API responses may omit this field; they must never display green. */
export function isOptionalAvailability(value: unknown): boolean {
  return value === undefined || isAvailability(value);
}

export function readAvailability(value: unknown): OpportunityAvailability {
  return isAvailability(value) ? value : {
    status: "TO_VERIFY", last_checked_at: null, verified_at: null,
  };
}
