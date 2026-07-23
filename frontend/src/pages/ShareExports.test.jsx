import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import ShareExports from "./ShareExports";

const authState = vi.hoisted(() => ({ isAdmin: true }));
vi.mock("@/lib/AuthContext", () => ({
  useAuth: () => authState,
}));

const preview = {
  request: {
    mode: "full_private",
    days: 90,
    generated_at: "2026-07-23T20:00:00Z",
    preview_checksum: `sha256:${"a".repeat(64)}`,
  },
  export: {
    policy: {
      version: "share-export-policy/1.0.0",
      mode: "full_private",
      field_policy: "explicit_allowlist",
      watermark: "PRIVATE — intended recipient only",
      generated_at: "2026-07-23T20:00:00Z",
      expires_at: "2026-07-30T20:00:00Z",
      invariant_exclusions: ["token", "email", "internal_id"],
    },
    content: { entities: { Diagnosis: [{ name: "Synthetic condition" }] } },
    checksum: `sha256:${"a".repeat(64)}`,
  },
};

describe("ShareExports", () => {
  beforeEach(() => {
    authState.isAdmin = true;
    vi.stubGlobal("fetch", vi.fn()
      .mockResolvedValueOnce({
        ok: true,
        json: async () => preview,
      })
      .mockResolvedValueOnce({
        ok: true,
        blob: async () => new Blob([JSON.stringify(preview.export)]),
      }));
    vi.stubGlobal("URL", {
      createObjectURL: vi.fn(() => "blob:synthetic-export"),
      revokeObjectURL: vi.fn(),
    });
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("downloads the exact server-bound request returned by the preview", async () => {
    render(<ShareExports />);

    expect(screen.getByText(/explicit allowlist/i)).toBeTruthy();
    expect(screen.getByText(/tokens, secret URLs, and internal IDs/i)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Generate privacy preview" }));

    expect(await screen.findByText("PRIVATE — intended recipient only")).toBeTruthy();
    expect(screen.getByLabelText("Exact export JSON preview").textContent).toContain(
      "Synthetic condition",
    );
    const previewCall = vi.mocked(fetch).mock.calls[0];
    expect(JSON.parse(String(previewCall[1].body))).toEqual({
      mode: "full_private",
      days: 90,
    });

    fireEvent.click(screen.getByRole("button", { name: "Download exact preview" }));
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
    const downloadCall = vi.mocked(fetch).mock.calls[1];
    expect(String(downloadCall[0])).toBe("/api/share-exports/download");
    expect(JSON.parse(String(downloadCall[1].body))).toEqual(preview.request);
    expect(URL.createObjectURL).toHaveBeenCalledTimes(1);
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:synthetic-export");
  });

  it("invalidates the preview when a meaningful export input changes", async () => {
    render(<ShareExports />);
    fireEvent.click(screen.getByRole("button", { name: "Generate privacy preview" }));
    await screen.findByText("Exact export preview");

    fireEvent.change(screen.getByLabelText("Export date range"), {
      target: { value: "30" },
    });

    expect(screen.queryByText("Exact export preview")).toBeNull();
    const download = /** @type {HTMLButtonElement} */ (
      screen.getByRole("button", { name: "Download exact preview" })
    );
    expect(download.disabled).toBe(true);
  });

  it("limits provider sessions to clinician and emergency modes", () => {
    authState.isAdmin = false;
    render(<ShareExports />);

    const select = /** @type {HTMLSelectElement} */ (
      screen.getByLabelText("Export mode")
    );
    const labels = Array.from(select.options).map(
      (option) => option.textContent,
    );
    expect(labels).toEqual(["Clinician copy", "Emergency summary"]);
    expect(select.value).toBe("clinician");
  });
});
