import { base44 } from "@/api/base44Client";

// This function must remain stable across renders. Several consumers use it as
// a callback/effect dependency, so recreating it inside the hook can turn an
// ordinary state update into another entity request.
async function fetchEntity(entityName, sort = "-created_date", limit = 5000, filter = {}) {
  const cappedLimit = Math.min(limit, 5000);
  return base44.entities[entityName].filter(filter, sort, cappedLimit);
}

/**
 * Personal single-user build: data fetching is always against your own data.
 * Keeps the original hook API so pages written against it work unchanged.
 */
export function useViewingData() {
  return { fetchEntity, isViewingShared: false, viewingEmail: null };
}
