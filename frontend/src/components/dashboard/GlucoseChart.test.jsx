import { cloneElement } from "react";
import { cleanup, fireEvent, render } from "@testing-library/react";
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
  return [...container.querySelectorAll(".recharts-reference-dot-dot")].map((c) => Number(c.getAttribute("cx")));
}

// x of reading i (36 readings across the plot).
function xOf(i) {
  return PLOT_LEFT + (i / 35) * (PLOT_RIGHT - PLOT_LEFT);
}

// Recharts throttles mouse moves, so each hover gets a fresh chart.
function tooltipAt(i, props) {
  const { container, unmount } = render(<GlucoseChart readings={readingsOverThreeHours()} {...props} />);
  const x = xOf(i);
  fireEvent.mouseMove(container.querySelector(".recharts-wrapper"), { clientX: x, clientY: 150, pageX: x, pageY: 150 });
  const text = container.querySelector(".recharts-tooltip-wrapper")?.textContent || "";
  unmount();
  return text;
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

describe("GlucoseChart tooltip", () => {
  it("shows the reading under the pointer while a marker is in the window", () => {
    const treatments = [carb("2026-10-25T23:00:00Z")];
    expect(tooltipAt(3, { treatments })).toContain("191 mg/dL");
    expect(tooltipAt(17, { treatments })).toContain("149 mg/dL");
    expect(tooltipAt(31, { treatments })).toContain("107 mg/dL");
  });

  it("lists carbs and insulin under the nearest reading only", () => {
    // Reading 18 is at 00:28:31.
    const treatments = [
      carb("2026-10-26T00:30:00Z"),
      { id: "i1", type: "insulin", amount: 1.5, timestamp: "2026-10-26T00:27:00Z" },
    ];
    const at18 = tooltipAt(18, { treatments });
    expect(at18).toContain("146 mg/dL");
    expect(at18).toContain("15g carbs");
    expect(at18).toContain("1.50 U");
    expect(tooltipAt(17, { treatments })).not.toContain("carbs");
  });

  it("lists a fingerstick under the nearest reading and draws its marker", () => {
    // Reading 9 is at 23:43:31.
    const fingersticks = [{ timestamp: "2026-10-25T23:44:00Z", value: 71, cgm_value: 173, delta: 102 }];
    const at9 = tooltipAt(9, { fingersticks });
    expect(at9).toContain("173 mg/dL");
    expect(at9).toContain("71 mg/dL fingerstick");
    expect(at9).toContain("CGM read 173");

    const { container } = render(<GlucoseChart readings={readingsOverThreeHours()} fingersticks={fingersticks} />);
    expect(container.querySelectorAll(".recharts-reference-dot .recharts-symbols")).toHaveLength(1);
  });
});
