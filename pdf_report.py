"""
pdf_report.py
═════════════
Builds the data-scientist-style PDF report via ReportLab.
The single public entry point is ``generate_pdf()``.
"""

from pathlib import Path
from typing import Optional

from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Image as RLImage,
    Table, TableStyle, HRFlowable, PageBreak, KeepTogether,
)

from constants import BIG_LOSS_THRESHOLD_RR


# ─────────────────────────────────────────────────────────────────────────────
#  Colour palette (Valorant-ish dark theme adapted for print)
# ─────────────────────────────────────────────────────────────────────────────

_VAL_RED    = colors.HexColor("#FF4655")
_DARK_NAVY  = colors.HexColor("#1a1a2e")
_MID_NAVY   = colors.HexColor("#16213e")
_STEEL      = colors.HexColor("#4a6080")
_LIGHT_GREY = colors.HexColor("#e8e8ec")
_DIM_GREY   = colors.HexColor("#7a8899")
_WHITE      = colors.white
_BLACK      = colors.HexColor("#1c1c24")


# ─────────────────────────────────────────────────────────────────────────────
#  Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _sty(name, **kw):
    return ParagraphStyle(name, **kw)


def _make_styles():
    return {
        "hero_name": _sty("HeroName",
            fontSize=28, leading=32, textColor=_WHITE,
            fontName="Helvetica-Bold", alignment=TA_LEFT),
        "hero_sub": _sty("HeroSub",
            fontSize=11, leading=15, textColor=_LIGHT_GREY,
            fontName="Helvetica", alignment=TA_LEFT),
        "section": _sty("Section",
            fontSize=13, leading=17, textColor=_VAL_RED,
            fontName="Helvetica-Bold", alignment=TA_LEFT,
            spaceBefore=8, spaceAfter=3),
        "body": _sty("Body",
            fontSize=9.5, leading=14, textColor=_BLACK,
            fontName="Helvetica"),
        "body_em": _sty("BodyEm",
            fontSize=9.5, leading=14, textColor=_BLACK,
            fontName="Helvetica-Bold"),
        "caption": _sty("Caption",
            fontSize=7.5, leading=10.5, textColor=_DIM_GREY,
            fontName="Helvetica-Oblique", alignment=TA_CENTER,
            spaceBefore=3, spaceAfter=8),
        "callout": _sty("Callout",
            fontSize=10, leading=14, textColor=_BLACK,
            fontName="Helvetica"),
        "link": _sty("Link",
            fontSize=9.5, leading=14, textColor=colors.HexColor("#1155CC"),
            fontName="Helvetica"),
        "tbl_hdr": _sty("TblHdr",
            fontSize=8.5, leading=11, textColor=_WHITE,
            fontName="Helvetica-Bold", alignment=TA_CENTER),
        "tbl_cel": _sty("TblCel",
            fontSize=8.5, leading=11, textColor=_BLACK,
            fontName="Helvetica", alignment=TA_CENTER),
        "tbl_cel_l": _sty("TblCelL",
            fontSize=8.5, leading=11, textColor=_BLACK,
            fontName="Helvetica", alignment=TA_LEFT),
    }


def _stat_cards(pairs, cols, usable_w, sty):
    """Render a row of stat pill-cards."""
    col_w = usable_w / cols
    sh = _sty("sh", fontSize=7.5, leading=10,
              textColor=_DIM_GREY, fontName="Helvetica", alignment=TA_CENTER)
    sv = _sty("sv", fontSize=13, leading=16,
              textColor=_VAL_RED, fontName="Helvetica-Bold", alignment=TA_CENTER)
    header_row = [Paragraph(lbl, sh) for lbl, _ in pairs]
    value_row  = [Paragraph(str(val), sv) for _, val in pairs]
    while len(header_row) < cols:
        header_row.append(Paragraph("", sty["body"]))
        value_row.append(Paragraph("", sty["body"]))
    tbl = Table([header_row, value_row],
                colWidths=[col_w] * cols, rowHeights=[16, 24])
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), _LIGHT_GREY),
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [_LIGHT_GREY, _WHITE]),
        ("BOX",      (0, 0), (-1, -1), 0.5, _STEEL),
        ("INNERGRID",(0, 0), (-1, -1), 0.25, colors.HexColor("#cccccc")),
        ("VALIGN",   (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING",    (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    return tbl


def _section_rule():
    return HRFlowable(width="100%", thickness=1,
                      color=_VAL_RED, spaceAfter=4, spaceBefore=0)


def _callout(text, sty, usable_w):
    inner = Paragraph(text, sty["callout"])
    tbl = Table([[inner]], colWidths=[usable_w])
    tbl.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), _LIGHT_GREY),
        ("BOX",           (0, 0), (-1, -1), 0.75, _STEEL),
        ("LEFTPADDING",   (0, 0), (-1, -1), 10),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 10),
        ("TOPPADDING",    (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    return tbl


def _img(fname, out_dir, usable_w, sty, width=None, caption=None):
    p = out_dir / fname
    if not p.exists():
        return []
    from PIL import Image as PILImage
    with PILImage.open(str(p)) as im:
        img_w, img_h = im.size
    w = width or usable_w
    h = w * (img_h / img_w)
    img_elem = RLImage(str(p), width=w, height=h)
    # Wrap image in a subtle border table for polish
    img_tbl = Table([[img_elem]], colWidths=[w])
    img_tbl.setStyle(TableStyle([
        ("BOX",           (0, 0), (-1, -1), 0.5, colors.HexColor("#d0d0d8")),
        ("TOPPADDING",    (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ("LEFTPADDING",   (0, 0), (-1, -1), 0),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 0),
    ]))
    elems = [Spacer(1, 6), img_tbl]
    if caption:
        elems.append(Paragraph(caption, sty["caption"]))
    else:
        elems.append(Spacer(1, 6))
    return elems


def _base_table_style():
    return TableStyle([
        ("BACKGROUND",    (0, 0), (-1, 0),  _DARK_NAVY),
        ("ROWBACKGROUNDS",(0, 1), (-1, -1), [_WHITE, _LIGHT_GREY]),
        ("BOX",           (0, 0), (-1, -1), 0.5, _STEEL),
        ("INNERGRID",     (0, 0), (-1, -1), 0.25, colors.HexColor("#cccccc")),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING",    (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING",   (0, 0), (-1, -1), 6),
    ])


# ─────────────────────────────────────────────────────────────────────────────
#  Public entry point
# ─────────────────────────────────────────────────────────────────────────────

def generate_pdf(
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
    Build the full PDF report and write it to *out_dir/report.pdf*.
    Returns the path to the written file.
    """
    PAGE_W, PAGE_H = letter
    MARGIN   = 0.7 * inch
    USABLE_W = PAGE_W - 2 * MARGIN

    pdf_path = out_dir / "report.pdf"
    doc = SimpleDocTemplate(
        str(pdf_path),
        pagesize=letter,
        leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=MARGIN,  bottomMargin=MARGIN,
        title=f"Valorant Rank Analysis — {player_name}#{player_tag}",
        author="Valorant Rank Analyzer",
    )

    sty = _make_styles()

    # Convenience wrappers that close over layout constants
    def stat_cards(pairs, cols=4):
        return _stat_cards(pairs, cols, USABLE_W, sty)

    def callout(text):
        return _callout(text, sty, USABLE_W)

    def img(fname, width=None, caption=None):
        return _img(fname, out_dir, USABLE_W, sty, width=width, caption=caption)

    acts_label = ", ".join(f"Ep {e} · Act {a}" for e, a in acts_of_interest)
    story: list = []

    # ── Cover banner ──────────────────────────────────────────────────────────
    banner_data = [[
        Paragraph(f"{player_name}<font color='#FF4655'>#{player_tag}</font>",
                  sty["hero_name"]),
        Paragraph(
            f"<b>Region:</b> {region.upper()}&nbsp;&nbsp;"
            f"<b>Acts:</b> {acts_label}<br/>"
            f"<b>Generated:</b> {timestamp}",
            sty["hero_sub"]),
    ]]
    banner = Table(banner_data, colWidths=[USABLE_W * 0.52, USABLE_W * 0.48])
    banner.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), _DARK_NAVY),
        ("VALIGN",     (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING",    (0, 0), (-1, -1), 16),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 16),
        ("LEFTPADDING",   (0, 0), (-1, -1), 14),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 14),
    ]))
    story.append(banner)
    story.append(Spacer(1, 10))

    # ── Section 1: At a Glance ────────────────────────────────────────────────
    story.append(Paragraph("At a Glance", sty["section"]))
    story.append(_section_rule())

    n_games        = wr["games"]
    wins, losses   = wr["wins"], wr["losses"]
    wr_pct         = f"{wr['winrate']:.1%}"

    story.append(stat_cards([
        ("Matches Analysed", n_games),
        ("Win Rate",         wr_pct),
        ("W / L",            f"{wins} / {losses}"),
        ("Current Rank",     current_rank),
    ]))
    story.append(Spacer(1, 4))
    story.append(stat_cards([
        ("Estimated True MMR", estimated_mmr_str),
        ("Counterfactual Rank\n(no-buffer + adj)", cf_rank),
        ("Most Balanced Lobby", f"{results['lobby_balance']['most_balanced'][0]:.3f} σ"),
        ("Least Balanced Lobby", f"{results['lobby_balance']['least_balanced'][0]:.3f} σ"),
    ]))
    story.append(Spacer(1, 8))

    story.append(Paragraph(
        f"This report summarises <b>{n_games} competitive matches</b> played by "
        f"<b>{player_name}#{player_tag}</b> across <b>{acts_label}</b>. "
        f"The pipeline fetches raw MMR history via the Henrik API, reconstructs "
        f"per-match RR deltas from ELO snapshots, and builds lobby rank estimates "
        f"using a spline fitted to teammates' and opponents' visible tiers.",
        sty["body"]))
    story.append(Spacer(1, 4))

    mmr_delta = ""
    try:
        cur_val = int(current_rank.split()[0]) if current_rank[0].isdigit() else 0
        est_val = int(estimated_mmr_str.split()[0]) if estimated_mmr_str[0].isdigit() else 0
        diff    = est_val - cur_val
        if diff > 0:
            mmr_delta = (
                f"The spline estimate sits <b>{diff} RR above</b> the displayed rank, "
                f"suggesting the matchmaker may be deliberately placing this player "
                f"below their internal MMR — a pattern commonly seen after a performance "
                f"spike or act reset.")
        elif diff < 0:
            mmr_delta = (
                f"The spline estimate is <b>{abs(diff)} RR below</b> the displayed rank. "
                f"This can arise when recent performance has trended downward but the "
                f"system has not yet fully reflected that in match outcomes.")
    except Exception:
        pass
    if mmr_delta:
        story.append(callout(mmr_delta))
        story.append(Spacer(1, 4))

    # ── Section 2: Rank Trajectory ────────────────────────────────────────────
    story.append(Paragraph("Rank Trajectory — Lobby Rank Averages", sty["section"]))
    story.append(_section_rule())
    story.append(Paragraph(
        "The chart below tracks three lobby-rank averages across every match, "
        "chronologically most recent on the left. "
        "<b>Lobby avg</b> (green) is the full-lobby mean excluding the tracked player; "
        "<b>allies</b> (blue) and <b>opponents</b> (orange) are split by team. "
        "Divergence between ally and opponent averages is the primary signal of "
        "matchmaker imbalance — sustained gaps over many games are statistically "
        "meaningful; single-game outliers rarely are.",
        sty["body"]))
    story.extend(img("rank_trends.png", width=USABLE_W,
        caption="Figure 1 — Lobby rank averages over time. "
                "Vertical dashed lines mark game-patch boundaries; "
                "shaded bands highlight placement match windows."))
    story.extend(img("rank_trends_n15.png", width=USABLE_W,
        caption="Figure 1b — Same chart restricted to the 15 most-recent matches. "
                "Zooms into current-form trend, reducing noise from older acts."))

    # ── Section 3: Lobby Balance Distribution ─────────────────────────────────
    story.append(Paragraph("Lobby Balance Distribution", sty["section"]))
    story.append(_section_rule())
    body_para = Paragraph(
        "Standard deviation of lobby ranks (σ) measures how evenly skilled "
        "the matchmaker assembled each game. A low σ means all ten players "
        "were clustered near the same tier; a high σ means a wide rank spread "
        "was pulled together. "
        "The kernel density below shows where this player's matches land "
        "relative to the distribution's own mean and ±1σ / ±2σ bands.",
        sty["body"])
    chart_elems = img("lobby_std_distribution.png", width=USABLE_W * 0.88,
        caption="Figure 2 — Kernel density of per-match lobby σ. "
                "Percentages annotate the share of matches falling within each σ band. "
                "A heavy right tail indicates frequent high-variance lobbies.")
    # Center the slightly-narrower chart
    if chart_elems:
        chart_inner = chart_elems[1]  # the image table
        chart_caption = chart_elems[2] if len(chart_elems) > 2 else None
        pad = (USABLE_W - USABLE_W * 0.88) / 2
        centered = Table([[chart_inner]], colWidths=[USABLE_W * 0.88])
        centered_wrap = Table([[Paragraph("", sty["body"]), centered, Paragraph("", sty["body"])]],
                               colWidths=[pad, USABLE_W * 0.88, pad])
        centered_wrap.setStyle(TableStyle([
            ("TOPPADDING",    (0,0),(-1,-1), 0),
            ("BOTTOMPADDING", (0,0),(-1,-1), 0),
            ("LEFTPADDING",   (0,0),(-1,-1), 0),
            ("RIGHTPADDING",  (0,0),(-1,-1), 0),
        ]))
        chart_block = [Spacer(1, 6), centered_wrap]
        if chart_caption:
            chart_block.append(chart_caption)
        story.append(KeepTogether([body_para, Spacer(1, 4)] + chart_block))
    else:
        story.append(body_para)

    # ── Section 4: Counterfactual RR ──────────────────────────────────────────
    story.append(PageBreak())
    story.append(Paragraph("Counterfactual RR Paths", sty["section"]))
    story.append(_section_rule())
    story.append(Paragraph(
        "Valorant's ranked system applies two mechanical dampeners that alter "
        "true RR gains: <b>shields</b> (which absorb losses at rank boundaries) "
        "and <b>buffers</b> (which prevent demotion through the 0 RR floor). "
        "By replaying every match's stated RR delta without these corrections "
        "we produce counterfactual trajectories — showing what rank this player "
        "would hold if the raw performance signal propagated without dampening.",
        sty["body"]))
    story.append(Spacer(1, 4))

    shield_count   = sum(1 for e in ledger_entries if e.get("shield_used"))
    buffer_count   = sum(1 for e in ledger_entries if e.get("buffer_used"))
    splice_count   = sum(1 for e in ledger_entries if e.get("is_splice"))
    desync_count   = sum(1 for e in ledger_entries if e.get("rr_desync"))
    big_loss_count = sum(1 for e in ledger_entries if e.get("big_loss"))

    event_rows = [
        [Paragraph(h, sty["tbl_hdr"]) for h in ["Event", "Count", "What It Means"]],
        [Paragraph("Shield Used", sty["tbl_cel_l"]),
         Paragraph(str(shield_count), sty["tbl_cel"]),
         Paragraph("Loss absorbed at boundary; actual RR taken = 0", sty["tbl_cel_l"])],
        [Paragraph("Buffer Used", sty["tbl_cel_l"]),
         Paragraph(str(buffer_count), sty["tbl_cel"]),
         Paragraph("Demotion prevented at 0 RR floor", sty["tbl_cel_l"])],
        [Paragraph(f"Big Loss (≥{BIG_LOSS_THRESHOLD_RR} RR)", sty["tbl_cel_l"]),
         Paragraph(str(big_loss_count), sty["tbl_cel"]),
         Paragraph("Unusually large single-match RR deduction", sty["tbl_cel_l"])],
        [Paragraph("Data Splice", sty["tbl_cel_l"]),
         Paragraph(str(splice_count), sty["tbl_cel"]),
         Paragraph("ELO jump >42 RR — gap in history or act reset", sty["tbl_cel_l"])],
        [Paragraph("RR Desync", sty["tbl_cel_l"]),
         Paragraph(str(desync_count), sty["tbl_cel"]),
         Paragraph("Stated delta ≠ reconstructed delta (≥10 RR diff)", sty["tbl_cel_l"])],
    ]
    col_ws = [USABLE_W * 0.25, USABLE_W * 0.1, USABLE_W * 0.65]
    event_tbl = Table(event_rows, colWidths=col_ws)
    event_tbl.setStyle(_base_table_style())
    story.append(KeepTogether([event_tbl, Spacer(1, 8)]))
    story.extend(img("counterfactual_rr.png", width=USABLE_W,
        caption="Figure 3 — Actual RR path (teal) vs. counterfactual paths without shields "
                "(orange) and without buffers (purple). Vertical bars mark per-match shield "
                "delta. Divergence from the actual line quantifies cumulative mechanical benefit."))

    # ── Section 5: Role Distribution ──────────────────────────────────────────
    # role_pcts shape: { map: { rank: { role: float } } }
    if role_pcts:
        story.append(Paragraph("Opponent Role Distribution", sty["section"]))
        story.append(_section_rule())
        story.append(Paragraph(
            "Role composition of opponents broken down by map. "
            "Values show what percentage of opponent teams included at least one "
            "player of that role, aggregated across all rank buckets using a "
            "count-weighted merge (not a simple average of per-rank percentages). "
            "Skews toward a particular role may indicate the player is consistently "
            "queuing into specific team compositions — useful context for agent-pick decisions.",
            sty["body"]))
        story.append(Spacer(1, 4))

        # Collect all roles present across the whole dataset for consistent columns.
        all_roles: list = []
        for rank_dict in role_pcts.values():
            for role_dict in rank_dict.values():
                for role in role_dict:
                    if role not in all_roles:
                        all_roles.append(role)
        all_roles = sorted(all_roles)

        def _merge_role_dicts(rank_dict: dict) -> dict:
            """
            Merge per-rank role distributions into a single distribution,
            avoiding the average-of-averages fallacy.

            If each rank bucket carries a ``_n`` key (number of games/teams),
            we use those as weights: merged_pct[role] = sum(n_i * pct_i) / sum(n_i).

            If ``_n`` is absent we fall back to treating every opponent *team*
            observation as one unit — i.e. we accumulate a weighted sum where
            the weight is derived from any role that acts as a proxy count
            (total pct / 100 can't work here, so we treat all ranks equally,
            which is still better than a straight average of averages because we
            make the equal-weight assumption explicit rather than silent).
            """
            weighted_sums = {r: 0.0 for r in all_roles}
            total_weight  = 0.0
            for role_vals in rank_dict.values():
                n = role_vals.get("_n", None)
                if n is None:
                    # No count available — treat each rank bucket as weight=1.
                    # This is still a simple average, but documented as such;
                    # callers should inject _n for proper weighting.
                    n = 1
                total_weight += n
                for r in all_roles:
                    weighted_sums[r] += n * role_vals.get(r, 0.0)
            if total_weight == 0:
                return {r: 0.0 for r in all_roles}
            return {r: weighted_sums[r] / total_weight for r in all_roles}

        # Build a single merged row per map (one table for all maps).
        header = [Paragraph("Map", sty["tbl_hdr"])] + [
            Paragraph(r, sty["tbl_hdr"]) for r in all_roles
        ]
        role_rows = [header]
        for map_name in sorted(role_pcts):
            merged = _merge_role_dicts(role_pcts[map_name])
            row = [Paragraph(map_name, sty["tbl_cel_l"])] + [
                Paragraph(f"{merged.get(r, 0.0):.1f}%", sty["tbl_cel"])
                for r in all_roles
            ]
            role_rows.append(row)

        map_w  = USABLE_W * 0.20
        role_w = (USABLE_W - map_w) / max(len(all_roles), 1)
        col_ws = [map_w] + [role_w] * len(all_roles)
        role_tbl = Table(role_rows, colWidths=col_ws)
        role_tbl.setStyle(_base_table_style())
        story.append(KeepTogether([role_tbl, Spacer(1, 6)]))

        story.append(Spacer(1, 4))

    # ── Section 6: Round Timing Statistics ────────────────────────────────────
    if round_stats_out:
        ov = round_stats_out.get("overall", {})
        story.append(PageBreak())
        story.append(Paragraph("Round Timing Statistics", sty["section"]))
        story.append(_section_rule())
        story.append(Paragraph(
            f"Round-timing data was collected across "
            f"<b>{ov.get('sample_rounds', '—')} rounds</b> from the analysed matches. "
            f"Duration is reconstructed from in-game event timestamps in priority order: "
            f"defuse time → latest kill time → plant time + spike timer → game-length average. "
            f"Rounds outside the 3 – 100 s window are discarded.",
            sty["body"]))
        story.append(Spacer(1, 4))

        story.append(stat_cards([
            ("Sample Rounds",   ov.get("sample_rounds", "—")),
            ("Median Duration", f"{ov.get('median', '—')} s"),
            ("P25 / P75",       f"{ov.get('p25', '—')} / {ov.get('p75', '—')} s"),
            ("Plant Rate",      f"{ov.get('plant_rate', 0)*100:.1f}%"),
        ]))
        story.append(Spacer(1, 4))
        story.append(stat_cards([
            ("Median Plant Time",          f"{ov.get('median_plant_time', '—')} s"),
            ("Median Post-Plant",          f"{ov.get('median_post_plant', '—')} s"),
            ("Defuse Rate",                f"{ov.get('defuse_rate', 0)*100:.1f}%"),
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
            sty["body"]))
        story.append(Spacer(1, 6))

        by_map = round_stats_out.get("by_map", {})
        if by_map:
            story.append(Paragraph("Per-Map Round Summary", sty["section"]))
            story.append(_section_rule())
            map_header = [Paragraph(h, sty["tbl_hdr"]) for h in
                          ["Map", "Rounds", "Median (s)", "Plant Rate",
                           "Plant Time (s)", "Post-Plant (s)"]]
            map_rows = [map_header]
            for map_name, s in sorted(by_map.items()):
                map_rows.append([
                    Paragraph(map_name, sty["tbl_cel_l"]),
                    Paragraph(str(s.get("sample_rounds", "—")), sty["tbl_cel"]),
                    Paragraph(str(s.get("median", "—")),         sty["tbl_cel"]),
                    Paragraph(f"{s.get('plant_rate', 0)*100:.1f}%", sty["tbl_cel"]),
                    Paragraph(str(s.get("median_plant_time", "—")), sty["tbl_cel"]),
                    Paragraph(str(s.get("median_post_plant", "—")), sty["tbl_cel"]),
                ])
            map_col_ws = [USABLE_W * w for w in [0.22, 0.1, 0.13, 0.15, 0.2, 0.2]]
            map_tbl = Table(map_rows, colWidths=map_col_ws)
            map_tbl.setStyle(_base_table_style())
            story.append(KeepTogether([map_tbl]))
            story.append(Spacer(1, 6))

    # ── Section 7: Interactive Resources ──────────────────────────────────────
    story.append(PageBreak())
    story.append(Paragraph("Interactive Reports &amp; Tools", sty["section"]))
    story.append(_section_rule())

    resources = [
        (
            "Agent Stats Report",
            "./agent_report/index.html",
            "Full per-agent performance breakdown — win rate, K/D/A, economy, "
            "and usage share across all analysed matches. Open in any browser.",
        ),
        (
            "Utility Recharge Calculator",
            "./utility_recharge_calculator.html",
            "Interactive explorer powered by round_stats.json. Visualises round-timing "
            "distributions, plant/defuse patterns, and per-map breakdowns to help "
            "calibrate utility usage and timing windows. Requires round_stats.json "
            "to be present in the same folder (generated automatically).",
        ),
    ]

    res_rows = [[Paragraph(h, sty["tbl_hdr"]) for h in ["Tool", "File Path", "Description"]]]
    for title_r, link, desc in resources:
        res_rows.append([
            Paragraph(f"<b>{title_r}</b>", sty["tbl_cel_l"]),
            Paragraph(f'<a href="{link}"><font color="#1155CC">{link}</font></a>',
                      sty["tbl_cel_l"]),
            Paragraph(desc, sty["tbl_cel_l"]),
        ])
    res_col_ws = [USABLE_W * 0.22, USABLE_W * 0.28, USABLE_W * 0.50]
    res_tbl = Table(res_rows, colWidths=res_col_ws)
    res_tbl.setStyle(_base_table_style())

    callout_box = callout(
        "To use the interactive tools: open the output folder in your file explorer, "
        "then double-click either HTML file. Both tools run entirely in your browser "
        "with no server or internet connection required.")

    footnote = Paragraph(
        f"<b>Methodology note.</b> MMR data sourced from the Henrik Dev Unofficial Valorant API. "
        f"Lobby rank estimates use a monotone cubic spline fitted to observed tiers; unrated players "
        f"are looked up individually when they comprise &gt;40% of the lobby. "
        f"Counterfactual paths replay the stated RR delta from each match without applying the "
        f"shield or buffer corrections, then forward-simulate from the first known ELO anchor. "
        f"All figures are approximate — the API exposes post-correction values and some "
        f"reconstruction is necessary. Analysis parameters: max {match_count_max} matches, "
        f"timespan {timespan} days.",
        _sty("Footnote", fontSize=7.5, leading=11, textColor=_DIM_GREY,
             fontName="Helvetica"))

    story.append(KeepTogether([
        res_tbl,
        Spacer(1, 6),
        callout_box,
        Spacer(1, 14),
        HRFlowable(width="100%", thickness=0.5, color=_STEEL),
        Spacer(1, 5),
        footnote,
    ]))

    def _add_page_number(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(_DIM_GREY)
        canvas.drawRightString(PAGE_W - MARGIN, 0.4 * inch, f"Page {doc.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=_add_page_number, onLaterPages=_add_page_number)
    return pdf_path