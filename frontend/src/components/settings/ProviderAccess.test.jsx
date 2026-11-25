import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import ProviderAccess, { describeDevice, providerSummary } from "./ProviderAccess";

const IPHONE_SAFARI =
  "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1";
const WINDOWS_EDGE =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36 Edg/131.0";

const responses = {
  "/api/provider/config": { providers: [{ username: "drlee" }, { username: "drpatel" }], max: 4 },
  "/api/provider/invites": { invites: [] },
  "/api/provider/activity": {
    recording_since: "2026-11-22T14:00:00.000Z",
    providers: [
      { username: "drlee", last_login: "2026-11-25T15:30:00Z", logins_30d: 3, logins_total: 3 },
      { username: "drpatel", last_login: null, logins_30d: 0, logins_total: 0 },
    ],
    events: [
      { occurred_at: "2026-11-25T15:30:00Z", event: "login", role: "provider", username: "drlee", client_ip: "81.2.69.160", user_agent: IPHONE_SAFARI },
      { occurred_at: "2026-11-24T09:00:00Z", event: "login_failed", role: null, username: null, client_ip: "198.51.100.9", user_agent: WINDOWS_EDGE },
    ],
  },
};

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("describeDevice", () => {
  it("names the browser and platform", () => {
    expect(describeDevice(IPHONE_SAFARI)).toBe("Safari on iPhone");
    expect(describeDevice(WINDOWS_EDGE)).toBe("Edge on Windows");
    expect(describeDevice("")).toBe("");
  });
});

describe("providerSummary", () => {
  it("says when nothing has been recorded instead of implying the provider never signed in", () => {
    expect(providerSummary({ last_login: null, logins_30d: 0 }, "2026-11-22T14:00:00Z")).toMatch(/^No sign-ins since /);
    expect(providerSummary(undefined, null)).toBe("No sign-ins recorded");
  });

  it("gives the last sign-in and the 30-day count", () => {
    expect(providerSummary({ last_login: "2026-11-25T15:30:00Z", logins_30d: 1 }, null)).toMatch(/1 sign-in in 30 days$/);
    expect(providerSummary({ last_login: "2026-11-25T15:30:00Z", logins_30d: 3 }, null)).toMatch(/3 sign-ins in 30 days$/);
  });
});

describe("ProviderAccess", () => {
  it("shows each provider's sign-in history and the recent activity list", async () => {
    vi.stubGlobal("fetch", vi.fn(async (path) => ({
      ok: true,
      json: async () => responses[path],
    })));

    render(<ProviderAccess />);

    expect(await screen.findByText(/3 sign-ins in 30 days/)).toBeTruthy();
    expect(screen.getByText(/^No sign-ins since /)).toBeTruthy();
    expect(screen.getByText("Failed sign-in")).toBeTruthy();
    expect(screen.getByText("Unknown username")).toBeTruthy();
    expect(screen.getByText(/Safari on iPhone · 81\.2\.69\.160/)).toBeTruthy();
  });

  it("keeps the provider controls when the activity request fails", async () => {
    vi.stubGlobal("fetch", vi.fn(async (path) => (
      path === "/api/provider/activity"
        ? { ok: false, status: 500, json: async () => ({ detail: "boom" }) }
        : { ok: true, json: async () => responses[path] }
    )));

    render(<ProviderAccess />);

    expect(await screen.findByText("drlee")).toBeTruthy();
    expect(screen.queryByText(/Sign-in activity/)).toBeNull();
  });
});
