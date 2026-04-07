"""
Scoring and matching engine for cross-reference lookups.

Spec result values — 4 levels (visible in the frontend):
  "exact"      – values are numerically / textually identical
  "compatible" – values differ but the relaxed rule says the candidate is acceptable
                 (e.g. tighter tolerance, higher voltage rating, wider temp range)
  "no"         – candidate fails the rule; this spec is a mismatch
  "na"         – data missing on master or candidate; spec cannot be evaluated

Scoring model (weighted per spec result):
  each spec contributes: exact=1.0pt  compatible=0.5pt  no=0.0pt  na=excluded

  essential_score = (sum of essential spec points / essential_total) × 100
  optional_score  = (sum of optional  spec points / optional_total)  × 100
  overall_score   = essential_score × 0.70 + optional_score × 0.30
  adjusted_score  = min(100, overall_score + franchised_bonus + stock_boost)

Example — 4 essential specs, 2 exact + 2 compatible:
  points = 2×1.0 + 2×0.5 = 3.0 / 4 total = 75%  (not 100%)

Essential specs  (capacitance, voltage, dielectric, caseCode) – 70%
Optional specs   (tolerance, tempMin, tempMax, packaging)      – 30%
"""

import logging
from typing import Dict, List, Optional

from services.models import NormalizedPart, to_frontend_part

logger = logging.getLogger(__name__)

ESSENTIAL_FIELDS = ["capacitance", "voltage", "dielectric", "caseCode"]
OPTIONAL_FIELDS  = ["tolerance",   "tempMin",  "tempMax",   "packaging"]

ESSENTIAL_WEIGHT   = 0.70
OPTIONAL_WEIGHT    = 0.30
FRANCHISED_BONUS   = 3.0   # bonus points when candidate is franchised
STOCK_BOOST        = 2.0   # bonus when prefer_franchised=True and part is in stock

# Per-result score contribution (out of 1.0 per spec slot)
EXACT_POINTS      = 1.0   # identical value
COMPATIBLE_POINTS = 0.5   # acceptable but different value
NO_POINTS         = 0.0   # fails the rule

# A result is a "match" (counted in essMatches/optMatches display) if not "no"/"na"
_IS_MATCH = {"exact", "compatible"}


def _spec_points(result: str) -> float:
    """Return the score contribution (0–1) for a single spec result."""
    if result == "exact":      return EXACT_POINTS
    if result == "compatible": return COMPATIBLE_POINTS
    return NO_POINTS  # "no" or "na"


# ── Individual spec matchers ───────────────────────────────────────────────────
# Each returns one of: "exact" | "compatible" | "no" | "na"

def _match_capacitance(m: Optional[float], c: Optional[float]) -> str:
    """
    exact      – difference < 0.1 % (effectively same value)
    compatible – difference ≤ 5 %  (within accepted engineering tolerance)
    no         – difference > 5 %
    """
    if m is None or c is None or m == 0:
        return "na"
    diff_pct = abs(m - c) / m
    if diff_pct < 0.001:
        return "exact"
    if diff_pct <= 0.05:
        return "compatible"
    return "no"


def _match_voltage(m: Optional[float], c: Optional[float]) -> str:
    """
    exact      – same voltage rating
    compatible – candidate is rated higher (safe drop-in)
    no         – candidate rated lower (not safe)
    """
    if m is None or c is None:
        return "na"
    if abs(m - c) < 0.01:
        return "exact"
    if c > m:
        return "compatible"
    return "no"


def _match_exact_str(m: str, c: str) -> str:
    """
    Strings must match exactly (case-insensitive).
    Used for: dielectric, case_code, packaging.
    No "compatible" concept for pure text fields.
    """
    if not m or m == "N/A" or not c or c == "N/A":
        return "na"
    return "exact" if m.upper() == c.upper() else "no"


def _match_tolerance(m: Optional[float], c: Optional[float]) -> str:
    """
    exact      – same tolerance percentage
    compatible – candidate is tighter (stricter spec; acceptable drop-in)
    no         – candidate is looser (wider tolerance; not acceptable)
    """
    if m is None or c is None:
        return "na"
    if abs(m - c) < 0.01:
        return "exact"
    if c < m:                # tighter → acceptable
        return "compatible"
    return "no"


def _match_temp_min(m: Optional[float], c: Optional[float]) -> str:
    """
    exact      – same minimum operating temperature
    compatible – candidate goes lower (wider range; acceptable)
    no         – candidate minimum is higher (narrower range; not acceptable)
    """
    if m is None or c is None:
        return "na"
    if abs(m - c) < 0.1:
        return "exact"
    if c < m:                # lower min → wider range → acceptable
        return "compatible"
    return "no"


def _match_temp_max(m: Optional[float], c: Optional[float]) -> str:
    """
    exact      – same maximum operating temperature
    compatible – candidate goes higher (wider range; acceptable)
    no         – candidate maximum is lower (narrower range; not acceptable)
    """
    if m is None or c is None:
        return "na"
    if abs(m - c) < 0.1:
        return "exact"
    if c > m:                # higher max → wider range → acceptable
        return "compatible"
    return "no"


# ── Core scorer ────────────────────────────────────────────────────────────────

def score_candidate(
    master: NormalizedPart,
    candidate: NormalizedPart,
    prefer_franchised: bool = False,
) -> dict:
    """
    Compute a full score breakdown comparing candidate against master.
    Returns a dict shaped for the frontend `score` object.
    specResults values are "exact" | "compatible" | "no" | "na".
    Both "exact" and "compatible" count as match points for scoring.
    """
    spec_results: Dict[str, str] = {
        "capacitance": _match_capacitance(master.capacitance_pf,  candidate.capacitance_pf),
        "voltage":     _match_voltage(master.voltage_v,           candidate.voltage_v),
        "dielectric":  _match_exact_str(master.dielectric,        candidate.dielectric),
        "caseCode":    _match_exact_str(master.case_code,         candidate.case_code),
        "tolerance":   _match_tolerance(master.tolerance_pct,     candidate.tolerance_pct),
        "tempMin":     _match_temp_min(master.temp_min_c,         candidate.temp_min_c),
        "tempMax":     _match_temp_max(master.temp_max_c,         candidate.temp_max_c),
        "packaging":   _match_exact_str(master.packaging,         candidate.packaging),
    }

    # ── Weighted scoring: exact=1.0pt · compatible=0.5pt · no=0.0pt · na=excluded ──

    # Essential specs
    ess_exact      = sum(1 for f in ESSENTIAL_FIELDS if spec_results[f] == "exact")
    ess_compatible = sum(1 for f in ESSENTIAL_FIELDS if spec_results[f] == "compatible")
    ess_matches    = ess_exact + ess_compatible          # count passing specs (for display)
    ess_total      = sum(1 for f in ESSENTIAL_FIELDS if spec_results[f] != "na")
    ess_points     = (ess_exact * EXACT_POINTS) + (ess_compatible * COMPATIBLE_POINTS)
    ess_score      = (ess_points / ess_total * 100) if ess_total > 0 else 0.0

    # Optional specs
    opt_exact      = sum(1 for f in OPTIONAL_FIELDS if spec_results[f] == "exact")
    opt_compatible = sum(1 for f in OPTIONAL_FIELDS if spec_results[f] == "compatible")
    opt_matches    = opt_exact + opt_compatible
    opt_total      = sum(1 for f in OPTIONAL_FIELDS if spec_results[f] != "na")
    opt_points     = (opt_exact * EXACT_POINTS) + (opt_compatible * COMPATIBLE_POINTS)
    opt_score      = (opt_points / opt_total * 100) if opt_total > 0 else 0.0

    overall_score  = ess_score * ESSENTIAL_WEIGHT + opt_score * OPTIONAL_WEIGHT
    bonus          = FRANCHISED_BONUS if candidate.franchised else 0.0
    stock_boost    = STOCK_BOOST if (prefer_franchised and candidate.in_stock) else 0.0
    adjusted_score = min(100.0, overall_score + bonus + stock_boost)

    matched     = [f for f in ESSENTIAL_FIELDS + OPTIONAL_FIELDS if spec_results[f] in _IS_MATCH]
    mismatched  = [f for f in ESSENTIAL_FIELDS + OPTIONAL_FIELDS if spec_results[f] == "no"]
    unavailable = [f for f in ESSENTIAL_FIELDS + OPTIONAL_FIELDS if spec_results[f] == "na"]

    # Human-readable reason
    reasons: List[str] = []
    if ess_total > 0:
        breakdown_parts = []
        if ess_exact:      breakdown_parts.append(f"{ess_exact} exact")
        if ess_compatible: breakdown_parts.append(f"{ess_compatible} compatible×0.5")
        reasons.append(
            f"Essential: {ess_matches}/{ess_total} pass "
            f"({', '.join(breakdown_parts)}) = {round(ess_score, 1)}%"
        )
    if opt_total > 0:
        breakdown_parts = []
        if opt_exact:      breakdown_parts.append(f"{opt_exact} exact")
        if opt_compatible: breakdown_parts.append(f"{opt_compatible} compatible×0.5")
        reasons.append(
            f"Optional: {opt_matches}/{opt_total} pass "
            f"({', '.join(breakdown_parts)}) = {round(opt_score, 1)}%"
        )
    if candidate.franchised:
        reasons.append("franchised bonus +3pts")
    if stock_boost > 0:
        reasons.append(f"in-stock boost +{stock_boost:.0f}pts")
    if mismatched:
        reasons.append(f"mismatches: {', '.join(mismatched)}")
    why = "; ".join(reasons) if reasons else "scored by available specs"

    return {
        "essScore":          round(ess_score, 2),
        "optScore":          round(opt_score, 2),
        "overallScore":      round(overall_score, 2),
        "adjustedScore":     round(adjusted_score, 2),
        "essMatches":        ess_matches,
        "essTotal":          ess_total,
        "essExact":          ess_exact,
        "essCompatible":     ess_compatible,
        "optMatches":        opt_matches,
        "optTotal":          opt_total,
        "optExact":          opt_exact,
        "optCompatible":     opt_compatible,
        "specResults":       spec_results,
        "matched_specs":     matched,
        "mismatched_specs":  mismatched,
        "unavailable_specs": unavailable,
        "why_ranked_here":   why,
    }


# ── Matcher ────────────────────────────────────────────────────────────────────

class Matcher:
    """Finds and ranks candidate parts against a master part."""

    def find_matches(
        self,
        master: NormalizedPart,
        all_parts: Dict[str, NormalizedPart],
        max_results: int = 10,
        in_stock_only: bool = False,
        prefer_franchised: bool = False,
    ) -> List[dict]:
        """
        Score all eligible candidates and return the top max_results
        as {pn, part, score} dicts shaped for the frontend.

        Candidate eligibility:
          - Must not be the master part itself
          - Must share the same product_category
          - Must be in stock when in_stock_only=True
        """
        candidates = []

        for pn, part in all_parts.items():
            if pn == master.part_number:
                continue

            same_cat = (
                part.product_category.strip().lower()
                == master.product_category.strip().lower()
                and master.product_category not in ("N/A", "")
            )
            if not same_cat:
                continue

            if in_stock_only and not part.in_stock:
                continue

            sc = score_candidate(master, part, prefer_franchised=prefer_franchised)
            candidates.append({"pn": pn, "_part": part, "score": sc})

        # Primary sort: adjusted score desc; tiebreak: franchised first, in-stock first
        candidates.sort(
            key=lambda x: (
                -x["score"]["adjustedScore"],
                0 if x["_part"].franchised else 1,
                0 if x["_part"].in_stock else 1,
            )
        )

        logger.debug(
            f"find_matches('{master.part_number}'): "
            f"{len(candidates)} candidates → top {max_results}"
        )

        return [
            {
                "pn":    item["pn"],
                "part":  to_frontend_part(item["_part"]),
                "score": item["score"],
            }
            for item in candidates[:max_results]
        ]
