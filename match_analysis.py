"""
match_analysis.py
─────────────────
Utility functions for analysing a batch of Valorant matches.

Covers:
  - Win-rate calculation
  - Lobby balance statistics (most/least balanced matches)
  - Sorting and slicing helpers
  - Change-index detection (used by the rank-trend plotter)
"""

from typing import Any, Dict, List, Optional, Tuple


# ── Win rate ──────────────────────────────────────────────────────────────────

def calculate_winrate(matches: list, player_puuid: str) -> Dict[str, Any]:
    """
    Calculate a player's win rate across a list of matches.

    Draw matches are excluded from the calculation. Matches where the player
    cannot be found are silently skipped.

    Parameters
    ----------
    matches : list[Match]
        List of Match objects.
    player_puuid : str
        PUUID of the player to evaluate.

    Returns
    -------
    dict with keys ``wins``, ``losses``, ``games``, ``winrate``.
    """
    wins = losses = 0

    for game in matches:
        try:
            blue_won = game.teams.blue.has_won
            red_won  = game.teams.red.has_won

            if not blue_won and not red_won:   # draw
                continue

            player = next(p for p in game.players if p.puuid == player_puuid)

            if   player.team_id == "Blue": won = blue_won
            elif player.team_id == "Red":  won = red_won
            else: continue

            if won: wins += 1
            else:   losses += 1
        except (StopIteration, AttributeError):
            continue

    games = wins + losses
    return {
        "wins":    wins,
        "losses":  losses,
        "games":   games,
        "winrate": wins / games if games > 0 else 0.0,
    }


# ── Balance analysis ──────────────────────────────────────────────────────────

def analyze_match_history(data: Dict[str, List[float]]) -> Dict[str, Any]:
    """
    Summarise a match-history rank-averages dict.

    Expected keys in *data*:
      ``allies_avg_rank``, ``opponents_avg_rank``,
      ``lobby_avg_rank``, ``lobby_std``

    Returns a dict with:
      - Per-field min/max (value, index) pairs
      - Ally–opponent signed delta min/max
      - Most/least balanced matches by lobby_std
    """
    result: Dict[str, Any] = {}

    # Per-field min / max
    for key, values in data.items():
        min_idx = min(range(len(values)), key=values.__getitem__)
        max_idx = max(range(len(values)), key=values.__getitem__)
        result[key] = {
            "min": (values[min_idx], min_idx),
            "max": (values[max_idx], max_idx),
        }

    # Ally vs opponent delta
    allies    = data["allies_avg_rank"]
    opponents = data["opponents_avg_rank"]
    deltas    = [opponents[i] - allies[i] for i in range(len(allies))]

    min_d = min(range(len(deltas)), key=deltas.__getitem__)
    max_d = max(range(len(deltas)), key=deltas.__getitem__)
    result["ally_opponent_delta"] = {
        "min": (deltas[min_d], min_d),
        "max": (deltas[max_d], max_d),
        "details": {
            "min": {"allies_avg_rank": allies[min_d], "opponents_avg_rank": opponents[min_d]},
            "max": {"allies_avg_rank": allies[max_d], "opponents_avg_rank": opponents[max_d]},
        },
    }

    # Most / least balanced by lobby_std
    lobby_std       = data["lobby_std"]
    most_balanced   = min(range(len(lobby_std)), key=lobby_std.__getitem__)
    least_balanced  = max(range(len(lobby_std)), key=lobby_std.__getitem__)
    result["lobby_balance"] = {
        "most_balanced":  (lobby_std[most_balanced],  most_balanced),
        "least_balanced": (lobby_std[least_balanced], least_balanced),
        "details": {
            "most_balanced": {
                "lobby_std": lobby_std[most_balanced],
                "allies_avg_rank": allies[most_balanced],
                "opponents_avg_rank": opponents[most_balanced],
            },
            "least_balanced": {
                "lobby_std": lobby_std[least_balanced],
                "allies_avg_rank": allies[least_balanced],
                "opponents_avg_rank": opponents[least_balanced],
            },
        },
    }

    return result


def sort_matches_by_lobby_std(
    recent_matches: List[Any],
    lobby_std: List[float],
) -> List[Tuple[float, Any]]:
    """
    Return matches sorted by lobby standard deviation (ascending).

    Parameters
    ----------
    recent_matches : list
        Match objects in the same order as *lobby_std*.
    lobby_std : list[float]
        Per-match rank spread values.

    Returns
    -------
    list of ``(std, match)`` tuples, sorted by std ascending.
    """
    paired = list(zip(lobby_std, recent_matches))
    paired.sort(key=lambda x: x[0])
    return paired


def slice_until_value(
    lst: List[Tuple[float, Any]],
    x: float,
) -> List[Tuple[float, Any]]:
    """
    Return the prefix of *lst* whose first-element (std value) is strictly
    below *x*.

    Useful for answering "what fraction of matches had std ≤ x?".

    Parameters
    ----------
    lst : list of (float, Any)
        Sorted list of (value, item) pairs.
    x : float
        Threshold value.

    Returns
    -------
    list
        All pairs whose value is strictly less than *x*.
        Returns an empty list if no such pair exists.
    """
    for i, (value, _) in enumerate(lst):
        if value >= x:
            return lst[:i]
    return []


# ── Plot helper ───────────────────────────────────────────────────────────────

def find_change_indices(lst: list) -> List[Tuple[int, Any]]:
    """
    Return ``(index, value)`` pairs for every position where the list value
    changes, including the first element.

    Used to detect game-version transitions for the rank-trend plot.
    """
    if not lst:
        return []
    result = [(0, lst[0])]
    for i in range(1, len(lst)):
        if lst[i] != lst[i - 1]:
            result.append((i, lst[i]))
    return result