import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import HypothesesCard from "./HypothesesCard";

vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

const base = {
  origin_kind: "patient",
  origin_label: "owner",
  description: "",
  suggested_verification: "",
  evidence_by_role: { supporting: [], opposing: [], missing: [] },
};

const proposed = {
  ...base,
  id: "hyp_open",
  title: "Synthetic adrenal question",
  description: "Synthetic fatigue pattern.",
  status: "proposed",
  suggested_verification: "Synthetic morning cortisol.",
  evidence_by_role: {
    supporting: [{ role: "supporting", summary: "s" }],
    opposing: [],
    missing: [{ role: "missing", summary: "m" }, { role: "missing", summary: "m2" }],
  },
};
const reviewing = { ...base, id: "hyp_review", title: "Synthetic thyroid question", status: "under_review" };
const confirmed = { ...base, id: "hyp_done", title: "Synthetic confirmed question", status: "confirmed" };

function listResponse(hypotheses, canEdit) {
  return { ok: true, json: async () => ({ hypotheses, can_edit: canEdit }) };
}

function renderCard() {
  return render(<MemoryRouter><HypothesesCard /></MemoryRouter>);
}

describe("dashboard hypotheses card", () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  it("lists open hypotheses with what would confirm them and leaves decided ones to Settings", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(listResponse([reviewing, proposed, confirmed], true)));

    renderCard();

    expect(await screen.findByText("Synthetic adrenal question")).toBeTruthy();
    expect(screen.getByText("Synthetic thyroid question")).toBeTruthy();
    expect(screen.queryByText("Synthetic confirmed question")).toBeNull();
    expect(screen.getByText("Synthetic fatigue pattern.")).toBeTruthy();
    expect(screen.getByText("Synthetic morning cortisol.")).toBeTruthy();
    expect(screen.getByText("1 supporting · 0 against · 2 still needed")).toBeTruthy();
    expect(screen.getByText("No evidence recorded yet.")).toBeTruthy();
    expect(screen.getByText("Needs workup")).toBeTruthy();
    expect(screen.getByText("Under review")).toBeTruthy();
    expect(screen.getByText(/1 decided by a clinician/)).toBeTruthy();
    expect(screen.getByRole("link", { name: /Health hypotheses/ }).getAttribute("href")).toBe("/settings#hypotheses");
    // The add form stays folded away while there are open entries.
    expect(screen.queryByLabelText("What might be wrong?")).toBeNull();
  });

  it("records a new patient hypothesis through the ledger API", async () => {
    const created = { ...base, id: "hyp_new", title: "Synthetic new question", status: "proposed" };
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(listResponse([reviewing], true))
      .mockResolvedValueOnce({ ok: true, json: async () => created });
    vi.stubGlobal("fetch", fetchMock);

    renderCard();
    fireEvent.click(await screen.findByRole("button", { name: /^Add$/ }));
    fireEvent.change(screen.getByLabelText("What might be wrong?"), { target: { value: "  Synthetic new question " } });
    fireEvent.change(screen.getByLabelText("Why do you think so? (optional)"), { target: { value: "Synthetic reason" } });
    fireEvent.change(screen.getByLabelText("What would confirm or rule it out? (optional)"), { target: { value: "Synthetic test" } });
    fireEvent.click(screen.getByRole("button", { name: /Add hypothesis/ }));

    expect(await screen.findByText("Synthetic new question")).toBeTruthy();
    const [path, options] = fetchMock.mock.calls[1];
    expect(path).toBe("/api/hypotheses");
    expect(options.method).toBe("POST");
    expect(JSON.parse(options.body)).toEqual({
      title: "Synthetic new question",
      description: "Synthetic reason",
      origin_kind: "patient",
      suggested_verification: "Synthetic test",
    });
    await waitFor(() => expect(screen.queryByLabelText("What might be wrong?")).toBeNull());
  });

  it("opens the add form when nothing is recorded yet", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(listResponse([], true)));

    renderCard();

    expect(await screen.findByLabelText("What might be wrong?")).toBeTruthy();
    expect(screen.getByRole("button", { name: /Add hypothesis/ }).hasAttribute("disabled")).toBe(true);
    expect(screen.queryByRole("button", { name: "Cancel" })).toBeNull();
  });

  it("shows provider sessions the list without edit controls", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(listResponse([proposed], false)));

    renderCard();

    expect(await screen.findByText("Synthetic adrenal question")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Add/ })).toBeNull();
    expect(screen.queryByLabelText("What might be wrong?")).toBeNull();
    expect(screen.queryByRole("link", { name: /Health hypotheses/ })).toBeNull();
  });

  it("renders nothing for provider sessions with no open hypotheses", async () => {
    const fetchMock = vi.fn().mockResolvedValue(listResponse([confirmed], false));
    vi.stubGlobal("fetch", fetchMock);

    const view = renderCard();

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    await waitFor(() => expect(view.container.innerHTML).toBe(""));
  });
});
