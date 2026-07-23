async function request(path, options = {}) {
  const response = await fetch(path, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (!response.ok) throw new Error(`Clinical review request failed (${response.status})`);
  return response.json();
}

export function listAuditReviews() {
  return request("/api/clinical-reviews");
}

export function logAudit(payload) {
  return request("/api/clinical-reviews/actions", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function decideAuditReview(reviewId, decision, reason) {
  return request(`/api/clinical-reviews/${encodeURIComponent(reviewId)}/owner-decision`, {
    method: "POST",
    body: JSON.stringify({ decision, reason }),
  });
}
