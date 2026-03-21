"""
constants.py
════════════
App-wide constants, colour palette, and config file helpers.
"""

import json
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────────
OUTPUTS_DIR = Path("outputs")
CONFIG_FILE = Path("config.json")

# ── Thresholds ────────────────────────────────────────────────────────────────
API_WARN_THRESHOLD    = 100
SPLICE_THRESHOLD_RR   = 42
BIG_LOSS_THRESHOLD_RR = 30
RR_DESYNC_THRESHOLD   = 10

# ── Colour palette (Valorant dark theme) ─────────────────────────────────────
RED  = "#FF4655"
DARK = "#1a1a2e"
MID  = "#16213e"
CARD = "#0f3460"
TEXT = "#e0e0e0"
DIM  = "#8899aa"


# ── Act label helper ──────────────────────────────────────────────────────────

def act_label(ep: int, act: int) -> str:
    """Human-readable act label, e.g. 'Ep 9 · Act 2' or 'V25 · Act 1'."""
    season = f"Ep {ep}" if ep <= 9 else f"V{ep + 15}"
    return f"{season} · Act {act}"


# ── Config persistence ────────────────────────────────────────────────────────

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