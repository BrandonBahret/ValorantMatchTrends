"""
rank_utils.py
─────────────
Core Valorant rank constants and conversion utilities.

Ranks are represented as floats throughout this codebase:
  integer part  → tier index (0 = Iron 1, 24 = Radiant)
  decimal part  → RR within that tier  (0.34 = 34 RR)

Example: 9.234 → Gold 1 23 RR
"""

import math
from typing import List, Optional, Tuple

# ── Rank table ────────────────────────────────────────────────────────────────

VALORANT_RANKS: List[str] = [
    "Iron 1",     "Iron 2",     "Iron 3",
    "Bronze 1",   "Bronze 2",   "Bronze 3",
    "Silver 1",   "Silver 2",   "Silver 3",
    "Gold 1",     "Gold 2",     "Gold 3",
    "Platinum 1", "Platinum 2", "Platinum 3",
    "Diamond 1",  "Diamond 2",  "Diamond 3",
    "Ascendant 1","Ascendant 2","Ascendant 3",
    "Immortal 1", "Immortal 2", "Immortal 3",
    "Radiant",
]

# (elo_value, rank_name) — each tier spans 100 ELO
TIER_THRESHOLDS: List[Tuple[int, str]] = [
    (idx * 100, name) for idx, name in enumerate(VALORANT_RANKS)
]

# ── Lookups ───────────────────────────────────────────────────────────────────

def lookup_rank(index: int) -> str:
    """Return the rank name for a tier index, clamped to [0, 24]."""
    return VALORANT_RANKS[max(0, min(24, index))]


def index_rank(rank_str: str) -> int:
    """Return the 0-based tier index for a rank name string."""
    return VALORANT_RANKS.index(rank_str)

def map_rank_to_float(rank):   
    if rank in ("Unrated", "Unknown", "Unranked"):
        return None
    
    # Check if the given rank is valid
    if rank not in VALORANT_RANKS:
        raise ValueError(f"Invalid Valorant rank {rank}")

    rank_value = VALORANT_RANKS.index(rank)
    return rank_value

def reverse_map_valorant_rank(rank_value: Optional[float], include_rr: bool = True) -> Optional[str]:
    """
    Convert a rank float to a human-readable string.

    Parameters
    ----------
    rank_value : float
        Tier index with fractional RR, e.g. 9.234.
    include_rr : bool
        If True, appends the RR value (e.g. "Gold 1 23 RR").
        If False, returns only the rank name (e.g. "Gold 1").

    Returns
    -------
    str | None
        Formatted rank string, or None if rank_value is None.

    Examples
    --------
    >>> map_rank_value(9.234)
    'Gold 1 23 RR'
    >>> map_rank_value(9.234, include_rr=False)
    'Gold 1'
    """    
    return map_rank_value(rank_value, include_rr)
    

def map_rank_value(rank_value: Optional[float], include_rr: bool = True) -> Optional[str]:
    """
    Convert a rank float to a human-readable string.

    Parameters
    ----------
    rank_value : float
        Tier index with fractional RR, e.g. 9.234.
    include_rr : bool
        If True, appends the RR value (e.g. "Gold 1 23 RR").
        If False, returns only the rank name (e.g. "Gold 1").

    Returns
    -------
    str | None
        Formatted rank string, or None if rank_value is None.

    Examples
    --------
    >>> map_rank_value(9.234)
    'Gold 1 23 RR'
    >>> map_rank_value(9.234, include_rr=False)
    'Gold 1'
    """
    if rank_value is None:
        return None
    if rank_value < 0:
        raise ValueError("rank_value must be non-negative")

    tier_index = int(math.floor(rank_value))
    rr = max(0, min(99, int((rank_value - tier_index) * 100)))
    rank_name = lookup_rank(tier_index)

    return f"{rank_name} {rr} RR" if include_rr else rank_name

# ── Act range helper ──────────────────────────────────────────────────────────

def valorant_act_in_range(range_start_act: Tuple[str, str], n: int) -> List[Tuple[str, str]]:
    """
    Return ``n`` consecutive (episode, act) tuples going backwards from
    ``range_start_act`` (inclusive).

    Episodes 1–9 have 3 acts each; episodes 10+ have 6 acts each.

    Parameters
    ----------
    range_start_act : tuple[str, str]
        Starting point, e.g. ``('10', '6')`` for E10A6.
    n : int
        Number of acts to include.

    Returns
    -------
    list[tuple[str, str]]

    Examples
    --------
    >>> valorant_act_in_range(('12', '1'), 5)
    [('12', '1'), ('11', '6'), ('11', '5'), ('11', '4'), ('11', '3')]
    """
    episode = int(range_start_act[0])
    act = int(range_start_act[1])
    result = []

    def max_acts(ep: int) -> int:
        return 6 if ep >= 10 else 3

    while len(result) < n and episode > 0:
        result.append((str(episode), str(act)))
        act -= 1
        if act == 0:
            episode -= 1
            if episode > 0:
                act = max_acts(episode)

    return result