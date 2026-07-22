"""Medical records: upload → local-VLM extraction → LabResult trends.

Files (PDF/JPG/PNG) are stored under DATA_DIR/records and never leave the
server; extraction runs through the configured LLM provider — with the local
vLLM (Qwen3-VL, a vision model) the PHI stays entirely on this machine.

Entities:
  MedicalRecord: filename, doc_type, record_date, summary, status, page_count
  LabResult: test_name, value, unit, reference_low, reference_high, flag,
             collected_date, category, record_id
"""

import asyncio
import base64
import hashlib
import logging
import subprocess
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.responses import FileResponse

from . import db
from .auth import require_admin, require_login
from .config import DATA_DIR, OWNER_EMAIL
from .connector_provenance import capture_file, run_connector, source_failure
from .llm import invoke_llm

log = logging.getLogger("glucopilot.records")

router = APIRouter(dependencies=[Depends(require_login)])

RECORDS_DIR = DATA_DIR / "records"
ALLOWED_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg", ".webp"}
MAX_PAGES = 40  # full multi-page records (25-30pp are common); beyond this we truncate + note
PAGE_BATCH = 4  # pages per vision-model call — the local vLLM caps images at 4 per prompt
MAX_FILE_MB = 60

EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "doc_type": {"type": "string", "description": "e.g. lab_report, imaging_report, visit_summary, other"},
        "source_name": {"type": "string", "description": "Short searchable label for the SOURCE/panel — lab vendor, imaging center, or test name, e.g. 'ACL Labs', 'DUTCH Hormones', 'Labcorp CMP', 'CT Abdomen/Pelvis'. No date."},
        "record_date": {"type": "string", "description": "Primary date on the document, YYYY-MM-DD; empty if unknown"},
        "summary": {"type": "string", "description": "2-4 sentence plain-language summary of the document"},
        "lab_results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "test_name": {"type": "string", "description": "Canonical test name, e.g. HbA1c, TSH, LDL Cholesterol"},
                    "value": {"type": "number"},
                    "unit": {"type": "string"},
                    "reference_low": {"type": ["number", "null"]},
                    "reference_high": {"type": ["number", "null"]},
                    "flag": {"type": "string", "description": "normal, high, low, critical, or empty"},
                    "collected_date": {"type": "string", "description": "YYYY-MM-DD; empty if unknown"},
                    "category": {"type": "string", "description": "Panel name, e.g. CBC, Metabolic Panel, Lipids, Thyroid"},
                },
                "required": ["test_name", "value"],
            },
        },
        "measurements": {
            "type": "array",
            "description": "For imaging/radiology reports (CT, MRI, ultrasound, X-ray, DEXA) or any report with quantitative anatomical measurements — organ sizes, cyst/nodule/lesion dimensions, etc. Empty for ordinary blood/urine panels.",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "What was measured, WITH its location, e.g. 'Spleen length', 'Right adnexal cyst', 'Thyroid nodule (left lobe)'"},
                    "value": {"type": "number"},
                    "unit": {"type": "string", "description": "e.g. cm, mm"},
                    "flag": {"type": "string", "description": "enlarged/abnormal/normal if the report indicates; empty otherwise"},
                    "category": {"type": "string", "description": "Modality + region, e.g. 'Imaging - CT Abdomen/Pelvis', 'Imaging - MRI Brain', 'Imaging - Thyroid Ultrasound'"},
                },
                "required": ["name", "value"],
            },
        },
    },
    "required": ["doc_type", "summary", "lab_results"],
}

EXTRACTION_PROMPT = """You are extracting structured data from a personal medical document for the patient's own health tracker.

Carefully read the document image(s) and extract:
1. doc_type (lab_report, imaging_report, visit_summary, other), the primary record_date, a short plain-language summary, and source_name — a short searchable label for the lab vendor / imaging center / panel (e.g. "ACL Labs", "DUTCH Hormones", "Labcorp CMP", "CT Abdomen/Pelvis"), WITHOUT a date. For an imaging or radiology report, the summary should capture the key findings and the impression.
2. lab_results: EVERY quantitative blood/urine lab result — test name (canonicalized, e.g. "Hemoglobin A1c" -> "HbA1c"), numeric value, unit, reference range bounds if printed, flag (high/low/normal/critical) if indicated, collection date, and panel/category.
3. measurements: for imaging/radiology reports (CT, MRI, ultrasound, X-ray, DEXA) OR any report with quantitative anatomical measurements, capture each one — organ sizes, cyst/nodule/lesion/mass dimensions — as name (WITH anatomical location), numeric value, unit (cm/mm), a flag if the report calls it enlarged/abnormal, and a category like "Imaging - CT Abdomen/Pelvis". Leave measurements empty for ordinary blood/urine panels.

Rules:
- Numbers only in "value" (strip comparison signs; for "<0.1" use 0.1 and flag as reported).
- Skip qualitative-only results (e.g. "negative", "not detected") unless they carry a number.
- Do not invent values or reference ranges that are not visible.
"""


def _pdf_to_images(pdf_path: Path) -> list[Path]:
    with tempfile.TemporaryDirectory() as tmp:
        prefix = Path(tmp) / "page"
        subprocess.run(
            ["pdftoppm", "-png", "-r", "150", "-l", str(MAX_PAGES), str(pdf_path), str(prefix)],
            check=True,
            capture_output=True,
            timeout=300,
        )
        pages = sorted(Path(tmp).glob("page*.png"))
        out = []
        for i, page in enumerate(pages):
            dest = pdf_path.parent / f"{pdf_path.stem}_page{i + 1}.png"
            dest.write_bytes(page.read_bytes())
            out.append(dest)
        return out


def _encode_images(paths: list[Path]) -> list[str]:
    encoded = []
    for p in paths[:MAX_PAGES]:
        media = "image/png" if p.suffix == ".png" else "image/webp" if p.suffix == ".webp" else "image/jpeg"
        encoded.append(f"{media}|{base64.b64encode(p.read_bytes()).decode()}")
    return encoded


async def _extract(images: list[str]) -> dict:
    return await invoke_llm(EXTRACTION_PROMPT, response_json_schema=EXTRACTION_SCHEMA, max_tokens=6000, images=images)


async def _extract_document(page_paths: list[Path]) -> dict:
    """Extract a whole document. Long records (25-30+ pages) are processed in
    page batches so each vision call stays within the local model's context,
    then merged. doc_type/date/summary come from the first batch that yields
    them; lab_results accumulate across all batches (deduped later).

    Resilient: a batch is retried once, and a persistently-failing batch (e.g.
    the local model returns non-JSON for one page) is skipped so the rest of a
    long document still extracts. Raises only if EVERY batch fails."""
    merged: dict = {"doc_type": "", "record_date": "", "summary": "", "lab_results": [], "measurements": []}
    batches = [page_paths[i : i + PAGE_BATCH] for i in range(0, len(page_paths), PAGE_BATCH)]
    failed = 0
    last_err: Exception | None = None
    for idx, batch in enumerate(batches):
        part = None
        for attempt in range(2):  # one retry — local-model JSON hiccups are often transient
            try:
                part = await _extract(_encode_images(batch))
                break
            except Exception as err:  # noqa: BLE001 - keep going through the document
                last_err = err
                log.warning("batch %d/%d extract failed (attempt %d): %s", idx + 1, len(batches), attempt + 1, str(err)[:150])
        if part is None:
            failed += 1
            continue
        if not merged["doc_type"] and part.get("doc_type"):
            merged["doc_type"] = part["doc_type"]
        if not merged["record_date"] and part.get("record_date"):
            merged["record_date"] = part["record_date"]
        if not merged["summary"] and part.get("summary"):
            merged["summary"] = part["summary"]
        merged["lab_results"].extend(part.get("lab_results") or [])
        merged["measurements"].extend(part.get("measurements") or [])
    if failed == len(batches):
        raise last_err or RuntimeError("extraction failed for every page batch")
    merged["_batches_failed"] = failed
    merged["_batches_total"] = len(batches)
    return merged


@router.post("/api/records/upload", dependencies=[Depends(require_admin)])
async def upload(file: UploadFile):
    return await run_connector(
        "medical_record_upload",
        "upload",
        lambda: _upload(file),
        trigger_type="upload",
        run_kind="upload",
    )


async def _upload(file: UploadFile):
    suffix = Path(file.filename or "upload").suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(status_code=400, detail=f"Unsupported file type {suffix}. Use PDF, PNG, or JPG.")
    content = await file.read()
    if len(content) > MAX_FILE_MB * 1024 * 1024:
        raise HTTPException(status_code=400, detail=f"File too large (max {MAX_FILE_MB} MB).")

    # Skip byte-identical re-uploads so a batch full of duplicates doesn't burn
    # GPU time re-extracting or double-count the same labs.
    content_hash = hashlib.sha256(content).hexdigest()
    dupes = db.query_entities(
        "MedicalRecord", {"owner_email": OWNER_EMAIL, "content_hash": content_hash}, "-created_date", 1
    )
    if dupes:
        prior = dupes[0]
        if prior.get("stored_as"):
            capture_file(
                prior["stored_as"],
                "sha256:" + content_hash,
                len(content),
                external_id=prior["id"],
                mime_type=file.content_type,
            )
        return {
            "ok": True,
            "duplicate": True,
            "skipped": 1,
            "duplicate_of": prior.get("filename"),
            "record": prior,
            "lab_results": prior.get("lab_count", 0),
        }

    RECORDS_DIR.mkdir(parents=True, exist_ok=True)
    rid = uuid.uuid4().hex
    stored = RECORDS_DIR / f"{rid}{suffix}"
    stored.write_bytes(content)
    capture_file(
        stored.name,
        "sha256:" + content_hash,
        len(content),
        external_id=rid,
        mime_type=file.content_type,
    )

    record = db.create_entity(
        "MedicalRecord",
        {
            "filename": file.filename,
            "stored_as": stored.name,
            "content_hash": content_hash,
            "status": "processing",
            "uploaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "owner_email": OWNER_EMAIL,
        },
    )

    try:
        record = await _extract_and_store(record, stored, suffix)
        if record.get("partial"):
            source_failure(record.get("error") or "medical-record extraction was partial")
        return {"ok": True, "record": record, "lab_results": record.get("lab_count", 0)}
    except Exception as err:
        log.exception("record extraction failed")
        db.update_entity("MedicalRecord", record["id"], {"status": "failed", "error": str(err)[:300]})
        raise HTTPException(status_code=502, detail=f"Extraction failed: {err}")


async def _extract_and_store(record: dict, stored: Path, suffix: str) -> dict:
    """Render → batched vision extraction → replace this record's LabResults →
    mark processed. Raises on failure; callers mark the record failed. Reused by
    upload and reprocess (reprocess re-runs on the already-stored file)."""
    if suffix == ".pdf":
        page_paths = await asyncio.to_thread(_pdf_to_images, stored)
        if not page_paths:
            raise RuntimeError("PDF rendered no pages")
    else:
        page_paths = [stored]
    extracted = await _extract_document(page_paths)

    results = _normalize_lab_results(extracted, record["id"])

    # Replace any prior labs for this record so reprocessing is idempotent.
    for old in db.query_entities("LabResult", {"record_id": record["id"], "owner_email": OWNER_EMAIL}):
        db.delete_entity("LabResult", old["id"])
    if results:
        db.bulk_create_entities("LabResult", results)

    b_failed = extracted.get("_batches_failed", 0)
    b_total = extracted.get("_batches_total", 1)
    record_date = extracted.get("record_date") or ""
    return db.update_entity(
        "MedicalRecord",
        record["id"],
        {
            "status": "processed",
            "doc_type": extracted.get("doc_type") or "other",
            "record_date": record_date,
            "summary": extracted.get("summary") or "",
            "title": _make_title(extracted.get("source_name"), record_date, record.get("filename")),
            "page_count": len(page_paths),
            "lab_count": len(results),
            # Note when some page-batches couldn't be read so the user can re-run.
            "error": f"Partial: {b_failed}/{b_total} page-batches failed to read" if b_failed else "",
            "partial": bool(b_failed),
        },
    )


def _normalize_lab_results(extracted: dict, record_id: str) -> list[dict]:
    """Normalize one synthetic or extracted document without touching storage.

    Keeping this transformation pure makes duplicate/range behavior directly
    regression-testable while preserving the existing persistence sequence.
    """
    results = []
    seen = set()
    for lab in extracted.get("lab_results") or []:
        if not lab.get("test_name") or lab.get("value") is None:
            continue
        try:
            value = float(lab["value"])
        except (TypeError, ValueError):
            continue
        test_name = str(lab["test_name"]).strip()
        collected = lab.get("collected_date") or extracted.get("record_date") or ""
        # A test can repeat across batched pages (summary + detail) — keep one.
        key = (test_name.lower(), collected, round(value, 4))
        if key in seen:
            continue
        seen.add(key)
        results.append(
            {
                "test_name": test_name,
                "value": value,
                "unit": (lab.get("unit") or "").strip(),
                "reference_low": lab.get("reference_low"),
                "reference_high": lab.get("reference_high"),
                "flag": (lab.get("flag") or "").lower(),
                "collected_date": collected,
                "category": (lab.get("category") or "").strip(),
                "record_id": record_id,
                "owner_email": OWNER_EMAIL,
            }
        )
    # Imaging/anatomical measurements → tracked LabResults (so organ/lesion sizes
    # trend over follow-up scans), deduped alongside the blood/urine results.
    flag_map = {"enlarged": "high", "elevated": "high", "large": "high", "small": "low", "decreased": "low"}
    for m in extracted.get("measurements") or []:
        if not m.get("name") or m.get("value") is None:
            continue
        try:
            value = float(m["value"])
        except (TypeError, ValueError):
            continue
        name = str(m["name"]).strip()
        collected = extracted.get("record_date") or ""
        key = (name.lower(), collected, round(value, 4))
        if key in seen:
            continue
        seen.add(key)
        raw_flag = (m.get("flag") or "").strip().lower()
        results.append(
            {
                "test_name": name,
                "value": value,
                "unit": (m.get("unit") or "").strip(),
                "reference_low": None,
                "reference_high": None,
                "flag": flag_map.get(raw_flag, raw_flag),
                "collected_date": collected,
                "category": (m.get("category") or "Imaging").strip(),
                "record_id": record_id,
                "owner_email": OWNER_EMAIL,
            }
        )
    return results


def _mdy(date_iso: str) -> str:
    """YYYY-MM-DD -> M-D-YYYY (matches how the labels read); passthrough otherwise."""
    try:
        y, m, d = date_iso.split("-")
        return f"{int(m)}-{int(d)}-{y}"
    except (ValueError, AttributeError):
        return date_iso or ""


def _make_title(source_name: str | None, record_date: str, filename: str | None) -> str:
    """Searchable '<source> <M-D-YYYY>' label. Falls back to the filename stem."""
    src = (source_name or "").strip()
    if not src:
        src = Path(filename or "Document").stem
    mdy = _mdy(record_date)
    return f"{src} {mdy}".strip() if mdy else src


@router.post("/api/records/{rid}/reprocess", dependencies=[Depends(require_admin)])
async def reprocess(rid: str):
    return await run_connector(
        "medical_record_upload",
        "reprocess",
        lambda: _reprocess(rid),
        trigger_type="reprocess",
        run_kind="reprocess",
    )


async def _reprocess(rid: str):
    rows = db.query_entities("MedicalRecord", {"id": rid, "owner_email": OWNER_EMAIL}, limit=1)
    if not rows:
        raise HTTPException(status_code=404, detail="Record not found")
    record = rows[0]
    stored = RECORDS_DIR / (record.get("stored_as") or "")
    if not record.get("stored_as") or not stored.is_file():
        raise HTTPException(status_code=404, detail="Stored file missing — please re-upload this document.")
    capture_file(
        stored.name,
        "sha256:" + hashlib.sha256(stored.read_bytes()).hexdigest(),
        stored.stat().st_size,
        external_id=rid,
    )
    db.update_entity("MedicalRecord", rid, {"status": "processing"})
    try:
        record = await _extract_and_store(record, stored, stored.suffix.lower())
        if record.get("partial"):
            source_failure(record.get("error") or "medical-record extraction was partial")
        return {"ok": True, "record": record, "lab_results": record.get("lab_count", 0)}
    except Exception as err:
        log.exception("record reprocess failed")
        db.update_entity("MedicalRecord", rid, {"status": "failed", "error": str(err)[:300]})
        raise HTTPException(status_code=502, detail=f"Reprocess failed: {err}")


@router.post("/api/records/backfill-titles", dependencies=[Depends(require_admin)])
async def backfill_titles():
    """One-time: give already-processed documents a searchable title from their
    summary (source/panel name) + date taken."""
    updated = 0
    for rec in db.query_entities("MedicalRecord", {"owner_email": OWNER_EMAIL}, "-created_date", 1000):
        if rec.get("title") or rec.get("status") != "processed":
            continue
        src = ""
        summary = rec.get("summary") or ""
        if summary:
            try:
                out = await invoke_llm(
                    "From this medical document summary, reply with ONLY a short searchable source/panel "
                    "label (lab vendor, imaging center, or test/panel name) — no date, max 5 words:\n\n"
                    + summary[:800],
                    max_tokens=30,
                )
                src = (out or "").strip().strip('"').splitlines()[0][:60] if out else ""
            except Exception:
                src = ""
        db.update_entity("MedicalRecord", rec["id"],
                         {"title": _make_title(src, rec.get("record_date") or "", rec.get("filename"))})
        updated += 1
    return {"ok": True, "updated": updated}


@router.get("/api/records/file/{rid}")
def get_file(rid: str):
    rows = db.query_entities("MedicalRecord", {"id": rid, "owner_email": OWNER_EMAIL}, limit=1)
    if not rows or not rows[0].get("stored_as"):
        raise HTTPException(status_code=404, detail="Record not found")
    path = RECORDS_DIR / rows[0]["stored_as"]
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Stored file missing")
    return FileResponse(path, filename=rows[0].get("filename") or path.name)


@router.delete("/api/records/{rid}", dependencies=[Depends(require_admin)])
def delete_record(rid: str):
    rows = db.query_entities("MedicalRecord", {"id": rid, "owner_email": OWNER_EMAIL}, limit=1)
    if not rows:
        raise HTTPException(status_code=404, detail="Record not found")
    stored = rows[0].get("stored_as")
    if stored:
        for f in RECORDS_DIR.glob(f"{Path(stored).stem}*"):
            f.unlink(missing_ok=True)
    for lab in db.query_entities("LabResult", {"record_id": rid, "owner_email": OWNER_EMAIL}):
        db.delete_entity("LabResult", lab["id"])
    db.delete_entity("MedicalRecord", rid)
    return {"ok": True}
