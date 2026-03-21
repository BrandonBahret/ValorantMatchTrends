"""
collect_round_stats.py
======================
Collect round-duration stats for the util charge analyzer.

Usage
-----
    python collect_round_stats.py --name 0dmg --tag sadge
    python collect_round_stats.py --name 0dmg --tag sadge --out my_stats.json
    python collect_round_stats.py --name 0dmg --tag sadge --max 200 --raw
    python collect_round_stats.py --name 0dmg --tag sadge --patch 11.11
    python collect_round_stats.py --name 0dmg --tag sadge --exact-patch 11.11
    python collect_round_stats.py --name 0dmg --tag sadge --patch 11.07 --timespan 365
    python collect_round_stats.py --name 0dmg --tag sadge --patch 11.07 --from-act e10a5 --timespan 365

Patch flags
-----------
--patch 11.11
    Include only matches on patch 11.11 *and earlier*.
    Patch numbers are compared numerically (major.minor), so
    11.09 < 11.10 < 11.11 < 12.01, etc.

--exact-patch 11.11
    Include only matches on exactly patch 11.11.
    Mutually exclusive with --patch.

Patch is read from match.metadata.game_version, which looks like
    'release-11.11-shipping-10-4091853'
Matches whose game_version cannot be parsed are always skipped when
either patch flag is active.

The script:
  1. Resolves the player PUUID from name#tag
  2. Pulls their MMR history to discover which acts they have games in
  3. Feeds everything into MatchHistoryProcessor (same call as your snippet)
  4. Walks every round of every match and computes real stats
  5. Writes round_stats.json (consumed by valorant_util_analyzer.html)
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from api_henrik import Match, MatchRoundsItem

# ── Project imports ──────────────────────────────────────────────────────────
try:
    from api_henrik import AffinitiesEnum, UnofficialApi
    from match_history_processor import MatchHistoryProcessor
    from db_valorant import ValorantDB
except ImportError as exc:
    sys.exit(
        f"[ERROR] Could not import project modules: {exc}\n"
        "Run from the project root, or add it to PYTHONPATH."
    )

# ── Constants ────────────────────────────────────────────────────────────────
ROUND_MAX_SEC = 100.0
HIST_BIN_SEC  = 5
HIST_BINS     = list(range(0, int(ROUND_MAX_SEC), HIST_BIN_SEC))

TIER_NORMALISE: Dict[str, str] = {
    "Iron 1": "iron",     "Iron 2": "iron",     "Iron 3": "iron",
    "Bronze 1": "bronze", "Bronze 2": "bronze", "Bronze 3": "bronze",
    "Silver 1": "silver", "Silver 2": "silver", "Silver 3": "silver",
    "Gold 1": "gold",     "Gold 2": "gold",     "Gold 3": "gold",
    "Platinum 1": "plat", "Platinum 2": "plat", "Platinum 3": "plat",
    "Diamond 1": "diamond","Diamond 2": "diamond","Diamond 3": "diamond",
    "Ascendant 1":"ascend","Ascendant 2":"ascend","Ascendant 3":"ascend",
    "Immortal 1":"immortal","Immortal 2":"immortal","Immortal 3":"immortal",
    "Radiant": "radiant",
}

# ── Patch helpers ─────────────────────────────────────────────────────────────

_PATCH_RE = re.compile(r'release-(\d+)\.(\d+)-')


def parse_patch(game_version: Optional[str]) -> Optional[Tuple[int, int]]:
    """
    Extract the numeric patch tuple from a game_version string.

    'release-11.11-shipping-10-4091853'  →  (11, 11)
    'release-10.09-shipping-…'           →  (10,  9)
    Returns None if the string is missing or doesn't match.
    """
    if not game_version:
        return None
    m = _PATCH_RE.search(game_version)
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)))


def patch_from_str(patch_str: str) -> Tuple[int, int]:
    """
    Parse a user-supplied patch string like '11.11' into (11, 11).
    Raises ValueError on bad input so argparse can surface it cleanly.
    """
    parts = patch_str.strip().split('.')
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        raise ValueError(f"Patch must be in MAJOR.MINOR format, e.g. '11.11'. Got: {patch_str!r}")
    return (int(parts[0]), int(parts[1]))


def patch_filter(
    match_patch: Optional[Tuple[int, int]],
    ceiling: Optional[Tuple[int, int]],
    exact: Optional[Tuple[int, int]],
) -> bool:
    """
    Return True if the match should be *included*.

    ceiling  → keep patches <= ceiling  (--patch)
    exact    → keep patches == exact    (--exact-patch)
    If neither flag is set, always return True.
    If match_patch is None and a filter is active, return False (unparseable → skip).
    """
    if ceiling is None and exact is None:
        return True                    # no filter, keep everything
    if match_patch is None:
        return False                   # can't determine patch → skip
    if exact is not None:
        return match_patch == exact
    return match_patch <= ceiling      # type: ignore[operator]


# ── Act discovery ─────────────────────────────────────────────────────────────


def discover_acts(api: UnofficialApi, name: str, tag: str) -> List[Tuple[str, str]]:
    """
    Discover acts that have data by reading the season.short field directly
    off the player's lifetime MMR history entries.

    Each V1LifetimeMmrHistoryItem already carries a .season.short string like
    'e7a3', so we just collect the unique values — no enum, no hardcoded list,
    no fragile attribute walking on V2mmrBySeason.

    Falls back to get_act_performance (V2mmr) if the MMR history call fails,
    and if that also fails, returns an empty list so the caller can decide.
    """
    print("[INFO] Discovering acts from MMR history …")

    def parse_short(short: str) -> Optional[Tuple[str, str]]:
        """'e7a3' → ('7', '3'), handles any episode/act number."""
        s = short.lower().strip()
        if not s.startswith('e') or 'a' not in s:
            return None
        try:
            ep, act = s[1:].split('a', 1)
            if ep.isdigit() and act.isdigit():
                return (ep, act)
        except ValueError:
            pass
        return None

    # ── Primary: read season.short from every MMR history entry ──────────
    try:
        history = api.get_recent_mmr_history_by_name(name, tag)
        seen: dict[str, Tuple[str, str]] = {}   # short → tuple, insertion-ordered

        for item in history.data:
            short = getattr(item.season, 'short', None)
            if not short or short in seen:
                continue
            tup = parse_short(short)
            if tup:
                seen[short] = tup
                print(f"  found act: {short}")

        if seen:
            # MMR history is returned newest-first by the API
            return list(seen.values())

        print("  [WARN] MMR history returned no season data, trying V2mmr …")

    except Exception as exc:
        print(f"  [WARN] MMR history call failed ({exc}), trying V2mmr …")

    # ── Fallback: V2mmr by_season — walk its __dict__ dynamically ────────
    try:
        v2 = api.get_act_performance_by_name(name, tag)
        by_season = v2.by_season
        found = []

        for attr, val in vars(by_season).items():
            # vars() includes private mangled names; skip those
            if attr.startswith('_'):
                continue
            tup = parse_short(attr)
            if tup is None:
                continue
            raw_games = getattr(val, 'number_of_games', None)
            try:
                games = int(raw_games) if raw_games is not None else 0
            except (TypeError, ValueError):
                games = 0
            if games > 0:
                found.append((attr, tup))
                print(f"  found act: {attr}  ({games} games)")

        if found:
            found.sort(key=lambda x: x[0], reverse=True)   # newest first
            return [tup for _, tup in found]

        print("  [WARN] V2mmr returned no acts with games.")

    except Exception as exc:
        print(f"  [WARN] V2mmr fallback failed ({exc}).")

    return []


# ── Round data extraction ─────────────────────────────────────────────────────

def extract_round_record(match: Match, round_index: int, round_obj: MatchRoundsItem, mmr_item, puuid: str) -> Optional[dict]:
    """
    Build one RoundRecord dict from a MatchRoundsItem.

    Duration reconstruction priority (API has no explicit per-round field):
      1. defuse_time_in_round   — round ended on defuse
      2. latest kill_time_in_round across all player_stats
      3. plant_time_in_round + 45s spike timer (upper-bound cap)
      4. match game_length / rounds_played   — last resort average
    """
    meta     = match.metadata
    planted  = bool(round_obj.bomb_planted)
    defused  = bool(round_obj.bomb_defused)
    end_type = round_obj.end_type or ""

    # Plant time (ms → s)
    plant_time_sec: Optional[float] = None
    if planted and round_obj.plant_events:
        t = round_obj.plant_events.plant_time_in_round
        if t is not None:
            plant_time_sec = t / 1000.0

    # Defuse time (ms → s)
    defuse_time_sec: Optional[float] = None
    if defused and round_obj.defuse_events:
        t = round_obj.defuse_events.defuse_time_in_round
        if t is not None:
            defuse_time_sec = t / 1000.0

    # Build duration
    duration_sec: Optional[float] = None

    if defuse_time_sec is not None:
        duration_sec = defuse_time_sec

    if duration_sec is None:
        latest_ms: Optional[int] = None
        for ps in round_obj.player_stats:
            for ke in ps.kill_events:
                t = ke.kill_time_in_round
                if t is not None and (latest_ms is None or t > latest_ms):
                    latest_ms = t
        if latest_ms is not None:
            duration_sec = latest_ms / 1000.0

    if duration_sec is None and plant_time_sec is not None:
        duration_sec = plant_time_sec + 45.0

    if duration_sec is None and meta.rounds_played:
        duration_sec = (meta.game_length / 1000.0) / meta.rounds_played

    if duration_sec is None:
        return None

    duration_sec = max(3.0, min(ROUND_MAX_SEC, duration_sec))

    post_plant_sec: Optional[float] = None
    if plant_time_sec is not None:
        post_plant_sec = max(0.0, duration_sec - plant_time_sec)

    # Find which team the tracked player was on this round
    player_team: Optional[str] = None
    for ps in round_obj.player_stats:
        if ps.player_puuid == puuid:
            player_team = ps.player_team
            break

    return {
        "match_id":       meta.match_id,
        "round_index":    round_index,
        "map":            meta.map or "Unknown",
        "act":            mmr_item.season.short or "unknown",
        "patch":          meta.game_version or "unknown",
        "tier_id":        int(mmr_item.tier.id)  if mmr_item.tier else -1,
        "tier_name":      mmr_item.tier.name      if mmr_item.tier else "Unknown",
        "elo":            int(mmr_item.elo)        if mmr_item.elo is not None else -1,
        "duration_sec":   round(duration_sec, 3),
        "planted":        planted,
        "plant_time_sec": round(plant_time_sec, 3) if plant_time_sec is not None else None,
        "post_plant_sec": round(post_plant_sec, 3) if post_plant_sec is not None else None,
        "defused":        defused if planted else None,
        "end_type":       end_type,
        "winning_team":   round_obj.winning_team or "",
        "player_team":    player_team,
    }


# ── Statistics ────────────────────────────────────────────────────────────────

def percentile(data: List[float], pct: float) -> float:
    if not data:
        return 0.0
    s = sorted(data)
    k = (len(s) - 1) * pct / 100.0
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def build_histogram(durations: List[float]) -> Dict[str, int]:
    counts = {str(b): 0 for b in HIST_BINS}
    for d in durations:
        b = min(int(d // HIST_BIN_SEC) * HIST_BIN_SEC, HIST_BINS[-1])
        counts[str(max(0, b))] += 1
    return counts


def safe(lst, fn):
    try:
        return round(fn(lst), 3) if lst else 0.0
    except Exception:
        return 0.0


def compute_stats(rounds: List[dict]) -> dict:
    if not rounds:
        return {}

    durations   = [r["duration_sec"]   for r in rounds]
    plant_times = [r["plant_time_sec"] for r in rounds if r["plant_time_sec"] is not None]
    post_plants = [r["post_plant_sec"] for r in rounds if r["post_plant_sec"] is not None]

    planted  = [r for r in rounds if r["planted"]]
    defused  = [r for r in planted if r.get("defused")]
    no_plant = [r for r in rounds if not r["planted"]]
    elim_end = [r for r in no_plant if "elim" in r.get("end_type", "").lower()]

    return {
        "sample_rounds":     len(rounds),
        "median":            round(percentile(durations, 50), 3),
        "mean":              safe(durations, statistics.mean),
        "p25":               round(percentile(durations, 25), 3),
        "p75":               round(percentile(durations, 75), 3),
        "sd":                safe(durations, statistics.pstdev),
        "plant_rate":        round(len(planted) / len(rounds), 4),
        "median_plant_time": round(percentile(plant_times, 50), 3) if plant_times else 0.0,
        "mean_plant_time":   safe(plant_times, statistics.mean),
        "sd_plant_time":     safe(plant_times, statistics.pstdev),
        "p25_plant_time":    round(percentile(plant_times, 25), 3) if plant_times else 0.0,
        "p75_plant_time":    round(percentile(plant_times, 75), 3) if plant_times else 0.0,
        "median_post_plant": round(percentile(post_plants, 50), 3) if post_plants else 0.0,
        "mean_post_plant":   safe(post_plants, statistics.mean),
        "sd_post_plant":     safe(post_plants, statistics.pstdev),
        "p25_post_plant":    round(percentile(post_plants, 25), 3) if post_plants else 0.0,
        "p75_post_plant":    round(percentile(post_plants, 75), 3) if post_plants else 0.0,
        "defuse_rate":       round(len(defused) / len(planted), 4) if planted else 0.0,
        "elim_rate":         round(len(elim_end) / len(no_plant), 4) if no_plant else 0.0,
        "duration_hist":     build_histogram(durations),
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def run(
    name: str,
    tag: str,
    match_max: int,
    out: str,
    include_raw: bool,
    patch_ceiling: Optional[Tuple[int, int]],
    patch_exact: Optional[Tuple[int, int]],
    timespan: int,
    from_act: Optional[str],
    region: str,
):

    api = UnofficialApi()
    db  = ValorantDB(region=region)

    print(f"[INFO] Region: {region}")

    # Describe the active patch filter for logging
    if patch_exact is not None:
        patch_desc = f"exact patch {patch_exact[0]}.{patch_exact[1]:02d}"
    elif patch_ceiling is not None:
        patch_desc = f"patches <= {patch_ceiling[0]}.{patch_ceiling[1]:02d}"
    else:
        patch_desc = "all patches"
    print(f"[INFO] Patch filter: {patch_desc}")

    # 1. Resolve PUUID
    print(f"[INFO] Looking up {name}#{tag} …")
    account = api.get_account_by_name(name, tag)
    puuid   = account.puuid
    print(f"[INFO] PUUID: {puuid}")

    # 2. Discover which acts have data
    acts = discover_acts(api, name, tag)
    if not acts:
        sys.exit("[ERROR] Could not discover any acts for this player. Check name/tag and try again.")

    # Optionally slice acts to start from a specific act (and older)
    if from_act is not None:
        acts_formatted = ['e{}a{}'.format(*a) for a in acts]
        if from_act not in acts_formatted:
            sys.exit(
                f"[ERROR] --from-act {from_act!r} not found in discovered acts: {acts_formatted}\n"
                "Check the act label (e.g. e10a5) and try again."
            )
        idx  = acts_formatted.index(from_act)
        acts = acts[idx:]
        print(f"[INFO] --from-act {from_act}: sliced to {['e{}a{}'.format(*a) for a in acts]}")
    else:
        print(f"[INFO] Using acts: {['e{}a{}'.format(*a) for a in acts]}")

    # 3. Pull match history — identical to your snippet
    print(f"[INFO] Building MatchHistoryProcessor (max {match_max} matches, timespan {timespan} days) …")
    history = MatchHistoryProcessor(
        puuid,
        acts,
        db,
        match_count_max=match_max,
        timespan=timespan,
    )

    matches      = history.get_recent_matches()
    mmr_by_match = history.get_all_mmr_data()
    print(f"[INFO] {len(matches)} matches loaded.")

    if not matches:
        sys.exit("[ERROR] No matches found. Check the name/tag or try --max with a higher value.")

    # 4. Walk every round of every match
    all_rounds: List[dict] = []
    skipped_matches = 0
    skipped_rounds  = 0
    patch_skipped_matches = 0

    for match in matches:
        mid      = match.metadata.match_id
        mmr_item = mmr_by_match.get(mid)

        if mmr_item is None:
            skipped_matches += 1
            continue

        # Skip placement / unranked matches
        if TIER_NORMALISE.get(mmr_item.tier.name if mmr_item.tier else "", None) is None:
            skipped_matches += 1
            continue

        # ── Patch filter ──────────────────────────────────────────────────
        match_patch = parse_patch(match.metadata.game_version)
        if not patch_filter(match_patch, patch_ceiling, patch_exact):
            patch_skipped_matches += 1
            continue

        map_name  = match.metadata.map or "Unknown"
        act_label = mmr_item.season.short or "unknown"
        tier_name = mmr_item.tier.name if mmr_item.tier else "?"
        patch_str = (
            f"{match_patch[0]}.{match_patch[1]:02d}" if match_patch else "?.??"
        )

        print(f"  {mid[:8]}…  {map_name:12s}  {act_label}  {tier_name:15s}  "
              f"patch {patch_str}  {match.metadata.rounds_played} rounds")

        for idx in range(match.number_of_rounds):
            try:
                rnd    = match.get_round(idx)
                record = extract_round_record(match, idx, rnd, mmr_item, puuid)
                if record is None:
                    skipped_rounds += 1
                    continue
                all_rounds.append(record)
            except Exception as exc:
                print(f"    [WARN] round {idx}: {exc}")
                skipped_rounds += 1

    print(f"\n[INFO] {len(all_rounds)} round records collected "
          f"({skipped_matches} matches skipped [rank/MMR], "
          f"{patch_skipped_matches} matches skipped [patch filter], "
          f"{skipped_rounds} rounds skipped)")

    if not all_rounds:
        sys.exit("[ERROR] No usable round data found.")

    # 5. Compute statistics sliced by rank / act / map
    print("[INFO] Computing statistics …")

    by_rank_raw: Dict[str, List[dict]] = defaultdict(list)
    by_act_raw:  Dict[str, List[dict]] = defaultdict(list)
    by_map_raw:  Dict[str, List[dict]] = defaultdict(list)

    for r in all_rounds:
        k = TIER_NORMALISE.get(r["tier_name"])
        if k:
            by_rank_raw[k].append(r)
        by_act_raw[r["act"]].append(r)
        by_map_raw[r["map"]].append(r)

    output = {
        "meta": {
            "name":          name,
            "tag":           tag,
            "region":        region,
            "puuid":         puuid,
            "acts":          ["e{}a{}".format(*a) for a in acts],
            "from_act":      from_act,
            "match_count":   len(matches) - skipped_matches - patch_skipped_matches,
            "round_count":   len(all_rounds),
            "patch_filter":  patch_desc,
            "timespan_days": timespan,
            "generated_at":  datetime.now(timezone.utc).isoformat(),
        },
        "overall": compute_stats(all_rounds),
        "by_rank": {k: compute_stats(v) for k, v in by_rank_raw.items() if v},
        "by_act":  {k: compute_stats(v) for k, v in by_act_raw.items()  if v},
        "by_map":  {k: compute_stats(v) for k, v in by_map_raw.items()  if v},
    }

    if include_raw:
        output["raw_rounds"] = all_rounds

    with open(out, "w", encoding="utf-8") as fh:
        json.dump(output, fh, indent=2, ensure_ascii=False)

    # 6. Print summary
    ov = output["overall"]
    print(f"\n── Written → {out} " + "─" * max(0, 44 - len(out)))
    print(f"  Patch filter  : {patch_desc}")
    print(f"  Rounds        : {ov['sample_rounds']}")
    print(f"  Median round  : {ov['median']}s  (P25 {ov['p25']}s – P75 {ov['p75']}s)")
    print(f"  Plant rate    : {ov['plant_rate']*100:.1f}%")
    print(f"  Median plant  : {ov['median_plant_time']}s")
    print(f"  Median post   : {ov['median_post_plant']}s")
    print(f"  Defuse rate   : {ov['defuse_rate']*100:.1f}% of planted rounds")

    if output["by_rank"]:
        rank_order = ["iron","bronze","silver","gold","plat","diamond","ascend","immortal","radiant"]
        print(f"\n  By rank:")
        for tier in rank_order:
            s = output["by_rank"].get(tier)
            if s:
                print(f"    {tier:10s}  n={s['sample_rounds']:4d}  "
                      f"med={s['median']}s  plant={s['median_plant_time']}s  "
                      f"post={s['median_post_plant']}s")

    if output["by_map"]:
        print(f"\n  By map:")
        for map_name, s in sorted(output["by_map"].items()):
            print(f"    {map_name:12s}  n={s['sample_rounds']:4d}  "
                  f"med={s['median']}s  plant={s['median_plant_time']}s")


# ── CLI ───────────────────────────────────────────────────────────────────────

def _patch_type(value: str) -> Tuple[int, int]:
    """argparse type converter for patch strings like '11.11'."""
    try:
        return patch_from_str(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Collect Valorant round stats. Just provide name and tag.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--name", default="0dmg",  help="Riot ID name  (e.g. 0dmg)")
    parser.add_argument("--tag",  default="sadge", help="Riot ID tag   (e.g. sadge)")
    parser.add_argument("--max",  type=int, default=200, metavar="N",
                        help="Max matches to pull (default: 200)")
    parser.add_argument("--out",  default="round_stats.json",
                        help="Output JSON path (default: round_stats.json)")
    parser.add_argument("--raw",  action="store_true",
                        help="Include raw per-round records in the JSON output")
    parser.add_argument("--timespan", type=int, default=60, metavar="DAYS",
                        help="How many days back to look for matches (default: 60)")
    parser.add_argument("--region", default="na",
                        choices=[e.value for e in AffinitiesEnum],
                        help="Server region (default: na)")
    parser.add_argument("--from-act", default=None, metavar="ACT", dest="from_act",
                        help="Only include this act and older (e.g. --from-act e10a5). "
                             "Useful to avoid newer acts eating the --max budget.")

    patch_group = parser.add_mutually_exclusive_group()
    patch_group.add_argument(
        "--patch", type=_patch_type, metavar="MAJOR.MINOR", default=None,
        help="Include only matches on this patch and earlier (e.g. --patch 11.11)",
    )
    patch_group.add_argument(
        "--exact-patch", type=_patch_type, metavar="MAJOR.MINOR", default=None,
        dest="exact_patch",
        help="Include only matches on exactly this patch (e.g. --exact-patch 11.11)",
    )

    args = parser.parse_args()
    run(
        args.name,
        args.tag,
        args.max,
        args.out,
        args.raw,
        patch_ceiling=args.patch,
        patch_exact=args.exact_patch,
        timespan=args.timespan,
        from_act=args.from_act,
        region=args.region,
    )