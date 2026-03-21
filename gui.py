"""
gui.py
══════
Tkinter App class — the entire GUI layer.
Import and instantiate ``App`` to launch the application.
"""

import json
import threading
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import tkinter as tk
from tkinter import ttk, messagebox

from constants import (
    OUTPUTS_DIR, API_WARN_THRESHOLD,
    RED, DARK, MID, CARD, TEXT, DIM,
    act_label, load_config, save_config,
)
from db_valorant import ValorantDB
from utils import open_folder
from engine import AnalysisEngine


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Valorant Rank Analyzer")
        self.resizable(True, True)
        self.minsize(900, 640)

        self._config = load_config()
        self._apply_theme()

        # Cache: (name, tag, region) → list of (ep, act, label, games_played|None)
        self._lookup_cache: Dict[Tuple[str, str, str], List] = {}
        self._lookup_running = False

        self._build_ui()
        self._load_player_history()
        self._running = False
        self._acts_status_var.set("🔍  Look up a player to see their acts.")

    # ── Theme ──────────────────────────────────────────────────────────────────

    def _apply_theme(self):
        self.configure(bg=DARK)
        style = ttk.Style(self)
        style.theme_use("clam")

        style.configure(".", background=DARK, foreground=TEXT, fieldbackground=MID,
                        troughcolor=MID, selectbackground=CARD, selectforeground=TEXT,
                        font=("Segoe UI", 10))

        style.configure("TFrame",            background=DARK)
        style.configure("Card.TFrame",       background=MID,  relief="flat")
        style.configure("TLabel",            background=DARK, foreground=TEXT)
        style.configure("Card.TLabel",       background=MID,  foreground=TEXT)
        style.configure("Dim.TLabel",        background=DARK, foreground=DIM,  font=("Segoe UI", 9))
        style.configure("Dim.Card.TLabel",   background=MID,  foreground=DIM,  font=("Segoe UI", 9))
        style.configure("Head.TLabel",       background=DARK, foreground=RED,
                        font=("Segoe UI", 13, "bold"))
        style.configure("Title.TLabel",      background=DARK, foreground=TEXT,
                        font=("Segoe UI", 11, "bold"))
        style.configure("Warn.TLabel",       background=DARK, foreground="#FFAA33",
                        font=("Segoe UI", 9))

        style.configure("Accent.TButton", background=RED, foreground="white",
                        font=("Segoe UI", 11, "bold"), relief="flat", borderwidth=0, padding=10)
        style.map("Accent.TButton",
                  background=[("active", "#cc2233"), ("disabled", "#555")],
                  foreground=[("disabled", "#888")])

        style.configure("TEntry", fieldbackground=CARD, foreground=TEXT,
                        insertcolor=TEXT, borderwidth=0, relief="flat", padding=6,
                        selectbackground=RED, selectforeground="white")
        style.map("TEntry",
                  selectbackground=[("focus", RED), ("!focus", RED)],
                  selectforeground=[("focus", "white"), ("!focus", "white")])
        style.configure("TSpinbox", fieldbackground=CARD, foreground=TEXT,
                        borderwidth=0, relief="flat", arrowcolor=DIM,
                        selectbackground=RED, selectforeground="white")
        style.configure("TCombobox", fieldbackground=CARD, foreground=TEXT,
                        background=CARD, selectbackground=CARD, selectforeground=TEXT,
                        borderwidth=0, relief="flat", arrowcolor=DIM, padding=4)
        style.map("TCombobox",
                  fieldbackground=[("readonly", CARD), ("disabled", MID)],
                  foreground=[("readonly", TEXT), ("disabled", DIM)],
                  selectbackground=[("readonly", CARD)],
                  selectforeground=[("readonly", TEXT)])
        self.option_add("*TCombobox*Listbox*Background",       CARD)
        self.option_add("*TCombobox*Listbox*Foreground",       TEXT)
        self.option_add("*TCombobox*Listbox*SelectBackground", RED)
        self.option_add("*TCombobox*Listbox*SelectForeground", "white")
        style.configure("TCheckbutton", background=DARK, foreground=TEXT, selectcolor=CARD)
        style.configure("Separator.TFrame", background="#333")

    # ── UI construction ────────────────────────────────────────────────────────

    def _build_ui(self):
        root = ttk.Frame(self, padding=0)
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=0, minsize=240)
        root.columnconfigure(1, weight=1)
        root.rowconfigure(0, weight=1)

        # ── Left sidebar ──────────────────────────────────────────────────────
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

        ttk.Button(sidebar, text="📂  Open Player Folder",
                   command=self._open_selected_player_folder,
                   padding=(8, 6)).grid(row=3, column=0, sticky="ew", padx=10, pady=(0, 12))

        # ── Right panel ───────────────────────────────────────────────────────
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
        region_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_region_change())

        ttk.Label(player_card, text="Henrik API Key", style="Card.TLabel",
                  background=MID).grid(row=3, column=0, sticky="w", padx=(0, 8), pady=(10, 0))
        self.api_key_var = tk.StringVar(value=self._config.get("api_key", ""))
        api_key_entry = ttk.Entry(player_card, textvariable=self.api_key_var,
                                  show="•", width=36)
        api_key_entry.grid(row=3, column=1, columnspan=3, sticky="ew", pady=(10, 0))
        api_key_entry.bind("<FocusIn>",  lambda _e: api_key_entry.config(show=""))
        api_key_entry.bind("<FocusOut>", lambda _e: api_key_entry.config(show="•"))
        ttk.Label(player_card, text="Required. Saved automatically.",
                  style="Dim.Card.TLabel", background=MID).grid(
                      row=3, column=4, sticky="w", padx=(10, 0), pady=(10, 0))
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

        self._acts_cb_frame = tk.Frame(acts_outer, bg=MID)
        self._acts_cb_frame.grid(row=2, column=0, sticky="ew")
        self.act_vars: Dict[Tuple[str, str], tk.BooleanVar] = {}

        self._acts_status_var = tk.StringVar(value="")
        ttk.Label(acts_outer, textvariable=self._acts_status_var,
                  style="Dim.Card.TLabel", background=MID).grid(row=3, column=0, sticky="w")

        # ── Options card ──────────────────────────────────────────────────────
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

        # ── Log ───────────────────────────────────────────────────────────────
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

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _on_match_count_change(self, *_):
        try:
            v = self.match_count_var.get()
        except Exception:
            return
        if v > API_WARN_THRESHOLD:
            self.warn_label.config(
                text="⚠️  High count — each match makes API calls and can be slow.")
        else:
            self.warn_label.config(text="")

    def _log(self, msg: str):
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

    # ── Player history ─────────────────────────────────────────────────────────

    def _load_player_history(self):
        self.player_list.delete(0, "end")
        self._history_entries: List[Dict] = []
        if not OUTPUTS_DIR.exists():
            return
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
        meta = self._history_entries[sel[0]]
        self.name_var.set(meta.get("player_name", ""))
        self.tag_var.set(meta.get("player_tag", ""))
        self._trigger_player_lookup()

    # ── Config persistence ─────────────────────────────────────────────────────

    def _save_api_key(self):
        self._config["api_key"] = self.api_key_var.get()
        save_config(self._config)

    def _on_region_change(self):
        self._config["region"] = self.region_var.get()
        save_config(self._config)
        self._lookup_cache.clear()

    # ── Acts population ────────────────────────────────────────────────────────

    def _populate_acts_checkboxes(
        self,
        acts_data: List[Tuple[str, str, str, Optional[int]]],
        source: str = "",
        preserve_selection: bool = False,
    ):
        if not acts_data:
            self._acts_status_var.set("⚠️  No acts found.")
            return

        old_checked = {k for k, v in self.act_vars.items() if v.get()} if preserve_selection else set()

        for w in self._acts_cb_frame.winfo_children():
            w.destroy()
        self.act_vars.clear()

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
            key     = (ep, act)
            checked = (key in old_checked) if preserve_selection else (key in default_checked)
            var = tk.BooleanVar(value=checked)
            self.act_vars[key] = var
            cb = tk.Checkbutton(
                self._acts_cb_frame, text=f"{label}  ({games_played})",
                variable=var,
                bg=MID, fg=TEXT, selectcolor=CARD,
                activebackground=MID, activeforeground=TEXT,
                font=("Segoe UI", 9), relief="flat", borderwidth=0,
                highlightthickness=0, cursor="hand2",
            )
            cb.grid(row=row, column=col, sticky="w", padx=(0, 12), pady=2)

        suffix = f"  ·  {source}" if source else ""
        self._acts_status_var.set(f"✅  {len(acts_data)} act(s){suffix}")

    # ── Player lookup ──────────────────────────────────────────────────────────

    def _trigger_player_lookup(self):
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
        def _done(acts_data, status_msg):
            def _ui():
                self._lookup_running = False
                self.lookup_btn.config(state="normal")
                self._populate_acts_checkboxes(acts_data, source=status_msg)
            self.after(0, _ui)

        try:
            db       = ValorantDB(region=region)
            seasonal = db.get_profile_by_name(name, tag).seasonal_performances or []

            gp_by_act: dict = defaultdict(int)
            for p in seasonal:
                gp = getattr(p, "games_played", None) or 0
                if gp > 0:
                    gp_by_act[(str(p.episode), str(p.act))] += gp

            acts_data = sorted(
                [
                    (ep, act, act_label(int(ep), int(act)), total_gp)
                    for (ep, act), total_gp in gp_by_act.items()
                ],
                key=lambda x: (int(x[0]), int(x[1])),
            )

            if not acts_data:
                raise ValueError("no seasonal data")

            self._lookup_cache[cache_key] = acts_data
            _done(acts_data, f"{name}#{tag}")

        except Exception:
            def _err_ui():
                self._lookup_running = False
                self.lookup_btn.config(state="normal")
                self._acts_status_var.set(
                    f"⚠️  No data found for {name}#{tag}. Try generating first.")
            self.after(0, _err_ui)

    def _open_selected_player_folder(self):
        sel = self.player_list.curselection()
        if not sel:
            messagebox.showinfo("No Selection", "Select a player from the list first.")
            return
        meta   = self._history_entries[sel[0]]
        folder = Path(meta["_run_dir"])
        if folder.exists():
            open_folder(folder)
        else:
            messagebox.showerror("Not Found", f"Folder no longer exists:\n{folder}")

    # ── Analysis trigger ───────────────────────────────────────────────────────

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

        if match_count > API_WARN_THRESHOLD:
            ok = messagebox.askyesno(
                "API Rate Warning",
                f"You've requested {match_count} matches.\n\n"
                "Each match may require multiple API calls. This can be slow and "
                "risks hitting the Henrik API rate limit.\n\nContinue anyway?",
            )
            if not ok:
                return

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
                        f"Analysis for {name}#{tag} is done!\n\nOpen the output folder?",
                    ):
                        open_folder(out_dir)

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

        threading.Thread(target=_run, daemon=True).start()