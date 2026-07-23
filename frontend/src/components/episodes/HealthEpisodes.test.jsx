import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import HealthEpisodes from "./HealthEpisodes";

const episode = {
  id: "episode_synthetic",
  episode_type: "symptom_flare",
  title: "Synthetic fatigue flare",
  description: "Test-only episode.",
  origin_kind: "manual",
  status: "proposed",
  start_time: "2026-07-20",
  end_time: "2026-07-22",
  confidence: { confidence_label: "not_assessed" },
  members: [{
    entity_type: "SymptomLog",
    entity_id: "symptom_synthetic",
    summary: "Synthetic fatigue",
    relationship_kind: "within_episode",
  }],
};

function response(data) {
  return Promise.resolve({ ok: true, json: async () => data });
}

describe("health episode ledger", () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  it("shows time ranges, temporal-only semantics, and guarded decisions", async () => {
    const fetchMock = vi.fn((path) => {
      if (String(path).includes("/decision")) {
        return response({ ...episode, status: "confirmed" });
      }
      return response({ episodes: [episode], can_edit: true });
    });
    vi.stubGlobal("fetch", fetchMock);
    vi.spyOn(window, "prompt").mockReturnValue("Owner verified the interval.");

    render(<HealthEpisodes />);

    expect(await screen.findByText("Synthetic fatigue flare")).toBeTruthy();
    expect(screen.getByText(/temporal context only/i)).toBeTruthy();
    expect(screen.getByText(/2026-07-20 → 2026-07-22/)).toBeTruthy();
    expect(screen.getByText(/Synthetic fatigue · within episode/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Confirm" }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/episodes/episode_synthetic/decision",
        expect.objectContaining({ method: "POST" }),
      );
    });
    const decision = JSON.parse(fetchMock.mock.calls.at(-1)[1].body);
    expect(decision).toEqual({
      status: "confirmed",
      reason: "Owner verified the interval.",
    });
  });

  it("discovers and links multi-source temporal members without a causal field", async () => {
    const candidate = {
      entity_type: "SymptomLog",
      entity_id: "symptom_candidate",
      role: "symptom",
      relationship_kind: "within_episode",
      observed_start: "2026-07-20",
      observed_end: "2026-07-20",
      source_version: "v1",
      summary: "Synthetic headache",
      causation_asserted: 0,
    };
    const fetchMock = vi.fn((path, options = {}) => {
      if (String(path).includes("/candidates")) return response({ candidates: [candidate] });
      if (options.method === "POST") {
        return response({ ...episode, id: "episode_created", title: "Created episode" });
      }
      return response({ episodes: [], can_edit: true });
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<HealthEpisodes />);
    await screen.findByText("No health episodes recorded yet.");
    fireEvent.change(screen.getByLabelText("Episode title"), {
      target: { value: "Created episode" },
    });
    fireEvent.click(screen.getByTitle("Find temporal context"));
    expect(await screen.findByText(/Synthetic headache/)).toBeTruthy();
    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.click(screen.getByRole("button", { name: "Record proposed episode" }));

    await waitFor(() => {
      expect(fetchMock.mock.calls.some(([path, options]) => (
        path === "/api/episodes" && options?.method === "POST"
      ))).toBe(true);
    });
    const createCall = fetchMock.mock.calls.find(([path, options]) => (
      path === "/api/episodes" && options?.method === "POST"
    ));
    const body = JSON.parse(createCall[1].body);
    expect(body.members).toHaveLength(1);
    expect(body.members[0].entity_type).toBe("SymptomLog");
    expect(body.origin_kind).toBe("manual");
  });

  it("keeps provider sessions read-only", async () => {
    vi.stubGlobal("fetch", vi.fn(() => response({ episodes: [episode], can_edit: false })));
    render(<HealthEpisodes />);

    expect(await screen.findByText("Synthetic fatigue flare")).toBeTruthy();
    expect(screen.queryByLabelText("Episode title")).toBeNull();
    expect(screen.queryByRole("button", { name: "Confirm" })).toBeNull();
  });
});
