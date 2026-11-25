// Search for the Documents tab on the Records page. Every word of the query
// must appear in the document's title, file name, type, summary, date or
// status, or in the name of a lab test extracted from it. "tsh 2024" finds the
// 2024 reports that contain a TSH result.

export function searchTerms(query) {
  return (query || "").toLowerCase().split(/\s+/).filter(Boolean);
}

// record id → the distinct lab test names extracted from that document, sorted.
// Results rejected in review are left out: their names were misread.
export function labNamesByRecord(labs) {
  const byRecord = new Map();
  for (const lab of labs || []) {
    if (!lab.record_id || !lab.test_name || lab.verification_status === "rejected") continue;
    if (!byRecord.has(lab.record_id)) byRecord.set(lab.record_id, new Set());
    byRecord.get(lab.record_id).add(lab.test_name);
  }
  const sorted = new Map();
  for (const [id, names] of byRecord) sorted.set(id, [...names].sort((a, b) => a.localeCompare(b)));
  return sorted;
}

// { match, labs }: whether the document matches every term, and which of its
// lab test names matched a term (shown on the card so a match is explained).
export function matchDocument(record, labNames, terms) {
  if (!terms.length) return { match: true, labs: [] };
  const status = record.status === "needs_review" ? "needs review" : record.status;
  const text = [record.title, record.filename, record.doc_type, record.summary, record.record_date, status]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
  const names = labNames || [];
  const lowerNames = names.map((name) => name.toLowerCase());
  const match = terms.every((term) => text.includes(term) || lowerNames.some((name) => name.includes(term)));
  if (!match) return { match: false, labs: [] };
  return { match: true, labs: names.filter((_, i) => terms.some((term) => lowerNames[i].includes(term))) };
}
