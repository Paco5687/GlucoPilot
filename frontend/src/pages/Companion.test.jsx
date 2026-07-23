import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Companion from "./Companion";

const mocks = vi.hoisted(() => ({
  invoke: vi.fn(),
}));

vi.mock("@/api/base44Client", () => ({
  base44: { functions: { invoke: mocks.invoke } },
}));

function streamedResponse(events) {
  const encoded = new TextEncoder().encode(events.map((event) => JSON.stringify(event)).join("\n") + "\n");
  let read = false;
  return {
    ok: true,
    body: {
      getReader: () => ({
        read: vi.fn(async () => {
          if (read) return { value: undefined, done: true };
          read = true;
          return { value: encoded, done: false };
        }),
      }),
    },
  };
}

describe("Companion grounded messages", () => {
  beforeEach(() => {
    Element.prototype.scrollIntoView = vi.fn();
    mocks.invoke.mockImplementation(async (_name, payload) => ({
      data: payload.action === "threads" ? { threads: [] } : { memories: [] },
    }));
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(streamedResponse([
      { thread: { id: "thread_synthetic", title: "Synthetic" } },
      { grounding: true },
      { delta: "Supported personal observation. [E1]" },
      {
        evidence: {
          contract_version: "companion-evidence-context/1.0.0",
          bundle: { id: "urn:synthetic:bundle", version: "2.0.0" },
        },
        message_id: "message_synthetic",
      },
      { done: true, remembered: [], thread_id: "thread_synthetic" },
    ])));
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  it("attaches the streamed evidence contract to the persisted assistant message", async () => {
    render(<Companion />);
    const input = screen.getByPlaceholderText("Ask about your health…");
    fireEvent.change(input, { target: { value: "What does my data show?" } });
    fireEvent.keyDown(input, { key: "Enter" });

    expect(await screen.findByText("Supported personal observation. [E1]")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Show evidence" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "What argues against this?" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "What changed?" })).toBeTruthy();
  });
});
