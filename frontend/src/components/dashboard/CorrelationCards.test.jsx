import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import CorrelationCards from "./CorrelationCards";


afterEach(cleanup);

describe("CorrelationCards confidence labels", () => {
  it("renders a large seven-day effect as exploratory", () => {
    const readings = [];
    const ouraData = [];
    // Recent local days (yesterday backwards): the cards ignore days outside
    // their own 60-day window and exclude today while it accumulates.
    // Timestamps are built in LOCAL time at mid-day hours so each reading
    // buckets onto its intended day in any test timezone.
    for (let dayIndex = 0; dayIndex < 7; dayIndex += 1) {
      const base = new Date();
      base.setDate(base.getDate() - (dayIndex + 1));
      const day = `${base.getFullYear()}-${String(base.getMonth() + 1).padStart(2, "0")}-${String(base.getDate()).padStart(2, "0")}`;
      ouraData.push({ date: day, sleep_score: 50 + dayIndex });
      for (let readingIndex = 0; readingIndex < 30; readingIndex += 1) {
        const at = new Date(base);
        at.setHours(8 + Math.floor(readingIndex / 4), (readingIndex % 4) * 15, 0, 0);
        readings.push({
          timestamp: at.toISOString(),
          // Higher sleep score days get more in-range readings: a clean,
          // large, positive correlation across only 7 pairs.
          value: readingIndex < 12 + dayIndex * 3 ? 110 : 220,
        });
      }
    }

    render(<CorrelationCards readings={readings} ouraData={ouraData} />);

    expect(screen.getByText("exploratory · large effect")).toBeTruthy();
    expect(document.body.textContent).toContain("7 days analyzed");
  });

  it("ignores sparse day fragments instead of treating them as days", () => {
    const readings = [];
    const ouraData = [];
    for (let dayIndex = 0; dayIndex < 7; dayIndex += 1) {
      const base = new Date();
      base.setDate(base.getDate() - (dayIndex + 1));
      const day = `${base.getFullYear()}-${String(base.getMonth() + 1).padStart(2, "0")}-${String(base.getDate()).padStart(2, "0")}`;
      ouraData.push({ date: day, sleep_score: 50 + dayIndex });
      // 10 readings — a 50-minute fragment, not a day.
      for (let readingIndex = 0; readingIndex < 10; readingIndex += 1) {
        const at = new Date(base);
        at.setHours(9, readingIndex * 5, 0, 0);
        readings.push({ timestamp: at.toISOString(), value: 120 });
      }
    }

    const { container } = render(<CorrelationCards readings={readings} ouraData={ouraData} />);
    expect(container.textContent).not.toContain("days analyzed");
  });
});
