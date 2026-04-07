"""
Millennium Cross Reference Tool – FastAPI backend

Endpoints:
  GET  /api/health              – health check + parts count
  POST /api/reload-data         – reload Excel from disk
  GET  /api/part/{part_number}  – get normalized part details
  GET  /api/parts/sample        – return N random part numbers from the DB
  POST /api/search/single       – single part cross-reference search
  POST /api/search/bulk         – bulk search via uploaded CSV/XLSX/XLS file
  POST /api/search/bulk-json    – bulk search via JSON list of part numbers
  GET  /                        – serve the frontend SPA
"""

import io
import logging
import random
import time
from pathlib import Path

import pandas as pd
from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from services.excel_loader import ExcelLoader
from services.matcher import Matcher
from services.models import (
    BulkJsonRequest,
    NormalizedPart,
    SearchSingleRequest,
    to_frontend_part,
)

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s – %(message)s",
)
logger = logging.getLogger("crt.main")

# ── Config ─────────────────────────────────────────────────────────────────────
EXCEL_PATH  = Path("POC_Test_MLCC_Data.xlsx")
STATIC_DIR  = Path("static")

# ── App ────────────────────────────────────────────────────────────────────────
app = FastAPI(title="Millennium Cross Reference Tool", version="2.0.0")

_loader   = ExcelLoader()
_parts_db: dict[str, NormalizedPart] = {}   # pn (uppercase) → NormalizedPart


# ── Data loading ───────────────────────────────────────────────────────────────

def _load_data() -> bool:
    """Load (or reload) the Excel workbook into memory.  Returns True on success."""
    global _parts_db
    if not EXCEL_PATH.exists():
        logger.error(
            f"Excel file not found: {EXCEL_PATH.absolute()}  "
            f"Place '{EXCEL_PATH.name}' in the project root, then call POST /api/reload-data"
        )
        _parts_db = {}
        return False
    try:
        _parts_db = _loader.load(EXCEL_PATH)
        logger.info(f"Data ready: {len(_parts_db)} parts loaded from '{EXCEL_PATH.name}'")
        return True
    except Exception as exc:
        logger.error(f"Failed to load Excel: {exc}", exc_info=True)
        _parts_db = {}
        return False


@app.on_event("startup")
async def _startup():
    ok = _load_data()
    if not ok:
        logger.warning(
            "App started with empty database.  "
            "Ensure 'POC_Test_MLCC_Data.xlsx' is in the project root."
        )


# ── API routes ─────────────────────────────────────────────────────────────────

@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "parts_loaded": len(_parts_db),
        "excel_file": str(EXCEL_PATH),
        "excel_exists": EXCEL_PATH.exists(),
    }


@app.post("/api/reload-data")
def reload_data():
    ok = _load_data()
    if not ok:
        raise HTTPException(500, f"Reload failed. See server logs.")
    return {"status": "ok", "parts_loaded": len(_parts_db)}


@app.get("/api/part/{part_number}")
def get_part(part_number: str):
    pn = part_number.strip().upper()
    part = _parts_db.get(pn)
    if not part:
        raise HTTPException(404, f"Part '{pn}' not found in database.")
    return to_frontend_part(part)


@app.get("/api/parts/sample")
def get_sample_parts(count: int = Query(default=4, ge=1, le=20)):
    """Return a random sample of part numbers from the loaded database."""
    pns = list(_parts_db.keys())
    if not pns:
        return {"part_numbers": []}
    sample = random.sample(pns, min(count, len(pns)))
    return {"part_numbers": sample}


@app.post("/api/search/single")
def search_single(req: SearchSingleRequest):
    pn = req.part_number.strip().upper()
    logger.info(
        f"Single search: '{pn}' "
        f"(max={req.max_results}, stock_only={req.in_stock_only}, "
        f"prefer_franchised={req.prefer_franchised})"
    )

    if not _parts_db:
        raise HTTPException(503, "No data loaded. Check Excel file and call /api/reload-data.")

    master = _parts_db.get(pn)
    found_pn = pn

    if master is None:
        # Try partial / prefix match
        suggestions = [
            k for k in _parts_db
            if pn in k or (len(pn) >= 6 and k.startswith(pn[:6]))
        ]
        if not suggestions:
            raise HTTPException(
                404,
                f"Part '{pn}' not found and no similar part numbers located. "
                f"Try a prefix like the first 6 characters."
            )
        found_pn = suggestions[0]
        master   = _parts_db[found_pn]
        logger.info(f"Partial match used: '{pn}' → '{found_pn}'")

    matcher = Matcher()
    matches = matcher.find_matches(
        master=master,
        all_parts=_parts_db,
        max_results=req.max_results,
        in_stock_only=req.in_stock_only,
        prefer_franchised=req.prefer_franchised,
    )

    logger.info(f"Single search '{found_pn}': {len(matches)} matches returned")
    return {
        "master_pn":   found_pn,
        "master_part": to_frontend_part(master),
        "matches":     matches,
    }


@app.post("/api/search/bulk")
async def search_bulk(file: UploadFile = File(...)):
    """Accept a CSV/XLSX/XLS file upload; return grouped results per part."""
    ext     = Path(file.filename or "upload.csv").suffix.lower()
    content = await file.read()
    logger.info(f"Bulk upload: '{file.filename}' ({len(content):,} bytes, ext='{ext}')")

    if not _parts_db:
        raise HTTPException(503, "No data loaded. Check Excel file and call /api/reload-data.")

    try:
        parts_list = _parse_bulk_file(content, ext)
    except Exception as exc:
        raise HTTPException(400, f"Could not parse uploaded file: {exc}")

    if len(parts_list) > 100:
        logger.warning(f"Bulk upload truncated from {len(parts_list)} to 100 rows")
        parts_list = parts_list[:100]

    return _run_bulk_search(parts_list)


@app.post("/api/search/bulk-json")
def search_bulk_json(req: BulkJsonRequest):
    """Accept a JSON list of part numbers; return grouped results."""
    if not _parts_db:
        raise HTTPException(503, "No data loaded. Check Excel file and call /api/reload-data.")

    parts_list = [{"part_number": pn.strip().upper(), "manufacturer": ""} for pn in req.parts if pn.strip()]
    if len(parts_list) > 100:
        parts_list = parts_list[:100]

    logger.info(f"Bulk-JSON search: {len(parts_list)} parts")
    return _run_bulk_search(
        parts_list,
        max_results=req.max_results,
        in_stock_only=req.in_stock_only,
        prefer_franchised=req.prefer_franchised,
    )


# ── Static files ───────────────────────────────────────────────────────────────

@app.get("/logo.png")
def serve_logo():
    logo_path = Path("logo.png")
    if logo_path.exists():
        return FileResponse(logo_path, media_type="image/png")
    raise HTTPException(404, "logo.png not found")


@app.get("/", response_class=HTMLResponse)
def index():
    html_path = STATIC_DIR / "index.html"
    if not html_path.exists():
        raise HTTPException(500, "Frontend not found. Expected static/index.html")
    return html_path.read_text(encoding="utf-8")


# Mount static directory (CSS, JS, fonts if added later)
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ── Helpers ────────────────────────────────────────────────────────────────────

def _parse_bulk_file(content: bytes, ext: str) -> list:
    """Parse uploaded CSV/XLSX/XLS bytes into a list of {part_number, manufacturer}."""
    if ext in (".xlsx", ".xls"):
        df = pd.read_excel(io.BytesIO(content), header=0, engine="openpyxl")
    elif ext == ".csv":
        df = pd.read_csv(io.BytesIO(content))
    else:
        raise ValueError(f"Unsupported file extension '{ext}'. Use .csv, .xlsx, or .xls")

    # Normalize column headers
    df.columns = [
        str(c).strip().lower().replace(" ", "_") for c in df.columns
    ]

    # Identify part-number column (first column is fallback)
    pn_col = next(
        (c for c in df.columns if any(kw in c for kw in ("part", "mpn", "pn", "number", "no"))),
        df.columns[0],
    )
    mfr_col = next(
        (c for c in df.columns if any(kw in c for kw in ("mfr", "manufacturer", "brand"))),
        None,
    )

    rows = []
    for _, row in df.iterrows():
        pn = str(row.get(pn_col, "")).strip().upper()
        mfr = str(row.get(mfr_col, "")).strip() if mfr_col else ""
        if pn and pn not in ("NAN", "NONE", ""):
            rows.append({"part_number": pn, "manufacturer": mfr})
    return rows


def _run_bulk_search(
    parts_list: list,
    max_results: int = 5,
    in_stock_only: bool = False,
    prefer_franchised: bool = False,
) -> dict:
    """Core bulk search logic shared by file-upload and JSON endpoints."""
    matcher    = Matcher()
    start_time = time.perf_counter()
    results    = []
    not_found  = 0

    for row in parts_list:
        pn     = row["part_number"]
        master = _parts_db.get(pn)

        if master is None:
            results.append({
                "master_pn":   pn,
                "master_part": None,
                "matches":     [],
                "found":       False,
                "message":     f"Part '{pn}' not found in database.",
            })
            not_found += 1
            continue

        matches = matcher.find_matches(
            master=master,
            all_parts=_parts_db,
            max_results=max_results,
            in_stock_only=in_stock_only,
            prefer_franchised=prefer_franchised,
        )
        results.append({
            "master_pn":   pn,
            "master_part": to_frontend_part(master),
            "matches":     matches,
            "found":       True,
            "message":     None,
        })

    elapsed = round(time.perf_counter() - start_time, 3)
    total_matches = sum(len(r["matches"]) for r in results)
    logger.info(
        f"Bulk search complete: {len(results)} parts, "
        f"{total_matches} total matches, "
        f"{not_found} not found, "
        f"{elapsed}s elapsed"
    )

    return {
        "results":       results,
        "total_parts":   len(results),
        "total_matches": total_matches,
        "elapsed_s":     elapsed,
    }
