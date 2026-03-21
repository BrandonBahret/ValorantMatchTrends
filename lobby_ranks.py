"""
lobby_ranks.py
══════════════
Helpers for computing average lobby ranks from a match, with optional
spline-based MMR look-up for unrated players.
"""

from typing import Dict, List, Optional, TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from db_valorant import ValorantDB
    from api_henrik import Match
    from match_history_processor import MatchHistoryProcessor

from mmr_spline import predict_mmr


def calculate_average_ranks_basic(match: "Match", exclude_puuid: str) -> Dict:
    """
    Compute ally / opponent / lobby rank averages for a single match using
    only the visible tier fields (no DB look-up for unrated players).
    """
    excluded = next((p for p in match.players if p.puuid == exclude_puuid), None)
    if not excluded:
        raise ValueError(f"Player {exclude_puuid} not found in match")
    ally_team = excluded.team_id
    ally, opp, lobby = [], [], []
    for p in match.players:
        if p.puuid == exclude_puuid:
            continue
        if p.currenttier is None or p.currenttier < 3:
            continue
        if p.currenttier_patched == "Unrated":
            continue
        r = p.currenttier - 3
        lobby.append(r)
        (ally if p.team_id == ally_team else opp).append(r)
    return {
        "allies_avg_rank":    np.average(ally)  if ally  else None,
        "opponents_avg_rank": np.average(opp)   if opp   else None,
        "lobby_avg_rank":     np.average(lobby) if lobby else None,
    }


def get_last_known_elo_from_puuid_spline(puuid: str, db: "ValorantDB",
                                          acts_of_interest) -> Optional[float]:
    """
    Estimate a player's current ELO via a spline fitted to their most-recent
    matches.  Used when a player shows as 'Unrated' in the lobby.
    """
    from rank_utils import valorant_act_in_range
    from match_history_processor import MatchHistoryProcessor

    recent_acts = valorant_act_in_range(acts_of_interest[-1], 3)[1:]
    try:
        h = MatchHistoryProcessor(puuid, recent_acts, db, match_count_max=5, timespan=120)
    except Exception:
        return None
    if not h.recent_matches:
        return None
    avgs = gather_rank_average_lists(h.recent_matches, puuid, db, acts_of_interest)
    average_value = np.average(avgs["lobby_avg_rank"])
    spline_value  = predict_mmr(avgs, 0) or average_value
    return float(np.average([spline_value, average_value]))


def calculate_average_ranks_spline(match: "Match", exclude_puuid: str,
                                    db: "ValorantDB", acts_of_interest,
                                    keep_lists: bool = False) -> Dict:
    """
    Like ``calculate_average_ranks_basic`` but looks up unrated players via
    the spline method when they make up more than 40 % of the lobby.
    When *keep_lists* is True the dict values are (average, raw_list) tuples.
    """
    excluded = next((p for p in match.players if p.puuid == exclude_puuid), None)
    if not excluded:
        raise ValueError(f"Player {exclude_puuid} not found in match")
    ally_team = excluded.team_id
    ally, opp, lobby = [], [], []
    unrated_count = sum(1 for p in match.players if p.currenttier_patched == "Unrated")
    lookup_unrated = (unrated_count / 9) > 0.4
    for p in match.players:
        if p.puuid == exclude_puuid:
            continue
        r = None
        if p.currenttier_patched == "Unrated" and lookup_unrated:
            r = get_last_known_elo_from_puuid_spline(p.puuid, db, acts_of_interest)
        elif p.currenttier_patched != "Unrated":
            r = p.currenttier - 3
        if r is None:
            continue
        lobby.append(r)
        (ally if p.team_id == ally_team else opp).append(r)
    if not keep_lists:
        return {
            "allies_avg_rank":    np.average(ally)  if ally  else None,
            "opponents_avg_rank": np.average(opp)   if opp   else None,
            "lobby_avg_rank":     np.average(lobby) if lobby else None,
            "lobby_std":          np.std(lobby)     if lobby else None,
        }
    return {
        "allies_avg_rank":    (np.average(ally),  ally)  if ally  else None,
        "opponents_avg_rank": (np.average(opp),   opp)   if opp   else None,
        "lobby_avg_rank":     (np.average(lobby), lobby) if lobby else None,
        "lobby_std":          (np.std(lobby),     lobby) if lobby else None,
    }


def gather_rank_average_lists(match_history: list, excluded_puuid: str,
                               db: "ValorantDB", acts_of_interest) -> Dict:
    """
    Run ``calculate_average_ranks_spline`` for every match in *match_history*
    and pivot the results into per-field lists.
    """
    results = [
        calculate_average_ranks_spline(m, excluded_puuid, db, acts_of_interest)
        for m in match_history
    ]
    fields = results[0].keys()
    return {
        field: ([r[field] for r in results if r[field] is not None] or None)
        for field in fields
    }