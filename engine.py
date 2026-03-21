"""
engine.py
═════════
AnalysisEngine — orchestrates the full analysis pipeline and saves all
outputs to a per-run directory under OUTPUTS_DIR.
"""

import json
import shutil
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from constants import OUTPUTS_DIR
from api_henrik import UnofficialApi
from db_valorant import ValorantDB
from match_history_processor import MatchHistoryProcessor
from match_analysis import calculate_winrate, analyze_match_history
from agent_stats import calculate_role_percentages, generate_html_report
from collect_round_stats import extract_round_record, compute_stats, TIER_NORMALISE, parse_patch
from rank_utils import map_rank_value
from mmr_spline import predict_mmr

from lobby_ranks import gather_rank_average_lists
from ledger import (
    build_match_ledger,
    compute_counterfactual_path,
    compute_counterfactual_nobuffer_path,
)
from plots import (
    plot_rank_trends,
    plot_lobby_std_distribution,
    plot_counterfactual,
    plot_actual_rr,
)
from pdf_report import generate_pdf


class AnalysisEngine:
    """Runs all analysis tasks and saves outputs to disk."""

    def __init__(self, log: Callable[[str], None]):
        self.log = log

    def run(
        self,
        player_name: str,
        player_tag: str,
        acts_of_interest: List[Tuple[str, str]],
        match_count_max: int,
        timespan: int = 30,
        region: str = "na",
        api_key: str = "",
    ) -> Path:
        """
        Run the full analysis pipeline and return the output directory path.
        Raises exceptions on failure — the GUI catches them.
        """
        # ── Output directory ─────────────────────────────────────────────────
        safe_name   = f"{player_name}_{player_tag}".replace(" ", "_").replace("#", "_")
        acts_slug   = "+".join(f"E{ep}A{act}" for ep, act in acts_of_interest)
        date_str    = datetime.now().strftime("%m%d")
        folder_name = f"{safe_name}_{acts_slug}_{match_count_max}m_{date_str}"
        out_dir     = OUTPUTS_DIR / folder_name
        if out_dir.exists():
            n = 2
            while (OUTPUTS_DIR / f"{folder_name}_{n}").exists():
                n += 1
            out_dir = OUTPUTS_DIR / f"{folder_name}_{n}"
        out_dir.mkdir(parents=True, exist_ok=True)

        # ── 1. API + DB setup ─────────────────────────────────────────────────
        self.log("🔌  Connecting to API and database...")
        api = UnofficialApi(api_key=api_key) if api_key else UnofficialApi()
        db  = ValorantDB(region=region)

        self.log(f"🔍  Looking up {player_name}#{player_tag}...")
        account  = api.get_account_by_name(player_name, player_tag)
        my_puuid = account.puuid
        self.log(f"✅  Found player — PUUID: {my_puuid[:12]}...")

        # ── 2. Match history ──────────────────────────────────────────────────
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
        recent_mmr            = history.recent_mmr
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

        # ── 3. Rank averages + MMR prediction ─────────────────────────────────
        self.log("📊  Computing lobby rank averages (spline method)...")
        rank_averages = gather_rank_average_lists(
            recent_matches, my_puuid, db, acts_of_interest
        )
        predicted_matches_mmr = {
            recent_matches[idx].metadata.match_id: predict_mmr(rank_averages, idx)
            for idx in range(len(recent_matches))
        }
        self.log(f"🎯  Predicted MMR at latest match: {map_rank_value(predict_mmr(rank_averages, 0))}")

        # ── 4. Win rate ───────────────────────────────────────────────────────
        wr = calculate_winrate(recent_matches, my_puuid)
        self.log(f"🏆  Win rate: {wr['wins']}W / {wr['losses']}L "
                 f"over {wr['games']} games — {wr['winrate']:.0%}")

        # ── 5. Plot: Rank Trends ──────────────────────────────────────────────
        self.log("🎨  Generating rank trends chart...")
        plot_rank_trends(rank_averages, recent_matches,
                         recent_is_placement, recent_is_newly_placed,
                         out_dir / "rank_trends.png")
        self.log("💾  Saved rank_trends.png")

        _N_RECENT = 15
        self.log(f"🎨  Generating rank trends chart (last {_N_RECENT} matches)...")
        plot_rank_trends(rank_averages, recent_matches,
                         recent_is_placement, recent_is_newly_placed,
                         out_dir / f"rank_trends_n{_N_RECENT}.png",
                         n=_N_RECENT)
        self.log(f"💾  Saved rank_trends_n{_N_RECENT}.png")

        # ── 6. Plot: Lobby Std Distribution ───────────────────────────────────
        self.log("🎨  Generating lobby balance distribution chart...")
        plot_lobby_std_distribution(rank_averages, out_dir / "lobby_std_distribution.png")
        self.log("💾  Saved lobby_std_distribution.png")

        # ── 7. Build ledger + counterfactual paths ────────────────────────────
        self.log("🔢  Building match ledger and counterfactual paths...")
        ledger = build_match_ledger(
            recent_mmr, recent_is_placement, recent_is_newly_placed,
            recent_previous_match, predicted_matches_mmr
        )
        ledger_entries = list(ledger.values())[1:]
        actual_elos    = [e["elo_after"] for e in ledger_entries]

        cf_elos        = compute_counterfactual_path(
            ledger_entries, recent_is_placement, predicted_matches_mmr, False)
        cf_adj_elos    = compute_counterfactual_path(
            ledger_entries, recent_is_placement, predicted_matches_mmr, True)
        cf_nb_elos     = compute_counterfactual_nobuffer_path(
            ledger_entries, recent_is_placement, predicted_matches_mmr, False)
        cf_nb_adj_elos = compute_counterfactual_nobuffer_path(
            ledger_entries, recent_is_placement, predicted_matches_mmr, True)

        # ── 8. Plot: Counterfactual RR ────────────────────────────────────────
        self.log("🎨  Generating counterfactual RR chart...")
        plot_counterfactual(
            ledger_entries, actual_elos,
            cf_elos, cf_adj_elos, cf_nb_elos, cf_nb_adj_elos,
            recent_is_placement, recent_is_newly_placed,
            recent_matches, out_dir / "counterfactual_rr.png"
        )
        self.log("💾  Saved counterfactual_rr.png")

        self.log("🎨  Generating actual RR chart (no counterfactuals)...")
        plot_actual_rr(
            ledger_entries, actual_elos,
            recent_is_placement, recent_is_newly_placed,
            recent_matches, out_dir / "actual_rr.png"
        )
        self.log("💾  Saved actual_rr.png")

        # ── 9. HTML Report ────────────────────────────────────────────────────
        self.log("🌐  Generating interactive HTML agent report...")
        html_dir = out_dir / "agent_report"
        html_dir.mkdir(exist_ok=True)
        generate_html_report(recent_matches, my_puuid,
                             output_file=str(html_dir / "index.html"))
        self.log("💾  Saved agent_report/index.html")

        # ── 10. Summary text ──────────────────────────────────────────────────
        self.log("📝  Writing analysis summary...")
        last_mmr_val      = recent_mmr[recent_matches[0].metadata.match_id]
        current_rank      = map_rank_value(last_mmr_val.elo / 100)
        estimated_mmr_str = map_rank_value(predict_mmr(rank_averages, 0))
        cf_rank           = map_rank_value(cf_nb_adj_elos[-1] / 100) if cf_nb_adj_elos else "N/A"
        role_pcts         = calculate_role_percentages(recent_matches, my_puuid)
        results           = analyze_match_history(rank_averages)
        timestamp         = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

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
        round_stats_out = self._collect_round_stats(
            recent_matches, recent_mmr, my_puuid,
            acts_of_interest, player_name, player_tag, region, out_dir
        )

        # ── 12. Copy utility_recharge_calculator.html ─────────────────────────
        util_src = Path(__file__).parent / "utility_recharge_calculator.html"
        if util_src.exists():
            shutil.copy2(util_src, out_dir / "utility_recharge_calculator.html")
            self.log("💾  Copied utility_recharge_calculator.html")
        else:
            self.log("⚠️  utility_recharge_calculator.html not found — skipped.")

        # ── 13. PDF Report ────────────────────────────────────────────────────
        self.log("📄  Generating PDF report...")
        try:
            generate_pdf(
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
            self.log("💾  Saved report.pdf")
        except Exception as exc:
            import traceback
            self.log(f"⚠️  PDF generation failed: {exc}\n{traceback.format_exc()}")

        # ── 14. Player metadata for GUI history ───────────────────────────────
        meta = {
            "player_name":    player_name,
            "player_tag":     player_tag,
            "puuid":          my_puuid,
            "timestamp":      timestamp,
            "matches":        len(recent_matches),
            "winrate":        f"{wr['winrate']:.0%}",
            "current_rank":   current_rank,
            "estimated_mmr":  estimated_mmr_str,
            "acts":           [f"Ep {e} Act {a}" for e, a in acts_of_interest],
        }
        with open(out_dir / "meta.json", "w") as f:
            json.dump(meta, f, indent=2)

        self.log(f"\n✨  All done! Output folder: {out_dir.resolve()}")
        return out_dir

    # ── Private helpers ───────────────────────────────────────────────────────

    def _collect_round_stats(self, recent_matches, recent_mmr, my_puuid,
                              acts_of_interest, player_name, player_tag,
                              region, out_dir) -> Optional[dict]:
        """Extract per-round records and write round_stats.json."""
        all_rounds = []
        skipped_rs = 0
        try:
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

            if not all_rounds:
                self.log("⚠️  No round data available — round_stats.json skipped.")
                return None

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
            return round_stats_out

        except Exception as exc:
            self.log(f"⚠️  round_stats.json skipped: {exc}")
            return None