import { base44 } from "@/api/base44Client";

/**
 * Personal single-user build: data fetching is always against your own data.
 * Keeps the original hook API so pages written against it work unchanged.
 */
export function useViewingData() {
  const fetchEntity = async (entityName, sort = "-created_date", limit = 5000, filter = {}) => {
    const cappedLimit = Math.min(limit, 5000);
    return base44.entities[entityName].filter(filter, sort, cappedLimit);
  };

  return { fetchEntity, isViewingShared: false, viewingEmail: null };
}
