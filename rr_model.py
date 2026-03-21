"""
rr_model.py
───────────
Counterfactual RR-change model.

Models how much RR a player *would have* gained or lost if they had been at a
different visible rank, assuming the same underlying MMR and match outcome.

Core logic
----------
Valorant's RR system adjusts gains/losses based on the gap between a player's
visible rank and their estimated MMR:

  rank < MMR  →  system is trying to pull rank up:
                 wins give a bonus, losses are softened
  rank = MMR  →  balanced; RR ≈ baseline
  rank > MMR  →  system is trying to push rank down:
                 wins are penalised, losses are amplified

The model first *infers* the neutral baseline RR from the player's current
rank–MMR gap, then *applies* the gap at the hypothetical rank to project what
they would have received there.

The ``sensitivity`` parameter (default 0.7) scales how aggressively the model
responds to rank–MMR gaps. Values above 1.0 are more aggressive; below 1.0
are gentler.
"""

from rank_utils import index_rank


def rank(rank_str: str) -> int:
    """Convert a rank name to its ELO value (tier_index * 100).

    Examples
    --------
    >>> rank("Silver 1")
    600
    >>> rank("Gold 2")
    900
    """
    return index_rank(rank_str) * 100


def predict_rr_change(
    delta_rr_current: int,
    current_visible_rank: int,
    estimated_mmr: int,
    hypothetical_rank: int,
    max_rr: int = 52,
    min_rr: int = 10,
    sensitivity: float = 0.7,
) -> int:
    """
    Predict RR change at ``hypothetical_rank``, assuming the same MMR and
    match outcome as the real game played at ``current_visible_rank``.

    Parameters
    ----------
    delta_rr_current : int
        Actual RR change (positive = win, negative = loss).
    current_visible_rank : int
        Player's visible rank as ELO, e.g. ``rank("Gold 1")`` → 900.
    estimated_mmr : int
        Estimated MMR as ELO.
    hypothetical_rank : int
        The "what-if" visible rank to model.
    max_rr : int
        Maximum absolute RR per game (cap for wins / floor for losses).
    min_rr : int
        Minimum absolute RR per game.
    sensitivity : float
        Scaling aggressiveness for rank–MMR gap effects.

    Returns
    -------
    int
        Predicted RR change, clamped to [min_rr, max_rr] in magnitude.
    """
    if current_visible_rank is None:
        return delta_rr_current

    # Work in normalised tier units
    cur_rank = current_visible_rank / 100
    mmr       = estimated_mmr / 100
    hypo_rank = hypothetical_rank / 100

    current_delta = cur_rank - mmr
    hypo_delta    = hypo_rank - mmr
    is_win        = delta_rr_current > 0

    # ── Step 1: infer neutral baseline from current situation ─────────────────
    if current_delta < 0:          # rank below MMR
        if is_win:
            baseline = delta_rr_current / (1 + abs(current_delta) * 0.15 * sensitivity)
        else:
            baseline = delta_rr_current / (1 - abs(current_delta) * 0.10 * sensitivity)
    elif current_delta > 0:        # rank above MMR
        if is_win:
            baseline = delta_rr_current / (1 - min(current_delta * 0.10 * sensitivity, 0.4))
        else:
            baseline = delta_rr_current / (1 + current_delta * 0.15 * sensitivity)
    else:                          # balanced
        baseline = delta_rr_current

    # ── Step 2: apply hypothetical gap ────────────────────────────────────────
    if hypo_delta < 0:             # hypo rank below MMR
        if is_win:
            predicted = baseline * (1 + abs(hypo_delta) * 0.15 * sensitivity)
        else:
            predicted = baseline * (1 - abs(hypo_delta) * 0.10 * sensitivity)
    elif hypo_delta > 0:           # hypo rank above MMR
        if is_win:
            predicted = baseline * (1 - min(hypo_delta * 0.10 * sensitivity, 0.4))
        else:
            predicted = baseline * (1 + hypo_delta * 0.15 * sensitivity)
    else:                          # balanced
        predicted = baseline

    # ── Clamp ─────────────────────────────────────────────────────────────────
    if is_win:
        predicted = max(min_rr, min(max_rr, predicted))
    else:
        predicted = max(-max_rr, min(-min_rr, predicted))

    return round(predicted)


# ── Quick smoke-test ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("Gold player / Silver MMR — win for +30 RR")
    print("  → As Silver 1:", predict_rr_change(30, rank("Gold 1"), rank("Silver 1"), rank("Silver 1")))
    print("  → As Bronze 1:", predict_rr_change(30, rank("Gold 1"), rank("Silver 1"), rank("Bronze 1")))
    print("  → As Plat 1:  ", predict_rr_change(30, rank("Gold 1"), rank("Silver 1"), rank("Platinum 1")))