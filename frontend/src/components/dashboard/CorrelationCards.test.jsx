import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import CorrelationCards from "./CorrelationCards";


afterEach(cleanup);

describe("CorrelationCards confidence labels", () => {
  it("renders a large seven-day effect as exploratory", () => {
    const readings = [];
    const ouraData = [];
    for (let dayIndex = 0; dayIndex < 7; dayIndex += 1) {
      const day = `2026-01-${String(dayIndex + 1).padStart(2, "0")}`;
      ouraData.push({ date: day, sleep_score: 50 + dayIndex });
      for (let readingIndex = 0; readingIndex < 10; readingIndex += 1) {
        readings.push({
          timestamp: `${day}T${String(readingIndex).padStart(2, "0")}:00:00Z`,
          value: readingIndex < 4 + dayIndex ? 110 : 220,
        });
      }
    }

    render(<CorrelationCards readings={readings} ouraData={ouraData} />);

    expect(screen.getByText("exploratory · large effect")).toBeTruthy();
    expect(document.body.textContent).toContain("7 days analyzed");
  });
});
