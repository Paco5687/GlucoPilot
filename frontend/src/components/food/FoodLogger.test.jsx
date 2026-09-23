import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import FoodLogger from "./FoodLogger";

const apiMocks = vi.hoisted(() => ({ invoke: vi.fn() }));
vi.mock("@/api/base44Client", () => ({ base44: { functions: { invoke: apiMocks.invoke } } }));
vi.mock("./BarcodeScanner", () => ({ default: () => <div>scanner open</div> }));
vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

const PRESET = { id: "p1", name: "Oat bar", carbs: 27, serving_label: "1 bar", default_servings: 1, use_count: 3 };

beforeEach(() => {
  apiMocks.invoke.mockImplementation(async (_name, body) => {
    if (body.action === "presets") return { data: { presets: [PRESET], recent: [{ id: "t1", timestamp: "2026-09-23T12:00:00Z", food_name: "Oat bar", servings: 1, amount: 27, protein_g: 4 }] } };
    if (body.action === "log") return { data: { ok: true, treatment: { id: "t2" } } };
    return { data: {} };
  });
});
afterEach(() => { cleanup(); apiMocks.invoke.mockReset(); });

describe("FoodLogger", () => {
  it("lists presets and logs one with a single tap", async () => {
    render(<FoodLogger />);
    expect(await screen.findByText("Oat bar")).toBeTruthy();
    fireEvent.click(screen.getByLabelText("log Oat bar"));
    await waitFor(() => expect(apiMocks.invoke).toHaveBeenCalledWith("food", { action: "log", preset_id: "p1", servings: 1 }));
  });

  it("adjusts servings before logging a preset", async () => {
    render(<FoodLogger />);
    await screen.findByText("Oat bar");
    fireEvent.click(screen.getAllByLabelText("more servings")[0]);
    fireEvent.click(screen.getByLabelText("log Oat bar"));
    await waitFor(() => expect(apiMocks.invoke).toHaveBeenCalledWith("food", { action: "log", preset_id: "p1", servings: 1.5 }));
  });

  it("opens the scanner on demand", async () => {
    render(<FoodLogger />);
    await screen.findByText("Oat bar");
    fireEvent.click(screen.getByText("Scan barcode"));
    expect(screen.getByText("scanner open")).toBeTruthy();
  });

  it("shows recent logs with protein detail", async () => {
    render(<FoodLogger />);
    expect(await screen.findByText(/4 g protein/)).toBeTruthy();
  });
});
