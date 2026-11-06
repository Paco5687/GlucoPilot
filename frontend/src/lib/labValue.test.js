import { describe, expect, it } from "vitest";
import { censorSign, isNotAResult, valueText } from "./labValue";

describe("below/above-limit lab results", () => {
  it("reads the sign the lab printed", () => {
    expect(censorSign({ value: 55, original_value: "<55" })).toBe("<");
    expect(censorSign({ value: 0.2, original_value: "< 0.2" })).toBe("<");
    expect(censorSign({ value: 60, original_value: ">=60" })).toBe("≥");
    expect(censorSign({ value: 5, value_text: "<5" })).toBe("<");
  });

  it("leaves ordinary and qualitative results alone", () => {
    expect(censorSign({ value: 101, original_value: "101 H" })).toBe("");
    expect(censorSign({ value: 5, original_value: "1:5" })).toBe("");
    expect(censorSign({ value: 6.8 })).toBe("");
  });

  it("shows the result as reported", () => {
    expect(valueText({ value: 55, original_value: "<55" })).toBe("<55");
    expect(valueText({ value: 6.8, original_value: "6.8" })).toBe("6.8");
  });

  it("recognises a placeholder that was never a result", () => {
    expect(isNotAResult({ value: 0, validation_issues: [{ code: "not_a_result" }] })).toBe(true);
    expect(isNotAResult({ value: 0, validation_issues: [{ code: "duplicate_result" }] })).toBe(false);
    expect(isNotAResult({ value: 0 })).toBe(false);
  });
});
