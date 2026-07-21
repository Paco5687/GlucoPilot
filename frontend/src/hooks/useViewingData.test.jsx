import { cleanup, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useViewingData } from "./useViewingData";

vi.mock("@/api/base44Client", () => ({
  base44: { entities: {} },
}));

afterEach(cleanup);

describe("useViewingData", () => {
  it("keeps fetchEntity referentially stable across renders", () => {
    const { result, rerender } = renderHook(() => useViewingData());
    const initialFetchEntity = result.current.fetchEntity;

    rerender();
    rerender();
    rerender();

    expect(result.current.fetchEntity).toBe(initialFetchEntity);
  });
});
