// Lightweight breadcrumb of recent routes, for bug reports ("what they were
// doing before"). In-memory, capped, no health data — just path names.
const trail = [];

export function pushTrail(path) {
  if (trail[trail.length - 1] !== path) {
    trail.push(path);
    while (trail.length > 8) trail.shift();
  }
}

export function getTrail() {
  return [...trail];
}
