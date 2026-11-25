import { describe, expect, it } from "vitest";
import { labNamesByRecord, matchDocument, searchTerms } from "./documentSearch";

const report = {
  id: "rec_1",
  title: "Thyroid panel",
  filename: "quest_2024-03-02.pdf",
  doc_type: "lab_report",
  summary: "TSH and free T4 within range.",
  record_date: "2024-03-02",
  status: "processed",
};

const labs = [
  { record_id: "rec_1", test_name: "TSH" },
  { record_id: "rec_1", test_name: "Free T4" },
  { record_id: "rec_1", test_name: "TSH" },
  { record_id: "rec_1", test_name: "Ferritin" },
  { record_id: "rec_1", test_name: "Thyroglobulin Ab", verification_status: "rejected" },
  { record_id: "rec_2", test_name: "Glucose" },
  { test_name: "No source" },
];

describe("document search", () => {
  it("splits the query into lowercase words", () => {
    expect(searchTerms("  TSH   2024 ")).toEqual(["tsh", "2024"]);
    expect(searchTerms("")).toEqual([]);
    expect(searchTerms(undefined)).toEqual([]);
  });

  it("groups distinct lab names by document and drops rejected results", () => {
    const names = labNamesByRecord(labs);
    expect(names.get("rec_1")).toEqual(["Ferritin", "Free T4", "TSH"]);
    expect(names.get("rec_2")).toEqual(["Glucose"]);
    expect(names.size).toBe(2);
  });

  it("matches everything when the query is empty", () => {
    expect(matchDocument(report, [], [])).toEqual({ match: true, labs: [] });
  });

  it("matches the title, file name, summary and date", () => {
    for (const query of ["thyroid", "quest", "within range", "2024-03"]) {
      expect(matchDocument(report, [], searchTerms(query)).match).toBe(true);
    }
  });

  it("finds a document by a lab test extracted from it and names the test", () => {
    const names = labNamesByRecord(labs).get("rec_1");
    expect(matchDocument(report, names, searchTerms("ferritin"))).toEqual({ match: true, labs: ["Ferritin"] });
  });

  it("requires every word to match somewhere", () => {
    const names = labNamesByRecord(labs).get("rec_1");
    expect(matchDocument(report, names, searchTerms("ferritin 2024")).match).toBe(true);
    expect(matchDocument(report, names, searchTerms("ferritin 2023")).match).toBe(false);
  });

  it("does not match a rejected lab name", () => {
    const names = labNamesByRecord(labs).get("rec_1");
    expect(matchDocument(report, names, searchTerms("thyroglobulin")).match).toBe(false);
  });

  it("matches the needs-review status as words", () => {
    const pending = { ...report, status: "needs_review" };
    expect(matchDocument(pending, [], searchTerms("needs review")).match).toBe(true);
  });
});
