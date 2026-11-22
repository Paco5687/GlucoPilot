import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import LabSuggestions from "./LabSuggestions";

vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

const hypothesis = { id: "hyp_synthetic", title: "Synthetic gland question", evidence: [] };

const storedEvidence = {
  role: "missing",
  source_kind: "missing",
  source_id: null,
  summary: "Synthetic earlier gap.",
  weight: 1,
  source_link: {},
};

function labItem(overrides = {}) {
  return {
    test: "Synthetic TSH",
    value: "0.1 mIU/L",
    date: "2026-06-01",
    flag: "low",
    reference: "0.4–4.5",
    earlier: [{ value: "0.9 mIU/L", date: "2026-01-10" }],
    draws: 2,
    if_true: "low",
    other_cause: "",
    why: "Synthetic reason.",
    suggested_role: "supporting",
    already_added: false,
    evidence: {
      source_kind: "entity",
      source_type: "LabResult",
      source_id: "lab_tsh",
      source_version: "v1",
      summary: "Synthetic TSH 0.1 mIU/L on 2026-06-01 (low).",
      weight: 1,
      source_link: { href: "/api/records/file/rec_synthetic" },
    },
    ...overrides,
  };
}

const suggestions = {
  method: "hypothesis-labs/1.2.0",
  lab_tests_used: true,
  usually_checked_by: "blood tests",
  labs: [
    labItem(),
    labItem({
      test: "Synthetic Free T4",
      flag: "",
      suggested_role: null,
      other_cause: "Synthetic Tablet",
      evidence: { ...labItem().evidence, source_id: "lab_ft4", summary: "Synthetic Free T4 1.0." },
    }),
    labItem({ test: "Synthetic Antibody", already_added: true, evidence: { ...labItem().evidence, source_id: "lab_ab" } }),
  ],
  not_on_file: [
    {
      test: "Synthetic Receptor Antibody",
      why: "Specific test.",
      already_added: false,
      evidence: { ...storedEvidence, summary: "No Synthetic Receptor Antibody result in the app's lab results." },
    },
  ],
  tests_on_file: 40,
};

function respond(body) {
  return { ok: true, json: async () => body };
}

describe("lab suggestions for a hypothesis", () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  it("shows record values with nothing ticked and saves only what the owner picks", async () => {
    const saved = { ...hypothesis, evidence: [storedEvidence] };
    const fetchMock = vi.fn(async (path, options = {}) => {
      if (path.endsWith("/lab-suggestions")) return respond(suggestions);
      if (options.method === "PUT") return respond(saved);
      return respond({ ...hypothesis, evidence: [storedEvidence] });
    });
    vi.stubGlobal("fetch", fetchMock);
    const onSaved = vi.fn();
    const onClose = vi.fn();

    render(<LabSuggestions hypothesis={hypothesis} onSaved={onSaved} onClose={onClose} />);

    expect(await screen.findByText("Synthetic TSH")).toBeTruthy();
    expect(screen.getAllByText("Earlier: 0.9 mIU/L on 2026-01-10")).toHaveLength(3);
    expect(screen.getByText(/Also affected by Synthetic Tablet/)).toBeTruthy();
    expect(screen.getByText("Already in evidence")).toBeTruthy();
    for (const box of screen.getAllByRole("checkbox")) expect(box.checked).toBe(false);
    expect(screen.getByRole("button", { name: "Add to evidence" }).hasAttribute("disabled")).toBe(true);

    fireEvent.click(screen.getByLabelText("Add Synthetic TSH"));
    fireEvent.click(screen.getByLabelText("Add Synthetic Free T4"));
    // A lab with no suggested role cannot be saved until the owner picks one.
    expect(screen.getByText(/Choose Supports or Against/)).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Role for Synthetic Free T4"), { target: { value: "opposing" } });
    fireEvent.click(screen.getByLabelText("Add Synthetic Receptor Antibody as still needed"));
    fireEvent.click(screen.getByRole("button", { name: "Add 3 to evidence" }));

    await waitFor(() => expect(onSaved).toHaveBeenCalledWith(saved));
    const put = fetchMock.mock.calls.find(([, options]) => options?.method === "PUT");
    expect(put[0]).toBe("/api/hypotheses/hyp_synthetic/evidence");
    const body = JSON.parse(put[1].body);
    // Built on the stored evidence, fetched just before saving.
    expect(body.evidence[0]).toEqual(storedEvidence);
    expect(body.evidence.slice(1).map((item) => [item.source_id, item.role])).toEqual([
      ["lab_tsh", "supporting"],
      ["lab_ft4", "opposing"],
      [null, "missing"],
    ]);
    expect(body.reason).toMatch(/hypothesis-labs\/1\.2\.0/);
    expect(onClose).toHaveBeenCalled();
  });

  it("says how a clinical diagnosis is made and can look for labs anyway", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(respond({ ...suggestions, lab_tests_used: false, usually_checked_by: "examination", labs: [], not_on_file: [] }))
      .mockResolvedValueOnce(respond(suggestions));
    vi.stubGlobal("fetch", fetchMock);

    render(<LabSuggestions hypothesis={hypothesis} onClose={vi.fn()} />);

    expect(await screen.findByText("Usually diagnosed by examination, not lab tests.")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Look for lab tests anyway" }));
    expect(await screen.findByText("Synthetic TSH")).toBeTruthy();
    expect(fetchMock.mock.calls[1][0]).toBe("/api/hypotheses/hyp_synthetic/lab-suggestions?anyway=true");
  });

  it("offers a retry when the lookup fails", async () => {
    vi.stubGlobal("fetch", vi.fn()
      .mockResolvedValueOnce({ ok: false, status: 502, json: async () => ({ detail: "Local LLM unreachable" }) })
      .mockResolvedValueOnce(respond(suggestions)));

    render(<LabSuggestions hypothesis={hypothesis} onClose={vi.fn()} />);

    expect(await screen.findByText("Local LLM unreachable")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("Synthetic TSH")).toBeTruthy();
  });
});
