import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Landing from "./Landing";

const auth = vi.hoisted(() => ({
  isAuthenticated: vi.fn(),
  redirectToLogin: vi.fn(),
}));

vi.mock("@/api/base44Client", () => ({ base44: { auth } }));

const renderLanding = () => render(<MemoryRouter><Landing /></MemoryRouter>);

describe("Landing", () => {
  beforeEach(() => {
    auth.isAuthenticated.mockResolvedValue(false);
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("leads with the GitHub repository and install steps", async () => {
    renderLanding();

    const cta = await screen.findByRole("link", { name: /view on github/i });
    expect(cta.getAttribute("href")).toBe("https://github.com/Paco5687/GlucoPilot");
    expect(screen.getByText(/no hosted service and no sign-up/i)).toBeTruthy();
    expect(screen.getByText(/\.\/install\.sh/)).toBeTruthy();
  });

  it("keeps sign-in as a single small link", async () => {
    renderLanding();

    const signIn = await screen.findByRole("button", { name: "Sign in" });
    expect(screen.queryByRole("button", { name: /get started/i })).toBeNull();
    fireEvent.click(signIn);
    expect(auth.redirectToLogin).toHaveBeenCalledWith("/dashboard");
  });
});
