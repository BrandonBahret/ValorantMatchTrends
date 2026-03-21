"""
mmr_spline.py
─────────────
Spline-based MMR (lobby average rank) prediction.

Given a history of per-match lobby averages, fits a smoothing spline and
evaluates it at a requested match index. This is used to estimate a player's
true MMR from the ranks of the lobbies they were placed into.

Smoothing strategy
------------------
- Smoothing factor s = n * var(y) * 0.75  (data-driven, variance-proportional)
- Spline degree k is remapped from match count: few matches → k=1 (linear),
  many matches → k=3 (cubic), clamped to [1, 3].
- Prediction is clamped to [min_observed, max_observed] to avoid extrapolation
  artefacts at the edges.
"""

import numpy as np
from scipy.interpolate import UnivariateSpline
from typing import Dict, List, Optional


# ── Math primitives ───────────────────────────────────────────────────────────

def lerp(a: float, b: float, t: float) -> float:
    """Linear interpolation from *a* to *b* by factor *t*."""
    return a + (b - a) * t


def ilerp(a: float, b: float, v: float) -> float:
    """Inverse lerp: return where *v* falls between *a* and *b*."""
    return (v - a) / (b - a) if b != a else 0.0


def remap(v: float, in_min: float, in_max: float, out_min: float, out_max: float) -> float:
    """Remap *v* from [in_min, in_max] to [out_min, out_max]."""
    return lerp(out_min, out_max, ilerp(in_min, in_max, v))


def clamp(v: float, min_val: float, max_val: float) -> float:
    """Clamp *v* to [min_val, max_val]."""
    return max(min_val, min(max_val, v))


# ── Spline prediction ─────────────────────────────────────────────────────────

def predict_mmr(
    rank_averages_lists: Dict[str, List[Optional[float]]],
    match_index: int,
) -> Optional[float]:
    """
    Predict the lobby-average rank (proxy for player MMR) at *match_index*
    using a smoothing spline fit to historical lobby averages.

    Parameters
    ----------
    rank_averages_lists : dict
        Must contain a ``'lobby_avg_rank'`` key with one float per match.
        ``None`` values are silently ignored.
    match_index : int
        1-based index at which to evaluate the spline.

    Returns
    -------
    float | None
        Predicted rank value, or ``None`` if there is insufficient data
        (fewer than 4 non-null observations).

    Notes
    -----
    The prediction is clamped to the observed [min, max] range to prevent
    runaway extrapolation near the edges of the data window.
    """
    lobby_values = np.array(
        rank_averages_lists.get("lobby_avg_rank", []), dtype=float
    )

    if len(lobby_values) == 0:
        return None

    max_rank = np.max(lobby_values)
    min_rank = np.min(lobby_values)
    mask = ~np.isnan(lobby_values)
    n = len(lobby_values)

    if mask.sum() < 4:
        return None

    x_clean = np.arange(1, n + 1)[mask]
    y_clean = lobby_values[mask]

    s = len(y_clean) * np.var(y_clean) * 0.75
    try:
        k = int(clamp(remap(n, 0, 20, 1, 3), 1, 3))
        spline = UnivariateSpline(x_clean, y_clean, s=s, k=k)
        return float(clamp(float(spline(match_index)), min_rank, max_rank))
    except Exception:
        return None