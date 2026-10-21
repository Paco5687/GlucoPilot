import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import MealResponse from "./MealResponse";

const apiMocks = vi.hoisted(() => ({ invoke: vi.fn() }));
vi.mock("@/api/base44Client", () => ({ base44: { functions: { invoke: apiMocks.invoke } } }));

const MEAL = { id: "m1", timestamp: "2026-09-10T18:00:00Z", food_name: "Rice bowl", start_glucose: 110, trend_30: 0, rise_start_minutes: 40, peak: 180, peak_minutes: 100, temp_basal: false, bolus: false, walking_minutes: 20, low_before: false, readable: true, reasons: [] };
const SET_ASIDE = { ...MEAL, id: "m2", food_name: "Toast", rise_start_minutes: null, peak: null, bolus: true, readable: false, reasons: ["bolus"] };

afterEach(() => { cleanup(); apiMocks.invoke.mockReset(); });

describe("MealResponse", () => {
  it("loads only when opened and shows the readable-meal medians", async () => {
    apiMocks.invoke.mockResolvedValue({ data: { meals_total: 7, meals_readable: 6, min_group: 5, not_readable_reasons: { bolus: 1 }, summary: { n: 6, n_with_rise: 6, rise_start_minutes: 40, peak_minutes: 100, rise: 70 }, meals: [MEAL, SET_ASIDE], semantics: {} } });
    render(<MealResponse />);
    expect(apiMocks.invoke).not.toHaveBeenCalled();
    fireEvent.click(screen.getByText("After meals"));
    await waitFor(() => expect(apiMocks.invoke).toHaveBeenCalledWith("food", { action: "meal_response" }));
    expect((await screen.findAllByText("40 min")).length).toBe(2);  // the median and the meal row
    expect(screen.getByText(/6 of 7 meals/)).toBeTruthy();
    expect(screen.getByText("walked 20m")).toBeTruthy();
    expect(screen.getByText("not readable: bolus")).toBeTruthy();
  });

  it("says when there are too few readable meals for a median", async () => {
    apiMocks.invoke.mockResolvedValue({ data: { meals_total: 3, meals_readable: 2, min_group: 5, not_readable_reasons: {}, summary: null, meals: [MEAL], semantics: {} } });
    render(<MealResponse />);
    fireEvent.click(screen.getByText("After meals"));
    expect(await screen.findByText(/median shows once 5 meals are readable/)).toBeTruthy();
  });
});
