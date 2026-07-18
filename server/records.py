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
from .llm import invoke_llm

log = logging.getLogger("glucopilot.records")

router = APIRouter(dependencies=[Depends(require_login)])

RECORDS_DIR = DATA_DIR / "records"
ALLOWED_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg", ".webp"}
MAX_PAGES = 8
MAX_FILE_MB = 25

EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "doc_type": {"type": "string", "description": "e.g. lab_report, imaging_report, visit_summary, other"},
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
    },
    "required": ["doc_type", "summary", "lab_results"],
}

EXTRACTION_PROMPT = """You are extracting structured data from a personal medical document for the patient's own health tracker.

Carefully read the document image(s) and extract:
1. doc_type, the primary record_date, and a short plain-language summary.
2. EVERY quantitative lab result you can find: test name (canonicalized — e.g. "Hemoglobin A1c" -> "HbA1c"), numeric value, unit, reference range bounds if printed, flag (high/low/normal/critical) if indicated, collection date, and panel/category.

Rules:
- Numbers only in "value" (strip comparison signs; for "<0.1" use 0.1 and flag as reported).
- Skip qualitative results (e.g. "negative") unless they carry a number.
- Do not invent values or reference ranges that are not visible.
"""


def _pdf_to_images(pdf_path: Path) -> list[Path]:
    with tempfile.TemporaryDirectory() as tmp:
        prefix = Path(tmp) / "page"
        subprocess.run(
            ["pdftoppm", "-png", "-r", "150", "-l", str(MAX_PAGES), str(pdf_path), str(prefix)],
            check=True,
            capture_output=True,
            timeout=120,
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


@router.post("/api/records/upload", dependencies=[Depends(require_admin)])
async def upload(file: UploadFile):
    suffix = Path(file.filename or "upload").suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(status_code=400, detail=f"Unsupported file type {suffix}. Use PDF, PNG, or JPG.")
    content = await file.read()
    if len(content) > MAX_FILE_MB * 1024 * 1024:
        raise HTTPException(status_code=400, detail=f"File too large (max {MAX_FILE_MB} MB).")

    RECORDS_DIR.mkdir(parents=True, exist_ok=True)
    rid = uuid.uuid4().hex
    stored = RECORDS_DIR / f"{rid}{suffix}"
    stored.write_bytes(content)

    record = db.create_entity(
        "MedicalRecord",
        {
            "filename": file.filename,
            "stored_as": stored.name,
            "status": "processing",
            "uploaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "owner_email": OWNER_EMAIL,
        },
    )

    try:
        if suffix == ".pdf":
            page_paths = await asyncio.to_thread(_pdf_to_images, stored)
            if not page_paths:
                raise RuntimeError("PDF rendered no pages")
        else:
            page_paths = [stored]
        images = _encode_images(page_paths)
        extracted = await _extract(images)

        results = []
        for lab in extracted.get("lab_results") or []:
            if not lab.get("test_name") or lab.get("value") is None:
                continue
            results.append(
                {
                    "test_name": str(lab["test_name"]).strip(),
                    "value": float(lab["value"]),
                    "unit": (lab.get("unit") or "").strip(),
                    "reference_low": lab.get("reference_low"),
                    "reference_high": lab.get("reference_high"),
                    "flag": (lab.get("flag") or "").lower(),
                    "collected_date": lab.get("collected_date") or extracted.get("record_date") or "",
                    "category": (lab.get("category") or "").strip(),
                    "record_id": record["id"],
                    "owner_email": OWNER_EMAIL,
                }
            )
        if results:
            db.bulk_create_entities("LabResult", results)

        record = db.update_entity(
            "MedicalRecord",
            record["id"],
            {
                "status": "processed",
                "doc_type": extracted.get("doc_type") or "other",
                "record_date": extracted.get("record_date") or "",
                "summary": extracted.get("summary") or "",
                "page_count": len(page_paths),
                "lab_count": len(results),
            },
        )
        return {"ok": True, "record": record, "lab_results": len(results)}
    except Exception as err:
        log.exception("record extraction failed")
        db.update_entity("MedicalRecord", record["id"], {"status": "failed", "error": str(err)[:300]})
        raise HTTPException(status_code=502, detail=f"Extraction failed: {err}")


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
