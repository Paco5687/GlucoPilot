import { useContext } from "react";
import { base44 } from "@/api/base44Client";
import { AuthContext } from "@/lib/AuthContext";

// This function must remain stable across renders. Several consumers use it as
// a callback/effect dependency, so recreating it inside the hook can turn an
// ordinary state update into another entity request.
async function fetchEntity(entityName, sort = "-created_date", limit = 5000, filter = {}) {
  const cappedLimit = Math.min(limit, 5000);
  return base44.entities[entityName].filter(filter, sort, cappedLimit);
}

/**
 * Personal single-user build: data fetching is always against the owner's data.
 * `isViewingShared` is true for read-only provider sessions — every write
 * affordance in the app gates on it, so it must reflect the real session role
 * rather than the single-user constant it once was.
 */
export function useViewingData() {
  // Null-safe: tests and isolated renders mount without an AuthProvider, and
  // "no session context" must never unlock write affordances anyway.
  const auth = useContext(AuthContext);
  return { fetchEntity, isViewingShared: auth?.isProvider === true, viewingEmail: null };
}
