"""
Loads, normalizes, and deduplicates parts data from an Excel workbook.

Design goals:
- Handle messy/varied column headers robustly via alias matching
- Normalize all string values and part numbers
- Convert capacitance from any unit column to a single comparable pF base
- Normalize boolean fields (yes/no/Y/N/1/0) to Python bool
- Deduplicate part numbers, keeping the highest-quality record
- Log row counts, duplicates, and invalid rows per sheet
"""

import re
import logging
from pathlib import Path
from typing import Dict, Optional, List, Tuple

import pandas as pd

from services.models import NormalizedPart

logger = logging.getLogger(__name__)

# ── Column alias registry ──────────────────────────────────────────────────────
# Maps each canonical field name to a list of possible normalized column names.
# "Normalized" means: stripped, lowercased, non-alnum replaced with _.
COLUMN_ALIASES: Dict[str, List[str]] = {
    "display_pn":       ["display_pn", "part_number", "part_no", "mpn", "pn",
                         "part", "item_no", "item_number"],
    "manufacturer":     ["manufacturer", "mfr", "brand", "make", "vendor",
                         "supplier"],
    "product_category": ["product_category", "category", "type", "part_type",
                         "component_type", "series"],
    "capacitance_uf":   ["capacitance_uf", "cap_uf", "capacitance_microfarad",
                         "cap_microfarad", "capacitance_f_10_6"],
    "capacitance_nf":   ["capacitance_nf", "cap_nf", "capacitance_nanofarad",
                         "cap_nanofarad", "capacitance_f_10_9"],
    "capacitance_pf":   ["capacitance_pf", "cap_pf", "capacitance_picofarad",
                         "cap_picofarad", "capacitance_f_10_12"],
    "capacitance_any":  ["capacitance", "cap", "capacitance_value",
                         "nominal_capacitance"],
    "voltage_rating":   ["voltage_rating_dc", "voltage", "voltage_dc",
                         "rated_voltage", "voltage_rating", "vdc", "volt",
                         "working_voltage"],
    "dielectric":       ["dielectric", "dielectric_material", "dielectric_type",
                         "temperature_characteristic"],
    "tolerance":        ["tolerance", "cap_tolerance", "tol",
                         "capacitance_tolerance"],
    "case_code_in":     ["case_code_in", "case_code_inch", "case_in",
                         "size_in", "footprint", "case_code", "package_size",
                         "eia_case_code"],
    "case_code_mm":     ["case_code_mm", "case_mm", "size_mm",
                         "metric_case_code"],
    "temp_min":         ["minimum_operating_temperature_min",
                         "minimum_operating_temperature",
                         "temp_min", "min_temp", "operating_temp_min",
                         "temp_min_c", "low_temp", "temperature_min"],
    "temp_max":         ["maximum_operating_temperature_max",
                         "maximum_operating_temperature",
                         "temp_max", "max_temp", "operating_temp_max",
                         "temp_max_c", "high_temp", "temperature_max"],
    "packaging":        ["packaging", "packing", "package", "package_type",
                         "tape_reel", "reel"],
    "franchised":       ["franchised_supplier", "franchised", "is_franchised",
                         "authorized", "authorized_supplier"],
    "stock":            ["inventory_in_stock", "in_stock", "stock",
                         "available", "qty_available", "quantity",
                         "availability"],
}

# EIA single-letter tolerance codes → numeric percentage
EIA_TOLERANCE: Dict[str, float] = {
    "B": 0.1, "C": 0.25, "D": 0.5, "F": 1.0,
    "G": 2.0, "J": 5.0, "K": 10.0, "M": 20.0,
}


# ── Header normalization ───────────────────────────────────────────────────────

def _normalize_header(h: str) -> str:
    """Convert any column header to lowercase snake_case."""
    h = str(h).strip()
    h = re.sub(r"[\s\-/\\()+]+", "_", h)
    h = re.sub(r"[^\w]", "", h)
    h = re.sub(r"_+", "_", h)
    return h.lower().strip("_")


def _find_column(norm_cols: List[str], field: str) -> Optional[str]:
    """
    Return the first matching normalized column name for a canonical field.
    Falls back to partial/substring matching when exact match fails.
    """
    aliases = COLUMN_ALIASES.get(field, [field])

    # Exact match first
    for alias in aliases:
        if alias in norm_cols:
            return alias

    # Substring match (alias inside col or col inside alias)
    for alias in aliases:
        for col in norm_cols:
            if alias in col or col in alias:
                return col

    return None


# ── Value parsers ──────────────────────────────────────────────────────────────

def _parse_float(val) -> Optional[float]:
    """Parse a possibly messy numeric cell value."""
    if val is None:
        return None
    if isinstance(val, float) and pd.isna(val):
        return None
    try:
        return float(str(val).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def _parse_bool(val) -> bool:
    """Normalize yes/no/true/false/1/0/Y/N to bool."""
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        if isinstance(val, float) and pd.isna(val):
            return False
        return bool(val)
    s = str(val).strip().lower()
    return s in ("yes", "y", "true", "1", "x", "✓", "available",
                 "in stock", "instock", "in-stock")


def _pf_to_display(pf: float) -> str:
    """Convert a picofarad value to a human-readable string."""
    if pf >= 1_000_000:
        v = pf / 1_000_000
        return f"{v:g}µF"
    if pf >= 1_000:
        v = pf / 1_000
        return f"{v:g}nF"
    return f"{pf:g}pF"


def _parse_capacitance_single(val) -> Tuple[Optional[float], str]:
    """
    Parse a capacitance value from a single cell that may embed the unit,
    e.g. '100nF', '10 µF', '100pF', '0.1uF', '10'.
    Returns (pF_value, display_string).
    """
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None, "N/A"
    s = str(val).strip().lower().replace(" ", "").replace(",", "")

    units = [
        ("uf", 1e6), ("µf", 1e6), ("microf", 1e6),
        ("nf", 1e3),
        ("pf", 1.0),
    ]
    for unit, mult in units:
        if s.endswith(unit):
            try:
                num = float(s[: -len(unit)])
                pf = num * mult
                if pf > 0:
                    return pf, _pf_to_display(pf)
            except ValueError:
                pass

    # Numeric only – assume pF if < 1000, else ambiguous
    try:
        v = float(s)
        if v > 0:
            return v, _pf_to_display(v)
    except ValueError:
        pass

    return None, "N/A"


def _capacitance_to_pf(
    uf_val, nf_val, pf_val, single_val=None
) -> Tuple[Optional[float], str]:
    """
    Resolve capacitance from multiple possible source columns.
    Priority: uF column → nF column → pF column → single embedded-unit column.
    Returns (pF_value, display_string).
    """
    uf = _parse_float(uf_val)
    nf = _parse_float(nf_val)
    pf_raw = _parse_float(pf_val)

    if uf is not None and uf > 0:
        pf = uf * 1e6
        return pf, _pf_to_display(pf)
    if nf is not None and nf > 0:
        pf = nf * 1e3
        return pf, _pf_to_display(pf)
    if pf_raw is not None and pf_raw > 0:
        return pf_raw, _pf_to_display(pf_raw)
    if single_val is not None:
        return _parse_capacitance_single(single_val)

    return None, "N/A"


def _parse_voltage(val) -> Tuple[Optional[float], str]:
    """Parse voltage to (numeric_V, display_str)."""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None, "N/A"
    s = str(val).strip()
    m = re.search(r"[\d.]+", s)
    if not m:
        return None, s or "N/A"
    v = float(m.group())
    return v, f"{v:g}V"


def _parse_tolerance(val) -> Tuple[Optional[float], str]:
    """
    Parse tolerance to (numeric_pct, display_str).
    Handles: '10%', '±10%', 'K' (EIA code), '10', '0.1'.
    """
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None, "N/A"
    s = str(val).strip()

    # Single-letter EIA code
    if len(s) == 1 and s.upper() in EIA_TOLERANCE:
        num = EIA_TOLERANCE[s.upper()]
        return num, f"±{num}%"

    m = re.search(r"[\d.]+", s)
    if not m:
        return None, s or "N/A"
    num = float(m.group())
    # Build display
    if "%" in s:
        disp = s if "±" in s else f"±{num}%"
    else:
        disp = f"±{num}%"
    return num, disp


def _parse_temp(val) -> Optional[float]:
    """Parse temperature string like '-55°C' or '-55' to float."""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    s = (str(val)
         .replace("°C", "").replace("°", "")
         .replace(" C", "").replace("C", "")
         .strip())
    try:
        return float(s)
    except ValueError:
        return None


def _clean_str(val, fallback: str = "N/A") -> str:
    """Return stripped string or fallback for null/nan values."""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return fallback
    s = str(val).strip()
    return s if s.lower() not in ("nan", "none", "") else fallback


def _data_quality(part: NormalizedPart) -> int:
    """Score a part record by field completeness (higher = more data)."""
    score = 0
    if part.capacitance_pf is not None: score += 2
    if part.voltage_v is not None:       score += 2
    if part.dielectric != "N/A":         score += 1
    if part.case_code != "N/A":          score += 2
    if part.tolerance_pct is not None:   score += 1
    if part.temp_min_c is not None:      score += 1
    if part.temp_max_c is not None:      score += 1
    if part.packaging != "N/A":          score += 1
    return score


# ── Row → NormalizedPart ───────────────────────────────────────────────────────

def _row_to_part(
    row: pd.Series,
    col_map: Dict[str, str],
) -> Optional[NormalizedPart]:
    """Convert one DataFrame row to a NormalizedPart using the resolved col_map."""

    def get(field):
        col = col_map.get(field)
        if col is None:
            return None
        v = row.get(col)
        if isinstance(v, float) and pd.isna(v):
            return None
        return v

    # Part number is mandatory
    pn_raw = get("display_pn")
    if not pn_raw:
        return None
    pn = str(pn_raw).strip().upper()
    if pn in ("NAN", "NONE", ""):
        return None

    manufacturer = _clean_str(get("manufacturer"), "Unknown")
    product_category = _clean_str(get("product_category"), "N/A")

    cap_pf, cap_disp = _capacitance_to_pf(
        get("capacitance_uf"),
        get("capacitance_nf"),
        get("capacitance_pf"),
        get("capacitance_any"),
    )

    voltage_v, voltage_disp = _parse_voltage(get("voltage_rating"))
    tolerance_pct, tolerance_disp = _parse_tolerance(get("tolerance"))

    dielectric = _clean_str(get("dielectric"), "N/A").upper()
    if dielectric in ("NAN", "NONE"):
        dielectric = "N/A"

    # Case code: prefer inch, fall back to mm
    case_code = _clean_str(get("case_code_in") or get("case_code_mm"), "N/A")
    if case_code.lower() in ("nan", "none"):
        case_code = "N/A"

    temp_min = _parse_temp(get("temp_min"))
    temp_max = _parse_temp(get("temp_max"))

    packaging = _clean_str(get("packaging"), "N/A")
    if packaging.lower() in ("nan", "none"):
        packaging = "N/A"

    franchised = _parse_bool(get("franchised") or False)
    in_stock = _parse_bool(get("stock") or False)

    # Auto-generate description from key specs
    desc_parts = [
        x for x in [cap_disp, voltage_disp, dielectric, case_code]
        if x and x != "N/A"
    ]
    description = " ".join(desc_parts) if desc_parts else product_category

    return NormalizedPart(
        part_number=pn,
        manufacturer=manufacturer,
        product_category=product_category,
        capacitance_pf=cap_pf,
        capacitance_display=cap_disp,
        voltage_v=voltage_v,
        voltage_display=voltage_disp,
        dielectric=dielectric,
        tolerance_pct=tolerance_pct,
        tolerance_display=tolerance_disp,
        case_code=case_code,
        temp_min_c=temp_min,
        temp_max_c=temp_max,
        packaging=packaging,
        franchised=franchised,
        in_stock=in_stock,
        description=description,
    )


# ── ExcelLoader ────────────────────────────────────────────────────────────────

class ExcelLoader:
    """Loads all sheets from an Excel workbook and returns a unified parts dict."""

    def load(self, path: Path) -> Dict[str, NormalizedPart]:
        """
        Read every sheet, normalize, deduplicate, and merge into one dict
        keyed by uppercase part number.  First-sheet-wins for cross-sheet duplicates.
        """
        logger.info(f"Opening workbook: {path.name}")
        xf = pd.ExcelFile(path, engine="openpyxl")
        all_parts: Dict[str, NormalizedPart] = {}

        for sheet in xf.sheet_names:
            logger.info(f"  Reading sheet: '{sheet}'")
            df = pd.read_excel(xf, sheet_name=sheet)
            sheet_parts = self._process_sheet(df, sheet)
            added = 0
            for pn, part in sheet_parts.items():
                if pn not in all_parts:
                    all_parts[pn] = part
                    added += 1
            logger.info(
                f"  Sheet '{sheet}': {len(sheet_parts)} unique parts "
                f"({added} new, {len(sheet_parts)-added} already present)"
            )

        logger.info(f"Total unique parts loaded: {len(all_parts)}")
        return all_parts

    def _process_sheet(
        self, df: pd.DataFrame, sheet_name: str
    ) -> Dict[str, NormalizedPart]:
        """Normalize one sheet into a part dict."""
        if df.empty:
            logger.warning(f"  Sheet '{sheet_name}' is empty – skipping")
            return {}

        # Normalize column headers
        df = df.copy()
        df.columns = [_normalize_header(c) for c in df.columns]
        norm_cols: List[str] = list(df.columns)

        # Resolve canonical field → actual column name
        col_map: Dict[str, str] = {}
        for field in COLUMN_ALIASES:
            resolved = _find_column(norm_cols, field)
            if resolved:
                col_map[field] = resolved

        if "display_pn" not in col_map:
            logger.warning(
                f"  Sheet '{sheet_name}': could not find a part-number column "
                f"(checked aliases for 'display_pn') – skipping. "
                f"Available columns: {norm_cols[:10]}"
            )
            return {}

        logger.debug(f"  Column map for '{sheet_name}': {col_map}")

        parts: Dict[str, NormalizedPart] = {}
        total_rows = len(df)
        invalid_rows = 0
        dup_count = 0

        for _, row in df.iterrows():
            part = _row_to_part(row, col_map)
            if part is None:
                invalid_rows += 1
                continue
            if part.part_number in parts:
                dup_count += 1
                # Keep record with more populated fields
                if _data_quality(part) > _data_quality(parts[part.part_number]):
                    parts[part.part_number] = part
            else:
                parts[part.part_number] = part

        logger.info(
            f"  Sheet '{sheet_name}' summary: "
            f"{total_rows} total rows | "
            f"{len(parts)} unique parts | "
            f"{dup_count} duplicates merged | "
            f"{invalid_rows} invalid/empty rows skipped"
        )
        return parts
