"use client";

import React from "react";
import {
  detectPushSupport,
  disablePush,
  enablePush,
  readPushStatus,
  type PushStatus,
  type PushSupport,
} from "@/lib/push-client";

const MESSAGES: Record<PushStatus, string> = {
  unsupported: "Ce navigateur ne prend pas en charge les notifications push.",
  "not-configured": "Les notifications ne sont pas configurées sur ce serveur.",
  denied:
    "Les notifications sont bloquées pour ce site. Autorisez-les dans le navigateur pour les réactiver.",
  inactive: "Les notifications sont désactivées sur cet appareil.",
  active: "Les notifications sont activées sur cet appareil.",
  error: "Les notifications n’ont pas pu être mises à jour.",
};

export default function PushNotifications() {
  const [status, setStatus] = React.useState<PushStatus | null>(null);
  const [busy, setBusy] = React.useState(false);
  const supportRef = React.useRef<PushSupport | null>(null);

  React.useEffect(() => {
    let active = true;
    // Reading the state never prompts: the permission dialog belongs to the
    // click handlers below and to nothing else.
    supportRef.current = detectPushSupport();
    readPushStatus(supportRef.current).then((next) => {
      if (active) setStatus(next);
    });
    return () => {
      active = false;
    };
  }, []);

  async function run(action: (support: PushSupport | null) => Promise<PushStatus>) {
    setBusy(true);
    try {
      setStatus(await action(supportRef.current));
    } finally {
      setBusy(false);
    }
  }

  if (status === null) return null;

  const actionable = status === "inactive" || status === "active" || status === "error";

  return (
    <section className="status-panel" aria-labelledby="push-notifications-heading">
      <h2 id="push-notifications-heading">Notifications</h2>
      <p role="status">{MESSAGES[status]}</p>
      {actionable && (
        <p>
          {status === "active" ? (
            <button type="button" disabled={busy} onClick={() => run(disablePush)}>
              Désactiver les notifications
            </button>
          ) : (
            <button type="button" disabled={busy} onClick={() => run(enablePush)}>
              Activer les notifications
            </button>
          )}
        </p>
      )}
    </section>
  );
}
