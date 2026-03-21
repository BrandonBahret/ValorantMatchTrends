"""
plots.py
════════
All matplotlib chart generation.  Every function receives its data and an
output *Path* to write to — no side-effects beyond writing the PNG.
"""

from pathlib import Path
from typing import List

import numpy as np
import matplotlib
matplotlib.use("Agg")
from matplotlib import pyplot as plt
from scipy.stats import gaussian_kde

from constants import RED, BIG_LOSS_THRESHOLD_RR
from rank_utils import VALORANT_RANKS, lookup_rank, map_rank_value
from mmr_spline import predict_mmr
from match_analysis import find_change_indices
from ledger import calculate_placement_regions


# ─────────────────────────────────────────────────────────────────────────────
#  Rank Trends
# ─────────────────────────────────────────────────────────────────────────────

def plot_rank_trends(rank_averages: dict, recent_matches: list,
                     recent_is_placement: dict, recent_is_newly_placed: dict,
                     out_path: Path, n: int = None) -> None:
    """Lobby rank averages over time with optional *n*-match window."""
    full_total = len(next(iter(rank_averages.values())))
    total = min(n, full_total) if n is not None else full_total
    rank_averages  = {k: v[:total] for k, v in rank_averages.items()}
    recent_matches = recent_matches[:total]
    game_versions  = [recent_matches[i].metadata.game_version for i in range(total)]
    game_versions  = [
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

    placement_ranges = calculate_placement_regions(
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


# ─────────────────────────────────────────────────────────────────────────────
#  Lobby Std Distribution
# ─────────────────────────────────────────────────────────────────────────────

def plot_lobby_std_distribution(rank_averages: dict, out_path: Path) -> None:
    """KDE density plot of per-match lobby standard deviation."""
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

    pct_1s       = pct((lobby_std >= mean_std - std_std)   & (lobby_std <= mean_std + std_std))
    pct_l12      = pct((lobby_std >= mean_std - 2*std_std) & (lobby_std < mean_std - std_std))
    pct_r12      = pct((lobby_std > mean_std + std_std)    & (lobby_std <= mean_std + 2*std_std))
    pct_beyond_l = pct(lobby_std < mean_std - 2*std_std)
    pct_beyond_r = pct(lobby_std > mean_std + 2*std_std)

    vlines = {
        "Mean": (mean_std, RED, "--"),
        "+1σ":  (mean_std + std_std, "#377eb8", "-."),
        "-1σ":  (mean_std - std_std, "#377eb8", "-."),
        "+2σ":  (mean_std + 2*std_std, "#e67e00", ":"),
        "-2σ":  (mean_std - 2*std_std, "#e67e00", ":"),
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


# ─────────────────────────────────────────────────────────────────────────────
#  Counterfactual RR
# ─────────────────────────────────────────────────────────────────────────────

def plot_counterfactual(ledger_entries: list, actual_elos: list,
                         cf_elos, cf_adj_elos, cf_nb_elos, cf_nb_adj_elos,
                         recent_is_placement: dict, recent_is_newly_placed: dict,
                         recent_matches: list, out_path: Path) -> None:
    """Actual vs. all counterfactual RR paths."""
    match_indices = list(range(len(ledger_entries)))
    line_colors = {
        "actual":         "#147C86",
        "no_shields":     "#F18F01",
        "no_shields_adj": "#F18F01",
        "no_buffer":      "#6A4C93",
        "no_buffer_adj":  "#6A4C93",
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
            color=line_colors["no_shields"], linewidth=2, marker="s", markersize=3, alpha=0.8)
    ax.plot(match_indices, cf_adj_elos, label="No Shields RR (adjusted)", linestyle=":",
            color=line_colors["no_shields_adj"], linewidth=1.5, marker="^", markersize=3, alpha=0.6)
    ax.plot(match_indices, cf_nb_elos, label="No Buffer RR", linestyle="-",
            color=line_colors["no_buffer"], linewidth=2, marker="d", markersize=3, alpha=0.8)
    ax.plot(match_indices, cf_nb_adj_elos, label="No Buffer RR (adjusted)", linestyle=":",
            color=line_colors["no_buffer_adj"], linewidth=1.5, marker="v", markersize=3, alpha=0.6)
    ax.plot(match_indices, actual_clean, label="Actual RR", color=line_colors["actual"],
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

    for xi in match_indices:
        ax.axvline(xi, color="#ccc", linestyle="dashed", alpha=0.5, linewidth=0.6)

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

    placement_ranges = calculate_placement_regions(
        recent_matches, recent_is_placement, recent_is_newly_placed,
        reverse=True, offset=-1
    )
    first_span = True
    for s, e in placement_ranges:
        ax.axvspan(s - 0.5, e + 0.5, alpha=0.13, color="#4C72B0",
                   label="Placement Matches" if first_span else None)
        first_span = False

    events = {
        "splice":       dict(cond=lambda e: e.get("is_splice"),
                             plot="axvline", color="#555", linewidth=2, alpha=0.5,
                             linestyle="--", label="Data Splice"),
        "shield":       dict(cond=lambda e: e.get("shield_used"),
                             plot="scatter", s=100, facecolors="none", edgecolors=RED,
                             linewidths=2, label="Shield Used"),
        "rr_desync":    dict(cond=lambda e: e.get("rr_desync"),
                             plot="scatter", s=40, facecolors="#ff8800", edgecolors="#7a0000",
                             zorder=100, linewidths=2, label="RR Desync"),
        "newly_placed": dict(cond=lambda e: e.get("is_newly_placed_rank"),
                             plot="scatter", s=40, facecolors="#ff8800", edgecolors="#006666",
                             zorder=100, linewidths=2, label="Newly Placed Rank"),
        "buffer":       dict(cond=lambda e: e.get("buffer_used") and not e.get("shield_used"),
                             plot="scatter", s=100, facecolors="none", edgecolors="#cc44cc",
                             linewidths=2, label="Buffer Used"),
        "big_loss":     dict(cond=lambda e: e.get("big_loss"),
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


# ─────────────────────────────────────────────────────────────────────────────
#  Actual RR only
# ─────────────────────────────────────────────────────────────────────────────

def plot_actual_rr(ledger_entries: list, actual_elos: list,
                   recent_is_placement: dict, recent_is_newly_placed: dict,
                   recent_matches: list, out_path: Path) -> None:
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

    for rname in VALORANT_RANKS:
        for target in ["Bronze", "Silver", "Gold", "Platinum", "Diamond", "Ascendant"]:
            if target in rname:
                ax.axhline(VALORANT_RANKS.index(rname) * 100,
                           color="#bbb", linestyle=":", linewidth=0.7, alpha=0.7, zorder=0)

    for xi in match_indices:
        ax.axvline(xi, color="#ccc", linestyle="dashed", alpha=0.5, linewidth=0.6)

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

    placement_ranges = calculate_placement_regions(
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