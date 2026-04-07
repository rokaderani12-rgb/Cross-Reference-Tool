# Millennium Cross Reference Tool (v2 – Excel Backend)

A production-style POC for finding compatible alternative part numbers.  
The FastAPI backend loads your Excel workbook at startup; the existing UI is preserved and wired to the API.

---

## Project Structure

```
CRT/
├── main.py                   ← FastAPI app + all API endpoints
├── requirements.txt
├── POC_Test_MLCC_Data.xlsx   ← Excel source of truth (place here)
├── logo.png                  ← Brand logo served by FastAPI
├── services/
│   ├── __init__.py
│   ├── models.py             ← NormalizedPart dataclass + API request models
│   ├── excel_loader.py       ← Excel loading, header normalization, deduplication
│   └── matcher.py            ← Scoring engine (essential 70% / optional 30%)
└── static/
    └── index.html            ← Frontend SPA (same UI, now calls backend API)
```

The old root `index.html` (hardcoded PARTS_DB) is preserved but no longer used.  
The app now serves `static/index.html` via FastAPI.

---

## Quick Start

### 1. Create and activate a virtual environment

```cmd
cd C:\Users\msipl-test\Downloads\CRT\CRT
python -m venv venv
venv\Scripts\activate
```

### 2. Install dependencies

```cmd
pip install -r requirements.txt
```

### 3. Place the Excel file

Ensure `POC_Test_MLCC_Data.xlsx` is in the project root (it is already there).  
The app reads **all sheets** and merges them into one in-memory parts database.

### 4. Run the server

```cmd
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

### 5. Open the app

```
http://localhost:8000
```

The header badge will show **"N parts loaded"** when the backend is ready.

---

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET`  | `/api/health` | Status check + parts count |
| `POST` | `/api/reload-data` | Reload Excel from disk (no restart needed) |
| `GET`  | `/api/part/{part_number}` | Get normalized details for one part |
| `GET`  | `/api/parts/sample?count=4` | Random sample of part numbers |
| `POST` | `/api/search/single` | Single part cross-reference search |
| `POST` | `/api/search/bulk` | Bulk search via file upload (CSV/XLSX/XLS) |
| `POST` | `/api/search/bulk-json` | Bulk search via JSON list of part numbers |

---

## Sample API Calls (curl)

### Health check
```bash
curl http://localhost:8000/api/health
```

### Single part search
```bash
curl -s -X POST http://localhost:8000/api/search/single \
  -H "Content-Type: application/json" \
  -d '{
    "part_number": "GRM155R61A104KA01D",
    "max_results": 5,
    "in_stock_only": false,
    "prefer_franchised": true
  }'
```

### Get one part's details
```bash
curl http://localhost:8000/api/part/GRM155R61A104KA01D
```

### Reload Excel without restarting
```bash
curl -X POST http://localhost:8000/api/reload-data
```

### Bulk search via JSON
```bash
curl -s -X POST http://localhost:8000/api/search/bulk-json \
  -H "Content-Type: application/json" \
  -d '{"parts": ["GRM155R61A104KA01D", "C1005X5R1A104K050BC"], "max_results": 5}'
```

### Bulk search via CSV file
```bash
curl -s -X POST http://localhost:8000/api/search/bulk \
  -F "file=@my_parts_list.csv"
```

**Expected CSV format:**
```csv
Part Number,Manufacturer
GRM155R61A104KA01D,Murata
C1005X5R1A104K050BC,TDK
```

---

## Excel File Requirements

The loader is tolerant of messy headers — it normalizes everything to `snake_case`  
and matches columns by alias. Recognized columns include:

| Your column name (any variant) | Internal field |
|-------------------------------|----------------|
| `display_pn`, `part_number`, `mpn` | Part number (required) |
| `manufacturer`, `brand`, `mfr` | Manufacturer |
| `product_category`, `type`, `category` | Product type |
| `capacitance_uF`, `capacitance_nF`, `capacitance_pF` | Capacitance |
| `voltage_rating_dc`, `voltage`, `vdc` | Voltage |
| `dielectric` | Dielectric material |
| `tolerance` | Tolerance (%, EIA code, or numeric) |
| `case_code_in`, `case_code_mm`, `case_code` | Package size |
| `minimum_operating_temperature_min` | Min operating temp |
| `maximum_operating_temperature_max` | Max operating temp |
| `packaging`, `packing` | Tape & Reel, etc. |
| `Franchised Supplier`, `franchised` | Boolean |
| `Inventory In Stock`, `in_stock`, `stock` | Boolean |

Multiple sheets are supported. All sheets are merged; first-sheet wins on duplicate part numbers.

---

## Matching / Scoring Logic

Candidates are scored against the master part and ranked by **adjusted score**:

```
essential_score = (essential_matches / essential_total) × 100
optional_score  = (optional_matches  / optional_total)  × 100
overall_score   = essential_score × 0.70 + optional_score × 0.30
adjusted_score  = min(100, overall_score + franchised_bonus + stock_boost)
```

| Category | Fields | Weight |
|----------|--------|--------|
| **Essential** | capacitance, voltage, dielectric, case_code | 70% |
| **Optional**  | tolerance, temp_min, temp_max, packaging    | 30% |
| Franchised bonus | +3 pts if candidate is franchised | — |
| In-stock boost   | +2 pts when `prefer_franchised=true` and part is in stock | — |

### Matching rules
- **Capacitance** — within ±5% (numeric comparison in pF)
- **Voltage** — candidate ≥ master (higher rating is acceptable)
- **Dielectric** — exact normalized text match
- **Case code** — exact normalized text match
- **Tolerance** — candidate ≤ master (tighter is acceptable)
- **Temp min** — candidate ≤ master (wider range is acceptable)
- **Temp max** — candidate ≥ master (wider range is acceptable)
- **Packaging** — exact normalized text match

---

## Reload Without Restart

If you update the Excel file while the server is running:

1. Click **↺ Reload Data** in the top-right of the UI, **or**
2. Call `POST /api/reload-data`

The in-memory database is replaced instantly; no server restart needed.

---

## What Changed from the Old Static Version

| Area | Old (static index.html) | New (FastAPI backend) |
|------|-------------------------|-----------------------|
| Data source | Hardcoded `PARTS_DB` JS object | Excel workbook loaded at startup |
| Bulk file parsing | `FileReader.readAsText()` — CSV only, broken for XLSX | Backend pandas parser — CSV, XLSX, XLS all work |
| Matching logic | Inline JS functions | `services/matcher.py` — reusable, testable Python |
| Search | Synchronous, blocking UI | Async `fetch()` calls with loading states |
| Part lookup | Dictionary key lookup | API endpoint with partial-match fallback |
| Data refresh | Required page reload + code edit | `POST /api/reload-data` or UI button |
| Sample data | Hardcoded part numbers (may not exist in DB) | Calls `/api/parts/sample` for real DB parts |
| Error handling | Silent failures / alert() | Proper HTTP status codes + inline UI error messages |
| Backend status | None | Live badge in header showing parts loaded count |
