import { describe, expect, it, vi } from "vitest";

import {
  detectPushSupport,
  disablePush,
  enablePush,
  readPushStatus,
  urlBase64ToUint8Array,
  type PushSupport,
} from "./push-client";

const VAPID_KEY = Buffer.from([4, ...Array.from({ length: 64 }, (_, i) => i)])
  .toString("base64url");

type Options = {
  configured?: boolean;
  permission?: NotificationPermission;
  existing?: boolean;
  registered?: boolean;
  subscribeFails?: boolean;
  persistFails?: boolean;
  revokeFails?: boolean;
};

function harness(options: Options = {}) {
  const {
    configured = true,
    permission = "default",
    existing = false,
    registered = true,
    subscribeFails = false,
    persistFails = false,
    revokeFails = false,
  } = options;

  const subscription = {
    endpoint: "https://push.example.invalid/subscription/abc",
    unsubscribe: vi.fn(async () => true),
    toJSON: () => ({
      endpoint: "https://push.example.invalid/subscription/abc",
      expirationTime: null,
      keys: { p256dh: "BNp256dhKey", auth: "authSecret" },
    }),
  };
  let current: typeof subscription | null = existing ? subscription : null;

  const pushManager = {
    getSubscription: vi.fn(async () => current),
    subscribe: vi.fn<(options: PushSubscriptionOptionsInit) => Promise<unknown>>(
      async () => {
        if (subscribeFails) throw new Error("subscribe refused");
        current = subscription;
        return subscription;
      },
    ),
  };
  const registration = { pushManager, scope: "/" };
  const serviceWorker = {
    register: vi.fn(async () => registration),
    getRegistration: vi.fn(async () => (registered ? registration : undefined)),
  };
  const requestPermission = vi.fn(async () => "granted" as NotificationPermission);
  const notification = { permission, requestPermission };

  const fetchMock = vi.fn(async (input: string, init?: RequestInit) => {
    if (input === "/api/push/config") {
      return new Response(
        JSON.stringify(
          configured
            ? { configured: true, vapid_public_key: VAPID_KEY }
            : { configured: false, vapid_public_key: null },
        ),
        { status: 200 },
      );
    }
    if (input === "/api/push/subscriptions" && init?.method === "POST") {
      return new Response(JSON.stringify({ status: "ACTIVE" }), {
        status: persistFails ? 503 : 201,
      });
    }
    if (input === "/api/push/subscriptions" && init?.method === "DELETE") {
      return new Response(JSON.stringify({ status: "REVOKED" }), {
        status: revokeFails ? 503 : 200,
      });
    }
    throw new Error(`unexpected request: ${input}`);
  });

  const support = {
    serviceWorker,
    notification,
    fetch: fetchMock,
  } as unknown as PushSupport;

  return { support, subscription, pushManager, serviceWorker, notification, fetchMock };
}

function posted(fetchMock: ReturnType<typeof vi.fn>) {
  const call = fetchMock.mock.calls.find(
    ([input, init]) => input === "/api/push/subscriptions" && init?.method === "POST",
  );
  return call === undefined ? null : JSON.parse(call[1].body as string);
}

describe("urlBase64ToUint8Array", () => {
  it("decodes an unpadded base64url VAPID key to its raw 65 bytes", () => {
    const bytes = urlBase64ToUint8Array(VAPID_KEY);

    expect(bytes).toBeInstanceOf(Uint8Array);
    expect(bytes.length).toBe(65);
    expect(bytes[0]).toBe(4);
    expect(Buffer.from(bytes).toString("base64url")).toBe(VAPID_KEY);
  });

  it.each(["", "not base64!", "abc=", "with space", "a/b+c"])(
    "refuses %o rather than handing the browser junk",
    (value) => {
      expect(() => urlBase64ToUint8Array(value)).toThrow();
    },
  );
});

describe("detectPushSupport", () => {
  it("returns null when any required API is missing", () => {
    const complete = {
      navigator: { serviceWorker: {} },
      PushManager: function PushManager() {},
      Notification: { permission: "default", requestPermission: async () => "granted" },
      fetch: async () => new Response("{}"),
    };

    expect(detectPushSupport(complete)).not.toBeNull();
    for (const missing of ["navigator", "PushManager", "Notification", "fetch"]) {
      const partial: Record<string, unknown> = { ...complete };
      delete partial[missing];
      expect(detectPushSupport(partial)).toBeNull();
    }
    expect(detectPushSupport({ ...complete, navigator: {} })).toBeNull();
  });
});

describe("readPushStatus", () => {
  it("reports an unsupported browser", async () => {
    await expect(readPushStatus(null)).resolves.toBe("unsupported");
  });

  it("never asks for permission while reading the state", async () => {
    const { support, notification } = harness({ permission: "default" });

    await expect(readPushStatus(support)).resolves.toBe("inactive");
    expect(notification.requestPermission).not.toHaveBeenCalled();
  });

  it.each([
    [{ configured: false }, "not-configured"],
    [{ permission: "denied" as NotificationPermission }, "denied"],
    [{ permission: "granted" as NotificationPermission }, "inactive"],
    [
      { permission: "granted" as NotificationPermission, existing: true },
      "active",
    ],
    [{ permission: "granted" as NotificationPermission, registered: false }, "inactive"],
  ])("maps %o to %s", async (options, expected) => {
    const { support } = harness(options);

    await expect(readPushStatus(support)).resolves.toBe(expected);
  });
});

describe("enablePush", () => {
  it("subscribes after an explicit permission grant and persists the credentials", async () => {
    const { support, notification, pushManager, serviceWorker, fetchMock } = harness();

    await expect(enablePush(support)).resolves.toBe("active");

    expect(notification.requestPermission).toHaveBeenCalledTimes(1);
    expect(serviceWorker.register).toHaveBeenCalledWith("/sw.js", { scope: "/" });
    const [options] = pushManager.subscribe.mock.calls[0];
    expect(options.userVisibleOnly).toBe(true);
    expect(
      Buffer.from(options.applicationServerKey as Uint8Array).toString("base64url"),
    ).toBe(VAPID_KEY);
    expect(posted(fetchMock)).toEqual({
      endpoint: "https://push.example.invalid/subscription/abc",
      keys: { p256dh: "BNp256dhKey", auth: "authSecret" },
    });
  });

  it("sends only the endpoint and the two keys, never the raw subscription", async () => {
    const { support, fetchMock } = harness();

    await enablePush(support);

    expect(Object.keys(posted(fetchMock))).toEqual(["endpoint", "keys"]);
    expect(Object.keys(posted(fetchMock).keys)).toEqual(["p256dh", "auth"]);
  });

  it("stops at a blocked permission without registering anything", async () => {
    const { support, serviceWorker, notification } = harness({ permission: "denied" });

    await expect(enablePush(support)).resolves.toBe("denied");
    expect(notification.requestPermission).not.toHaveBeenCalled();
    expect(serviceWorker.register).not.toHaveBeenCalled();
  });

  it("treats a dismissed prompt as denied", async () => {
    const { support, serviceWorker, notification } = harness();
    notification.requestPermission.mockResolvedValueOnce("default");

    await expect(enablePush(support)).resolves.toBe("denied");
    expect(serviceWorker.register).not.toHaveBeenCalled();
  });

  it("stops when the server has no VAPID key, before prompting", async () => {
    const { support, notification, serviceWorker } = harness({ configured: false });

    await expect(enablePush(support)).resolves.toBe("not-configured");
    expect(notification.requestPermission).not.toHaveBeenCalled();
    expect(serviceWorker.register).not.toHaveBeenCalled();
  });

  it("rolls the browser subscription back when the backend refuses it", async () => {
    const { support, subscription } = harness({ persistFails: true });

    await expect(enablePush(support)).resolves.toBe("error");
    expect(subscription.unsubscribe).toHaveBeenCalledTimes(1);
  });

  it("keeps a subscription it did not create when the backend refuses", async () => {
    const { support, subscription, pushManager } = harness({
      permission: "granted",
      existing: true,
      persistFails: true,
    });

    await expect(enablePush(support)).resolves.toBe("error");
    expect(pushManager.subscribe).not.toHaveBeenCalled();
    expect(subscription.unsubscribe).not.toHaveBeenCalled();
  });

  it("reports an error when the browser refuses to subscribe", async () => {
    const { support, subscription } = harness({ subscribeFails: true });

    await expect(enablePush(support)).resolves.toBe("error");
    expect(subscription.unsubscribe).not.toHaveBeenCalled();
  });

  it("reports an unsupported browser without touching anything", async () => {
    await expect(enablePush(null)).resolves.toBe("unsupported");
  });
});

describe("disablePush", () => {
  it("revokes on the server first and only then unsubscribes the browser", async () => {
    const order: string[] = [];
    const { support, subscription, fetchMock } = harness({
      permission: "granted",
      existing: true,
    });
    fetchMock.mockImplementation(async (input: string, init?: RequestInit) => {
      order.push(`${init?.method ?? "GET"} ${input}`);
      return new Response(JSON.stringify({ status: "REVOKED" }), { status: 200 });
    });
    subscription.unsubscribe.mockImplementation(async () => {
      order.push("unsubscribe");
      return true;
    });

    await expect(disablePush(support)).resolves.toBe("inactive");

    expect(order).toEqual(["DELETE /api/push/subscriptions", "unsubscribe"]);
  });

  it("keeps the browser subscription when the server revoke fails", async () => {
    const { support, subscription } = harness({
      permission: "granted",
      existing: true,
      revokeFails: true,
    });

    await expect(disablePush(support)).resolves.toBe("error");
    expect(subscription.unsubscribe).not.toHaveBeenCalled();
  });

  it("is a no-op when nothing is subscribed", async () => {
    const { support, fetchMock } = harness({ permission: "granted" });

    await expect(disablePush(support)).resolves.toBe("inactive");
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
