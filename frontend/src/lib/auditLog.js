// Personal single-user build: HIPAA-style audit logging was removed with the
// sharing/provider layer. Kept as a no-op so existing call sites still work.
export async function logAudit() {}
