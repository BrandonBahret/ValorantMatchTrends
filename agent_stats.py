"""
agent_stats.py
──────────────
Per-agent and per-role statistics across a set of matches.

Generates one output file (place next to index.html):

  report_data.js   – ``window.REPORT_DATA = {...}`` loaded by index.html
                     via a plain <script src="report_data.js"> tag.  Works
                     from file:// URLs with no server required.

index.html is a fully static file — edit it independently; re-running the
Python script never overwrites it.

Usage
-----
    from agent_stats import generate_report
    generate_report(matches, excluded_puuid, output_dir="outputs/agent_report")

─────────────────────────────────────────────────────────────────────────────
HOW THE JSON IS STRUCTURED (the big picture)
─────────────────────────────────────────────────────────────────────────────

The webpage needs to filter stats by map, rank, and agent simultaneously.
Rather than recomputing everything at page-load time or shipping raw match
records to the browser, we pre-slice the stats in Python and write a compact
JSON blob that the JS can look up with a simple key.

The core strategy is: compute "ground-truth" stats for every combination of
(map, rank) up front, then let the JS average/merge multiple slices together
as needed when the user selects more than one rank or map.

For example, to show Jett's pick-rate for Gold + Silver on Bind:

    // JS side — merge the two pre-computed slices:
    mergeAgentSlices([
        slices.by_map_rank["Bind|gold"],
        slices.by_map_rank["Bind|silver"],
    ])

    // mergeAgentSlices averages win_rate / kda / team_percentage across
    // the supplied slices, weighted by picks (where applicable).

The JSON shape the webpage consumes looks like this at the top level:

    REPORT_DATA = {
        "summary":  { matches, total_teams, unique_agents,
                      match_totals: {
                          total: int,
                          by_map: { map: { total: int, by_rank: { rank: int } } }
                      } },
        "agents":   [ { name, icon, role, loadout }, ... ],
        "repr":     { map: { rank: { role: float } } },  # opponent-role presence rates
        "meta": {
            "agent_stats_slices": {
                # Pre-sliced pick/win/KDA for every map × rank combination.
                # See build_agent_stats_slices() for the full shape.
                "maps":        ["Ascent", "Bind", ...],
                "ranks":       ["iron", "bronze", ...],
                "by_map":      { map:   { agent: { ... } } },
                "by_rank":     { rank:  { agent: { ... } } },
                "by_map_rank": { "map|rank": { agent: { ... } } },
            },
            "precompute": {
                # Flat map|rank|act|phase index for loadout / weapon / spend data.
                # Used by the filter sidebar — same averaging strategy as above.
                "weapons":  { "Bind|silver|Act1|Full Buy": { ... } },
                "shields":  { ... },
                "utility":  { ... },
                "spend":    { ... },
                "utility_by_agent": {
                    # Per-agent ability CPR (casts-per-round) sliced by map × rank.
                    # Mirrors the agent_stats_slices shape.
                    "by_map":      { map:   { agent: { ... } } },
                    "by_rank":     { rank:  { agent: { ... } } },
                    "by_map_rank": { "map|rank": { agent: { ... } } },
                },
            },
            "weapons_by_phase":   { phase: { weapon: { count, buy_rate_pct } } },
            "spend_by_phase":     { phase: SpendStats },
            "phase_distribution": { phase: { count, pct } },
            "agent_spend":        { agent: SpendStats },
        },
    }

─────────────────────────────────────────────────────────────────────────────
ENTRY POINTS
─────────────────────────────────────────────────────────────────────────────

  generate_report(matches, excluded_puuid, output_dir)   ← main public API
      └─ build_report_data()        assembles the full dict
          ├─ calculate_agent_stats()         raw per-agent counters
          ├─ calculate_role_percentages()    opponent-role presence by map × rank
          ├─ build_loadout_data()            per-agent loadout + meta
          └─ build_agent_stats_slices()      pre-sliced pick/win/KDA
      └─ write_report_data()        serialises to report_data.js

  generate_html_report()  is a deprecated alias — prefer generate_report().
"""

import json
import os
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from api_valorant_assets import ValAssetApi
from api_henrik import Match, Player
from loadout_lens import MatchAnalyser


# ── Agent presence ────────────────────────────────────────────────────────────

def agent_team_percentage(matches: List[Match], agent_name: str, excluded_puuid: str = "") -> float:
    """Return the percentage of *opponent* teams that had at least one *agent_name* player.

    Mirrors the scope used by calculate_agent_stats() / _agent_stats_to_rates():
    only the team that does NOT include *excluded_puuid* is counted.  Each match
    therefore contributes exactly one opponent team to the denominator.

    If *excluded_puuid* is empty (e.g. for a quick sanity check with no tracked
    player), all teams are counted and the denominator is len(matches) * 2.

    This is a convenience helper used outside the main pipeline (e.g. for
    quick sanity checks); the main pipeline uses calculate_agent_stats()
    which computes the same figure alongside other metrics.
    """
    total_teams = agent_teams = 0
    for match in matches:
        # Identify which team the tracked player is on so we can exclude it.
        excluded_team = None
        if excluded_puuid:
            for player in match.players:
                if player.puuid == excluded_puuid:
                    excluded_team = player.team_id
                    break

        # Group players by their team ID to get the two sides.
        teams: Dict[str, list] = {}
        for player in match.players:
            teams.setdefault(player.team_id, []).append(player)

        for team_id, team_players in teams.items():
            if excluded_team and team_id == excluded_team:
                continue  # skip the tracked player's team
            total_teams += 1
            if any(p.character == agent_name for p in team_players):
                agent_teams += 1
    return (agent_teams / total_teams * 100) if total_teams else 0.0


# ── Comprehensive agent stats ─────────────────────────────────────────────────

def calculate_agent_stats(matches: List[Match], excluded_puuid: str) -> dict:
    """Aggregate per-agent stats across all matches, excluding *excluded_puuid*.

    The tracked player is excluded so the stats reflect opponent behaviour
    only — we are analysing what agents enemies run, not the tracked player.

    Returns a dict keyed by agent name. Each value is a raw counters dict:

        {
            "picks":             int,   # total individual player-picks (opponents only)
            "teams":             int,   # total teams that included this agent
            "wins":              int,   # picks that ended in a win
            "kills":             int,   # cumulative kills across all picks
            "deaths":            int,
            "assists":           int,
            "matches_seen":      int,   # distinct matches where this agent appeared,
                                        # counted for any player except the tracked player
                                        # (both allies and opponents)
            "non_mirror_picks":  int,   # picks where this agent appeared on one team
                                        # but not the other (either side)
            "non_mirror_wins":   int,   # wins among non_mirror_picks
        }

    These raw counters are later converted to rates by _agent_stats_to_rates().

    Two-pass design
    ---------------
    Pass 1 (player loop): counts picks, kills/deaths/assists, wins, and
        matches_seen. All counters except matches_seen are restricted to opponent
        players only. matches_seen counts any match where the agent appeared on
        either team, excluding only the tracked player themselves.
        Non-mirror picks are also computed per-match after the player loop,
        using a symmetric set difference across both teams.

    Pass 2 (team loop): counts how many *teams* fielded each agent. This must
        be a separate pass because "teams" is a team-level count — an agent
        appearing on the same team twice would only count as one team presence.
        Depends on agent_stats being populated by Pass 1.
    """
    agent_stats: Dict[str, dict] = {}

    # ── Pass 1: per-player counters ──────────────────────────────────────────
    for match in matches:
        # Identify which team won. The API stores this as has_won on each team;
        # we extract the winning team_id (or None if no winner recorded).
        # Lowercased at extraction to ensure consistent comparison with
        # player.team_id.lower() below.
        winning_team_id = (
            [tid.lower() for tid, data in match.teams.as_dict().items() if data["has_won"]] + [None]
        )[0]

        # Group all players by team for mirror-detection below.
        teams: Dict[str, list] = {}
        for player in match.players:
            teams.setdefault(player.team_id, []).append(player)

        # Build {team_id: set_of_agents} for opponent players only, so that
        # non-mirror detection uses the same player scope as picks/wins counters.
        # _get_nonexcluded_players strips the entire tracked player's team, so
        # agents_per_team will only contain the one opponent team.  We still
        # build the dict keyed by team_id so the symmetric-difference logic
        # below generalises if the scope ever changes.
        nonexcluded_set = set(id(p) for p in _get_nonexcluded_players(match, excluded_puuid))
        agents_per_team: Dict[str, set] = {}
        for tid, players in teams.items():
            opponent_agents = {p.character for p in players if id(p) in nonexcluded_set}
            if opponent_agents:
                agents_per_team[tid] = opponent_agents

        nonexcluded = _get_nonexcluded_players(match, excluded_puuid)
        for player in match.players:
            agent = player.character

            # Initialise the bucket the first time we see this agent.
            if agent not in agent_stats:
                agent_stats[agent] = {
                    "picks": 0, "teams": 0, "wins": 0,
                    "kills": 0, "deaths": 0, "assists": 0,
                    "matches_seen": set(),
                    "non_mirror_picks": 0, "non_mirror_wins": 0,
                }

            s = agent_stats[agent]

            # Track any appearance of this agent regardless of team,
            # but never count the tracked player themselves.
            if player.puuid != excluded_puuid:
                s["matches_seen"].add(match.metadata.match_id)

            # All other counters are opponents only.
            if player not in nonexcluded:
                continue

            s["picks"]   += 1
            s["kills"]   += player.stats.kills
            s["deaths"]  += player.stats.deaths
            s["assists"] += player.stats.assists

            won = player.team_id.lower() == winning_team_id
            if won:
                s["wins"] += 1

        # Non-mirror: agent appeared on exactly one of the two teams.
        # Must use ALL players (both teams) so we can compare both sides.
        # agents_per_team is opponent-only and typically has just one entry,
        # so we build a separate all_agents_per_team from the full teams dict,
        # excluding only the tracked player themselves.
        all_agents_per_team: Dict[str, set] = {}
        for tid, players in teams.items():
            all_agents_per_team[tid] = {p.character for p in players if p.puuid != excluded_puuid}

        team_ids = list(all_agents_per_team.keys())
        if len(team_ids) == 2:
            agents_a = all_agents_per_team[team_ids[0]]
            agents_b = all_agents_per_team[team_ids[1]]
            won_a = team_ids[0].lower() == winning_team_id
            won_b = team_ids[1].lower() == winning_team_id
            for agent in agents_a ^ agents_b:
                # Initialise the bucket if not yet seen (can happen when the
                # agent only appeared in non-mirror positions so far).
                if agent not in agent_stats:
                    agent_stats[agent] = {
                        "picks": 0, "teams": 0, "wins": 0,
                        "kills": 0, "deaths": 0, "assists": 0,
                        "matches_seen": set(),
                        "non_mirror_picks": 0, "non_mirror_wins": 0,
                    }
                s = agent_stats[agent]
                s["non_mirror_picks"] += 1
                if (agent in agents_a and won_a) or (agent in agents_b and won_b):
                    s["non_mirror_wins"] += 1

    # ── Pass 2: per-team counts ──────────────────────────────────────────────
    # Counts how many distinct teams ran each agent across all matches.
    # Restricted to opponent players only, consistent with picks/wins counters.
    for match in matches:
        teams: Dict[str, list] = {}
        for player in _get_nonexcluded_players(match, excluded_puuid):
            teams.setdefault(player.team_id, []).append(player)

        for team_players in teams.values():
            # Use a set so an agent appearing twice on the same team (shouldn't
            # happen in Valorant, but defensive) only increments once.
            agents_on_team = {p.character for p in team_players}
            for agent in agents_on_team:
                if agent in agent_stats:
                    agent_stats[agent]["teams"] += 1

    # Convert matches_seen sets to plain integers for JSON serialisation.
    for agent in agent_stats:
        agent_stats[agent]["matches_seen"] = len(agent_stats[agent]["matches_seen"])

    return agent_stats


# ── Role percentages ──────────────────────────────────────────────────────────

def calculate_role_percentages(
    matches: List[Match], excluded_puuid: str
) -> Dict[str, Dict[str, Dict[str, str]]]:
    """
    Returns nested structure:
    {
        map: {
            rank: {
                role: "xx.x%"
            }
        }
    }
    """
    
    def tier_to_rank(tier: int) -> str:
        if tier <= 2: return "Unranked"
        elif tier <= 5: return "Iron"
        elif tier <= 8: return "Bronze"
        elif tier <= 11: return "Silver"
        elif tier <= 14: return "Gold"
        elif tier <= 17: return "Platinum"
        elif tier <= 20: return "Diamond"
        elif tier <= 23: return "Ascendant"
        elif tier <= 26: return "Immortal"
        else: return "Radiant"

    agent_api = ValAssetApi()

    # map -> rank -> stats
    data = defaultdict(
        lambda: defaultdict(
            lambda: {"total_teams": 0, "role_counts": defaultdict(int)}
        )
    )

    for match in matches:
        map_name = match.metadata.map

        teams: Dict[str, list[Player]] = {}
        excluded_team = None

        for player in match.players:
            if player.puuid == excluded_puuid:
                excluded_team = player.team_id

            teams.setdefault(player.team_id, []).append(player)

        for team_id, team_players in teams.items():
            if team_id == excluded_team:
                continue

            # Use average or first player's rank (same as your current logic)
            rank = tier_to_rank(team_players[0].currenttier)

            data[map_name][rank]["total_teams"] += 1

            roles_in_team = set()
            for player in team_players:
                role = agent_api.agents[player.character].role.displayName
                roles_in_team.add(role)

            for role in roles_in_team:
                data[map_name][rank]["role_counts"][role] += 1
    
    ALL_ROLES = ["Duelist", "Initiator", "Controller", "Sentinel"]

    # Convert to percentages
    result: Dict[str, Dict[str, Dict[str, str]]] = {}

    for map_name, ranks in data.items():
        result[map_name] = {}

        for rank, stats in ranks.items():
            total = stats["total_teams"]
            role_counts = stats["role_counts"]

            result[map_name][rank] = {}

            for role in ALL_ROLES:
                count = role_counts.get(role, 0)
                percentage = (count / total * 100) if total > 0 else 0
                result[map_name][rank][role] = percentage

    return result


# ── Agent stats slice builder ─────────────────────────────────────────────────

def _agent_stats_to_rates(
    stats: dict,
    total_matches: int,
    total_players_in_slice: int,
    total_teams_in_slice: int,
) -> dict:
    """Convert a raw calculate_agent_stats bucket into the rate fields the JS reads.

    This is the normalisation step: raw counters (picks, kills, …) become
    percentages and ratios that the UI can display directly without further
    maths.

    Parameters
    ----------
    stats : dict
        One agent's raw bucket from calculate_agent_stats().
    total_matches : int
        Number of matches in the slice being converted.  Used only as a raw
        counter stored on the leaf so JS can re-derive matches_seen_percentage
        when merging slices.
    total_players_in_slice : int
        Total opponent player-slots across all agents in this map+rank slice.
        This is the correct denominator for pick_rate:
            pick_rate = picks / total_players_in_slice * 100
        It is computed agent-agnostically (all opponent players of this rank
        on this map), so merging two rank slices is just:
            picks_A + picks_B / (total_players_A + total_players_B) * 100
    total_teams_in_slice : int
        Total distinct opponent *teams* in this map+rank slice, regardless of
        which agent they picked.  This is the correct denominator for both
        team_percentage and matches_seen_percentage.  Computed via
        _count_teams_in_rank_band() so it counts EVERY team (not just the
        subset of matches where this specific agent appeared).

        For example, with 51 total matches and Clove appearing in 11:
            team_percentage   = 11 / 51 * 100 ≈ 21.6%   ✓
            (old, wrong):       11 / 11 * 100  = 100%    ✗

        Merging two rank slices is simply adding the two counts:
            total_teams(Gold+Bronze) = total_teams(Gold) + total_teams(Bronze)

    Returns
    -------
    dict with keys:
        -- Rates (derived, for display) --
        pick_rate               – picks / total_players_in_slice * 100
                                  "X% of Gold players on Split picked Gekko"
        team_percentage         – teams / total_teams_in_slice * 100
                                  "X% of opponent teams on Split at Gold ran Gekko"
        matches_seen_percentage – matches_seen / total_teams_in_slice * 100
                                  "Gekko appeared in X% of Gold matches on Split"
                                  NOTE: denominator is total_teams (the full slice team count),
                                  NOT total_matches (the agent-scoped match count which equals
                                  matches_seen and would produce ~100% for every agent).
        win_rate                – wins / picks * 100
        non_mirror_win_rate     – non_mirror_wins / non_mirror_picks * 100
                                  (None if no non-mirror picks)
        kda                     – (kills + assists) / deaths
                                  (deaths == 0 → kills + assists, finite sentinel)

        -- Raw counters (denominators, for correct JS merging) --
        picks                   – agent pick count in this slice
        total_players_in_slice  – total opponent players of this rank on this map
                                  (denominator for pick_rate)
        teams                   – teams that fielded this agent
        total_teams             – total_teams_in_slice: all opponent teams in slice
                                  (denominator for team_percentage and
                                   matches_seen_percentage; NOT restricted to
                                   matches where this agent appeared)
        matches_seen            – distinct matches where agent appeared (either team)
        total_matches           – matches in this slice
                                  (raw counter; rates use total_teams as denominator)
        wins                    – raw win count (denominator for win_rate is picks)
        non_mirror_picks        – raw non-mirror pick count
        non_mirror_wins         – raw non-mirror win count
        kills                   – raw kills  ─┐
        assists                 – raw assists  ├─ denominators for merged kda
        deaths                  – raw deaths  ─┘

        JS merge contract
        -----------------
        To merge two slices A and B for the same agent:

            pick_rate    = (A.picks + B.picks) / (A.total_players_in_slice + B.total_players_in_slice) * 100
            team_pct     = (A.teams + B.teams) / (A.total_teams + B.total_teams) * 100
            matches_seen_pct = (A.matches_seen + B.matches_seen) /
                               (A.total_teams + B.total_teams) * 100
                               ^^^^^^^^^^^^ NOT total_matches — total_matches is the agent-scoped
                               match count which equals matches_seen and would produce ~100%.
                               total_teams is the full slice count (all opponent teams in the
                               map+rank bucket, regardless of whether this agent appeared).
            win_rate     = (A.wins  + B.wins)  / (A.picks + B.picks) * 100
            kda          = (A.kills + A.assists + B.kills + B.assists) / (A.deaths + B.deaths)
            nm_win_rate  = (A.non_mirror_wins + B.non_mirror_wins) /
                           (A.non_mirror_picks + B.non_mirror_picks) * 100

        Never average the rate fields directly — always re-derive from the
        raw counters after summing across slices.
    """
    picks    = stats["picks"]
    nm_picks = stats["non_mirror_picks"]
    kills    = stats["kills"]
    deaths   = stats["deaths"]
    assists  = stats["assists"]
    wins     = stats["wins"]
    nm_wins  = stats["non_mirror_wins"]
    # matches_seen may still be a set if called before the finalisation loop
    # in calculate_agent_stats — handle both forms defensively.
    matches_seen = stats["matches_seen"] if isinstance(stats["matches_seen"], int) \
                   else len(stats["matches_seen"])
    teams = stats["teams"]

    # total_teams_in_slice is the agent-agnostic denominator for team_percentage
    # and matches_seen_percentage.  It is NOT the count of matches where this
    # specific agent appeared — it is the count of ALL opponent teams in the
    # map+rank slice, so the rate correctly reflects presence among all possible
    # opponent teams, not just the subset that ran this agent.
    total_teams = total_teams_in_slice

    return {
        # ── Rates (display-ready) ──────────────────────────────────────────
        "pick_rate":               picks / total_players_in_slice * 100 if total_players_in_slice else 0,
        "team_percentage":         teams / total_teams * 100 if total_teams else 0,
        "matches_seen_percentage": matches_seen / total_teams * 100 if total_teams else 0,
        "win_rate":                wins / picks * 100 if picks else 0,
        "non_mirror_win_rate":     nm_wins / nm_picks * 100 if nm_picks else None,
        "kda":                     (kills + assists) / deaths if deaths else kills + assists,

        # ── Raw counters (merge denominators) ─────────────────────────────
        "picks":                   picks,
        "total_players_in_slice":  total_players_in_slice,
        "teams":                   teams,
        "total_teams":             total_teams,          # = total_teams_in_slice (all opp. teams)
        "matches_seen":            matches_seen,
        "total_matches":           total_matches,        # raw match count (for reference)
        "wins":                    wins,
        "non_mirror_picks":        nm_picks,
        "non_mirror_wins":         nm_wins,
        "kills":                   kills,
        "assists":                 assists,
        "deaths":                  deaths,
    }


def _filter_matches_by_map(matches: List[Match], map_name: str) -> List[Match]:
    """Return only the matches played on *map_name* (case-insensitive)."""
    return [m for m in matches if (m.metadata.map or "").lower() == map_name.lower()]


# Valorant API currenttier raw values:
#   0-2  = Unrated / Unknown / Unranked (excluded — not in any rank band)
#   3-5  = Iron 1 / 2 / 3
#   6-8  = Bronze 1 / 2 / 3
#   9-11 = Silver 1 / 2 / 3
#   12-14= Gold 1 / 2 / 3
#   15-17= Platinum 1 / 2 / 3
#   18-20= Diamond 1 / 2 / 3
#   21-23= Ascendant 1 / 2 / 3
#   24-26= Immortal 1 / 2 / 3
#   27   = Radiant


def _filter_matches_by_rank(matches: List[Match], excluded_puuid: str, character: str, min_tier: int, max_tier: int) -> List[Match]:
    out = []
    for m in matches:
        players = _get_nonexcluded_players(m, excluded_puuid)
        subject = next((p for p in players if getattr(p, "character", None) == character), None)
        if subject is None:
            continue
        tier = getattr(subject, "currenttier", 0) or 0
        if min_tier <= tier <= max_tier:
            out.append(m)
    return out


def _filter_matches_by_rank_any_player(
    matches: List[Match], excluded_puuid: str, min_tier: int, max_tier: int
) -> List[Match]:
    """Return matches where ANY non-excluded opponent player falls in [min_tier, max_tier].

    Unlike _filter_matches_by_rank (which requires a specific agent to be
    present), this is agent-agnostic.  It is used to compute the denominator
    for pick_rate: the total number of opponent players of a given rank on a
    given map, regardless of which agent they picked.
    """
    out = []
    for m in matches:
        players = _get_nonexcluded_players(m, excluded_puuid)
        if any(min_tier <= (getattr(p, "currenttier", 0) or 0) <= max_tier for p in players):
            out.append(m)
    return out


def _count_players_in_rank_band(
    matches: List[Match], excluded_puuid: str, min_tier: int, max_tier: int
) -> int:
    """Count total opponent player-slots whose currenttier falls in [min_tier, max_tier].

    This is the correct denominator for pick_rate in a map+rank slice:
        pick_rate(Gekko, Split, Gold) = gekko_picks / _count_players_in_rank_band(
            split_matches, excluded_puuid, 12, 14
        ) * 100

    Counts every qualifying player across all supplied matches, so merging
    two rank slices is simply adding the two counts:
        total_players(Gold+Bronze) = total_players(Gold) + total_players(Bronze)
    """
    return sum(
        1
        for m in matches
        for p in _get_nonexcluded_players(m, excluded_puuid)
        if min_tier <= (getattr(p, "currenttier", 0) or 0) <= max_tier
    )


def _count_teams_in_rank_band(
    matches: List[Match], excluded_puuid: str, min_tier: int, max_tier: int
) -> int:
    """Count distinct opponent *teams* where at least one player falls in [min_tier, max_tier].

    This is the correct denominator for team_percentage and matches_seen_percentage
    in a map+rank slice.  Each qualifying match contributes exactly one opponent
    team, so the count equals the number of matches that have at least one
    opponent player in the rank band.

    Merging two rank slices is simply adding the two counts:
        total_teams(Gold+Bronze) = total_teams(Gold) + total_teams(Bronze)

    Note: using ANY-player semantics (a team qualifies if *any* of its players
    is in the band) is intentional — this mirrors how rank is displayed in the
    UI (the team's rank label comes from the first/representative player) and
    avoids undercounting mixed-rank teams.
    """
    return sum(
        1
        for m in matches
        if any(
            min_tier <= (getattr(p, "currenttier", 0) or 0) <= max_tier
            for p in _get_nonexcluded_players(m, excluded_puuid)
        )
    )


# Rank band definitions — name maps to [min, max] raw API currenttier values.
# These mirror the bands defined in loadout_lens.RANK_BANDS / _RANK_BAND_KEY.
# Each rank covers three sub-tiers (e.g. Gold 1/2/3 → tiers 12/13/14).
#
# The JS rank filter sidebar uses these names as keys (lower-case):
#   slices.by_rank["gold"]   or   slices.by_map_rank["Bind|gold"]
_RANK_BANDS: List[Tuple[str, int, int]] = [
    ("iron",     3,  5), ("bronze",   6,  8), ("silver",   9, 11),
    ("gold",    12, 14), ("plat",    15, 17), ("diamond", 18, 20),
    ("ascend",  21, 23), ("immortal",24, 26), ("radiant", 27, 27),
]


def build_agent_stats_slices(
    matches: List[Match],
    excluded_puuid: str,
) -> Dict[str, Any]:
    """Pre-compute agent-level pick/win/KDA stats for every filter combination.

    Returns a nested dict consumed by ``meta_data["agent_stats_slices"]``.

    ─────────────────────────────────────────────────────────────────────────
    WHY PRE-SLICE?
    ─────────────────────────────────────────────────────────────────────────
    The webpage lets users filter by map AND rank simultaneously.  Computing
    this in the browser from raw match records would be slow and would require
    shipping much more data.  Instead, we compute every (map, rank) combination
    here in Python and write a compact lookup table to the JSON.

    The JS then re-combines slices as needed.  For example, if the user
    selects "Silver + Gold on Bind", the browser merges:
        slices.by_map_rank["Bind|silver"]
        slices.by_map_rank["Bind|gold"]
    using a mergeAgentSlices() helper.  Merging must sum the raw counters
    (picks, total_players_in_slice, wins, kills, etc.) and re-derive the
    rates — never average rate fields directly.

    ─────────────────────────────────────────────────────────────────────────
    OUTPUT SHAPE
    ─────────────────────────────────────────────────────────────────────────

        {
            "maps":  ["Ascent", "Bind", ...],   # maps present in the data
            "ranks": ["iron", "bronze", ..., "radiant"],  # ranks present
        }

    Only the dimension lists are returned — the full per-agent slices
    (by_map, by_rank, by_map_rank) are now computed in
    ``analyser.agent_nested_tree()`` and exposed via ``meta["agent_tree"]``.
    Keys that would be empty are omitted to keep the JSON small.

    The act dimension is intentionally omitted here — pick-rate meta is
    stable across acts; act-level slicing lives in ``meta.precompute`` for
    loadout/weapon data where it matters.
    """
    # ── Dimension discovery ────────────────────────────────────────────────
    # Only include maps and ranks that are actually present in the dataset.
    # This keeps the JSON small and ensures the filter sidebar doesn't offer
    # options that would always return empty results.
    maps_present: List[str] = sorted({
        m.metadata.map for m in matches if m.metadata.map
    })
    all_tiers = {
        getattr(p, "currenttier", 0) or 0
        for m in matches for p in _get_nonexcluded_players(m, excluded_puuid)
    }
    ranks_present: List[str] = [
        key for key, lo, hi in _RANK_BANDS
        if any(lo <= t <= hi for t in all_tiers)
    ]

    return {
        "maps":  maps_present,
        "ranks": ranks_present,
    }


def _get_nonexcluded_players(match: Match, excluded_puuid: str):
    # Find the excluded player's team
    excluded_team = None
    for p in match.players:
        if p.puuid == excluded_puuid:
            excluded_team = p.team_id
            break

    # Return only players NOT on that team
    return [p for p in match.players if p.team_id != excluded_team]

def build_agent_stats_nested(
    matches: List[Match],
    excluded_puuid: str,
) -> Dict[str, Any]:
    """Pre-compute agent pick/win/KDA stats as a nested ``agent → map → rank`` tree.

    This is the nested counterpart to :func:`build_agent_stats_slices`.
    Rather than parallel flat dicts (``by_map``, ``by_rank``, ``by_map_rank``),
    the data is stored as a proper hierarchy where every parent node's stats
    are derived by merging its children.

    Phase is intentionally omitted — pick/win/KDA are match-level metrics that
    don't vary by buy phase.

    Tree shape
    ----------
    ::

        {
            "maps":  ["Ascent", "Bind", ...],
            "ranks": ["iron", "bronze", ..., "radiant"],
            "by_agent": {
                "Jett": {
                    "by_map": {
                        "Bind": {
                            "by_rank": {
                                "gold":   { pick_rate, picks, total_players_in_slice,
                                            win_rate, wins, kda, kills, assists, deaths,
                                            team_percentage, teams, total_teams,
                                            matches_seen_percentage, matches_seen, total_matches,
                                            non_mirror_win_rate, non_mirror_picks, non_mirror_wins },
                                "silver": { ... },
                                ...
                            }
                        },
                        "Ascent": { ... },
                        ...
                    }
                },
                "Sova": { ... },
                ...
            }
        }

    Leaf nodes sit at the ``by_rank`` level and contain both display-ready
    rates AND the raw counters needed to re-derive those rates after merging.

    JS merge contract
    -----------------
    To combine two or more rank (or map) slices for the same agent, ALWAYS
    sum the raw counters first, then re-derive the rates.  Never average the
    rate fields directly.

    Example — Gekko pick rate for Gold + Bronze on Split::

        const gold   = by_agent["Gekko"]["by_map"]["Split"]["by_rank"]["gold"];
        const bronze = by_agent["Gekko"]["by_map"]["Split"]["by_rank"]["bronze"];

        const picks       = gold.picks  + bronze.picks;
        const totalPlayers= gold.total_players_in_slice + bronze.total_players_in_slice;
        const pickRate    = picks / totalPlayers * 100;

        const wins        = gold.wins   + bronze.wins;
        const winRate     = wins / picks * 100;

        const kda         = (gold.kills + gold.assists + bronze.kills + bronze.assists)
                            / (gold.deaths + bronze.deaths);

        const teams       = gold.teams  + bronze.teams;
        const totalTeams  = gold.total_teams + bronze.total_teams;
        const teamPct     = teams / totalTeams * 100;

        // matches_seen_percentage also uses total_teams as denominator:
        const matchesSeen    = gold.matches_seen + bronze.matches_seen;
        const matchesSeenPct = matchesSeen / totalTeams * 100;

    Returns
    -------
    Dict with keys ``maps``, ``ranks``, and ``by_agent``.
    """
    # ── Dimension discovery ────────────────────────────────────────────────
    maps_present: List[str] = sorted({
        m.metadata.map for m in matches if m.metadata.map
    })
    
    all_tiers = {
        getattr(p, "currenttier", 0) or 0
        for m in matches for p in _get_nonexcluded_players(m, excluded_puuid)
    }
    ranks_present: List[str] = [
        key for key, lo, hi in _RANK_BANDS
        if any(lo <= t <= hi for t in all_tiers)
    ]

    # ── Pre-compute per-(map, rank) player and team counts ────────────────
    # Both denominators must be agent-agnostic so they represent the full
    # slice population, not just the subset of matches where a given agent
    # happened to appear.
    #
    #   total_players_in_slice[map][rank]  → denominator for pick_rate
    #   total_teams_in_slice[map][rank]    → denominator for team_percentage
    #                                        and matches_seen_percentage
    #
    # Merging two rank slices is simply adding the two counts for each:
    #   total_players(Gold+Bronze) = total_players(Gold) + total_players(Bronze)
    #   total_teams(Gold+Bronze)   = total_teams(Gold)   + total_teams(Bronze)
    all_map_matches: Dict[str, List[Match]] = {
        map_name: _filter_matches_by_map(matches, map_name)
        for map_name in maps_present
    }
    total_players_in_slice: Dict[str, Dict[str, int]] = {
        map_name: {
            rank_key: _count_players_in_rank_band(map_m, excluded_puuid, lo, hi)
            for rank_key, lo, hi in _RANK_BANDS
            if rank_key in ranks_present
        }
        for map_name, map_m in all_map_matches.items()
    }
    # total_teams_in_slice is the count of opponent teams (= matches) that had
    # at least one player in the rank band — the correct denominator for
    # team_percentage and matches_seen_percentage.  Without this, those rates
    # are computed over only the matches where the specific agent appeared,
    # causing them to be artificially inflated (e.g. 100% instead of ~21%).
    total_teams_in_slice: Dict[str, Dict[str, int]] = {
        map_name: {
            rank_key: _count_teams_in_rank_band(map_m, excluded_puuid, lo, hi)
            for rank_key, lo, hi in _RANK_BANDS
            if rank_key in ranks_present
        }
        for map_name, map_m in all_map_matches.items()
    }

    def _compute(
        subset: List[Match],
        slice_total_players: int,
        slice_total_teams: int,
    ) -> Dict[str, dict]:
        """Run calculate_agent_stats on *subset* and convert to rates.

        slice_total_players and slice_total_teams are the agent-agnostic
        denominators for this exact map+rank slice, passed through to
        _agent_stats_to_rates.  They must NOT be derived from len(subset)
        because *subset* is filtered to matches where the specific agent
        appeared — using it as the denominator would inflate team_percentage
        and matches_seen_percentage to 100%.
        """
        if not subset:
            return {}
        raw = calculate_agent_stats(subset, excluded_puuid)
        n   = len(subset)
        return {
            agent: _agent_stats_to_rates(s, n, slice_total_players, slice_total_teams)
            for agent, s in raw.items()
        }

    # ── Per-agent nested tree ──────────────────────────────────────────────
    # Collect all agent names seen across all matches (excluding tracked player).
    agent_names: List[str] = sorted({
        p.character
        for m in matches
        for p in _get_nonexcluded_players(m, excluded_puuid)
    })

    by_agent: Dict[str, Any] = {}
    for agent_name in agent_names:
        # Filter to matches where this agent appeared (any player, excl. tracked).
        agent_matches = [
            m for m in matches
            if any(p.character == agent_name for p in _get_nonexcluded_players(m, excluded_puuid))
        ]
        if not agent_matches:
            continue

        by_map_node: Dict[str, Any] = {}
        for map_name in maps_present:
            map_matches = _filter_matches_by_map(agent_matches, map_name)
            if not map_matches:
                continue

            by_rank_node: Dict[str, dict] = {}
            for rank_key, lo, hi in _RANK_BANDS:
                if rank_key not in ranks_present:
                    continue

                # Agent-scoped rank filter: only matches where THIS agent was
                # played by someone in this rank band.  Defines which matches
                # contribute picks/wins/kda for this agent at this rank.
                rank_matches = _filter_matches_by_rank(
                    map_matches, excluded_puuid, agent_name, lo, hi
                )
                if not rank_matches:
                    continue

                # The denominator for pick_rate comes from the agent-agnostic
                # count pre-computed above — NOT from len(rank_matches) * players.
                # This ensures the denominator is "all Gold players on Split",
                # not "all Gold players in matches where Gekko was also played".
                slice_total = total_players_in_slice[map_name].get(rank_key, 0)

                # The denominators for team_percentage and matches_seen_percentage
                # come from the agent-agnostic team count — NOT from len(rank_matches).
                # rank_matches is filtered to matches where this specific agent appeared,
                # so using it would inflate both rates to 100% (or near it).
                slice_teams = total_teams_in_slice[map_name].get(rank_key, 0)

                leaf = _compute(rank_matches, slice_total, slice_teams).get(agent_name)
                if leaf:
                    by_rank_node[rank_key] = leaf

            node: Dict[str, Any] = {}
            if by_rank_node:
                node["by_rank"] = by_rank_node
            by_map_node[map_name] = node

        entry: Dict[str, Any] = {}
        if by_map_node:
            entry["by_map"] = by_map_node
        by_agent[agent_name] = entry

    return {
        "maps":     maps_present,
        "ranks":    ranks_present,
        "by_agent": by_agent,
    }


# ── Loadout data builder ──────────────────────────────────────────────────────

def build_loadout_data(
    matches: List[Any], excluded_puuid: str
) -> Tuple[Dict[str, dict], Dict[str, Any]]:
    """
    Build per-agent loadout analysis and cross-agent meta stats via loadout_lens.

    Returns (per_agent_data, meta_data).
    Returns ({}, {}) gracefully if loadout_lens is unavailable.

    ─────────────────────────────────────────────────────────────────────────
    PER-AGENT LOADOUT DICT SHAPE
    ─────────────────────────────────────────────────────────────────────────
    Each entry in the returned per_agent dict has the following keys:

        {
            "utility":          {
                # Casts-per-round for each ability slot.
                # c/q/e/x correspond to Valorant's keybinds.
                "c": float, "q": float, "e": float, "x": float,
            },
            "ability_names":    {"c": str, "q": str, "e": str, "x": str},
            "ability_icons":    {"c": url, "q": url, "e": url, "x": url},
            "ability_descriptions": {"c": str, "q": str, "e": str, "x": str},
            "agent_media":      {
                # All Valorant asset URLs for the agent — used for card rendering.
                "displayIcon": url, "displayIconSmall": url,
                "bustPortrait": url, "fullPortrait": url, "fullPortraitV2": url,
                "killfeedPortrait": url, "background": url,
                "backgroundGradientColors": [hex, ...],
                "isFullPortraitRightFacing": bool,
                "description": str, "characterTags": [...],
                "role": {"displayName": str, "description": str, "displayIcon": url},
            },
            "shields":               {name: {count, buy_rate_pct}, ...},  # top 3
            "weapons":               {name: {count, buy_rate_pct}, ...},  # top 8
            "by_phase":         {
                # Loadout breakdown for each buy phase.
                # "2nd Round" = post-pistol round (rounds 1 and 13).
                "Pistol"|"Eco"|"Force"|"Full Buy"|"2nd Round": {
                    "weapons": {...}, "shields": {...}
                }
            },
            # The three slice dicts below follow the same averaging strategy
            # as agent_stats_slices — each is a flat lookup the JS merges
            # client-side when multiple filters are active.
            "by_map":           {map_name: {weapons, shields, spend}},
            "by_rank":          {rank_key: {weapons, shields, spend}},
            "by_act":           {act_id:   {weapons, shields, spend}},
        }

    ─────────────────────────────────────────────────────────────────────────
    META DICT SHAPE
    ─────────────────────────────────────────────────────────────────────────
        {
            "weapons_by_phase":   {phase: {weapon: {count, buy_rate_pct}}},
            "spend_by_phase":     {phase: SpendStats},
            "phase_distribution": {phase: {count, pct}},
            "utility_by_agent":   {
                # Per-agent ability CPR (casts-per-round) sliced by map × rank.
                # Mirrors agent_stats_slices for consistency.
                # See the note on averaging strategy in build_agent_stats_slices().
                "by_map":      {map:   {agent: {c, q, e, x}}},
                "by_rank":     {rank:  {agent: {c, q, e, x}}},
                "by_map_rank": {"map|rank": {agent: {c, q, e, x}}},
                # note: by_side is not computed (API does not expose side data)
            },
            "agent_spend":        {agent: SpendStats},
            "precompute":         {
                # Flat map|rank|act|phase index for JS filter sidebar lookups.
                # Key format:  map (or '__all__') | rank | act | phase
                # Example key: "Bind|silver|Act1|Full Buy"
                # '__all__' is used when a dimension is unfiltered.
                "maps": [...], "ranks": [...], "acts": [...], "phases": [...],
                "weapons":  {"map|rank|act|phase": {...}},
                "shields":  {"map|rank|act|phase": {...}},
                "utility":  {"map|rank|act|phase": {C_cpr, Q_cpr, E_cpr, X_cpr}},
                "spend":    {"map|rank|act|phase": SpendStats},
            },
            "url_catalogue":      {category: [{label, template, method, notes}]},
        }

    The ``precompute`` flat-key index is what the JS filter sidebar consumes:
        key = [map||'__all__', rank||'__all__', act||'__all__', phase].join('|')
        metaData.precompute.weapons[key]

    The ``url_catalogue`` lets index.html display data-provenance info without
    any hard-coded API strings.
    """
    try:
        from loadout_lens import (
            MatchAnalyser, AgentAbilityMap, AnalysisFilters,
            WeaponReport, ShieldReport, SpendReport,
        )
    except ImportError:
        # loadout_lens is an optional dependency.  Without it, the report still
        # works — pick/win/KDA stats come from build_agent_stats_slices() instead.
        print("  [loadout_lens] not available — skipping loadout data.")
        return {}, {}

    # AgentAbilityMap maps generic slot keys (c/q/e/x) to human-readable ability
    # names, icons, and descriptions.  It's optional — if unavailable we fall
    # back to generic key names ("C", "Q", etc.).
    ability_map = None
    try:
        ability_map = AgentAbilityMap.build()
    except Exception as exc:
        print(f"  [AgentAbilityMap] could not build: {exc}")

    analyser    = MatchAnalyser(matches, ability_map=ability_map, excluded_puuids={excluded_puuid})
    full_report = analyser.analyse()

    # "2nd Round" (post-pistol) data: rounds at index 1 and 13 represent the
    # first buy round after each pistol round.  We collect these records per
    # agent so the by_phase breakdown can include a "2nd Round" entry.
    round2_by_agent: Dict[str, list] = defaultdict(list)
    for r in analyser.iter_records():
        if r.round_index in (1, 13) and r.puuid != excluded_puuid:
            round2_by_agent[r.agent].append(r)

    def records_to_summary(records: list) -> dict:
        """Collapse a list of round records into weapons/shields summary.

        Returns empty sub-dicts if records is empty (no data for this slice).
        """
        if not records:
            return {"weapons": {}, "shields": {}}
        return {
            "weapons": dict(list(WeaponReport(records).distribution().items())[:10]),
            "shields": ShieldReport(records).distribution(),
        }

    # ── Per-agent slices (by_map / by_rank / by_act) ────────────────────────
    # agent_loadout_slices() returns a dict keyed by agent name; each value
    # has "by_map", "by_rank", "by_act" dicts built from loadout_lens filters.
    # We build these once here and pull from them per-agent below.
    agent_slices = analyser.agent_loadout_slices()

    # ── Per-agent data ──────────────────────────────────────────────────────
    per_agent: Dict[str, dict] = {}
    agent_names = list(full_report.weapons.by_agent().keys())

    for agent_name in agent_names:
        # Analyse the full match set filtered to this agent only.
        a_filter = AnalysisFilters(agents=[agent_name])
        a_report = analyser.analyse(a_filter)

        # CPR = casts-per-round.  cast_rates() returns {"C_cpr": float, ...}
        # where C/Q/E/X are the Valorant keybind slot names.
        cpr = a_report.utility.cast_rates()

        # Default to generic slot keys if ability_map is unavailable.
        ability_names        = {"c": "C",  "q": "Q",  "e": "E",  "x": "X"}
        ability_icons        = {"c": "",   "q": "",   "e": "",   "x": ""}
        ability_descriptions = {"c": "",   "q": "",   "e": "",   "x": ""}
        agent_media: Dict[str, Any] = {}

        if ability_map:
            slots = ability_map.slots(agent_name)
            ability_names        = {k: slots.get(k, k.upper()) for k in ("c", "q", "e", "x")}
            ability_icons        = ability_map.icon_slots(agent_name)
            ability_descriptions = ability_map.description_slots(agent_name)
            agent_media          = ability_map.agent_media(agent_name)

        shields  = a_report.shields.distribution()
        weapons  = dict(list(a_report.weapons.distribution().items())[:10])   # top 10 weapons

        # Per-phase breakdown — four standard buy phases plus the special 2nd-round entry.
        phases_data: Dict[str, dict] = {}
        for phase in ("Pistol", "Eco", "Force", "Full Buy"):
            p_filter = AnalysisFilters(agents=[agent_name], buy_phases=[phase])
            p_report = analyser.analyse(p_filter)
            p_spend  = p_report.spend._stats_for(p_report.spend._records)
            phases_data[phase] = {
                "weapons":       dict(list(p_report.weapons.distribution().items())[:10]),
                "shields":       p_report.shields.distribution(),
                "sample_rounds": p_spend["sample_rounds"],
                "spend":         p_spend,
            }
        phases_data["2nd Round"] = records_to_summary(round2_by_agent.get(agent_name, []))

        # Pull pre-built map / rank / act slices from the batch-computed dict.
        slices = agent_slices.get(agent_name, {})

        # Normalise CPR keys to lower-case (c/q/e/x) for consistency with the
        # rest of the agent data shape.
        _utility_dict: Dict[str, Any] = {
            "c": cpr.get("C_cpr", 0), "q": cpr.get("Q_cpr", 0),
            "e": cpr.get("E_cpr", 0), "x": cpr.get("X_cpr", 0),
        }

        per_agent[agent_name] = {
            "ability_names":         ability_names,
            "ability_icons":         ability_icons,
            "ability_descriptions":  ability_descriptions,
            "agent_media":           agent_media,
            "utility":               _utility_dict,
            "shields":               shields,
            "weapons":               weapons,
            "by_phase":              phases_data,
            "by_map":                slices.get("by_map", {}),
            "by_rank":               slices.get("by_rank", {}),
            "by_act":                slices.get("by_act", {}),
        }

    # ── Meta-level data ─────────────────────────────────────────────────────

    # meta_precompute() returns the flat map|rank|act|phase index.
    # This powers the main filter sidebar on the meta analysis tab.
    precompute = analyser.meta_precompute()

    # Extend precompute with per-agent utility (CPR) sliced by map × rank.
    # We add this here (rather than inside meta_precompute) because it needs
    # agent-level filtering, which meta_precompute() doesn't do.

    _rank_tiers = {k: (lo, hi) for k, lo, hi in _RANK_BANDS}

    def _agent_cpr_for_filter(
        map_filt: Optional[List[str]],
        min_t: Optional[int],
        max_t: Optional[int],
    ) -> Dict[str, Dict[str, float]]:
        """Return per-agent CPR dict for the given map/rank filter combination.

        Iterates over all known agents and runs a filtered analysis for each.
        Returns { agent_name: { "c": float, "q": float, "e": float, "x": float } }.

        Note: side-based filtering is intentionally excluded — the API does
        not expose reliable side data.
        """
        result: Dict[str, Dict[str, float]] = {}
        for a_name in agent_names:
            f = AnalysisFilters(
                agents   = [a_name],
                maps     = map_filt,
                min_tier = min_t,
                max_tier = max_t,
            )
            cpr = analyser.analyse(f).utility.cast_rates()
            result[a_name] = {
                "c": cpr.get("C_cpr", 0), "q": cpr.get("Q_cpr", 0),
                "e": cpr.get("E_cpr", 0), "x": cpr.get("X_cpr", 0),
            }
        return result

    maps_for_util  = precompute["maps"]
    ranks_for_util = precompute["ranks"]

    

    # by_map CPR — one entry per map, all ranks.
    u_by_map: Dict[str, Dict[str, Dict[str, float]]] = {
        m: _agent_cpr_for_filter([m], None, None)
        for m in maps_for_util
    }

    # by_rank CPR — one entry per rank band, all maps.
    # JS lookup: utility_by_agent.by_rank["silver"]["Jett"]
    u_by_rank: Dict[str, Dict[str, Dict[str, float]]] = {}
    for rank_key in ranks_for_util:
        lo_r, hi_r = _rank_tiers.get(rank_key, (None, None))
        u_by_rank[rank_key] = _agent_cpr_for_filter(None, lo_r, hi_r)

    # by_map_rank CPR — combined map × rank for filters like "silver, gold on Bind".
    # JS lookup: utility_by_agent.by_map_rank["Bind|silver"]["Jett"]
    u_by_map_rank: Dict[str, Dict[str, Dict[str, float]]] = {}
    for m in maps_for_util:
        for rank_key in ranks_for_util:
            lo_r, hi_r = _rank_tiers.get(rank_key, (None, None))
            u_by_map_rank[f"{m}|{rank_key}"] = _agent_cpr_for_filter([m], lo_r, hi_r)

    # by_side is intentionally omitted — the API does not support side data.
    utility_by_agent_sliced = {
        "by_map":      u_by_map,
        "by_rank":     u_by_rank,
        "by_map_rank": u_by_map_rank,
    }
    precompute["utility_by_agent"] = utility_by_agent_sliced

    agents_nested: Dict = build_agent_stats_nested(matches, excluded_puuid)
    meta_data: Dict[str, Any] = {
        # "weapons_by_phase":   {},
        # "spend_by_phase":     full_report.spend.average_by_phase(),
        # "phase_distribution": full_report.spend.buy_phase_distribution(),
        # Flat map|rank|act|phase index for JS filter sidebar lookups.
        # ── Single unified tree: agent → map → rank → all statistics ──────
        #
        # Every rank node contains EVERYTHING in one place:
        #   picks, win_rate, non_mirror_win_rate, kda, teams, team_percentage,
        #   matches_seen, matches_seen_percentage, non_mirror_picks,
        #   utility { c, q, e, x },
        #   by_phase { Pistol|Eco|Force|Full Buy → { weapons, shields, spend } }
        #
        # (aggregate across all children) for the common no-filter case.
        #
        # To get any aggregate, average the relevant rank-node leaves:
        #   Jett Force Buy Bind all ranks →
        #     avg tree["Jett"]["by_map"]["Bind"]["by_rank"][r]["by_phase"]["Force"]
        #     for r in tree["Jett"]["by_map"]["Bind"]["by_rank"]
        "agent_tree": analyser.agent_nested_tree(
            match_stats=agents_nested,
        ),
        # Retained for backwards compatibility — now only carries dimension
        # lists (maps, ranks).  Full per-agent slices live in agent_tree.
        "agent_stats_slices": build_agent_stats_slices(matches, excluded_puuid),
    }

    return per_agent, meta_data


# ── Match-count waterfall ─────────────────────────────────────────────────────

def _build_match_totals(matches: List[Match], excluded_puuid: str) -> dict:
    """Build a waterfall of total match counts by map and rank.

    This gives the JS a reliable denominator for matches_seen_percentage at
    every level of the filter hierarchy, without having to sum agent leaf nodes.

    Shape
    -----
    ::

        {
            "total": 51,           # all matches in the selection
            "by_map": {
                "Lotus": {
                    "total": 6,    # matches played on Lotus
                    "by_rank": {
                        "gold":   3,   # Lotus matches with ≥1 gold opponent
                        "silver": 2,
                        ...
                    }
                },
                ...
            }
        }

    Notes
    -----
    - ``by_rank`` counts use ``_count_teams_in_rank_band`` — the same
      agent-agnostic denominator used by ``_agent_stats_to_rates``.
      A match contributes 1 if *any* non-excluded opponent player falls in the
      rank band, so mixed-rank matches are counted for each relevant band.
    - Keys with a zero count are omitted to keep the JSON small.
    """
    maps_present: List[str] = sorted({
        m.metadata.map for m in matches if m.metadata.map
    })

    by_map: dict = {}
    for map_name in maps_present:
        map_matches = _filter_matches_by_map(matches, map_name)
        by_rank: dict = {}
        for rank_key, lo, hi in _RANK_BANDS:
            n = _count_teams_in_rank_band(map_matches, excluded_puuid, lo, hi)
            if n:
                by_rank[rank_key] = n
        by_map[map_name] = {
            "total":   len(map_matches),
            "by_rank": by_rank,
        }

    return {
        "total":  len(matches),
        "by_map": by_map,
    }


# ── Report data builder ───────────────────────────────────────────────────────

def build_report_data(matches: List[Match], excluded_puuid: str) -> dict:
    """
    Assemble the complete REPORT_DATA dict that index.html consumes.

    Returns a plain Python dict — call ``write_report_data`` to serialise it.

    This function is the composition root: it calls all the stat builders and
    merges their outputs into the single JSON shape described at the top of
    this file.  See the module docstring for the full shape.
    """
    assets = ValAssetApi()
    stats  = calculate_agent_stats(matches, excluded_puuid)
    repr_s = calculate_role_percentages(matches, excluded_puuid)
    loadout_per_agent, meta_data = build_loadout_data(matches, excluded_puuid)

    # Ensure agent_stats_slices (dimension lists) is always present even when
    # loadout_lens is unavailable (build_loadout_data returns {} in that case).
    if 'agent_stats_slices' not in meta_data:
        meta_data['agent_stats_slices'] = build_agent_stats_slices(matches, excluded_puuid)

    total_teams     = len(matches) * 2
    agent_data_list = []

    for agent_name, agent_data in stats.items():
        # Look up display assets (icon, role) from the Valorant asset API.
        agent_info = assets.agents.get(agent_name)
        if agent_info:
            icon = agent_info.displayIconSmall or agent_info.displayIcon
            role = agent_info.role.displayName if agent_info.role else "Unknown"
        else:
            icon, role = "", "Unknown"

        # This flat entry lives under REPORT_DATA["agents"].  Granular slices
        # (by_map, by_rank, etc.) live under REPORT_DATA["meta"]["agent_tree"].
        agent_data_list.append({
            "name":                    agent_name,
            "icon":                    icon,
            "role":                    role,
            # Loadout data is None if loadout_lens was unavailable.
            "loadout":                 loadout_per_agent.get(agent_name),
        })

    return {
        "summary": {
            "matches":       len(matches),
            "total_teams":   total_teams,
            "unique_agents": len(agent_data_list),
            # Waterfall of match counts by map → rank.  Gives the JS a reliable
            # denominator for matches_seen_percentage at every filter level.
            # Shape: { total, by_map: { map: { total, by_rank: { rank: int } } } }
            "match_totals":  _build_match_totals(matches, excluded_puuid),
        },
        # Flat list of agents with display assets + loadout data.
        "agents": agent_data_list,
        # Opponent-role presence rates: { map: { rank: { role: float } } }
        "repr":   repr_s,
        # All sliced / precomputed data for the filter sidebar.
        "meta":   meta_data,
    }


# ── File writers ──────────────────────────────────────────────────────────────

def write_report_data(report_data: dict, output_dir: str = ".") -> str:
    """
    Write *report_data* as ``report_data.js`` in *output_dir*.

    The file sets ``window.REPORT_DATA`` so index.html loads it via
    ``<script src="report_data.js">``.  Works from file:// with no server.

    Using a JS assignment (rather than a raw JSON file) means the browser
    can load it with a plain <script> tag and access window.REPORT_DATA
    without any fetch() or XMLHttpRequest — important for local file:// usage.

    Returns the path written.
    """
    os.makedirs(output_dir, exist_ok=True)
    out_path = os.path.join(output_dir, "report_data.js")
    payload  = json.dumps(report_data, indent=2, ensure_ascii=False)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("window.REPORT_DATA = ")
        f.write(payload)
        f.write(";\n")
    return out_path


def generate_report(
    matches: List[Match],
    excluded_puuid: str,
    output_dir: str = ".",
) -> str:
    """
    Build all stats + loadout data and write ``report_data.js`` to *output_dir*.

    Place ``index.html`` in the same directory and open it in a browser.

    This is the main public entry point.  Typical usage:

        from agent_stats import generate_report
        generate_report(my_matches, my_puuid, output_dir="outputs/agent_report")

    Parameters
    ----------
    matches : list[Match]
        Processed match objects from api_henrik / MatchHistoryProcessor.
    excluded_puuid : str
        The tracked player's PUUID (excluded from opponent stats).
    output_dir : str
        Directory to write ``report_data.js``.  Created if it does not exist.

    Returns
    -------
    str
        Path to the written ``report_data.js``.
    """
    print(f"Building report data for {len(matches)} matches...")
    data     = build_report_data(matches, excluded_puuid)
    out_path = write_report_data(data, output_dir)
    s = data["summary"]
    print(f"  {s['unique_agents']} agents  |  {s['matches']} matches  |  {s['total_teams']} teams")
    print(f"  Written -> {out_path}")
    print(f"  Open index.html from the same directory in your browser.")
    return out_path


# ── Debug entry-point ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Run this file directly to regenerate report_data.js for local testing.
    # Adjust the constants below to target a different player / time window.
    from api_henrik import AffinitiesEnum, UnofficialApi
    from db_valorant import ValorantDB
    from match_history_processor import MatchHistoryProcessor

    TIMESPAN        = 30          # days of history to fetch
    MATCH_COUNT_MAX = 20          # cap on number of matches
    ACTS_OF_INTEREST = [('10', '6'), ('11', '1')]   # (episode, act) tuples
    REGION = str(AffinitiesEnum.NA)

    api = UnofficialApi()
    db  = ValorantDB(region=REGION)

    my_puuid = api.get_account_by_name("0dmg", "sadge").puuid
    # Uncomment the line below to refresh the local DB before generating:
    # db.update_match_history_for_puuid(my_puuid)

    history = MatchHistoryProcessor(
        my_puuid, ACTS_OF_INTEREST, db,
        match_count_max=MATCH_COUNT_MAX,
        timespan=TIMESPAN,
    )
    recent_matches = history.recent_matches
    print(f"Loaded {len(recent_matches)} matches")

    generate_report(recent_matches, my_puuid, output_dir="debug_output")


# ── Backwards-compat alias ────────────────────────────────────────────────────

def generate_html_report(
    matches: List[Match],
    excluded_puuid: str,
    output_file: str = "index.html",
) -> None:
    """
    Deprecated: previously generated a self-contained HTML file.
    Now writes report_data.js to the same directory as *output_file*.
    Update call sites to use generate_report() when convenient.
    """
    output_dir = os.path.dirname(os.path.abspath(output_file))
    generate_report(matches, excluded_puuid, output_dir=output_dir)