"""
Internal data models and API request/response schemas.
"""

from dataclasses import dataclass
from typing import Optional, List, Dict
from pydantic import BaseModel


@dataclass
class NormalizedPart:
    """Normalized part record loaded from Excel (internal use)."""
    part_number: str              # uppercase, trimmed display_pn
    manufacturer: str
    product_category: str
    # Capacitance
    capacitance_pf: Optional[float]    # picofarads – used for numeric comparison
    capacitance_display: str           # human-readable e.g. "100nF", "10µF"
    # Voltage
    voltage_v: Optional[float]         # volts – used for numeric comparison
    voltage_display: str               # e.g. "10V"
    # Other specs
    dielectric: str                    # normalized uppercase e.g. "X5R"
    tolerance_pct: Optional[float]     # numeric e.g. 10.0 for ±10%
    tolerance_display: str             # e.g. "±10%"
    case_code: str                     # e.g. "0402"
    temp_min_c: Optional[float]        # °C
    temp_max_c: Optional[float]        # °C
    packaging: str
    franchised: bool
    in_stock: bool
    description: str


def to_frontend_part(part: NormalizedPart) -> dict:
    """
    Shape a NormalizedPart into the camelCase dict the frontend expects.
    Matches the old PARTS_DB entry structure exactly.
    """
    temp_min_s = f"{part.temp_min_c:.0f}°C" if part.temp_min_c is not None else "N/A"
    temp_max_s = f"{part.temp_max_c:.0f}°C" if part.temp_max_c is not None else "N/A"
    return {
        "manufacturer": part.manufacturer,
        "type":         part.product_category,
        "capacitance":  part.capacitance_display,
        "voltage":      part.voltage_display,
        "dielectric":   part.dielectric,
        "tolerance":    part.tolerance_display,
        "caseCode":     part.case_code,
        "tempMin":      temp_min_s,
        "tempMax":      temp_max_s,
        "packaging":    part.packaging,
        "stock":        part.in_stock,
        "franchised":   part.franchised,
        "description":  part.description,
    }


# ── API Request Models ─────────────────────────────────────────────────────────

class SearchSingleRequest(BaseModel):
    part_number: str
    max_results: int = 10
    in_stock_only: bool = False
    prefer_franchised: bool = False


class BulkJsonRequest(BaseModel):
    parts: List[str]          # list of part numbers
    max_results: int = 5
    in_stock_only: bool = False
    prefer_franchised: bool = False
