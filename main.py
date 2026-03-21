#!/usr/bin/env python3
"""
Valorant Rank Analyzer — GUI Launcher
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Generates all charts, graphs, and the interactive HTML report
for any player in one click. Outputs everything to a per-player folder.
"""

from collections import defaultdict
import os
import re
import sys
import json
import threading
import subprocess
import tkinter as tk
from tkinter import ttk, messagebox
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, Dict, List, Tuple, Callable

import shutil

import numpy as np
import matplotlib
matplotlib.use("Agg")                   # Non-interactive — saves to disk, no windows
from matplotlib import pyplot as plt
import matplotlib.patheffects as path_effects
from scipy.stats import gaussian_kde

from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Image as RLImage,
    Table, TableStyle, HRFlowable, PageBreak, KeepTogether,
)
from reportlab.platypus.flowables import Flowable

# ── Project imports ───────────────────────────────────────────────────────────
from api_henrik import AffinitiesEnum, UnofficialApi, Match, V1LifetimeMmrHistoryItem
from db_valorant import ValorantDB
from match_history_processor import MatchHistoryProcessor

from rank_utils      import VALORANT_RANKS, lookup_rank, map_rank_value, valorant_act_in_range
from mmr_spline      import predict_mmr
from rr_model        import predict_rr_change
from match_analysis  import (calculate_winrate, analyze_match_history,
                              sort_matches_by_lobby_std, slice_until_value,
                              find_change_indices)
from agent_stats     import (calculate_agent_stats, calculate_role_percentages,
                              generate_html_report)
from collect_round_stats import (extract_round_record, compute_stats,
                                  TIER_NORMALISE, parse_patch)

# ─────────────────────────────────────────────────────────────────────────────
#  Constants & Defaults
# ─────────────────────────────────────────────────────────────────────────────

OUTPUTS_DIR  = Path("outputs")
CONFIG_FILE  = Path("config.json")
API_WARN_THRESHOLD    = 100
SPLICE_THRESHOLD_RR   = 42
BIG_LOSS_THRESHOLD_RR = 30
RR_DESYNC_THRESHOLD   = 10

# Master list of released acts using Henrik API episode numbering.
# Add new acts here as they ship — the player-lookup will annotate them
# with game counts automatically.
# Episode → season label (Ep 9 = "Ep 9", Ep 10 = "V25", Ep 11 = "V26", ...)
def _act_label(ep: int, act: int) -> str:
    season = f"Ep {ep}" if ep <= 9 else f"V{ep + 15}"
    return f"{season} · Act {act}"

# ─────────────────────────────────────────────────────────────────────────────
#  Config
# ─────────────────────────────────────────────────────────────────────────────

def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {"api_key": ""}

def save_config(cfg: dict) -> None:
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass

# Valorant accent red
RED  = "#FF4655"
DARK = "#1a1a2e"
MID  = "#16213e"
CARD = "#0f3460"
TEXT = "#e0e0e0"
DIM  = "#8899aa"


# ─────────────────────────────────────────────────────────────────────────────
#  Analysis Engine
# ─────────────────────────────────────────────────────────────────────────────

class AnalysisEngine:
    """Runs all analysis tasks and saves outputs to disk."""

    def __init__(self, log: Callable[[str], None]):
        self.log = log

    # ── Internal helpers that mirror the notebook ─────────────────────────────

    def _get_last_known_elo(self, match_id: str, recent_mmr, recent_previous_match) -> Optional[int]:
        last_elo = recent_mmr[match_id].elo
        if last_elo != 0:
            return last_elo
        match_id = recent_previous_match[match_id]
        if match_id is None:
            return None
        return self._get_last_known_elo(match_id, recent_mmr, recent_previous_match)

    def _get_last_known_elo_from_puuid_spline(self, puuid: str, db: ValorantDB,
                                               acts_of_interest) -> Optional[float]:
        recent_acts = valorant_act_in_range(acts_of_interest[-1], 3)[1:]
        try:
            h = MatchHistoryProcessor(puuid, recent_acts, db, match_count_max=5, timespan=120)
        except Exception:
            return None
        if not h.recent_matches:
            return None
        lobby_avgs = self._gather_rank_average_lists(h.recent_matches, puuid, db, acts_of_interest)
        average_value = np.average(lobby_avgs["lobby_avg_rank"])
        spline_value  = predict_mmr(lobby_avgs, 0) or average_value
        return float(np.average([spline_value, average_value]))

    def _calculate_average_ranks_basic(self, match: Match, exclude_puuid: str) -> Dict:
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
            "allies_avg_rank":    np.average(ally)   if ally   else None,
            "opponents_avg_rank": np.average(opp)    if opp    else None,
            "lobby_avg_rank":     np.average(lobby)  if lobby  else None,
        }

    def _calculate_average_ranks_spline(self, match: Match, exclude_puuid: str,
                                         db: ValorantDB, acts_of_interest,
                                         keep_lists: bool = False) -> Dict:
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
                r = self._get_last_known_elo_from_puuid_spline(p.puuid, db, acts_of_interest)
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

    def _gather_rank_average_lists(self, match_history: List[Match], excluded_puuid: str,
                                    db: ValorantDB, acts_of_interest) -> Dict:
        results = [
            self._calculate_average_ranks_spline(m, excluded_puuid, db, acts_of_interest)
            for m in match_history
        ]
        fields = results[0].keys()
        return {
            field: ([r[field] for r in results if r[field] is not None] or None)
            for field in fields
        }

    def _calculate_placement_regions(self, recent_matches, recent_is_placement,
                                      recent_is_newly_placed,
                                      reverse=False, offset=0,
                                      matches_selection=None):
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

    def _build_match_ledger(self, recent_mmr, recent_is_placement, recent_is_newly_placed,
                             recent_previous_match, predicted_matches_mmr):
        ledger = {}
        matches = list(reversed(recent_mmr.items()))
        prev_elo_after = prev_tier_after = None

        for match_id, match in matches:
            elo_after   = match.elo
            tier_after  = match.tier.name
            elo_before  = prev_elo_after
            tier_before = prev_tier_after

            if recent_is_placement[match_id]:
                elo_before = self._get_last_known_elo(match_id, recent_mmr, recent_previous_match)
                if elo_after == 0:
                    elo_after = self._get_last_known_elo(match_id, recent_mmr, recent_previous_match)

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
                match.ranking_in_tier != 0 and recent_is_placement[prev_mid]
                if prev_mid is not None else False
            )
            ledger[match.match_id] = {
                "elo_before":          elo_before,
                "elo_after":           elo_after,
                "tier_before":         tier_before,
                "tier_after":          tier_after,
                "rr_supposed_to_take": rr_supposed_take,
                "rr_actual_taken":     rr_actual_taken,
                "shield_used":         shield,
                "buffer_used":         buffer,
                "is_splice":           splice,
                "big_loss":            big_loss,
                "rr_desync":           desync and not is_newly_placed,
                "datecode":            match.datetime,
                "map":                 match.map.name,
                "match_data":          match,
                "is_placement":        is_placement or is_newly_placed,
                "is_newly_placed_rank": is_newly_placed,
                "predicted_mmr":       predicted_matches_mmr[match.match_id] * 100,
            }
        return ledger

    def _compute_counterfactual_path(self, ledger_entries, recent_is_placement,
                                      predicted_matches_mmr, model_adj=False):
        first = next(e for e in ledger_entries if e["elo_before"] is not None)
        cur = first["elo_before"]
        result = []
        for e in ledger_entries:
            mid      = e["match_data"].match_id
            rr       = e["rr_supposed_to_take"] or 0
            is_new   = e["is_newly_placed_rank"]
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

    def _compute_counterfactual_nobuffer_path(self, ledger_entries, recent_is_placement,
                                               predicted_matches_mmr, model_adj=False):
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

    # ── Plotting ──────────────────────────────────────────────────────────────

    def _plot_rank_trends(self, rank_averages, recent_matches, recent_is_placement,
                           recent_is_newly_placed, out_path: Path, n: int = None):
        full_total = len(next(iter(rank_averages.values())))
        total = min(n, full_total) if n is not None else full_total
        rank_averages  = {k: v[:total] for k, v in rank_averages.items()}
        recent_matches = recent_matches[:total]
        game_versions = [recent_matches[i].metadata.game_version for i in range(total)]
        game_versions = [
            ver[:ver.index("-shipping")].replace("release-", "ver-")
            for ver in game_versions
        ]
        version_changes = find_change_indices(game_versions)
        matches = np.arange(1, total + 1)
        line_colors = {
            "allies_avg_rank":    "#4C72B0",
            "opponents_avg_rank": "#DD8452",
            "lobby_avg_rank":     "#2ca02c",
        }
        emphasis = {"allies_avg_rank": 0.35, "opponents_avg_rank": 0.35, "lobby_avg_rank": 1.0}
        fig, ax = plt.subplots(figsize=(14, 8))
        fig.patch.set_facecolor("white")
        ax.set_facecolor("#f8f9fa")

        for idx, version in version_changes:
            if idx < total:
                ax.axvline(idx + 1, color="#999", linestyle="--", alpha=0.6, linewidth=1)
                ax.text(idx + 1, 0.02, version, rotation=0, va="bottom", ha="center",
                        fontsize=9, transform=ax.get_xaxis_transform(), color="#666")

        for field, values in rank_averages.items():
            if field == "lobby_std":
                continue
            y = np.array([v if v is not None else np.nan for v in values[:total]])
            ax.plot(matches, y,
                    color=line_colors.get(field, "#888"),
                    linewidth=3 if field == "lobby_avg_rank" else 1.2,
                    alpha=emphasis.get(field, 0.5),
                    label=field.replace("_", " ").title())
            if field == "lobby_avg_rank" and (~np.isnan(y)).sum() >= 4:
                trend_y = np.array([
                    v if v is not None else np.nan
                    for v in (predict_mmr(rank_averages, i) for i in matches)
                ])
                ax.plot(matches, trend_y, linestyle="--", color="#333",
                        linewidth=2, alpha=0.8, label="Lobby Trend (Spline)")

        lobby = np.array(rank_averages["lobby_avg_rank"][:total], dtype=float)
        lobby = lobby[~np.isnan(lobby)]
        if len(lobby):
            mean, std = lobby.mean(), lobby.std()
            ax.axhline(mean, color=RED, linewidth=1, alpha=0.9, label="Lobby Mean")
            ax.axhline(mean + std, color="#555", linestyle=":", alpha=0.5, linewidth=1)
            ax.axhline(mean - std, color="#555", linestyle=":", alpha=0.5, linewidth=1)

        placement_ranges = self._calculate_placement_regions(
            recent_matches, recent_is_placement, recent_is_newly_placed,
            reverse=False, offset=1, matches_selection=recent_matches
        )
        first_span = True
        for s, e in placement_ranges:
            ax.axvline(s, linestyle="--", linewidth=1, color="#4C72B0", alpha=0.5)
            ax.axvline(e, linestyle="--", linewidth=1, color="#4C72B0", alpha=0.5)
            ax.axvspan(s - 0.5, e + 0.5, alpha=0.13, color="#4C72B0",
                       label="Placement Matches" if first_span else None)
            first_span = False

        step = max(1, total // 15)
        ax.set_xticks(matches[::step])
        all_values = [
            v for key, vals in rank_averages.items()
            if key != "lobby_std"
            for v in vals[:total]
            if v is not None
        ]
        if all_values:
            y_min = int(np.floor(min(all_values)))
            y_max = int(np.ceil(max(all_values))) + 1
            y_ticks = np.arange(y_min, y_max, 1)
            ax.set_yticks(y_ticks)
            ax.set_yticklabels([lookup_rank(int(t)) for t in y_ticks])

        ax.set_title(
            f"Rank Trends — Last {total} Matches (Spline)"
            + (f"  [n={n} window]" if n is not None else ""),
            fontsize=16, pad=12
        )
        ax.set_xlabel("Match Index (Recent → Older)", color="#444")
        ax.set_ylabel("Average Rank", color="#444")
        ax.tick_params(colors="#444")
        for spine in ax.spines.values():
            spine.set_edgecolor("#ccc")
        ax.grid(True, linestyle="--", alpha=0.4, color="#ccc")
        ax.legend(frameon=True, framealpha=0.9, ncol=2, fontsize=9)
        plt.tight_layout()
        plt.savefig(out_path, dpi=150, bbox_inches="tight", facecolor="white")
        plt.close(fig)

    def _plot_lobby_std_distribution(self, rank_averages, out_path: Path):
        lobby_std = np.array(rank_averages["lobby_std"])
        mean_std  = np.mean(lobby_std)
        std_std   = np.std(lobby_std)
        kde = gaussian_kde(lobby_std)
        x   = np.linspace(lobby_std.min(), lobby_std.max(), 1000)
        y   = kde(x)

        fig, ax = plt.subplots(figsize=(12, 6))
        fig.patch.set_facecolor("white")
        ax.set_facecolor("#f8f9fa")

        ax.plot(x, y, color="#1f77b4", lw=2)
        ax.fill_between(x, 0, y, color="#1f77b4", alpha=0.15)
        ax.fill_between(x, 0, y,
                         where=(x >= mean_std - 2*std_std) & (x <= mean_std + 2*std_std),
                         color="#6fb0d3", alpha=0.4)
        ax.fill_between(x, 0, y,
                         where=(x >= mean_std - std_std) & (x <= mean_std + std_std),
                         color="#2778b5", alpha=0.35)

        total = len(lobby_std)

        def pct(cond): return np.sum(cond) / total * 100

        pct_1s      = pct((lobby_std >= mean_std - std_std) & (lobby_std <= mean_std + std_std))
        pct_l12     = pct((lobby_std >= mean_std - 2*std_std) & (lobby_std < mean_std - std_std))
        pct_r12     = pct((lobby_std > mean_std + std_std) & (lobby_std <= mean_std + 2*std_std))
        pct_beyond_l = pct(lobby_std < mean_std - 2*std_std)
        pct_beyond_r = pct(lobby_std > mean_std + 2*std_std)

        vlines = {
            "Mean": (mean_std, RED, "--"),
            "+1σ": (mean_std + std_std, "#377eb8", "-."),
            "-1σ": (mean_std - std_std, "#377eb8", "-."),
            "+2σ": (mean_std + 2*std_std, "#e67e00", ":"),
            "-2σ": (mean_std - 2*std_std, "#e67e00", ":"),
        }
        for label, (xv, col, ls) in vlines.items():
            ax.axvline(xv, color=col, linestyle=ls, lw=1.5)
            ax.text(xv, 0.1, f"{xv:.2f}", ha="center", va="top",
                    fontsize=10, color="#222", fontweight="bold",
                    transform=ax.get_xaxis_transform())

        def annotate(xp, yp, txt):
            ax.text(xp, yp, txt, ha="center", va="bottom",
                    fontsize=12, fontweight="bold", color="#c0500a")

        annotate(mean_std,               max(y) * 0.7, f"{pct_1s:.1f}%")
        annotate(mean_std - 1.5*std_std, max(y) * 0.6, f"{pct_l12:.1f}%")
        annotate(mean_std + 1.5*std_std, max(y) * 0.6, f"{pct_r12:.1f}%")
        if pct_beyond_l > 0:
            annotate(mean_std - 2.25*std_std, max(y) * 0.5, f"{pct_beyond_l:.2f}%")
        if pct_beyond_r > 0:
            annotate(mean_std + 2.25*std_std, max(y) * 0.5, f"{pct_beyond_r:.2f}%")

        ax.set_title("Lobby Std Distribution", fontsize=16, pad=12)
        ax.set_xlabel("Lobby Std of Ranks", color="#444")
        ax.set_ylabel("Density", color="#444")
        ax.tick_params(colors="#444")
        for spine in ax.spines.values():
            spine.set_edgecolor("#ccc")
        ax.set_ylim(-0.005)
        ax.legend(
            handles=[plt.Line2D([0], [0], color=RED, linestyle="--",
                                label=f"Mean: {mean_std:.2f}")],
            frameon=True, framealpha=0.9
        )
        ax.grid(True, linestyle="--", alpha=0.4, color="#ccc")
        plt.tight_layout()
        plt.savefig(out_path, dpi=150, bbox_inches="tight", facecolor="white")
        plt.close(fig)

    def _plot_counterfactual(self, ledger_entries, actual_elos,
                              cf_elos, cf_adj_elos, cf_nb_elos, cf_nb_adj_elos,
                              recent_is_placement, recent_is_newly_placed,
                              recent_matches, out_path: Path):
        match_indices = list(range(len(ledger_entries)))
        colors = {
            "actual":        "#147C86",
            "no_shields":    "#F18F01",
            "no_shields_adj":"#F18F01",
            "no_buffer":     "#6A4C93",
            "no_buffer_adj": "#6A4C93",
        }

        actual_clean = [
            elo if elo is not None else cf_nb_adj_elos[i]
            for i, elo in enumerate(actual_elos)
        ]
        all_plotted = [actual_clean, cf_elos, cf_adj_elos, cf_nb_elos, cf_nb_adj_elos]
        min_elo = min(min(p) for p in all_plotted if p)
        max_elo = max(max(p) for p in all_plotted if p)

        fig, ax = plt.subplots(figsize=(14 * 1.4, 6 * 1.4))
        fig.patch.set_facecolor("white")
        ax.set_facecolor("#f8f9fa")

        ax.plot(match_indices, cf_elos, label="No Shields RR", linestyle="-",
                color=colors["no_shields"], linewidth=2, marker="s", markersize=3, alpha=0.8)
        ax.plot(match_indices, cf_adj_elos, label="No Shields RR (adjusted)", linestyle=":",
                color=colors["no_shields_adj"], linewidth=1.5, marker="^", markersize=3, alpha=0.6)
        ax.plot(match_indices, cf_nb_elos, label="No Buffer RR", linestyle="-",
                color=colors["no_buffer"], linewidth=2, marker="d", markersize=3, alpha=0.8)
        ax.plot(match_indices, cf_nb_adj_elos, label="No Buffer RR (adjusted)", linestyle=":",
                color=colors["no_buffer_adj"], linewidth=1.5, marker="v", markersize=3, alpha=0.6)
        ax.plot(match_indices, actual_clean, label="Actual RR", color=colors["actual"],
                linewidth=2.75, marker="o", markersize=4, zorder=100, alpha=0.9)

        # Per-match shield delta bars
        prev_delta = 0
        for i, (cf, actual) in enumerate(zip(cf_elos, actual_clean)):
            delta = (actual - cf) - prev_delta
            prev_delta += delta
            if delta != 0 and abs(delta) < 100:
                ymin, ymax = (cf, cf + delta) if delta > 0 else (cf + delta, cf)
                ax.vlines(i, ymin, ymax,
                          colors="#c044c0" if delta > 0 else "#1a8fc4",
                          linestyles="solid", linewidth=2, zorder=100)

        # Rank tier grid lines
        for rname in VALORANT_RANKS:
            for target in ["Bronze", "Silver", "Gold", "Platinum", "Diamond", "Ascendant"]:
                if target in rname:
                    ax.axhline(VALORANT_RANKS.index(rname) * 100,
                               color="#bbb", linestyle=":", linewidth=0.7, alpha=0.7, zorder=0)

        # Vertical match lines
        for xi in match_indices:
            ax.axvline(xi, color="#ccc", linestyle="dashed", alpha=0.5, linewidth=0.6)

        # Rank-up / derank lines
        rank_up_plotted = derank_plotted = False
        for i, e in enumerate(ledger_entries):
            if e["elo_after"] is None or e["elo_before"] is None:
                continue
            if e["tier_after"] != e["tier_before"] and e["tier_before"] is not None:
                if e["elo_after"] > e["elo_before"]:
                    ax.axvline(i, color="#2a9d2a", alpha=0.6, linestyle="-", linewidth=1.2,
                               label="Rank-Up" if not rank_up_plotted else "")
                    rank_up_plotted = True
                else:
                    ax.axvline(i, color=RED, alpha=0.6, linestyle="-", linewidth=1.2,
                               label="Derank" if not derank_plotted else "")
                    derank_plotted = True

        # Placement shading
        placement_ranges = self._calculate_placement_regions(
            recent_matches, recent_is_placement, recent_is_newly_placed,
            reverse=True, offset=-1
        )
        first_span = True
        for s, e in placement_ranges:
            ax.axvspan(s - 0.5, e + 0.5, alpha=0.13, color="#4C72B0",
                       label="Placement Matches" if first_span else None)
            first_span = False

        # Event annotations
        events = {
            "splice": dict(cond=lambda e: e.get("is_splice"),
                           plot="axvline", color="#555", linewidth=2, alpha=0.5,
                           linestyle="--", label="Data Splice"),
            "shield": dict(cond=lambda e: e.get("shield_used"),
                           plot="scatter", s=100, facecolors="none", edgecolors=RED,
                           linewidths=2, label="Shield Used"),
            "rr_desync": dict(cond=lambda e: e.get("rr_desync"),
                              plot="scatter", s=40, facecolors="#ff8800", edgecolors="#7a0000",
                              zorder=100, linewidths=2, label="RR Desync"),
            "newly_placed": dict(cond=lambda e: e.get("is_newly_placed_rank"),
                                 plot="scatter", s=40, facecolors="#ff8800", edgecolors="#006666",
                                 zorder=100, linewidths=2, label="Newly Placed Rank"),
            "buffer": dict(cond=lambda e: e.get("buffer_used") and not e.get("shield_used"),
                           plot="scatter", s=100, facecolors="none", edgecolors="#cc44cc",
                           linewidths=2, label="Buffer Used"),
            "big_loss": dict(cond=lambda e: e.get("big_loss"),
                             plot="scatter", marker="*", s=80, color="#b8a800",
                             edgecolors="#333", linewidths=0.8, zorder=100,
                             label=f"Big Loss (≥{BIG_LOSS_THRESHOLD_RR} RR)"),
        }
        plotted = {k: False for k in events}
        for i, e in enumerate(ledger_entries):
            for k, v in events.items():
                if v["cond"](e):
                    kw = {kk: vv for kk, vv in v.items() if kk not in ("cond", "plot")}
                    if plotted[k]:
                        kw.pop("label", None)
                    plotted[k] = True
                    if v["plot"] == "axvline":
                        ax.axvline(i, **kw)
                    else:
                        ax.scatter(i, e["elo_after"], **kw)

        ax.set_ylim(min_elo - 10, max_elo + 10)
        yticks  = list(range((min_elo // 100) * 100, ((max_elo // 100) + 2) * 100, 100))
        ylabels = [map_rank_value(v / 100, include_rr=False) if v > 0 else "" for v in yticks]
        ax.set_yticks(yticks)
        ax.set_yticklabels(ylabels)
        ax.set_xticks(match_indices)
        ax.set_xticklabels(match_indices, rotation=0, fontsize=8)
        ax.set_xlabel("Match Index (Oldest → Most Recent)", color="#444")
        ax.set_ylabel("Rank", color="#444")
        ax.set_title("Valorant Rank Trend — Actual vs Counterfactual", fontsize=16, pad=12)
        for spine in ax.spines.values():
            spine.set_edgecolor("#ccc")
        ax.legend(loc="best", framealpha=0.9, fontsize=9)
        plt.tight_layout()
        plt.savefig(out_path, dpi=150, bbox_inches="tight", facecolor="white")
        plt.close(fig)

    def _plot_actual_rr(self, ledger_entries, actual_elos,
                        recent_is_placement, recent_is_newly_placed,
                        recent_matches, out_path: Path):
        """Plot only the actual RR trend — no counterfactual lines."""
        match_indices = list(range(len(ledger_entries)))

        actual_clean = [
            elo if elo is not None else (actual_elos[i - 1] if i > 0 else 0)
            for i, elo in enumerate(actual_elos)
        ]
        min_elo = min(v for v in actual_clean if v)
        max_elo = max(v for v in actual_clean if v)

        fig, ax = plt.subplots(figsize=(14 * 1.4, 6 * 1.4))
        fig.patch.set_facecolor("white")
        ax.set_facecolor("#f8f9fa")

        ax.plot(match_indices, actual_clean, label="Actual RR", color="#147C86",
                linewidth=2.75, marker="o", markersize=4, zorder=100, alpha=0.9)

        # Rank tier grid lines
        for rname in VALORANT_RANKS:
            for target in ["Bronze", "Silver", "Gold", "Platinum", "Diamond", "Ascendant"]:
                if target in rname:
                    ax.axhline(VALORANT_RANKS.index(rname) * 100,
                               color="#bbb", linestyle=":", linewidth=0.7, alpha=0.7, zorder=0)

        # Vertical match lines
        for xi in match_indices:
            ax.axvline(xi, color="#ccc", linestyle="dashed", alpha=0.5, linewidth=0.6)

        # Rank-up / derank lines
        rank_up_plotted = derank_plotted = False
        for i, e in enumerate(ledger_entries):
            if e["elo_after"] is None or e["elo_before"] is None:
                continue
            if e["tier_after"] != e["tier_before"] and e["tier_before"] is not None:
                if e["elo_after"] > e["elo_before"]:
                    ax.axvline(i, color="#2a9d2a", alpha=0.6, linestyle="-", linewidth=1.2,
                               label="Rank-Up" if not rank_up_plotted else "")
                    rank_up_plotted = True
                else:
                    ax.axvline(i, color=RED, alpha=0.6, linestyle="-", linewidth=1.2,
                               label="Derank" if not derank_plotted else "")
                    derank_plotted = True

        # Placement shading (blue)
        placement_ranges = self._calculate_placement_regions(
            recent_matches, recent_is_placement, recent_is_newly_placed,
            reverse=True, offset=-1
        )
        first_span = True
        for s, e in placement_ranges:
            ax.axvspan(s - 0.5, e + 0.5, alpha=0.13, color="#4C72B0",
                       label="Placement Matches" if first_span else None)
            first_span = False

        ax.set_ylim(min_elo - 10, max_elo + 10)
        yticks  = list(range((min_elo // 100) * 100, ((max_elo // 100) + 2) * 100, 100))
        ylabels = [map_rank_value(v / 100, include_rr=False) if v > 0 else "" for v in yticks]
        ax.set_yticks(yticks)
        ax.set_yticklabels(ylabels)
        ax.set_xticks(match_indices)
        ax.set_xticklabels(match_indices, rotation=0, fontsize=8)
        ax.set_xlabel("Match Index (Oldest → Most Recent)", color="#444")
        ax.set_ylabel("Rank", color="#444")
        ax.set_title("Valorant Rank Trend — Actual RR", fontsize=16, pad=12)
        for spine in ax.spines.values():
            spine.set_edgecolor("#ccc")
        ax.legend(loc="best", framealpha=0.9, fontsize=9)
        plt.tight_layout()
        plt.savefig(out_path, dpi=150, bbox_inches="tight", facecolor="white")
        plt.close(fig)

    # ── PDF Report ────────────────────────────────────────────────────────────

    def _generate_pdf(
        self,
        out_dir: Path,
        player_name: str,
        player_tag: str,
        acts_of_interest,
        region: str,
        timestamp: str,
        wr: dict,
        current_rank: str,
        estimated_mmr_str: str,
        cf_rank: str,
        results: dict,
        role_pcts: dict,
        ledger_entries: list,
        round_stats_out: Optional[dict],
        match_count_max: int,
        timespan: int,
    ) -> Path:
        """
        Build a data-scientist-style PDF report and save it to out_dir/report.pdf.
        Returns the path to the written file.
        """
        # ── Colour palette (Valorant-ish dark theme adapted for print) ────────
        VAL_RED    = colors.HexColor("#FF4655")
        DARK_NAVY  = colors.HexColor("#1a1a2e")
        MID_NAVY   = colors.HexColor("#16213e")
        STEEL      = colors.HexColor("#4a6080")
        LIGHT_GREY = colors.HexColor("#e8e8ec")
        DIM_GREY   = colors.HexColor("#7a8899")
        WHITE      = colors.white
        BLACK      = colors.HexColor("#1c1c24")

        PAGE_W, PAGE_H = letter
        MARGIN = 0.7 * inch

        pdf_path = out_dir / "report.pdf"
        doc = SimpleDocTemplate(
            str(pdf_path),
            pagesize=letter,
            leftMargin=MARGIN, rightMargin=MARGIN,
            topMargin=MARGIN,  bottomMargin=MARGIN,
            title=f"Valorant Rank Analysis — {player_name}#{player_tag}",
            author="Valorant Rank Analyzer",
        )

        # ── Style sheet ───────────────────────────────────────────────────────
        SS = getSampleStyleSheet()

        def _sty(name, **kw):
            return ParagraphStyle(name, **kw)

        sty_hero_name = _sty("HeroName",
            fontSize=28, leading=32, textColor=WHITE,
            fontName="Helvetica-Bold", alignment=TA_LEFT)
        sty_hero_sub = _sty("HeroSub",
            fontSize=11, leading=15, textColor=LIGHT_GREY,
            fontName="Helvetica", alignment=TA_LEFT)
        sty_section = _sty("Section",
            fontSize=13, leading=17, textColor=VAL_RED,
            fontName="Helvetica-Bold", alignment=TA_LEFT,
            spaceBefore=6, spaceAfter=4)
        sty_body = _sty("Body",
            fontSize=9.5, leading=14, textColor=BLACK,
            fontName="Helvetica")
        sty_body_em = _sty("BodyEm",
            fontSize=9.5, leading=14, textColor=BLACK,
            fontName="Helvetica-Bold")
        sty_caption = _sty("Caption",
            fontSize=8, leading=11, textColor=DIM_GREY,
            fontName="Helvetica-Oblique", alignment=TA_CENTER,
            spaceAfter=10)
        sty_callout = _sty("Callout",
            fontSize=10, leading=14, textColor=BLACK,
            fontName="Helvetica")
        sty_link = _sty("Link",
            fontSize=9.5, leading=14, textColor=colors.HexColor("#1155CC"),
            fontName="Helvetica")
        sty_tbl_hdr = _sty("TblHdr",
            fontSize=8.5, leading=11, textColor=WHITE,
            fontName="Helvetica-Bold", alignment=TA_CENTER)
        sty_tbl_cel = _sty("TblCel",
            fontSize=8.5, leading=11, textColor=BLACK,
            fontName="Helvetica", alignment=TA_CENTER)
        sty_tbl_cel_l = _sty("TblCelL",
            fontSize=8.5, leading=11, textColor=BLACK,
            fontName="Helvetica", alignment=TA_LEFT)

        # ── Helper: coloured stat card table ──────────────────────────────────
        USABLE_W = PAGE_W - 2 * MARGIN

        def _stat_cards(pairs, cols=4):
            """pairs = [(label, value), ...]  rendered as a pill-card row."""
            col_w = USABLE_W / cols
            header_row = [Paragraph(lbl, _sty("sh", fontSize=7.5, leading=10,
                textColor=DIM_GREY, fontName="Helvetica", alignment=TA_CENTER))
                for lbl, _ in pairs]
            value_row  = [Paragraph(str(val), _sty("sv", fontSize=14, leading=17,
                textColor=VAL_RED, fontName="Helvetica-Bold", alignment=TA_CENTER))
                for _, val in pairs]
            # pad to cols
            while len(header_row) < cols:
                header_row.append(Paragraph("", sty_body))
                value_row.append(Paragraph("", sty_body))
            tbl = Table([header_row, value_row],
                        colWidths=[col_w] * cols, rowHeights=[18, 26])
            tbl.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), LIGHT_GREY),
                ("ROWBACKGROUNDS", (0, 0), (-1, -1), [LIGHT_GREY, WHITE]),
                ("BOX",      (0, 0), (-1, -1), 0.5, STEEL),
                ("INNERGRID",(0, 0), (-1, -1), 0.25, colors.HexColor("#cccccc")),
                ("VALIGN",   (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING",    (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]))
            return tbl

        def _section_rule():
            return HRFlowable(width="100%", thickness=1,
                              color=VAL_RED, spaceAfter=4, spaceBefore=0)

        def _callout(text):
            """Shaded callout box — replaces sty_callout which doesn't support backColor."""
            inner = Paragraph(text, sty_callout)
            tbl = Table([[inner]], colWidths=[USABLE_W])
            tbl.setStyle(TableStyle([
                ("BACKGROUND",    (0, 0), (-1, -1), LIGHT_GREY),
                ("BOX",           (0, 0), (-1, -1), 0.75, STEEL),
                ("LEFTPADDING",   (0, 0), (-1, -1), 10),
                ("RIGHTPADDING",  (0, 0), (-1, -1), 10),
                ("TOPPADDING",    (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ]))
            return tbl

        def _img(fname, width=None, caption=None):
            p = out_dir / fname
            if not p.exists():
                return []
            from PIL import Image as PILImage
            with PILImage.open(str(p)) as im:
                img_w, img_h = im.size
            w = width or USABLE_W
            h = w * (img_h / img_w)
            img_elem = RLImage(str(p), width=w, height=h)
            elems = [Spacer(1, 4), img_elem]
            if caption:
                elems.append(Paragraph(caption, sty_caption))
            else:
                elems.append(Spacer(1, 6))
            return elems

        # ── Cover / header block ──────────────────────────────────────────────
        acts_label = ", ".join(f"Ep {e} · Act {a}" for e, a in acts_of_interest)
        story: list = []

        # Dark header banner via a 1-row table
        banner_data = [[
            Paragraph(f"{player_name}<font color='#FF4655'>#{player_tag}</font>",
                      sty_hero_name),
            Paragraph(
                f"<b>Region:</b> {region.upper()}&nbsp;&nbsp;"
                f"<b>Acts:</b> {acts_label}<br/>"
                f"<b>Generated:</b> {timestamp}",
                sty_hero_sub),
        ]]
        banner = Table(banner_data, colWidths=[USABLE_W * 0.52, USABLE_W * 0.48])
        banner.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), DARK_NAVY),
            ("VALIGN",     (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING",    (0, 0), (-1, -1), 16),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 16),
            ("LEFTPADDING",   (0, 0), (-1, -1), 14),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 14),
        ]))
        story.append(banner)
        story.append(Spacer(1, 10))

        # ── Section 1: At a Glance ────────────────────────────────────────────
        story.append(Paragraph("At a Glance", sty_section))
        story.append(_section_rule())

        n_games   = wr["games"]
        wins, losses = wr["wins"], wr["losses"]
        wr_pct    = f"{wr['winrate']:.1%}"

        story.append(_stat_cards([
            ("Matches Analysed", n_games),
            ("Win Rate",         wr_pct),
            ("W / L",            f"{wins} / {losses}"),
            ("Current Rank",     current_rank),
        ]))
        story.append(Spacer(1, 4))
        story.append(_stat_cards([
            ("Estimated True MMR", estimated_mmr_str),
            ("Counterfactual Rank\n(no-buffer + adj)", cf_rank),
            ("Most Balanced Lobby", f"{results['lobby_balance']['most_balanced'][0]:.3f} σ"),
            ("Least Balanced Lobby", f"{results['lobby_balance']['least_balanced'][0]:.3f} σ"),
        ]))
        story.append(Spacer(1, 8))

        # ── Explanatory prose ─────────────────────────────────────────────────
        story.append(Paragraph(
            f"This report summarises <b>{n_games} competitive matches</b> played by "
            f"<b>{player_name}#{player_tag}</b> across <b>{acts_label}</b>. "
            f"The pipeline fetches raw MMR history via the Henrik API, reconstructs "
            f"per-match RR deltas from ELO snapshots, and builds lobby rank estimates "
            f"using a spline fitted to teammates' and opponents' visible tiers.",
            sty_body))
        story.append(Spacer(1, 4))

        mmr_delta = ""
        try:
            cur_val  = int(current_rank.split()[0]) if current_rank[0].isdigit() else 0
            est_val  = int(estimated_mmr_str.split()[0]) if estimated_mmr_str[0].isdigit() else 0
            diff     = est_val - cur_val
            if diff > 0:
                mmr_delta = (f"The spline estimate sits <b>{diff} RR above</b> the displayed rank, "
                             f"suggesting the matchmaker may be deliberately placing this player "
                             f"below their internal MMR — a pattern commonly seen after a performance "
                             f"spike or act reset.")
            elif diff < 0:
                mmr_delta = (f"The spline estimate is <b>{abs(diff)} RR below</b> the displayed rank. "
                             f"This can arise when recent performance has trended downward but the "
                             f"system has not yet fully reflected that in match outcomes.")
        except Exception:
            pass
        if mmr_delta:
            story.append(_callout(mmr_delta))
            story.append(Spacer(1, 4))

        # ── Section 2: Rank Trajectory ────────────────────────────────────────
        story.append(Paragraph("Rank Trajectory — Lobby Rank Averages", sty_section))
        story.append(_section_rule())
        story.append(Paragraph(
            "The chart below tracks three lobby-rank averages across every match, "
            "chronologically most recent on the left. "
            "<b>Lobby avg</b> (green) is the full-lobby mean excluding the tracked player; "
            "<b>allies</b> (blue) and <b>opponents</b> (orange) are split by team. "
            "Divergence between ally and opponent averages is the primary signal of "
            "matchmaker imbalance — sustained gaps over many games are statistically "
            "meaningful; single-game outliers rarely are.",
            sty_body))
        story.extend(_img("rank_trends.png",
            caption="Figure 1 — Lobby rank averages over time. "
                    "Vertical dashed lines mark game-patch boundaries; "
                    "shaded bands highlight placement match windows."))
        story.extend(_img("rank_trends_n15.png",
            caption="Figure 1b — Same chart restricted to the 15 most-recent matches. "
                    "Zooms into current-form trend, reducing noise from older acts."))

        # ── Section 3: Lobby Balance Distribution ─────────────────────────────
        story.append(Paragraph("Lobby Balance Distribution", sty_section))
        story.append(_section_rule())
        story.append(Paragraph(
            "Standard deviation of lobby ranks (σ) measures how evenly skilled "
            "the matchmaker assembled each game. A low σ means all ten players "
            "were clustered near the same tier; a high σ means a wide rank spread "
            "was pulled together."
            "The kernel density below shows where this player's matches land "
            "relative to the distribution's own mean and ±1σ / ±2σ bands.",
            sty_body))
        story.extend(_img("lobby_std_distribution.png",
            caption="Figure 2 — Kernel density of per-match lobby σ. "
                    "Percentages annotate the share of matches falling within each σ band. "
                    "A heavy right tail indicates frequent high-variance lobbies."))

        # ── Section 4: Counterfactual RR ─────────────────────────────────────
        story.append(PageBreak())
        story.append(Paragraph("Counterfactual RR Paths", sty_section))
        story.append(_section_rule())
        story.append(Paragraph(
            "Valorant's ranked system applies two mechanical dampeners that alter "
            "true RR gains: <b>shields</b> (which absorb losses at rank boundaries) "
            "and <b>buffers</b> (which prevent demotion through the 0 RR floor). "
            "By replaying every match's stated RR delta without these corrections "
            "we produce counterfactual trajectories — showing what rank this player "
            "would hold if the raw performance signal propagated without dampening.",
            sty_body))
        story.append(Spacer(1, 4))

        # Build a compact ledger summary table
        shield_count = sum(1 for e in ledger_entries if e.get("shield_used"))
        buffer_count = sum(1 for e in ledger_entries if e.get("buffer_used"))
        splice_count = sum(1 for e in ledger_entries if e.get("is_splice"))
        desync_count = sum(1 for e in ledger_entries if e.get("rr_desync"))
        big_loss_count = sum(1 for e in ledger_entries if e.get("big_loss"))

        event_rows = [
            [Paragraph(h, sty_tbl_hdr) for h in
             ["Event", "Count", "What It Means"]],
            [Paragraph("Shield Used", sty_tbl_cel_l),
             Paragraph(str(shield_count), sty_tbl_cel),
             Paragraph("Loss absorbed at boundary; actual RR taken = 0", sty_tbl_cel_l)],
            [Paragraph("Buffer Used", sty_tbl_cel_l),
             Paragraph(str(buffer_count), sty_tbl_cel),
             Paragraph("Demotion prevented at 0 RR floor", sty_tbl_cel_l)],
            [Paragraph("Big Loss (≥30 RR)", sty_tbl_cel_l),
             Paragraph(str(big_loss_count), sty_tbl_cel),
             Paragraph("Unusually large single-match RR deduction", sty_tbl_cel_l)],
            [Paragraph("Data Splice", sty_tbl_cel_l),
             Paragraph(str(splice_count), sty_tbl_cel),
             Paragraph("ELO jump >42 RR — gap in history or act reset", sty_tbl_cel_l)],
            [Paragraph("RR Desync", sty_tbl_cel_l),
             Paragraph(str(desync_count), sty_tbl_cel),
             Paragraph("Stated delta ≠ reconstructed delta (≥10 RR diff)", sty_tbl_cel_l)],
        ]
        col_ws = [USABLE_W * 0.25, USABLE_W * 0.1, USABLE_W * 0.65]
        event_tbl = Table(event_rows, colWidths=col_ws)
        event_tbl.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1, 0),  DARK_NAVY),
            ("ROWBACKGROUNDS",(0, 1), (-1, -1), [WHITE, LIGHT_GREY]),
            ("BOX",           (0, 0), (-1, -1), 0.5, STEEL),
            ("INNERGRID",     (0, 0), (-1, -1), 0.25, colors.HexColor("#cccccc")),
            ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING",    (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("LEFTPADDING",   (0, 0), (-1, -1), 6),
        ]))
        story.append(KeepTogether([event_tbl, Spacer(1, 6)]))
        story.extend(_img("counterfactual_rr.png",
            caption="Figure 3 — Actual RR path (teal) vs. counterfactual paths without shields "
                    "(orange) and without buffers (purple). Vertical bars mark per-match shield "
                    "delta. Divergence from the actual line quantifies cumulative mechanical benefit."))

        # ── Section 5: Role Distribution ──────────────────────────────────────
        if role_pcts:
            story.append(Paragraph("Opponent Role Distribution", sty_section))
            story.append(_section_rule())
            story.append(Paragraph(
                "Role composition of opponents across all analysed matches. "
                "Skews toward a particular role may indicate the player is "
                "consistently queuing into specific team compositions — "
                "useful context for agent-pick decisions.",
                sty_body))
            story.append(Spacer(1, 4))
            role_header = [Paragraph(h, sty_tbl_hdr) for h in ["Role", "Share"]]
            role_rows = [role_header] + [
                [Paragraph(role, sty_tbl_cel_l),
                 Paragraph(str(pct), sty_tbl_cel)]
                for role, pct in sorted(role_pcts.items())
            ]
            role_tbl = Table(role_rows, colWidths=[USABLE_W * 0.5, USABLE_W * 0.5])
            role_tbl.setStyle(TableStyle([
                ("BACKGROUND",    (0, 0), (-1, 0),  DARK_NAVY),
                ("ROWBACKGROUNDS",(0, 1), (-1, -1), [WHITE, LIGHT_GREY]),
                ("BOX",           (0, 0), (-1, -1), 0.5, STEEL),
                ("INNERGRID",     (0, 0), (-1, -1), 0.25, colors.HexColor("#cccccc")),
                ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING",    (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ("LEFTPADDING",   (0, 0), (-1, -1), 8),
            ]))
            story.append(KeepTogether([role_tbl]))
            story.append(Spacer(1, 6))

        # ── Section 6: Round Timing Statistics ───────────────────────────────
        if round_stats_out:
            ov = round_stats_out.get("overall", {})
            story.append(PageBreak())
            story.append(Paragraph("Round Timing Statistics", sty_section))
            story.append(_section_rule())
            story.append(Paragraph(
                f"Round-timing data was collected across "
                f"<b>{ov.get('sample_rounds', '—')} rounds</b> from the analysed matches. "
                f"Duration is reconstructed from in-game event timestamps in priority order: "
                f"defuse time → latest kill time → plant time + spike timer → game-length average. "
                f"Rounds outside the 3 – 100 s window are discarded.",
                sty_body))
            story.append(Spacer(1, 4))

            story.append(_stat_cards([
                ("Sample Rounds",    ov.get("sample_rounds", "—")),
                ("Median Duration",  f"{ov.get('median', '—')} s"),
                ("P25 / P75",        f"{ov.get('p25', '—')} / {ov.get('p75', '—')} s"),
                ("Plant Rate",       f"{ov.get('plant_rate', 0)*100:.1f}%"),
            ]))
            story.append(Spacer(1, 4))
            story.append(_stat_cards([
                ("Median Plant Time", f"{ov.get('median_plant_time', '—')} s"),
                ("Median Post-Plant", f"{ov.get('median_post_plant', '—')} s"),
                ("Defuse Rate",       f"{ov.get('defuse_rate', 0)*100:.1f}%"),
                ("Elim Rate\n(no-plant rounds)", f"{ov.get('elim_rate', 0)*100:.1f}%"),
            ]))
            story.append(Spacer(1, 6))

            story.append(Paragraph(
                f"A median round of <b>{ov.get('median', '—')} s</b> with a plant rate of "
                f"<b>{ov.get('plant_rate', 0)*100:.1f}%</b> tells us how 'eco-round heavy' "
                f"this player's games tend to be — lower plant rates suggest more frequent "
                f"early exits or aggressive retakes. "
                f"The post-plant median of <b>{ov.get('median_post_plant', '—')} s</b> reflects "
                f"how quickly defenders respond after the spike is placed; values below 20 s "
                f"typically indicate fast rotations and aggressive defuse attempts.",
                sty_body))
            story.append(Spacer(1, 6))

            # Per-map breakdown
            by_map = round_stats_out.get("by_map", {})
            if by_map:
                story.append(Paragraph("Per-Map Round Summary", sty_section))
                story.append(_section_rule())
                map_header = [Paragraph(h, sty_tbl_hdr) for h in
                              ["Map", "Rounds", "Median (s)", "Plant Rate", "Plant Time (s)", "Post-Plant (s)"]]
                map_rows = [map_header]
                for map_name, s in sorted(by_map.items()):
                    map_rows.append([
                        Paragraph(map_name, sty_tbl_cel_l),
                        Paragraph(str(s.get("sample_rounds", "—")), sty_tbl_cel),
                        Paragraph(str(s.get("median", "—")),         sty_tbl_cel),
                        Paragraph(f"{s.get('plant_rate', 0)*100:.1f}%", sty_tbl_cel),
                        Paragraph(str(s.get("median_plant_time", "—")), sty_tbl_cel),
                        Paragraph(str(s.get("median_post_plant", "—")), sty_tbl_cel),
                    ])
                map_col_ws = [USABLE_W * w for w in [0.22, 0.1, 0.13, 0.15, 0.2, 0.2]]
                map_tbl = Table(map_rows, colWidths=map_col_ws)
                map_tbl.setStyle(TableStyle([
                    ("BACKGROUND",    (0, 0), (-1, 0),  DARK_NAVY),
                    ("ROWBACKGROUNDS",(0, 1), (-1, -1), [WHITE, LIGHT_GREY]),
                    ("BOX",           (0, 0), (-1, -1), 0.5, STEEL),
                    ("INNERGRID",     (0, 0), (-1, -1), 0.25, colors.HexColor("#cccccc")),
                    ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
                    ("TOPPADDING",    (0, 0), (-1, -1), 5),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                    ("LEFTPADDING",   (0, 0), (-1, -1), 6),
                ]))
                story.append(KeepTogether([map_tbl]))
                story.append(Spacer(1, 6))

        # ── Section 7: Interactive Resources ─────────────────────────────────
        story.append(Paragraph("Interactive Reports & Tools", sty_section))
        story.append(_section_rule())

        agent_link   = "./agent_report/index.html"
        utility_link = "./utility_recharge_calculator.html"

        resources = [
            (
                "Agent Stats Report",
                agent_link,
                "Full per-agent performance breakdown — win rate, K/D/A, economy, "
                "and usage share across all analysed matches. Open in any browser.",
            ),
            (
                "Utility Recharge Calculator",
                utility_link,
                "Interactive explorer powered by round_stats.json. Visualises round-timing "
                "distributions, plant/defuse patterns, and per-map breakdowns to help "
                "calibrate utility usage and timing windows. Requires round_stats.json "
                "to be present in the same folder (generated automatically).",
            ),
        ]

        res_rows = [[Paragraph(h, sty_tbl_hdr) for h in ["Tool", "File Path", "Description"]]]
        for title_r, link, desc in resources:
            res_rows.append([
                Paragraph(f"<b>{title_r}</b>", sty_tbl_cel_l),
                Paragraph(f'<a href="{link}"><font color="#1155CC">{link}</font></a>', sty_tbl_cel_l),
                Paragraph(desc, sty_tbl_cel_l),
            ])
        res_col_ws = [USABLE_W * 0.22, USABLE_W * 0.28, USABLE_W * 0.50]
        res_tbl = Table(res_rows, colWidths=res_col_ws)
        res_tbl.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1, 0),  DARK_NAVY),
            ("ROWBACKGROUNDS",(0, 1), (-1, -1), [WHITE, LIGHT_GREY]),
            ("BOX",           (0, 0), (-1, -1), 0.5, STEEL),
            ("INNERGRID",     (0, 0), (-1, -1), 0.25, colors.HexColor("#cccccc")),
            ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING",    (0, 0), (-1, -1), 7),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ("LEFTPADDING",   (0, 0), (-1, -1), 6),
        ]))
        story.append(KeepTogether([res_tbl]))
        story.append(Spacer(1, 6))
        story.append(_callout(
            "To use the interactive tools: open the output folder in your file explorer, "
            "then double-click either HTML file. Both tools run entirely in your browser "
            "with no server or internet connection required."))

        # ── Footer / methodology note ─────────────────────────────────────────
        story.append(Spacer(1, 10))
        story.append(HRFlowable(width="100%", thickness=0.5, color=STEEL))
        story.append(Spacer(1, 4))
        story.append(Paragraph(
            f"<b>Methodology note.</b> MMR data sourced from the Henrik Dev Unofficial Valorant API. "
            f"Lobby rank estimates use a monotone cubic spline fitted to observed tiers; unrated players "
            f"are looked up individually when they comprise >40% of the lobby. "
            f"Counterfactual paths replay the stated RR delta from each match without applying the "
            f"shield or buffer corrections, then forward-simulate from the first known ELO anchor. "
            f"All figures are approximate — the API exposes post-correction values and some "
            f"reconstruction is necessary. Analysis parameters: max {match_count_max} matches, "
            f"timespan {timespan} days.",
            _sty("Footnote", fontSize=7.5, leading=11, textColor=DIM_GREY, fontName="Helvetica")))

        def _add_page_number(canvas, doc):
            canvas.saveState()
            canvas.setFont("Helvetica", 7)
            canvas.setFillColor(DIM_GREY)
            canvas.drawRightString(PAGE_W - MARGIN, 0.4 * inch, f"Page {doc.page}")
            canvas.restoreState()

        doc.build(story, onFirstPage=_add_page_number, onLaterPages=_add_page_number)
        return pdf_path

    # ── Main run method ───────────────────────────────────────────────────────

    def run(self, player_name: str, player_tag: str,
            acts_of_interest: List[Tuple[str, str]],
            match_count_max: int, timespan: int = 30,
            region: str = "na", api_key: str = "") -> Path:
        """
        Run the full analysis pipeline and return the output directory path.
        Raises exceptions on failure — the GUI catches them.
        """
        safe_name   = f"{player_name}_{player_tag}".replace(" ", "_").replace("#", "_")
        acts_slug   = "+".join(f"E{ep}A{act}" for ep, act in acts_of_interest)
        date_str    = datetime.now().strftime("%m%d")
        folder_name = f"{safe_name}_{acts_slug}_{match_count_max}m_{date_str}"
        out_dir     = OUTPUTS_DIR / folder_name
        # If this exact folder already exists (e.g. re-run same day), append _2, _3, ...
        if out_dir.exists():
            n = 2
            while (OUTPUTS_DIR / f"{folder_name}_{n}").exists():
                n += 1
            out_dir = OUTPUTS_DIR / f"{folder_name}_{n}"
        out_dir.mkdir(parents=True, exist_ok=True)

        # ── 1. API + DB setup ────────────────────────────────────────────────
        self.log("🔌  Connecting to API and database...")
        api = UnofficialApi(api_key=api_key) if api_key else UnofficialApi()
        db  = ValorantDB(region=region)

        self.log(f"🔍  Looking up {player_name}#{player_tag}...")
        account  = api.get_account_by_name(player_name, player_tag)
        my_puuid = account.puuid
        self.log(f"✅  Found player — PUUID: {my_puuid[:12]}...")

        # ── 2. Match history ─────────────────────────────────────────────────
        self.log("📡  Fetching and updating match history (this may take a moment)...")
        db.update_match_history_for_puuid(my_puuid)

        self.log(f"📂  Loading up to {match_count_max} matches across "
                 f"{len(acts_of_interest)} selected act(s)...")
        history = MatchHistoryProcessor(
            my_puuid, acts_of_interest, db,
            match_count_max=match_count_max,
            timespan=timespan,
        )
        recent_matches        = history.recent_matches
        recent_matches_by_id  = history.recent_matches_by_id
        recent_mmr            = history.recent_mmr
        recent_season_labels  = history.recent_season_labels
        recent_is_placement   = history.recent_is_placement
        recent_previous_match = history.recent_previous_match

        recent_is_newly_placed = {
            mid: (
                recent_is_placement[recent_previous_match[mid]]
                and not recent_is_placement[mid]
            ) if recent_previous_match[mid] is not None else False
            for mid in recent_is_placement
        }
        self.log(f"✅  Loaded {len(recent_matches)} matches.")

        # ── 3. Rank averages + MMR prediction ────────────────────────────────
        self.log("📊  Computing lobby rank averages (spline method)...")
        rank_averages = self._gather_rank_average_lists(
            recent_matches, my_puuid, db, acts_of_interest
        )
        predicted_matches_mmr = {
            recent_matches[idx].metadata.match_id: predict_mmr(rank_averages, idx)
            for idx in range(len(recent_matches))
        }
        last_mid = recent_matches[0].metadata.match_id
        self.log(f"🎯  Predicted MMR at latest match: {map_rank_value(predict_mmr(rank_averages, 0))}")

        # ── 4. Win rate ──────────────────────────────────────────────────────
        wr = calculate_winrate(recent_matches, my_puuid)
        self.log(f"🏆  Win rate: {wr['wins']}W / {wr['losses']}L "
                 f"over {wr['games']} games — {wr['winrate']:.0%}")

        # ── 5. Plot: Rank Trends ─────────────────────────────────────────────
        self.log("🎨  Generating rank trends chart...")
        self._plot_rank_trends(
            rank_averages, recent_matches,
            recent_is_placement, recent_is_newly_placed,
            out_dir / "rank_trends.png"
        )
        self.log("💾  Saved rank_trends.png")

        # ── 5b. Plot: Rank Trends (recent 15 matches) ────────────────────────
        _N_RECENT = 15
        self.log(f"🎨  Generating rank trends chart (last {_N_RECENT} matches)...")
        self._plot_rank_trends(
            rank_averages, recent_matches,
            recent_is_placement, recent_is_newly_placed,
            out_dir / f"rank_trends_n{_N_RECENT}.png",
            n=_N_RECENT,
        )
        self.log(f"💾  Saved rank_trends_n{_N_RECENT}.png")

        # ── 6. Plot: Lobby Std Distribution ──────────────────────────────────
        self.log("🎨  Generating lobby balance distribution chart...")
        self._plot_lobby_std_distribution(rank_averages, out_dir / "lobby_std_distribution.png")
        self.log("💾  Saved lobby_std_distribution.png")

        # ── 7. Build ledger + counterfactual paths ────────────────────────────
        self.log("🔢  Building match ledger and counterfactual paths...")
        ledger = self._build_match_ledger(
            recent_mmr, recent_is_placement, recent_is_newly_placed,
            recent_previous_match, predicted_matches_mmr
        )
        ledger_entries = list(ledger.values())[1:]
        actual_elos    = [e["elo_after"] for e in ledger_entries]

        cf_elos         = self._compute_counterfactual_path(
            ledger_entries, recent_is_placement, predicted_matches_mmr, False)
        cf_adj_elos     = self._compute_counterfactual_path(
            ledger_entries, recent_is_placement, predicted_matches_mmr, True)
        cf_nb_elos      = self._compute_counterfactual_nobuffer_path(
            ledger_entries, recent_is_placement, predicted_matches_mmr, False)
        cf_nb_adj_elos  = self._compute_counterfactual_nobuffer_path(
            ledger_entries, recent_is_placement, predicted_matches_mmr, True)

        # ── 8. Plot: Counterfactual RR ────────────────────────────────────────
        self.log("🎨  Generating counterfactual RR chart...")
        self._plot_counterfactual(
            ledger_entries, actual_elos,
            cf_elos, cf_adj_elos, cf_nb_elos, cf_nb_adj_elos,
            recent_is_placement, recent_is_newly_placed,
            recent_matches, out_dir / "counterfactual_rr.png"
        )
        self.log("💾  Saved counterfactual_rr.png")

        # ── 8b. Plot: Actual RR only ──────────────────────────────────────────
        self.log("🎨  Generating actual RR chart (no counterfactuals)...")
        self._plot_actual_rr(
            ledger_entries, actual_elos,
            recent_is_placement, recent_is_newly_placed,
            recent_matches, out_dir / "actual_rr.png"
        )
        self.log("💾  Saved actual_rr.png")

        # ── 9. HTML Report ────────────────────────────────────────────────────
        self.log("🌐  Generating interactive HTML agent report...")
        acts_slug_readable = "_".join(f"E{ep}A{act}" for ep, act in acts_of_interest)
        html_folder_name   = f"agent_report"
        html_dir           = out_dir / html_folder_name
        html_dir.mkdir(exist_ok=True)
        generate_html_report(recent_matches, my_puuid,
                             output_file=str(html_dir / "index.html"))
        self.log(f"💾  Saved {html_folder_name}/index.html")

        # ── 10. Summary text file ─────────────────────────────────────────────
        self.log("📝  Writing analysis summary...")
        last_mmr_val = recent_mmr[recent_matches[0].metadata.match_id]
        current_rank = map_rank_value(last_mmr_val.elo / 100)
        estimated_mmr_str = map_rank_value(predict_mmr(rank_averages, 0))
        cf_rank = map_rank_value(cf_nb_adj_elos[-1] / 100) if cf_nb_adj_elos else "N/A"

        role_pcts = calculate_role_percentages(recent_matches, my_puuid)
        results   = analyze_match_history(rank_averages)
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        summary_lines = [
            f"Valorant Rank Analysis — {player_name}#{player_tag}",
            f"Generated: {timestamp}",
            f"Acts: {', '.join(f'Ep {e} Act {a}' for e, a in acts_of_interest)}",
            "",
            f"Matches Analysed  : {len(recent_matches)}",
            f"Win Rate          : {wr['winrate']:.0%}  ({wr['wins']}W / {wr['losses']}L)",
            f"Current Rank      : {current_rank}",
            f"Estimated True MMR: {estimated_mmr_str}",
            f"Counterfactual Rank (no-buffer+adj): {cf_rank}",
            "",
            "Lobby Balance",
            f"  Most balanced:  {results['lobby_balance']['most_balanced'][0]:.3f} std",
            f"  Least balanced: {results['lobby_balance']['least_balanced'][0]:.3f} std",
            "",
            "Opponent Role Distribution",
        ]
        for role, pct in sorted(role_pcts.items()):
            summary_lines.append(f"  {role:<14} {pct}")

        with open(out_dir / "summary.txt", "w") as f:
            f.write("\n".join(summary_lines))
        self.log("💾  Saved summary.txt")

        # ── 11. Round stats JSON ──────────────────────────────────────────────
        self.log("🎲  Collecting round stats...")
        round_stats_out = None
        all_rounds = []
        try:
            from collections import defaultdict
            skipped_rs = 0
            for match in recent_matches:
                mid      = match.metadata.match_id
                mmr_item = recent_mmr.get(mid)
                if mmr_item is None or TIER_NORMALISE.get(
                        mmr_item.tier.name if mmr_item.tier else "", None) is None:
                    skipped_rs += 1
                    continue
                for idx in range(match.number_of_rounds):
                    try:
                        rnd    = match.get_round(idx)
                        record = extract_round_record(match, idx, rnd, mmr_item, my_puuid)
                        if record is not None:
                            all_rounds.append(record)
                    except Exception:
                        pass

            if all_rounds:
                by_rank_raw: dict = defaultdict(list)
                by_act_raw:  dict = defaultdict(list)
                by_map_raw:  dict = defaultdict(list)
                for r in all_rounds:
                    k = TIER_NORMALISE.get(r["tier_name"])
                    if k:
                        by_rank_raw[k].append(r)
                    by_act_raw[r["act"]].append(r)
                    by_map_raw[r["map"]].append(r)

                round_stats_out = {
                    "meta": {
                        "name":         player_name,
                        "tag":          player_tag,
                        "region":       region,
                        "puuid":        my_puuid,
                        "acts":         [f"e{ep}a{act}" for ep, act in acts_of_interest],
                        "match_count":  len(recent_matches) - skipped_rs,
                        "round_count":  len(all_rounds),
                        "generated_at": datetime.now(timezone.utc).isoformat(),
                    },
                    "overall": compute_stats(all_rounds),
                    "by_rank": {k: compute_stats(v) for k, v in by_rank_raw.items() if v},
                    "by_act":  {k: compute_stats(v) for k, v in by_act_raw.items()  if v},
                    "by_map":  {k: compute_stats(v) for k, v in by_map_raw.items()  if v},
                }
                with open(out_dir / "round_stats.json", "w", encoding="utf-8") as f:
                    json.dump(round_stats_out, f, indent=2, ensure_ascii=False)
                self.log(f"💾  Saved round_stats.json ({len(all_rounds)} rounds)")
            else:
                self.log("⚠️  No round data available — round_stats.json skipped.")
        except Exception as exc:
            self.log(f"⚠️  round_stats.json skipped: {exc}")

        # ── 12. Copy utility_recharge_calculator.html ─────────────────────────
        util_src = Path(__file__).parent / "utility_recharge_calculator.html"
        if util_src.exists():
            shutil.copy2(util_src, out_dir / "utility_recharge_calculator.html")
            self.log("💾  Copied utility_recharge_calculator.html")
        else:
            self.log("⚠️  utility_recharge_calculator.html not found next to main.py — skipped.")

        # ── 13. PDF Report ────────────────────────────────────────────────────
        self.log("📄  Generating PDF report...")
        try:
            pdf_path = self._generate_pdf(
                out_dir=out_dir,
                player_name=player_name,
                player_tag=player_tag,
                acts_of_interest=acts_of_interest,
                region=region,
                timestamp=timestamp,
                wr=wr,
                current_rank=current_rank,
                estimated_mmr_str=estimated_mmr_str,
                cf_rank=cf_rank,
                results=results,
                role_pcts=role_pcts,
                ledger_entries=ledger_entries,
                round_stats_out=round_stats_out,
                match_count_max=match_count_max,
                timespan=timespan,
            )
            self.log(f"💾  Saved report.pdf")
        except Exception as exc:
            import traceback
            self.log(f"⚠️  PDF generation failed: {exc}\n{traceback.format_exc()}")

        # ── 14. Player metadata for GUI history ──────────────────────────────
        meta = {
            "player_name": player_name,
            "player_tag":  player_tag,
            "puuid":       my_puuid,
            "timestamp":   timestamp,
            "matches":     len(recent_matches),
            "winrate":     f"{wr['winrate']:.0%}",
            "current_rank": current_rank,
            "estimated_mmr": estimated_mmr_str,
            "acts": [f"Ep {e} Act {a}" for e, a in acts_of_interest],
        }
        with open(out_dir / "meta.json", "w") as f:
            json.dump(meta, f, indent=2)

        self.log(f"\n✨  All done! Output folder: {out_dir.resolve()}")
        return out_dir


# ─────────────────────────────────────────────────────────────────────────────
#  GUI
# ─────────────────────────────────────────────────────────────────────────────

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Valorant Rank Analyzer")
        self.resizable(True, True)
        self.minsize(900, 640)

        # Load persisted settings
        self._config = load_config()

        self._apply_theme()

        # Cache: (name, tag, region) → list of (ep, act, label, games_played|None)
        self._lookup_cache: Dict[Tuple[str, str, str], List] = {}
        # Whether a lookup thread is currently running
        self._lookup_running = False

        self._build_ui()
        self._load_player_history()
        self._running = False

        # No acts until a player is looked up
        self._acts_status_var.set("🔍  Look up a player to see their acts.")

    # ── Theme ─────────────────────────────────────────────────────────────────

    def _apply_theme(self):
        self.configure(bg=DARK)
        style = ttk.Style(self)
        style.theme_use("clam")

        style.configure(".", background=DARK, foreground=TEXT, fieldbackground=MID,
                        troughcolor=MID, selectbackground=CARD, selectforeground=TEXT,
                        font=("Segoe UI", 10))

        style.configure("TFrame",       background=DARK)
        style.configure("Card.TFrame",  background=MID,  relief="flat")
        style.configure("TLabel",       background=DARK, foreground=TEXT)
        style.configure("Card.TLabel",  background=MID,  foreground=TEXT)
        style.configure("Dim.TLabel",   background=DARK, foreground=DIM,  font=("Segoe UI", 9))
        style.configure("Dim.Card.TLabel", background=MID, foreground=DIM, font=("Segoe UI", 9))
        style.configure("Head.TLabel",  background=DARK, foreground=RED,
                        font=("Segoe UI", 13, "bold"))
        style.configure("Title.TLabel", background=DARK, foreground=TEXT,
                        font=("Segoe UI", 11, "bold"))
        style.configure("Warn.TLabel",  background=DARK, foreground="#FFAA33",
                        font=("Segoe UI", 9))

        style.configure("Accent.TButton", background=RED, foreground="white",
                        font=("Segoe UI", 11, "bold"), relief="flat", borderwidth=0, padding=10)
        style.map("Accent.TButton",
                  background=[("active", "#cc2233"), ("disabled", "#555")],
                  foreground=[("disabled", "#888")])

        style.configure("TEntry",       fieldbackground=CARD, foreground=TEXT,
                        insertcolor=TEXT, borderwidth=0, relief="flat", padding=6,
                        selectbackground=RED, selectforeground="white")
        style.map("TEntry",
                  selectbackground=[("focus", RED), ("!focus", RED)],
                  selectforeground=[("focus", "white"), ("!focus", "white")])
        style.configure("TSpinbox",     fieldbackground=CARD, foreground=TEXT,
                        borderwidth=0, relief="flat", arrowcolor=DIM,
                        selectbackground=RED, selectforeground="white")
        style.configure("TCombobox",    fieldbackground=CARD, foreground=TEXT,
                        background=CARD, selectbackground=CARD, selectforeground=TEXT,
                        borderwidth=0, relief="flat", arrowcolor=DIM, padding=4)
        style.map("TCombobox",
                  fieldbackground=[("readonly", CARD), ("disabled", MID)],
                  foreground=[("readonly", TEXT), ("disabled", DIM)],
                  selectbackground=[("readonly", CARD)],
                  selectforeground=[("readonly", TEXT)])
        # Force the dropdown listbox colours (ttk doesn't expose these via style)
        self.option_add("*TCombobox*Listbox*Background",       CARD)
        self.option_add("*TCombobox*Listbox*Foreground",       TEXT)
        self.option_add("*TCombobox*Listbox*SelectBackground", RED)
        self.option_add("*TCombobox*Listbox*SelectForeground", "white")
        style.configure("TCheckbutton", background=DARK, foreground=TEXT,
                        selectcolor=CARD)
        style.configure("Separator.TFrame", background="#333")

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        root = ttk.Frame(self, padding=0)
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=0, minsize=240)
        root.columnconfigure(1, weight=1)
        root.rowconfigure(0, weight=1)

        # ── Left sidebar: player history ──────────────────────────────────────
        sidebar = ttk.Frame(root, style="Card.TFrame", padding=(0, 0))
        sidebar.grid(row=0, column=0, sticky="nsew")
        sidebar.rowconfigure(1, weight=1)

        ttk.Label(sidebar, text="PAST PLAYERS", style="Head.TLabel",
                  padding=(14, 14, 14, 6)).grid(row=0, column=0, sticky="ew")

        list_frame = ttk.Frame(sidebar, style="Card.TFrame")
        list_frame.grid(row=1, column=0, sticky="nsew", padx=6)
        list_frame.rowconfigure(0, weight=1)
        list_frame.columnconfigure(0, weight=1)

        scrollbar = ttk.Scrollbar(list_frame, orient="vertical")
        self.player_list = tk.Listbox(
            list_frame,
            bg=CARD, fg=TEXT, selectbackground=RED, selectforeground="white",
            relief="flat", borderwidth=0, highlightthickness=0,
            font=("Segoe UI", 10), activestyle="none",
            yscrollcommand=scrollbar.set
        )
        scrollbar.config(command=self.player_list.yview)
        self.player_list.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.player_list.bind("<<ListboxSelect>>", self._on_history_select)

        ttk.Label(sidebar, text="Click a player to prefill their details.",
                  style="Dim.Card.TLabel", padding=(10, 6, 10, 4),
                  wraplength=200).grid(row=2, column=0, sticky="ew")

        load_btn = ttk.Button(sidebar, text="📂  Open Player Folder",
                              command=self._open_selected_player_folder,
                              padding=(8, 6))
        load_btn.grid(row=3, column=0, sticky="ew", padx=10, pady=(0, 12))

        # ── Right panel: config + generate ───────────────────────────────────
        right = ttk.Frame(root, padding=(24, 20))
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(5, weight=1)

        ttk.Label(right, text="🎮  VALORANT RANK ANALYZER", style="Head.TLabel",
                  padding=(0, 0, 0, 4)).grid(row=0, column=0, sticky="w")
        ttk.Label(right,
                  text="Configure a player below and click Generate to produce all charts and the HTML report.",
                  style="Dim.TLabel", wraplength=600).grid(row=1, column=0, sticky="w", pady=(0, 16))

        # ── Player details card ───────────────────────────────────────────────
        player_card = ttk.Frame(right, style="Card.TFrame", padding=16)
        player_card.grid(row=2, column=0, sticky="ew", pady=(0, 12))
        player_card.columnconfigure(1, weight=1)
        player_card.columnconfigure(3, weight=1)

        ttk.Label(player_card, text="PLAYER DETAILS", style="Title.TLabel",
                  background=MID).grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 10))

        ttk.Label(player_card, text="Name", style="Card.TLabel",
                  background=MID).grid(row=1, column=0, sticky="w", padx=(0, 8))
        self.name_var = tk.StringVar()
        ttk.Entry(player_card, textvariable=self.name_var,
                  width=22).grid(row=1, column=1, sticky="ew")

        ttk.Label(player_card, text="Tag  #", style="Card.TLabel",
                  background=MID).grid(row=1, column=2, sticky="w", padx=(20, 8))
        self.tag_var = tk.StringVar()
        tag_entry = ttk.Entry(player_card, textvariable=self.tag_var, width=14)
        tag_entry.grid(row=1, column=3, sticky="ew")
        # Fire lookup when user tabs/clicks away from the tag field — natural finishing point
        tag_entry.bind("<FocusOut>", lambda _e: self._trigger_player_lookup())

        self.lookup_btn = ttk.Button(player_card, text="🔍  Look Up",
                                     command=self._trigger_player_lookup, padding=(6, 4))
        self.lookup_btn.grid(row=1, column=4, sticky="w", padx=(10, 0))

        ttk.Label(player_card, text="Region", style="Card.TLabel",
                  background=MID).grid(row=2, column=0, sticky="w", padx=(0, 8), pady=(10, 0))
        self.region_var = tk.StringVar(value=self._config.get("region", "na"))
        region_combo = ttk.Combobox(player_card, textvariable=self.region_var,
                                    values=["na", "eu", "ap", "latam", "br", "kr"],
                                    width=8, state="readonly")
        region_combo.grid(row=2, column=1, sticky="w", pady=(10, 0))
        # Invalidate lookup cache when region changes (different DB)
        region_combo.bind("<<ComboboxSelected>>",
                          lambda _e: self._on_region_change())

        ttk.Label(player_card, text="Henrik API Key", style="Card.TLabel",
                  background=MID).grid(row=3, column=0, sticky="w", padx=(0, 8), pady=(10, 0))
        self.api_key_var = tk.StringVar(value=self._config.get("api_key", ""))
        api_key_entry = ttk.Entry(player_card, textvariable=self.api_key_var,
                                  show="•", width=36)
        api_key_entry.grid(row=3, column=1, columnspan=3, sticky="ew", pady=(10, 0))
        # Show plaintext while editing, mask when focus leaves
        api_key_entry.bind("<FocusIn>",  lambda _e: api_key_entry.config(show=""))
        api_key_entry.bind("<FocusOut>", lambda _e: api_key_entry.config(show="•"))
        ttk.Label(player_card,
                  text="Required. Saved automatically.",
                  style="Dim.Card.TLabel", background=MID).grid(
                      row=3, column=4, sticky="w", padx=(10, 0), pady=(10, 0))
        # Persist the key whenever it changes
        self.api_key_var.trace_add("write", lambda *_: self._save_api_key())

        # ── Acts card ─────────────────────────────────────────────────────────
        acts_outer = ttk.Frame(right, style="Card.TFrame", padding=16)
        acts_outer.grid(row=3, column=0, sticky="ew", pady=(0, 12))
        acts_outer.columnconfigure(0, weight=1)

        ttk.Label(acts_outer, text="EPISODES / ACTS", style="Title.TLabel",
                  background=MID).grid(row=0, column=0, sticky="w", pady=(0, 4))
        ttk.Label(acts_outer,
                  text="Look up a player to see how many games they have per act.",
                  style="Dim.Card.TLabel", background=MID).grid(
                      row=1, column=0, sticky="w", pady=(0, 8))

        # Container where checkboxes will be injected after async API load
        self._acts_cb_frame = tk.Frame(acts_outer, bg=MID)
        self._acts_cb_frame.grid(row=2, column=0, sticky="ew")
        self.act_vars: Dict[Tuple[str, str], tk.BooleanVar] = {}

        self._acts_status_var = tk.StringVar(value="")
        ttk.Label(acts_outer, textvariable=self._acts_status_var,
                  style="Dim.Card.TLabel", background=MID).grid(row=3, column=0, sticky="w")

        # ── Match count + options ─────────────────────────────────────────────
        opts_card = ttk.Frame(right, style="Card.TFrame", padding=16)
        opts_card.grid(row=4, column=0, sticky="ew", pady=(0, 12))
        opts_card.columnconfigure(3, weight=1)

        ttk.Label(opts_card, text="OPTIONS", style="Title.TLabel",
                  background=MID).grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 10))

        ttk.Label(opts_card, text="Max Matches", style="Card.TLabel",
                  background=MID).grid(row=1, column=0, sticky="w", padx=(0, 8))
        self.match_count_var = tk.IntVar(value=20)
        spin = ttk.Spinbox(opts_card, from_=1, to=200, width=6,
                           textvariable=self.match_count_var,
                           command=self._on_match_count_change)
        spin.grid(row=1, column=1, sticky="w")
        self.match_count_var.trace_add("write", lambda *_: self._on_match_count_change())

        self.warn_label = ttk.Label(opts_card, text="", style="Warn.TLabel", background=MID)
        self.warn_label.grid(row=1, column=2, sticky="w", padx=(12, 0))

        ttk.Label(opts_card, text="Timespan (days)", style="Card.TLabel",
                  background=MID).grid(row=2, column=0, sticky="w", padx=(0, 8), pady=(10, 0))
        self.timespan_var = tk.IntVar(value=30)
        ttk.Spinbox(opts_card, from_=1, to=365, width=6,
                    textvariable=self.timespan_var).grid(row=2, column=1, sticky="w", pady=(10, 0))

        # ── Generate button ───────────────────────────────────────────────────
        gen_frame = ttk.Frame(right)
        gen_frame.grid(row=5, column=0, sticky="ew", pady=(0, 12))
        gen_frame.columnconfigure(0, weight=1)

        self.gen_btn = ttk.Button(gen_frame, text="⚡  Generate Analysis",
                                  style="Accent.TButton",
                                  command=self._start_analysis)
        self.gen_btn.grid(row=0, column=0, sticky="ew", ipady=4)

        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(gen_frame, textvariable=self.status_var,
                  style="Dim.TLabel").grid(row=1, column=0, sticky="w", pady=(6, 0))

        # ── Progress log ──────────────────────────────────────────────────────
        ttk.Label(right, text="LOG", style="Dim.TLabel").grid(
            row=6, column=0, sticky="w", pady=(0, 4))

        log_frame = ttk.Frame(right)
        log_frame.grid(row=7, column=0, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        right.rowconfigure(7, weight=1)

        log_scroll = ttk.Scrollbar(log_frame)
        log_scroll.grid(row=0, column=1, sticky="ns")

        self.log_text = tk.Text(
            log_frame, height=10, wrap="word",
            bg="#0a0a12", fg="#7dcfaa", insertbackground=TEXT,
            selectbackground=RED, selectforeground="white",
            relief="flat", borderwidth=0, font=("Consolas", 9),
            yscrollcommand=log_scroll.set, state="disabled",
            padx=10, pady=8
        )
        self.log_text.grid(row=0, column=0, sticky="nsew")
        log_scroll.config(command=self.log_text.yview)

        self._on_match_count_change()

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _on_match_count_change(self, *_):
        try:
            v = self.match_count_var.get()
        except Exception:
            return
        if v > API_WARN_THRESHOLD:
            self.warn_label.config(
                text=f"⚠️  High count — each match makes API calls and can be slow."
            )
        else:
            self.warn_label.config(text="")

    def _log(self, msg: str):
        """Append a line to the log widget (thread-safe via after)."""
        def _append():
            self.log_text.config(state="normal")
            self.log_text.insert("end", msg + "\n")
            self.log_text.see("end")
            self.log_text.config(state="disabled")
        self.after(0, _append)

    def _set_status(self, msg: str):
        self.after(0, lambda: self.status_var.set(msg))

    def _set_generating(self, active: bool):
        def _do():
            self.gen_btn.config(state="disabled" if active else "normal")
            self._running = active
        self.after(0, _do)

    # ── Player history ────────────────────────────────────────────────────────

    def _load_player_history(self):
        """Scan outputs/ and populate the sidebar listbox."""
        self.player_list.delete(0, "end")
        self._history_entries: List[Dict] = []
        if not OUTPUTS_DIR.exists():
            return
        # Folders are now flat: outputs/<name_tag_acts_Nmatches_date>/meta.json
        candidates = sorted(
            OUTPUTS_DIR.iterdir(),
            key=lambda p: p.stat().st_mtime,
            reverse=True
        )
        seen_puuids: set = set()
        for run_dir in candidates:
            if not run_dir.is_dir():
                continue
            meta_path = run_dir / "meta.json"
            if not meta_path.exists():
                continue
            try:
                with open(meta_path) as f:
                    meta = json.load(f)

                # Deduplicate by PUUID — keep only the most recent run per player.
                # Fall back to name#tag for older meta.json files that lack puuid.
                dedup_key = meta.get("puuid") or (
                    f"{meta.get('player_name','').lower()}#{meta.get('player_tag','').lower()}"
                )
                if dedup_key in seen_puuids:
                    continue
                seen_puuids.add(dedup_key)

                meta["_run_dir"] = str(run_dir)
                self._history_entries.append(meta)
                label = (
                    f"{meta.get('player_name','?')}#{meta.get('player_tag','?')}  "
                    f"— {meta.get('current_rank','?')}  "
                    f"({meta.get('matches','?')} games)"
                )
                self.player_list.insert("end", label)
            except Exception:
                pass

    def _on_history_select(self, _event=None):
        sel = self.player_list.curselection()
        if not sel:
            return
        idx  = sel[0]
        meta = self._history_entries[idx]
        self.name_var.set(meta.get("player_name", ""))
        self.tag_var.set(meta.get("player_tag", ""))
        # Sidebar click is an explicit "I want this player" signal — look them up
        self._trigger_player_lookup()

    # ── Config persistence ────────────────────────────────────────────────────

    def _save_api_key(self):
        self._config["api_key"] = self.api_key_var.get()
        save_config(self._config)

    def _on_region_change(self):
        self._config["region"] = self.region_var.get()
        save_config(self._config)
        # Bust the lookup cache so re-lookup uses the new region's DB
        self._lookup_cache.clear()

    # ── Acts population ───────────────────────────────────────────────────────

    def _populate_acts_checkboxes(
        self,
        acts_data: List[Tuple[str, str, str, Optional[int]]],
        source: str = "",
        preserve_selection: bool = False,
    ):
        """
        (Re)build the acts checkbox grid.

        acts_data entries: (episode_str, act_str, display_label, games_played|None)

        - games_played → shown as count in parentheses
        - preserve_selection → keep existing checked state where acts overlap
        """
        if not acts_data:
            self._acts_status_var.set("⚠️  No acts found.")
            return

        old_checked = {k for k, v in self.act_vars.items() if v.get()} if preserve_selection else set()

        for w in self._acts_cb_frame.winfo_children():
            w.destroy()
        self.act_vars.clear()

        # Default selection: two most-recent acts that have recorded games;
        # if no game data available yet, default to the two most-recent acts.
        acts_with_data = [(ep, act) for ep, act, _, gp in acts_data if gp and gp > 0]
        if acts_with_data:
            default_checked = set(acts_with_data[-2:]) if len(acts_with_data) >= 2 \
                              else {acts_with_data[-1]}
        else:
            all_pairs = [(ep, act) for ep, act, _, _ in acts_data]
            default_checked = set(all_pairs[-2:]) if len(all_pairs) >= 2 else {all_pairs[-1]}

        cols = 5
        for idx, (ep, act, label, games_played) in enumerate(acts_data):
            row = idx // cols
            col = idx % cols

            display  = f"{label}  ({games_played})"
            fg_color = TEXT

            key     = (ep, act)
            checked = (key in old_checked) if preserve_selection else (key in default_checked)

            var = tk.BooleanVar(value=checked)
            self.act_vars[key] = var

            cb = tk.Checkbutton(
                self._acts_cb_frame, text=display, variable=var,
                bg=MID, fg=fg_color, selectcolor=CARD,
                activebackground=MID, activeforeground=TEXT,
                font=("Segoe UI", 9), relief="flat", borderwidth=0,
                highlightthickness=0, cursor="hand2",
            )
            cb.grid(row=row, column=col, sticky="w", padx=(0, 12), pady=2)

        suffix = f"  ·  {source}" if source else ""
        self._acts_status_var.set(f"✅  {len(acts_data)} act(s){suffix}")

    # ── Player lookup ─────────────────────────────────────────────────────────

    def _trigger_player_lookup(self):
        """
        Single entry point for Look Up button, FocusOut on tag, and history click.
        Uses cache keyed on (name, tag, region) — no network hit if already known.
        """
        name   = self.name_var.get().strip()
        tag    = self.tag_var.get().strip()
        region = self.region_var.get()

        if not name or not tag:
            return

        cache_key = (name.lower(), tag.lower(), region)

        if cache_key in self._lookup_cache:
            self._populate_acts_checkboxes(
                self._lookup_cache[cache_key],
                source=f"{name}#{tag} (cached)"
            )
            return

        if self._lookup_running:
            return

        self._lookup_running = True
        self._acts_status_var.set(f"⏳  Looking up {name}#{tag}…")
        self.lookup_btn.config(state="disabled")

        threading.Thread(
            target=self._do_player_lookup,
            args=(name, tag, region, cache_key),
            daemon=True,
        ).start()

    def _do_player_lookup(self, name: str, tag: str, region: str, cache_key: tuple):
        """
        Background thread: fetch seasonal_performances from the DB and build
        the acts list from only what the player has actually played.
        """
        def _done(acts_data, status_msg):
            def _ui():
                self._lookup_running = False
                self.lookup_btn.config(state="normal")
                self._populate_acts_checkboxes(acts_data, source=status_msg)
            self.after(0, _ui)

        try:
            db       = ValorantDB(region=region)
            seasonal = db.get_profile_by_name(name, tag).seasonal_performances or []

            # Only show acts the player has actually played (games_played > 0).
            gp_by_act: dict = defaultdict(int)
            for p in seasonal:
                gp = getattr(p, "games_played", None) or 0
                if gp > 0:
                    gp_by_act[(str(p.episode), str(p.act))] += gp

            acts_data: List[Tuple[str, str, str, Optional[int]]] = sorted(
                [
                    (ep, act, _act_label(int(ep), int(act)), total_gp)
                    for (ep, act), total_gp in gp_by_act.items()
                ],
                key=lambda x: (int(x[0]), int(x[1])),
            )            

            if not acts_data:
                raise ValueError("no seasonal data")

            self._lookup_cache[cache_key] = acts_data
            _done(acts_data, f"{name}#{tag}")

        except Exception as e:
            # Player not in DB yet or network error — show nothing, just inform
            def _err_ui():
                self._lookup_running = False
                self.lookup_btn.config(state="normal")
                self._acts_status_var.set(f"⚠️  No data found for {name}#{tag}. Try generating first.")
            self.after(0, _err_ui)

    def _open_selected_player_folder(self):
        sel = self.player_list.curselection()
        if not sel:
            messagebox.showinfo("No Selection", "Select a player from the list first.")
            return
        idx = sel[0]
        meta = self._history_entries[idx]
        folder = Path(meta["_run_dir"])
        if folder.exists():
            _open_folder(folder)
        else:
            messagebox.showerror("Not Found", f"Folder no longer exists:\n{folder}")

    # ── Analysis trigger ──────────────────────────────────────────────────────

    def _start_analysis(self):
        if self._running:
            return

        name = self.name_var.get().strip()
        tag  = self.tag_var.get().strip()
        if not name or not tag:
            messagebox.showwarning("Missing Info", "Please enter both a player name and tag.")
            return

        acts = [(ep, act) for (ep, act), var in self.act_vars.items() if var.get()]
        if not acts:
            messagebox.showwarning("No Acts Selected", "Please select at least one episode / act.")
            return

        api_key = self.api_key_var.get().strip()
        if not api_key:
            messagebox.showwarning("API Key Required",
                                   "Please enter your Henrik API key before generating.")
            return

        match_count = self.match_count_var.get()
        timespan    = self.timespan_var.get()
        region      = self.region_var.get()

        # Confirmation for high match counts
        if match_count > API_WARN_THRESHOLD:
            ok = messagebox.askyesno(
                "API Rate Warning",
                f"You've requested {match_count} matches.\n\n"
                "Each match may require multiple API calls. This can be slow and "
                "risks hitting the Henrik API rate limit.\n\n"
                "Continue anyway?",
            )
            if not ok:
                return

        # Clear log
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.config(state="disabled")

        self._set_generating(True)
        self._set_status(f"Running analysis for {name}#{tag}...")
        self._log(f"Starting analysis for {name}#{tag}")
        self._log(f"Acts: {', '.join(f'Ep {e} Act {a}' for e, a in acts)}")
        self._log(f"Max matches: {match_count}  |  Timespan: {timespan} days  |  Region: {region}\n")

        engine = AnalysisEngine(log=self._log)

        def _run():
            try:
                out_dir = engine.run(
                    player_name=name,
                    player_tag=tag,
                    acts_of_interest=acts,
                    match_count_max=match_count,
                    timespan=timespan,
                    region=region,
                    api_key=api_key,
                )
                self._set_status(f"✅  Done! Output in: {out_dir}")
                self._load_player_history()

                def _offer_open():
                    if messagebox.askyesno(
                        "Analysis Complete",
                        f"Analysis for {name}#{tag} is done!\n\n"
                        f"Open the output folder?",
                    ):
                        _open_folder(out_dir)

                self.after(0, _offer_open)

            except Exception as exc:
                import traceback
                tb = traceback.format_exc()
                self._log(f"\n❌  Error: {exc}\n{tb}")
                self._set_status(f"❌  Failed: {exc}")
                self.after(0, lambda: messagebox.showerror(
                    "Analysis Failed",
                    f"An error occurred:\n\n{exc}\n\nSee the log for details."
                ))
            finally:
                self._set_generating(False)

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()


# ─────────────────────────────────────────────────────────────────────────────
#  Utilities
# ─────────────────────────────────────────────────────────────────────────────

def _open_folder(path: Path):
    """Open a folder in the system file explorer, cross-platform."""
    path = path.resolve()
    if sys.platform == "win32":
        os.startfile(str(path))
    elif sys.platform == "darwin":
        subprocess.run(["open", str(path)], check=False)
    else:
        subprocess.run(["xdg-open", str(path)], check=False)


# ─────────────────────────────────────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    OUTPUTS_DIR.mkdir(exist_ok=True)
    app = App()
    app.mainloop()
