"""
generate_match_history.py
=========================
Generates ``match_history.js`` — consumed by ``match-history.html`` via
``<script src="match_history.js">``.

Sets ``window.MATCH_HISTORY_DATA = {...}`` so the page works from file://
with no server required (same pattern as report_data.js / round_stats.js).

Standalone usage
----------------
    python generate_match_history.py --name 0dmg --tag sadge

Programmatic usage (from engine.py or similar)
-----------------------------------------------
    from generate_match_history import write_match_history
    write_match_history(
        recent_matches  = recent_matches,   # list[Match], newest-first
        recent_mmr      = recent_mmr,       # dict[match_id, MmrItem]
        my_puuid        = my_puuid,
        player_name     = player_name,
        player_tag      = player_tag,
        region          = region,
        output_dir      = html_dir,         # Path to interactive_report/
    )

Output shape
------------
    {
      "meta": { "name", "tag", "puuid", "region", "generated_at",
                "match_count", "wins", "losses", "draws", "winrate" },
      "assets": {
        "maps": {
          "<displayName>": {
            "displayIcon":             str,   # small icon
            "listViewIcon":            str,
            "listViewIconTall":        str,
            "splash":                  str,   # full loading-screen art
            "stylizedBackgroundImage": str,
            "premierBackgroundImage":  str,
            "mapUrl":                  str,   # minimap / overhead
          }, …
        },
        "agents": {
          "<displayName>": {
            "displayIcon":      str,
            "displayIconSmall": str,
            "bustPortrait":     str,
            "fullPortrait":     str,
            "killfeedPortrait": str,
          }, …
        }
      },
      "matches": [
        {
          "id":           str,
          "map":          str,
          "queue":        str,           # "Competitive" / "Unrated" / …  (game mode)
          "cluster":      str,           # server cluster, e.g. "London"
          "result":       "win"|"loss"|"draw",
          "score_us":     int,           # rounds won by player's team
          "score_them":   int,           # rounds won by opponent
          "rounds":       int,
          "game_start_ms": int,          # epoch milliseconds
          "duration_sec": int,
          "season":       str,           # "e11a2"
          "patch":        str,           # "12.05"
          "rank":         str,           # "Gold 1"
          "rr":           int,           # RR after this match
          "rr_delta":     int|null,      # null for earliest match
          "agent":        str,
          "agent_icon":   str,           # URL or ""
          "kills":        int,
          "deaths":       int,
          "assists":      int,
          "acs":          float,         # score / rounds
          "adr":          float|null,    # damage_made / rounds (if available)
          "hs_pct":       float|null,    # headshots / total shots × 100
          "scoreboard": {
            "blue": [ {player_row}, … ],
            "red":  [ {player_row}, … ],
          }
        },
        …                                # newest match first
      ]
    }

    player_row: {
      "puuid":         str,
      "name":          str,
      "tag":           str,
      "is_me":         bool,
      "party_id":      str,            # party UUID — players sharing an ID queued together
      "agent":         str,
      "agent_icon":    str,
      "team":          str,            # "Blue" | "Red"
      "rank":          str,            # "Gold 1" (currenttier_patched)
      "rank_short":    str,            # "gold" — for CSS class
      "kills":         int,
      "deaths":        int,
      "assists":       int,
      "acs":           float,
      "adr":           float|null,
      "hs_pct":        float|null,
      "ability_casts": {               # match-level ability usage totals
        "c": int,
        "q": int,
        "e": int,
        "x": int,
      },
      "first_bloods":  int,            # rounds in which this player got the first kill
      "first_deaths":  int,            # rounds in which this player was the first to die
    }
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from api_henrik import Match

# ── Project imports (same assumptions as the other scripts) ──────────────────
try:
    from api_henrik import UnofficialApi
    from match_history_processor import MatchHistoryProcessor
    from db_valorant import ValorantDB
    from api_valorant_assets import ValAssetApi
    from collect_round_stats import discover_acts, TIER_NORMALISE
except ImportError as exc:
    sys.exit(
        f"[ERROR] Could not import project modules: {exc}\n"
        "Run from the project root or add it to PYTHONPATH."
    )


# ── Rank helpers ──────────────────────────────────────────────────────────────

# Maps the full tier name string to a short CSS-friendly key
_RANK_SHORT: Dict[str, str] = {
    **{f"Iron {n}":      "iron"     for n in (1, 2, 3)},
    **{f"Bronze {n}":    "bronze"   for n in (1, 2, 3)},
    **{f"Silver {n}":    "silver"   for n in (1, 2, 3)},
    **{f"Gold {n}":      "gold"     for n in (1, 2, 3)},
    **{f"Platinum {n}":  "plat"     for n in (1, 2, 3)},
    **{f"Diamond {n}":   "diamond"  for n in (1, 2, 3)},
    **{f"Ascendant {n}": "ascend"   for n in (1, 2, 3)},
    **{f"Immortal {n}":  "immortal" for n in (1, 2, 3)},
    "Radiant":           "radiant",
}

def rank_short(tier_name: str) -> str:
    return _RANK_SHORT.get(tier_name, "iron")


# ── Patch string helper ───────────────────────────────────────────────────────

import re
_PATCH_RE = re.compile(r'release-(\d+)\.(\d+)-')

def extract_patch(game_version: Optional[str]) -> str:
    if not game_version:
        return "?.??"
    m = _PATCH_RE.search(game_version)
    if not m:
        return "?.??"
    return f"{m.group(1)}.{int(m.group(2)):02d}"


# ── Safe attribute helpers ────────────────────────────────────────────────────

def _safe_int(obj, *attrs, default=0) -> int:
    """Walk a chain of attributes safely, returning an int."""
    for attr in attrs:
        if obj is None:
            return default
        obj = getattr(obj, attr, None)
    try:
        return int(obj) if obj is not None else default
    except (TypeError, ValueError):
        return default


def _safe_float(obj, *attrs, default=None):
    for attr in attrs:
        if obj is None:
            return default
        obj = getattr(obj, attr, None)
    try:
        return float(obj) if obj is not None else default
    except (TypeError, ValueError):
        return default


# ── Player row builder ────────────────────────────────────────────────────────

def _player_row(
    player,
    my_puuid:    str,
    rounds:      int,
    agent_icons: Dict[str, str],
    first_bloods: Dict[str, int],
    first_deaths:  Dict[str, int],
) -> dict:
    """Build a single scoreboard player record from a Match.players entry."""
    agent_name = player.character or "Unknown"
    kills   = _safe_int(player, "stats", "kills")
    deaths  = _safe_int(player, "stats", "deaths")
    assists = _safe_int(player, "stats", "assists")
    score   = _safe_int(player, "stats", "score")
    hs      = _safe_int(player, "stats", "headshots")
    bs      = _safe_int(player, "stats", "bodyshots")
    ls      = _safe_int(player, "stats", "legshots")
    total_shots = hs + bs + ls

    # ADR — try several attribute paths the Henrik wrapper might expose
    damage = (
        _safe_float(player, "damage_made") or
        _safe_float(player, "stats", "damage_made") or
        _safe_float(player, "stats", "damage")
    )

    # Rank — use .currenttier_patched when available (e.g. "Gold 1"),
    # else fall back to .currenttier (int) via TIER_NORMALISE
    rank_full = (
        getattr(player, "currenttier_patched", None)
        or getattr(player, "current_tier_patched", None)
        or ""
    )
    if not rank_full:
        tier_int = getattr(player, "currenttier", 0) or 0
        # Approximate reverse lookup from tier integer (1–27 → name)
        rank_full = f"Tier {tier_int}"

    name = getattr(player, "name", "") or ""
    tag  = getattr(player, "tag",  "") or ""

    # Ability casts (match-level totals from player.ability_casts)
    ac = getattr(player, "ability_casts", None)
    ability_casts = {
        "c": _safe_int(ac, "c_cast") if ac else 0,
        "q": _safe_int(ac, "q_cast") if ac else 0,
        "e": _safe_int(ac, "e_cast") if ac else 0,
        "x": _safe_int(ac, "x_cast") if ac else 0,
    }

    puuid = player.puuid

    return {
        "puuid":         puuid,
        "name":          name,
        "tag":           tag,
        "is_me":         puuid == my_puuid,
        "party_id":      getattr(player, "party_id", None) or "",
        "agent":         agent_name,
        "agent_icon":    agent_icons.get(agent_name, ""),
        "team":          (player.team_id or "").capitalize(),
        "rank":          rank_full,
        "rank_short":    rank_short(rank_full),
        "kills":         kills,
        "deaths":        deaths,
        "assists":       assists,
        "acs":           round(score / rounds, 1) if rounds else 0,
        "adr":           round(damage / rounds, 1) if (damage and rounds) else None,
        "hs_pct":        round(hs / total_shots * 100, 1) if total_shots else None,
        "ability_casts": ability_casts,
        "first_bloods":  first_bloods.get(puuid, 0),
        "first_deaths":  first_deaths.get(puuid, 0),
    }


# ── Shared assets builder ─────────────────────────────────────────────────────

def _build_assets() -> Dict[str, Any]:
    """
    Fetch all map and agent image assets from the Valorant Assets API and
    return them as a single self-contained dict suitable for embedding in
    the JS output.

    Shape
    -----
    {
      "maps": {
        "<displayName>": {
          "displayIcon":            str,   # small icon used in UI lists
          "listViewIcon":           str,   # medium icon for list views
          "listViewIconTall":       str,   # tall variant
          "splash":                 str,   # full loading-screen art
          "stylizedBackgroundImage": str,  # stylized bg (used in career screens)
          "premierBackgroundImage": str,   # premier mode bg
          "mapUrl":                 str,   # minimap / overhead image
        },
        …
      },
      "agents": {
        "<displayName>": {
          "displayIcon":      str,
          "displayIconSmall": str,
          "bustPortrait":     str,
          "fullPortrait":     str,
          "killfeedPortrait": str,
        },
        …
      }
    }
    """
    api = ValAssetApi()

    # ── Maps ──────────────────────────────────────────────────────────────────
    maps_assets: Dict[str, Any] = {}
    for map_item in api.maps:                          # List[MapItem]
        maps_assets[map_item.displayName] = {
            "displayIcon":             getattr(map_item, "displayIcon",             "") or "",
            "listViewIcon":            getattr(map_item, "listViewIcon",            "") or "",
            "listViewIconTall":        getattr(map_item, "listViewIconTall",        "") or "",
            "splash":                  getattr(map_item, "splash",                  "") or "",
            "stylizedBackgroundImage": getattr(map_item, "stylizedBackgroundImage", "") or "",
            "premierBackgroundImage":  getattr(map_item, "premierBackgroundImage",  "") or "",
            "mapUrl":                  getattr(map_item, "mapUrl",                  "") or "",
        }

    # ── Agents ────────────────────────────────────────────────────────────────
    agents_assets: Dict[str, Any] = {}
    for agent_name, agent_info in api.agents.items():  # Dict[str, AgentItem]
        agents_assets[agent_name] = {
            "displayIcon":      getattr(agent_info, "displayIcon",      "") or "",
            "displayIconSmall": getattr(agent_info, "displayIconSmall", "") or "",
            "bustPortrait":     getattr(agent_info, "bustPortrait",     "") or "",
            "fullPortrait":     getattr(agent_info, "fullPortrait",     "") or "",
            "killfeedPortrait": getattr(agent_info, "killfeedPortrait", "") or "",
        }

    return {"maps": maps_assets, "agents": agents_assets}


# ── Core builder ─────────────────────────────────────────────────────────────

def build_match_history(
    recent_matches: list[Match], # newest-first
    recent_mmr: Dict,            # match_id → MmrItem
    my_puuid:    str,
    player_name: str,
    player_tag:  str,
    region:      str = "na",
) -> dict:
    """
    Build the complete MATCH_HISTORY_DATA dict.

    Parameters mirror what engine.py already computes and passes around,
    so calling this from engine.py costs zero extra API calls.
    """
    # Pre-build shared assets (one singleton call; fully cached).
    # agent_icons is a flat name→URL shortcut derived from the assets block
    # so match records don't need to re-look up the full assets dict at runtime.
    shared_assets = _build_assets()
    agent_icons: Dict[str, str] = {
        name: info["displayIconSmall"] or info["displayIcon"]
        for name, info in shared_assets["agents"].items()
    }

    match_records = []
    wins = losses = draws = 0

    for idx, match in enumerate(recent_matches):
        mid      = match.metadata.match_id
        mmr_item = recent_mmr.get(mid)
        meta     = match.metadata
        rounds   = meta.rounds_played or 1   # avoid div-by-zero

        # ── MMR / rank / RR ──────────────────────────────────────────────────
        rank_name = ""
        rr        = None
        season    = ""
        if mmr_item:
            rank_name = mmr_item.tier.name if mmr_item.tier else ""
            rr        = _safe_int(mmr_item, "ranking_in_tier")
            season    = getattr(mmr_item, "season", None)
            season    = getattr(season,   "short",  "") if season else ""

        # RR delta: compare with the next-older match's MMR
        rr_delta = None
        if mmr_item and idx + 1 < len(recent_matches):
            prev_mid  = recent_matches[idx + 1].metadata.match_id
            prev_mmr  = recent_mmr.get(prev_mid)
            if prev_mmr and mmr_item.elo is not None and prev_mmr.elo is not None:
                rr_delta = int(mmr_item.elo) - int(prev_mmr.elo)

        # ── Teams + result ────────────────────────────────────────────────────
        my_team_id      = None
        my_player       = None
        teams_dict      = {}

        try:
            teams_dict = match.teams.as_dict()   # {team_id: {"has_won": bool, ...}}
        except Exception:
            pass

        for player in match.players:
            if player.puuid == my_puuid:
                my_team_id = player.team_id.lower()
                my_player  = player
                break

        winning_team_id = next(
            (tid for tid, data in teams_dict.items() if data.get("has_won")), None
        )
        # Determine result: draw when no team is flagged as winner, or scores are tied
        if winning_team_id is None:
            result = "draw"
            draws += 1
        elif my_team_id and my_team_id == winning_team_id:
            result = "win"
            wins += 1
        else:
            result = "loss"
            losses += 1

        # Score (rounds won per team)
        score_us   = 0
        score_them = 0
        if my_team_id and teams_dict:
            for tid, data in teams_dict.items():
                won_rounds = data.get("rounds_won", 0) or data.get("rounds_won", 0)
                if tid == my_team_id:
                    score_us   = won_rounds
                else:
                    score_them = won_rounds

        # ── My player stats ───────────────────────────────────────────────────
        if my_player:
            agent_name = my_player.character or "Unknown"
            kills      = _safe_int(my_player, "stats", "kills")
            deaths     = _safe_int(my_player, "stats", "deaths")
            assists    = _safe_int(my_player, "stats", "assists")
            score      = _safe_int(my_player, "stats", "score")
            hs         = _safe_int(my_player, "stats", "headshots")
            bs         = _safe_int(my_player, "stats", "bodyshots")
            ls         = _safe_int(my_player, "stats", "legshots")
            total_shots = hs + bs + ls

            damage = (
                _safe_float(my_player, "damage_made") or
                _safe_float(my_player, "stats", "damage_made") or
                _safe_float(my_player, "stats", "damage")
            )
            acs    = round(score  / rounds, 1) if rounds else 0
            adr    = round(damage / rounds, 1) if (damage and rounds) else None
            hs_pct = round(hs / total_shots * 100, 1) if total_shots else None
            agent_icon = agent_icons.get(agent_name, "")
        else:
            agent_name = kills = deaths = assists = 0
            acs = adr = hs_pct = None
            agent_icon = ""

        # ── First bloods and first deaths (per round) ─────────────────────────
        # For each round the first kill event (by kill_time_in_round) identifies
        # the first-blood getter and the first-death victim.
        first_bloods: Dict[str, int] = {}
        first_deaths:  Dict[str, int] = {}
        try:
            for rnd in match.rounds:
                all_kills = [
                    ke
                    for ps in rnd.player_stats
                    for ke in ps.kill_events
                ]
                if all_kills:
                    earliest = min(
                        all_kills,
                        key=lambda ke: ke.kill_time_in_round if ke.kill_time_in_round is not None else float("inf"),
                    )
                    killer = earliest.killer_puuid
                    victim = earliest.victim_puuid
                    if killer:
                        first_bloods[killer] = first_bloods.get(killer, 0) + 1
                    if victim:
                        first_deaths[victim] = first_deaths.get(victim, 0) + 1
        except Exception:
            pass  # rounds unavailable for this match — silently skip

        # ── Scoreboard ────────────────────────────────────────────────────────
        blue_rows = []
        red_rows  = []
        for player in match.players:
            row = _player_row(player, my_puuid, rounds, agent_icons, first_bloods, first_deaths)
            if (player.team_id or "").lower() in ("blue", "red"):
                target = blue_rows if player.team_id.lower() == "blue" else red_rows
            else:
                # Fallback: split by my team vs opponent
                target = blue_rows if player.team_id == my_team_id else red_rows
            target.append(row)

        # Sort each team by ACS descending
        blue_rows.sort(key=lambda r: r["acs"], reverse=True)
        red_rows.sort(key=lambda r:  r["acs"], reverse=True)

        # ── Queue / game mode ─────────────────────────────────────────────────
        queue = (
            getattr(meta, "queue",     None) or
            getattr(meta, "mode",      None) or
            getattr(meta, "game_mode", None) or
            "Competitive"
        )
        # Normalise the queue string (API sometimes returns raw enum-like values)
        _queue_map = {
            "competitive":  "Competitive",
            "unrated":      "Unrated",
            "deathmatch":   "Deathmatch",
            "spikerush":    "Spike Rush",
            "escalation":   "Escalation",
            "teamdeathmatch": "Team DM",
            "swiftplay":    "Swift Play",
        }
        queue_norm = _queue_map.get(str(queue).lower().replace(" ", ""), str(queue).title())

        # ── Timestamp + duration ──────────────────────────────────────────────
        game_start_ms = int(meta.game_start or 0)
        # game_length is in milliseconds in the Henrik API
        duration_raw  = getattr(meta, "game_length", None)
        duration_sec  = int(duration_raw / 1000) if duration_raw else None

        # ── Cluster (server) ──────────────────────────────────────────────────
        cluster = (
            getattr(meta, "cluster", None) or
            getattr(meta, "region",  None) or
            ""
        )

        match_records.append({
            "id":             mid,
            "map":            meta.map or "Unknown",
            "queue":          queue_norm,
            "cluster":        cluster,
            "result":         result,
            "score_us":       score_us,
            "score_them":     score_them,
            "rounds":         meta.rounds_played or 0,
            "game_start_ms":  game_start_ms,
            "duration_sec":   duration_sec,
            "season":         season,
            "patch":          extract_patch(getattr(meta, "game_version", None)),
            "rank":           rank_name,
            "rr":             rr,
            "rr_delta":       rr_delta,
            "agent":          agent_name if isinstance(agent_name, str) else "Unknown",
            "agent_icon":     agent_icon,
            "kills":          kills,
            "deaths":         deaths,
            "assists":        assists,
            "acs":            acs,
            "adr":            adr,
            "hs_pct":         hs_pct,
            "scoreboard": {
                "blue": blue_rows,
                "red":  red_rows,
            },
        })

    total = wins + losses + draws
    return {
        "meta": {
            "name":          player_name,
            "tag":           player_tag,
            "puuid":         my_puuid,
            "region":        region,
            "match_count":   total,
            "wins":          wins,
            "losses":        losses,
            "draws":         draws,
            "winrate":       round(wins / total * 100, 1) if total else 0,
            "generated_at":  datetime.now(timezone.utc).isoformat(),
        },
        # All static image assets keyed by display name.
        # Consumers (match-history.html) can look up any map or agent asset
        # directly from this block without extra network requests:
        #   MATCH_HISTORY_DATA.assets.maps["Ascent"].splash
        #   MATCH_HISTORY_DATA.assets.agents["Jett"].displayIconSmall
        "assets": shared_assets,
        "matches": match_records,
    }


# ── File writer ───────────────────────────────────────────────────────────────

def write_match_history(
    recent_matches,
    recent_mmr: Dict,
    my_puuid:    str,
    player_name: str,
    player_tag:  str,
    region:      str   = "na",
    output_dir         = ".",
    filename:    str   = "match_history.js",
) -> Path:
    """
    Build and write ``match_history.js`` to *output_dir*.

    Returns the path written.
    """
    from pathlib import Path as _Path
    out_dir = _Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / filename

    data = build_match_history(
        recent_matches, recent_mmr, my_puuid, player_name, player_tag, region
    )
    payload = json.dumps(data, indent=2, ensure_ascii=False)

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("// Auto-generated by generate_match_history.py — do not edit manually\n")
        f.write("window.MATCH_HISTORY_DATA = ")
        f.write(payload)
        f.write(";\n")

    m = data["meta"]
    draws_str = f" / {m['draws']}D" if m.get("draws") else ""
    print(f"[match_history] {m['match_count']} matches  "
          f"({m['wins']}W / {m['losses']}L{draws_str}, {m['winrate']:.1f}% WR)")
    print(f"[match_history] Written → {out_path}")
    return out_path


# ── Standalone entry point ────────────────────────────────────────────────────

def run(name: str, tag: str, match_max: int, out: str, region: str) -> None:
    api  = UnofficialApi()
    db   = ValorantDB(region=region)

    print(f"[INFO] Looking up {name}#{tag}…")
    account  = api.get_account_by_name(name, tag)
    my_puuid = account.puuid
    print(f"[INFO] PUUID: {my_puuid[:12]}…")

    print("[INFO] Updating match history…")
    db.update_match_history_for_puuid(my_puuid) 

    print("[INFO] Discovering acts…")
    acts = discover_acts(api, name, tag)
    if not acts:
        sys.exit("[ERROR] No acts found.")

    print(f"[INFO] Building MatchHistoryProcessor (max {match_max} matches)…")
    history      = MatchHistoryProcessor(my_puuid, acts, db, match_count_max=match_max)
    matches      = history.recent_matches
    mmr_by_match = history.recent_mmr
    print(f"[INFO] {len(matches)} matches loaded.")

    write_match_history(
        recent_matches = matches,
        recent_mmr     = mmr_by_match,
        my_puuid       = my_puuid,
        player_name    = name,
        player_tag     = tag,
        region         = region,
        output_dir     = str(Path(out).parent),
        filename       = Path(out).name,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Generate match_history.js for match-history.html",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--name",   default="0dmg",  help="Riot ID name")
    parser.add_argument("--tag",    default="sadge", help="Riot ID tag")
    parser.add_argument("--max",    type=int, default=100, metavar="N",
                        help="Max matches to pull (default: 100)")
    parser.add_argument("--out",    default="match_history.js",
                        help="Output JS path (default: match_history.js)")
    parser.add_argument("--region", default="na", help="Server region (default: na)")

    args = parser.parse_args()
    run(args.name, args.tag, args.max, args.out, args.region)