"""
ledger.py
═════════
Builds the per-match RR ledger from MMR history and computes counterfactual
RR paths (no-shields and no-buffer variants).
"""

from typing import Dict, List, Optional

from constants import SPLICE_THRESHOLD_RR, BIG_LOSS_THRESHOLD_RR, RR_DESYNC_THRESHOLD
from rr_model import predict_rr_change


# ─────────────────────────────────────────────────────────────────────────────
#  ELO look-up helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_last_known_elo(match_id: str, recent_mmr: dict,
                        recent_previous_match: dict) -> Optional[int]:
    """Recursively walk back through match history to find the last non-zero ELO."""
    last_elo = recent_mmr[match_id].elo
    if last_elo != 0:
        return last_elo
    prev_id = recent_previous_match[match_id]
    if prev_id is None:
        return None
    return get_last_known_elo(prev_id, recent_mmr, recent_previous_match)


# ─────────────────────────────────────────────────────────────────────────────
#  Placement region helpers
# ─────────────────────────────────────────────────────────────────────────────

def calculate_placement_regions(recent_matches, recent_is_placement: dict,
                                  recent_is_newly_placed: dict,
                                  reverse: bool = False, offset: int = 0,
                                  matches_selection=None) -> List:
    """
    Return a list of (start, end) index pairs marking contiguous placement-match
    windows.  Pass *matches_selection* to restrict the computation to a subset.
    """
    if matches_selection is not None:
        placements   = [recent_is_placement[g.metadata.match_id]    for g in matches_selection]
        newly_placed = [recent_is_newly_placed[g.metadata.match_id] for g in matches_selection]
    else:
        placements   = list(recent_is_placement.values())
        newly_placed = list(recent_is_newly_placed.values())
    placements = [a or b for a, b in zip(placements, newly_placed)]
    ranges, in_block, start = [], False, None
    for i, is_p in enumerate(placements):
        if is_p and not in_block:
            in_block, start = True, i
        elif not is_p and in_block:
            ranges.append((start, i - 1))
            in_block = False
    if in_block:
        ranges.append((start, len(placements) - 1))
    if reverse:
        n = len(placements)
        ranges = [(n - 1 - e, n - 1 - s) for s, e in ranges]
    return [(max(0, s + offset), e + offset) for s, e in ranges]


# ─────────────────────────────────────────────────────────────────────────────
#  Ledger construction
# ─────────────────────────────────────────────────────────────────────────────

def build_match_ledger(recent_mmr: dict, recent_is_placement: dict,
                        recent_is_newly_placed: dict,
                        recent_previous_match: dict,
                        predicted_matches_mmr: dict) -> Dict:
    """
    Build a dict keyed by match_id with per-match RR accounting fields including
    shield/buffer/splice/desync detection.
    """
    ledger = {}
    matches = list(reversed(recent_mmr.items()))
    prev_elo_after = prev_tier_after = None

    for match_id, match in matches:
        elo_after   = match.elo
        tier_after  = match.tier.name
        elo_before  = prev_elo_after
        tier_before = prev_tier_after

        if recent_is_placement[match_id]:
            elo_before = get_last_known_elo(match_id, recent_mmr, recent_previous_match)
            if elo_after == 0:
                elo_after = get_last_known_elo(match_id, recent_mmr, recent_previous_match)

        rr_supposed_take = match.last_mmr_change
        rr_actual_taken  = None
        shield = buffer = splice = big_loss = desync = False

        if elo_before is not None:
            rr_actual_taken = elo_after - elo_before
            if abs(rr_actual_taken) > SPLICE_THRESHOLD_RR:
                splice           = True
                rr_supposed_take = rr_actual_taken
                elo_before = tier_before = rr_actual_taken = None
                prev_elo_after  = elo_after
                prev_tier_after = tier_after
            else:
                if match.last_mmr_change <= -BIG_LOSS_THRESHOLD_RR:
                    big_loss = True
                if (match.last_mmr_change < 0
                        and elo_after % 100 == 0
                        and elo_before % 100 != 0
                        and match.last_mmr_change != rr_actual_taken):
                    buffer = True
                if match.last_mmr_change < 0 and elo_after == elo_before:
                    shield = True
                if match.last_mmr_change is not None:
                    delta = abs(rr_actual_taken - match.last_mmr_change)
                    if delta >= RR_DESYNC_THRESHOLD:
                        is_rankup = tier_after != tier_before and elo_after % 100 == 10
                        if not (is_rankup or shield or buffer):
                            desync = True
                prev_elo_after  = elo_after
                prev_tier_after = tier_after
        else:
            prev_elo_after  = elo_after
            prev_tier_after = tier_after

        prev_mid = recent_previous_match[match_id]
        is_placement = recent_is_placement[match_id]
        is_newly_placed = (
            recent_is_placement[prev_mid] and not recent_is_placement[match_id]
            if prev_mid is not None else False
        )
        ledger[match.match_id] = {
            "elo_before":           elo_before,
            "elo_after":            elo_after,
            "tier_before":          tier_before,
            "tier_after":           tier_after,
            "rr_supposed_to_take":  rr_supposed_take,
            "rr_actual_taken":      rr_actual_taken,
            "shield_used":          shield,
            "buffer_used":          buffer,
            "is_splice":            splice,
            "big_loss":             big_loss,
            "rr_desync":            desync and not is_newly_placed,
            "datecode":             match.datetime,
            "map":                  match.map.name,
            "match_data":           match,
            "is_placement":         is_placement or is_newly_placed,
            "is_newly_placed_rank": is_newly_placed,
            "predicted_mmr":        predicted_matches_mmr[match.match_id] * 100,
        }
    return ledger


# ─────────────────────────────────────────────────────────────────────────────
#  Counterfactual path computation
# ─────────────────────────────────────────────────────────────────────────────

def compute_counterfactual_path(ledger_entries: list, recent_is_placement: dict,
                                  predicted_matches_mmr: dict,
                                  model_adj: bool = False) -> List:
    """
    Replay RR deltas without shield/buffer corrections.
    With *model_adj=True*, each RR delta is additionally adjusted by the
    predict_rr_change model before replaying.
    """
    first = next(e for e in ledger_entries if e["elo_before"] is not None)
    cur = first["elo_before"]
    result = []
    for e in ledger_entries:
        mid    = e["match_data"].match_id
        rr     = e["rr_supposed_to_take"] or 0
        is_new = e["is_newly_placed_rank"]
        if model_adj and not recent_is_placement[mid]:
            rr = predict_rr_change(rr, e["elo_before"], predicted_matches_mmr[mid] * 100, cur)
        if rr < 0 and not e["is_splice"]:
            rem = cur % 100
            rr  = max(rr, -rem if rem != 0 else rr)
        nxt = cur + rr
        if nxt // 100 > cur // 100:
            rem = nxt % 100
            if rem < 10:
                nxt += 10 - rem
        if is_new or recent_is_placement[mid]:
            nxt = e["elo_after"] if e["elo_after"] is not None else cur
        else:
            nxt = min(nxt, e["elo_after"])
        result.append(nxt)
        cur = nxt
    return result


def compute_counterfactual_nobuffer_path(ledger_entries: list, recent_is_placement: dict,
                                           predicted_matches_mmr: dict,
                                           model_adj: bool = False) -> List:
    """
    Like ``compute_counterfactual_path`` but also removes the buffer floor
    (i.e., allows RR to go below 0 within a tier).
    """
    first = next(e for e in ledger_entries if e["elo_before"] is not None)
    cur = first["elo_before"]
    result = []
    for e in ledger_entries:
        mid    = e["match_data"].match_id
        rr     = e["rr_supposed_to_take"] or 0
        is_new = e["is_newly_placed_rank"]
        if model_adj and not recent_is_placement[mid]:
            rr = predict_rr_change(rr, e["elo_before"], predicted_matches_mmr[mid] * 100, cur)
        nxt = cur + rr
        if is_new or recent_is_placement[mid]:
            nxt = e["elo_after"] if e["elo_after"] is not None else cur
        else:
            nxt = min(nxt, e["elo_after"])
        result.append(nxt)
        cur = nxt
    return result