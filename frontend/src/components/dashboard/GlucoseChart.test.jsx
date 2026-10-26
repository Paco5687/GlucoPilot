import { cloneElement } from "react";
import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import GlucoseChart from "./GlucoseChart";

// jsdom has no layout, so ResponsiveContainer would measure 0×0 and draw
// nothing. Hand the chart a fixed size instead.
vi.mock("recharts", async (importOriginal) => {
  const actual = await importOriginal();
  return {
    ...actual,
    ResponsiveContainer: ({ children }) => cloneElement(children, { width: 800, height: 300 }),
  };
});

// Plot area: 800 wide, minus the 35px y-axis on the left and the 10px right margin.
const PLOT_LEFT = 35;
const PLOT_RIGHT = 790;

const START = Date.parse("2026-10-25T22:58:31Z");

function readingsOverThreeHours() {
  return Array.from({ length: 36 }, (_, i) => ({
    id: `r${i}`,
    timestamp: new Date(START + i * 5 * 60000).toISOString(),
    value: 200 - i * 3,
  }));
}

function carb(timestamp) {
  return { id: "c1", type: "carb", amount: 15, timestamp };
}

function traceXs(container) {
  const d = container.querySelector(".recharts-area-curve")?.getAttribute("d") || "";
  // Monotone curves are M/C commands; every x,y pair lies within the readings' span.
  return [...d.matchAll(/(-?[\d.]+),(-?[\d.]+)/g)].map((m) => Number(m[1]));
}

function markerXs(container) {
  return [...container.querySelectorAll(".recharts-scatter-symbol path")].map((p) =>
    Number(/translate\((-?[\d.]+)/.exec(p.getAttribute("transform"))[1]),
  );
}

afterEach(cleanup);

describe("GlucoseChart x-axis", () => {
  it("spans the readings when one carb marker is in the window", () => {
    const { container } = render(
      <GlucoseChart readings={readingsOverThreeHours()} treatments={[carb("2026-10-25T23:00:00Z")]} />,
    );
    const xs = traceXs(container);
    expect(Math.min(...xs)).toBeCloseTo(PLOT_LEFT, 0);
    expect(Math.max(...xs)).toBeCloseTo(PLOT_RIGHT, 0);
  });

  it("widens to keep a marker that falls before the first reading on the plot", () => {
    const { container } = render(
      <GlucoseChart readings={readingsOverThreeHours()} treatments={[carb("2026-10-25T22:50:00Z")]} />,
    );
    const [marker] = markerXs(container);
    const xs = traceXs(container);
    expect(marker).toBeCloseTo(PLOT_LEFT, 0);
    expect(Math.min(...xs)).toBeGreaterThan(marker);
    expect(Math.max(...xs)).toBeCloseTo(PLOT_RIGHT, 0);
  });
});
