import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
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

const SANDWICH = {
  ok: true,
  transcript: "chicken sandwich on gluten free bread",
  meal: {
    title: "Chicken sandwich on GF bread",
    confidence: 0.7,
    items: [
      { name: "Gluten-free bread", portion: "2 slices", preset_id: null, servings: null, carbs: 34, fiber: 4, sugars: 3, protein: 4, fat: 4, calories: 180 },
      { name: "Chicken breast", portion: "3 oz", preset_id: null, servings: null, carbs: 0, fiber: 0, sugars: 0, protein: 26, fat: 3, calories: 130 },
    ],
    totals: { carbs: 34 },
  },
  timestamp: null,
};

describe("FoodLogger voice", () => {
  beforeEach(() => {
    const base = apiMocks.invoke.getMockImplementation();
    apiMocks.invoke.mockImplementation(async (name, body) => {
      if (body.action === "estimate_meal") return { data: SANDWICH };
      if (body.action === "log_meal") return { data: { ok: true, treatment: { id: "t3" } } };
      return base(name, body);
    });
  });
  afterEach(() => { delete window.MediaRecorder; delete navigator.mediaDevices; });

  it("falls back to typing when the browser can't record, then estimates and logs one meal", async () => {
    render(<FoodLogger />);
    await screen.findByText("Oat bar");
    fireEvent.click(screen.getByText("Say it"));
    expect(await screen.findByText("What did you eat?")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("what you ate"), { target: { value: "chicken sandwich on gluten free bread" } });
    fireEvent.click(screen.getByText("Estimate"));
    await waitFor(() => expect(apiMocks.invoke).toHaveBeenCalledWith("food", expect.objectContaining({ action: "estimate_meal", text: "chicken sandwich on gluten free bread" })));
    expect(await screen.findByDisplayValue("Gluten-free bread")).toBeTruthy();
    expect(screen.getByText("34 g carbs")).toBeTruthy();

    fireEvent.change(screen.getByLabelText("Carbs g for Gluten-free bread"), { target: { value: "40" } });
    expect(screen.getByText("40 g carbs")).toBeTruthy();
    fireEvent.click(screen.getByText("Log it"));
    await waitFor(() => expect(apiMocks.invoke).toHaveBeenCalledWith("food", expect.objectContaining({
      action: "log_meal",
      name: "Chicken sandwich on GF bread",
      entry_method: "text",
      save_preset: false,
      items: [
        expect.objectContaining({ name: "Gluten-free bread", carbs: 40, protein: 4 }),
        expect.objectContaining({ name: "Chicken breast", carbs: 0, protein: 26 }),
      ],
    })));
  });

  it("removing a food updates the totals", async () => {
    render(<FoodLogger />);
    await screen.findByText("Oat bar");
    fireEvent.click(screen.getByText("Say it"));
    fireEvent.change(await screen.findByLabelText("what you ate"), { target: { value: "sandwich" } });
    fireEvent.click(screen.getByText("Estimate"));
    await screen.findByDisplayValue("Chicken breast");
    fireEvent.click(screen.getByLabelText("remove Gluten-free bread"));
    expect(screen.getByText("0 g carbs")).toBeTruthy();
    expect(screen.queryByDisplayValue("Gluten-free bread")).toBeNull();
  });

  it("won't log foods estimated from words that were changed afterwards", async () => {
    const SALAD = { ...SANDWICH, transcript: "a garden salad", meal: { title: "Garden salad", confidence: 0.6, items: [{ name: "Garden salad", portion: "1 bowl", preset_id: null, servings: null, carbs: 12, fiber: 4, sugars: 5, protein: 3, fat: 1, calories: 70 }], totals: { carbs: 12 } } };
    render(<FoodLogger />);
    await screen.findByText("Oat bar");
    fireEvent.click(screen.getByText("Say it"));
    fireEvent.change(await screen.findByLabelText("what you ate"), { target: { value: "chicken sandwich on gluten free bread" } });
    fireEvent.click(screen.getByText("Estimate"));
    await screen.findByDisplayValue("Gluten-free bread");
    expect(screen.queryByRole("alert")).toBeNull();

    fireEvent.change(screen.getByLabelText("what you ate"), { target: { value: "a garden salad" } });
    expect(screen.getByRole("alert").textContent).toMatch(/changed what you ate/);
    expect(screen.getByText("Log it").closest("button").disabled).toBe(true);
    fireEvent.click(screen.getByText("Log it"));
    expect(apiMocks.invoke).not.toHaveBeenCalledWith("food", expect.objectContaining({ action: "log_meal" }));

    // Re-estimating from the new words clears the warning and replaces the foods.
    apiMocks.invoke.mockImplementationOnce(async () => ({ data: SALAD }));
    fireEvent.click(within(screen.getByRole("alert")).getByText("Re-estimate"));
    await waitFor(() => expect(apiMocks.invoke).toHaveBeenCalledWith("food", expect.objectContaining({ action: "estimate_meal", text: "a garden salad" })));
    await waitFor(() => expect(screen.getByLabelText("food 1 name").value).toBe("Garden salad"));
    expect(screen.queryByDisplayValue("Gluten-free bread")).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
    fireEvent.click(screen.getByText("Log it"));
    await waitFor(() => expect(apiMocks.invoke).toHaveBeenCalledWith("food", expect.objectContaining({
      action: "log_meal", transcript: "a garden salad", items: [expect.objectContaining({ name: "Garden salad", carbs: 12 })],
    })));
  });

  it("logs the listed foods after an edit only when asked to explicitly", async () => {
    render(<FoodLogger />);
    await screen.findByText("Oat bar");
    fireEvent.click(screen.getByText("Say it"));
    fireEvent.change(await screen.findByLabelText("what you ate"), { target: { value: "chicken sandwich on gluten free bread" } });
    fireEvent.click(screen.getByText("Estimate"));
    await screen.findByDisplayValue("Gluten-free bread");
    fireEvent.change(screen.getByLabelText("what you ate"), { target: { value: "chicken sandwich on gluten-free bread, toasted" } });
    fireEvent.click(screen.getByText("Log foods as listed"));
    await waitFor(() => expect(apiMocks.invoke).toHaveBeenCalledWith("food", expect.objectContaining({
      action: "log_meal", transcript: "chicken sandwich on gluten-free bread, toasted", items: [expect.objectContaining({ name: "Gluten-free bread" }), expect.objectContaining({ name: "Chicken breast" })],
    })));
  });

  it("records from the microphone and sends the clip for an estimate", async () => {
    const stopTrack = vi.fn();
    navigator.mediaDevices = { getUserMedia: vi.fn(async () => ({ getTracks: () => [{ stop: stopTrack }] })) };
    window.MediaRecorder = class {
      static isTypeSupported(t) { return t === "audio/webm;codecs=opus"; }
      constructor(_stream, opts) { this.mimeType = opts.mimeType; this.state = "inactive"; }
      start() { this.state = "recording"; }
      stop() {
        this.state = "inactive";
        this.ondataavailable({ data: new Blob(["fake-audio"], { type: this.mimeType }) });
        this.onstop();
      }
    };
    render(<FoodLogger />);
    await screen.findByText("Oat bar");
    fireEvent.click(screen.getByText("Say it"));
    expect(await screen.findByText(/Listening/)).toBeTruthy();
    fireEvent.click(screen.getByText("Done"));
    await waitFor(() => expect(apiMocks.invoke).toHaveBeenCalledWith("food", expect.objectContaining({ action: "estimate_meal", audio: `audio/webm|${btoa("fake-audio")}` })));
    expect(await screen.findByDisplayValue("Gluten-free bread")).toBeTruthy();
    expect(screen.getByDisplayValue("chicken sandwich on gluten free bread")).toBeTruthy();
    expect(stopTrack).toHaveBeenCalled();
  });

  it("shows a logged meal in Recent without a servings count", async () => {
    apiMocks.invoke.mockImplementation(async () => ({ data: { presets: [], recent: [{ id: "t9", timestamp: "2026-10-16T16:00:00Z", food_name: "Snack", servings: 1, amount: 48, fiber_g: 3.1, items: [{ name: "Banana" }] }] } }));
    render(<FoodLogger />);
    expect(await screen.findByText(/Snack ·/)).toBeTruthy();
    expect(screen.queryByText(/× 1/)).toBeNull();
    expect(screen.getByText(/3.1 g fiber/)).toBeTruthy();
  });
});
