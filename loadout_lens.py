"""
loadout_lens.py
===============
Data-analysis engine for VALORANT player in-game choices.

Accepts a list of ``Match`` objects (from api_henrik.py) and produces
structured breakdowns of loadout, economy, and utility decisions sliced
by map, agent, rank, and buy phase.

Data availability notes
-----------------------
The HenrikDev API exposes the following economy fields **per round**:

  - ``economy.weapon``        → weapon name + ID carried into the round
  - ``economy.armor``         → armor name + ID carried into the round
  - ``economy.loadout_value`` → total credit value of weapon + armor + abilities
  - ``economy.spent``         → credits spent this round (weapon + armor + any
                                ability charges bought — NOT broken out per item)
  - ``economy.remaining``     → credits left after buying

And the following **ability cast counts** per round (not credits):

  - ``ability_casts.c_casts`` → C-slot casts this round  (signature ability)
  - ``ability_casts.q_casts`` → Q-slot casts this round  (basic / purchasable)
  - ``ability_casts.e_casts`` → E-slot casts this round  (secondary ability)
  - ``ability_casts.x_casts`` → X-slot casts this round  (ultimate)

Important: per-round spend **cannot** be split into "gun spend" vs "util spend"
from the API data alone.  The ``SpendReport`` therefore exposes total round spend
and loadout value as separate concerns from cast rates.  Utility cost can only be
inferred indirectly — e.g. by comparing spend between rounds where weapon/armor
are the same but cast counts differ, or by noting that agents with expensive
purchasable abilities (like Skye's Q at 250 cr/charge) will show higher average
spend than agents with free abilities holding the same loadout.

Match-level totals (``Player.economy.spent.overall``, etc.) are kept alongside
round-level data so callers can compute per-match aggregates without fighting
with round-level filtering.

URL catalogue
-------------
:class:`HenrikUrlBuilder` catalogues every endpoint the app calls in one place.
Call ``HenrikUrlBuilder.url_catalogue()`` to get a JSON-serialisable dict of all
Henrik API + valorant-api.com URLs; embed it in ``report_data.js`` so the
frontend can display data-provenance information without hard-coded strings.

Compound-slice methods
----------------------
Each report class now exposes a four-level compound-slice hierarchy that mirrors
the filter axes available in the UI (act → rank → map → agent).  The JS page
will use ``meta_precompute``'s flat ``"act|rank|map|phase"`` key index for most
lookups, but these methods are available for Python-side drill-downs and for
seeding the per-agent detail panels:

  - ``by_act()``                          → act → distribution / CPR / spend
  - ``by_act_by_rank()``                  → act → rank → …
  - ``by_act_by_rank_by_map()``           → act → rank → map → …
  - ``by_act_by_rank_by_map_by_agent()``  → act → rank → map → agent → …

All four levels exist on :class:`WeaponReport`, :class:`ShieldReport`,
:class:`UtilityReport` (as ``cast_rate_by_act*``), and :class:`SpendReport`
(as ``average_by_act*``).

Typical usage
-------------
    from loadout_lens import MatchAnalyser, AnalysisFilters, AgentAbilityMap
    from loadout_lens import HenrikUrlBuilder

    # ── URL catalogue (embed in report_data.js) ──────────────────────────────
    catalogue = HenrikUrlBuilder.url_catalogue()
    # → {"henrik_player": [...], "henrik_matches": [...], "assets_agents": [...], ...}

    builder = HenrikUrlBuilder(region="eu")
    print(builder.matches_by_name("Henrik3", "EUW3"))
    # → "https://api.henrikdev.xyz/valorant/v3/matches/eu/Henrik3/EUW3"

    # ── Standard analysis ────────────────────────────────────────────────────
    # Optional: attach real ability names to per-agent utility breakdowns.
    # One HTTP call on first use; fully cached via ValAssetApi thereafter.
    ability_map = AgentAbilityMap.build()

    analyser = MatchAnalyser(matches, ability_map=ability_map)

    # Broad meta view across all data
    report = analyser.analyse()
    print(report.summary())

    # Narrow to Diamond+ on Ascent, full-buy rounds only
    report = analyser.analyse(AnalysisFilters(
        maps       = ["Ascent"],
        min_tier   = 16,
        buy_phases = ["Full Buy"],
    ))

    # ── Weapon questions ─────────────────────────────────────────────────────
    report.weapons.buy_rate("Ghost")             # Ghost pick rate overall
    report.weapons.by_buy_phase()["Eco"]         # what do players run on eco?
    report.weapons.by_agent()["Sova"]            # what does Sova typically run?
    report.weapons.by_rank_band()["Diamond"]     # Diamond weapon meta
    report.weapons.by_act()["e9a2"]              # weapon meta in act e9a2

    # Compound slices (act → rank → map → agent)
    report.weapons.by_act_by_rank()["e9a2"]["diamond"]
    report.weapons.by_act_by_rank_by_map()["e9a2"]["diamond"]["Ascent"]
    report.weapons.by_act_by_rank_by_map_by_agent()["e9a2"]["diamond"]["Ascent"]["Jett"]

    # ── Shield questions ─────────────────────────────────────────────────────
    report.shields.distribution()                # Heavy vs Light vs None breakdown
    report.shields.by_buy_phase()["Force"]       # shields on force-buy rounds
    report.shields.by_agent()["Reyna"]           # does Reyna always buy Heavy?
    report.shields.by_act()["e9a2"]              # shield trends in act e9a2

    # Compound slices
    report.shields.by_act_by_rank()["e9a2"]["diamond"]
    report.shields.by_act_by_rank_by_map()["e9a2"]["diamond"]["Ascent"]
    report.shields.by_act_by_rank_by_map_by_agent()["e9a2"]["diamond"]["Ascent"]["Reyna"]

    # ── Utility cast rate questions  (cast counts — separate from credits) ───
    report.utility.cast_rates()                               # overall CPR per slot
    report.utility.cast_rate_by_agent("Skye")                 # named keys: "Q – Trailblazer"
    report.utility.cast_rate_by_map()                         # util usage per map
    report.utility.cast_rate_by_buy_phase()                   # eco vs full-buy CPR
    report.utility.cast_rate_by_rank_band()                   # do better players use more ult?
    report.utility.cast_rate_by_act()                         # util trends across acts

    # Compound slices
    report.utility.cast_rate_by_act_by_rank()["e9a2"]["diamond"]
    report.utility.cast_rate_by_act_by_rank_by_map()["e9a2"]["diamond"]["Ascent"]
    report.utility.cast_rate_by_act_by_rank_by_map_by_agent()["e9a2"]["diamond"]["Ascent"]["Skye"]

    report.utility.agents_by_slot_usage("x")   # ultimate-hungry agents ranked
    report.utility.agents_by_slot_usage("q")   # Q-slot usage ranking

    # ── Spend questions  (credits — separate from cast counts) ───────────────
    report.spend.average_by_phase()              # how much do players spend per phase?
    report.spend.average_by_agent()              # which agents invest most in util charges?
    report.spend.average_by_map()                # map economy tendencies
    report.spend.by_rank_band()                  # do higher ranks spend more aggressively?
    report.spend.average_by_act()                # economy trends across acts/patches
    report.spend.buy_phase_distribution()        # how often is each phase hit?
    report.spend.match_totals_by_player()        # per-player per-match total spend

    # Compound slices
    report.spend.average_by_act_by_rank()["e9a2"]["diamond"]
    report.spend.average_by_act_by_rank_by_map()["e9a2"]["diamond"]["Ascent"]
    report.spend.average_by_act_by_rank_by_map_by_agent()["e9a2"]["diamond"]["Ascent"]["Skye"]

    # ── Precomputed flat index (for JS frontend filter lookups) ───────────────
    # meta_precompute builds a flat "map|rank|act|phase" → data index that the
    # JS page can query in O(1) without nested traversal.  The compound-slice
    # methods above are the Python-side complement for drill-downs.
    pre = analyser.meta_precompute()
    key = "Ascent|diamond|e9a2|Full Buy"
    print(pre["weapons"][key])
    print(pre["utility"][key])

    # ── Raw record access for custom aggregation ─────────────────────────────
    for record in analyser.iter_records(AnalysisFilters(agents=["Jett"])):
        print(record.weapon_name, record.spent, record.q_casts)
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from api_henrik import Match
from typing import TYPE_CHECKING, Any, Callable, Dict, Iterator, List, Optional, Set, Tuple, Union

if TYPE_CHECKING:
    from api_valorant_assets import ValAssetApi

# ---------------------------------------------------------------------------
# UNSET sentinel import
# ---------------------------------------------------------------------------

try:
    from jsoninjest import UNSET as _UNSET_TYPE
    _UNSET_CLS: type = type(_UNSET_TYPE)
except ImportError:
    # Fallback: a private class that will never match any real value
    _UNSET_CLS = type("_NeverMatch", (), {})


#: Valid competitive tier numbers (1–27) mapped to label strings, used for
#: ``/leaderboard`` and ``/mmr`` URL parameters.
HENRIK_TIER_LABELS: Dict[int, str] = {
    1:  "Iron 1",      2:  "Iron 2",      3:  "Iron 3",
    4:  "Bronze 1",    5:  "Bronze 2",    6:  "Bronze 3",
    7:  "Silver 1",    8:  "Silver 2",    9:  "Silver 3",
    10: "Gold 1",      11: "Gold 2",      12: "Gold 3",
    13: "Platinum 1",  14: "Platinum 2",  15: "Platinum 3",
    16: "Diamond 1",   17: "Diamond 2",   18: "Diamond 3",
    19: "Ascendant 1", 20: "Ascendant 2", 21: "Ascendant 3",
    22: "Immortal 1",  23: "Immortal 2",  24: "Immortal 3",
    25: "Radiant",     26: "Radiant",     27: "Radiant",
}

# ---------------------------------------------------------------------------
# AgentAbilityMap — slot-key → ability display name lookup
# ---------------------------------------------------------------------------

#: Maps valorant-api.com ``slot`` strings to our internal one-letter slot keys.
#: "Passive" is deliberately omitted — it has no keybind and no cast count.
_RIOT_SLOT_TO_KEY: Dict[str, str] = {
    "Grenade": "c",
    "Ability1": "q",
    "Ability2": "e",
    "Ultimate": "x",
}


class AgentAbilityMap:
    """Maps agent name + slot key → ability display name and icon URL, built from ``ValAssetApi``.

    Also stores all agent-level media fields from :class:`AgentItem` so that
    callers can embed portrait images, background art, gradient colours, role
    metadata, and agent bio text into serialised output without extra API calls.

    Built once and passed into :class:`MatchAnalyser`.  Falls back gracefully
    to the uppercased slot key (``"C"``, ``"Q"``, ``"E"``, ``"X"``) when an
    agent or slot is not found — so the rest of the codebase never has to
    null-check this.

    Example::

        ability_map = AgentAbilityMap.build()

        ability_map.name("Skye", "q")    # → "Trailblazer"
        ability_map.name("Jett", "c")    # → "Updraft"
        ability_map.label("Skye", "q")   # → "Q – Trailblazer"
        ability_map.slots("Sova")
        # → {"c": "Owl Drone", "q": "Shock Bolt", "e": "Recon Bolt", "x": "Hunter's Fury"}
        ability_map.icon("Sova", "q")
        # → "https://media.valorant-api.com/agents/.../abilities/ability1/displayicon.png"
        ability_map.icon_slots("Sova")
        # → {"c": "<url>", "q": "<url>", "e": "<url>", "x": "<url>"}
        ability_map.description("Sova", "q")
        # → "Fire a Shock Bolt that bounces..."
        ability_map.description_slots("Sova")
        # → {"c": "...", "q": "...", "e": "...", "x": "..."}

        # Agent-level media
        ability_map.agent_media("Jett")
        # → {
        #       "displayIcon":             "<url>",
        #       "displayIconSmall":        "<url>",
        #       "bustPortrait":            "<url>",
        #       "fullPortrait":            "<url>",
        #       "fullPortraitV2":          "<url>",
        #       "killfeedPortrait":        "<url>",
        #       "background":              "<url>",
        #       "backgroundGradientColors": ["<hex>", ...],
        #       "isFullPortraitRightFacing": False,
        #       "description":             "...",
        #       "characterTags":           [...],
        #       "role": {
        #           "displayName": "Duelist",
        #           "description": "...",
        #           "displayIcon": "<url>",
        #       },
        #   }
    """

    def __init__(
        self,
        data:         Dict[str, Dict[str, str]],
        icons:        Optional[Dict[str, Dict[str, str]]] = None,
        media:        Optional[Dict[str, Dict[str, Any]]] = None,
        descriptions: Optional[Dict[str, Dict[str, str]]] = None,
    ) -> None:
        # data layout: { "Skye": {"c": "Guiding Light", "q": "Trailblazer", ...}, ... }
        self._data: Dict[str, Dict[str, str]] = data
        # icons layout: { "Skye": {"c": "<url>", "q": "<url>", ...}, ... }
        self._icons: Dict[str, Dict[str, str]] = icons or {}
        # media layout: { "Jett": {"displayIcon": "<url>", "bustPortrait": "<url>", ...}, ... }
        self._media: Dict[str, Dict[str, Any]] = media or {}
        # descriptions layout: { "Skye": {"c": "Send out Guiding Light...", ...}, ... }
        self._descriptions: Dict[str, Dict[str, str]] = descriptions or {}

    # ── Construction ─────────────────────────────────────────────────────────

    @classmethod
    def build(cls, api: Optional[ValAssetApi] = None) -> AgentAbilityMap:
        """Fetch all agents from ``ValAssetApi`` and index their abilities by slot key.

        Also collects all agent-level media fields (portraits, backgrounds,
        gradient colours, role info, description, and character tags) from each
        :class:`AgentItem` so they can be embedded in serialised output without
        additional API calls.

        Uses the singleton ``ValAssetApi`` instance if *api* is not supplied.
        The API client's ``agents`` property is a ``lazy_property``, so network
        traffic only occurs on the very first call.

        Args:
            api: Optional ``ValAssetApi`` instance.  Defaults to the singleton.

        Returns:
            A fully-populated :class:`AgentAbilityMap` with ability names,
            ability icon URLs, and all agent-level media assets.
        """
        from api_valorant_assets import ValAssetApi as _Api  # local to avoid circular import
        _api = api or _Api()

        data:         Dict[str, Dict[str, str]] = {}
        icons:        Dict[str, Dict[str, str]] = {}
        media:        Dict[str, Dict[str, Any]] = {}
        descriptions: Dict[str, Dict[str, str]] = {}

        for agent_name, agent in _api.agents.items():
            # ── Ability names, icons, and descriptions ───────────────────────
            slots:        Dict[str, str] = {}
            slot_icons:   Dict[str, str] = {}
            slot_descs:   Dict[str, str] = {}
            for ability in agent.abilities:
                key = _RIOT_SLOT_TO_KEY.get(ability.slot)
                if key is not None:
                    slots[key]      = ability.displayName
                    slot_icons[key] = ability.displayIcon or ""
                    slot_descs[key] = ability.description or ""
            data[agent_name]         = slots
            icons[agent_name]        = slot_icons
            descriptions[agent_name] = slot_descs

            # ── Agent-level media ────────────────────────────────────────────
            role_obj = getattr(agent, "role", None)
            role_dict: Dict[str, str] = {}
            if role_obj is not None:
                role_dict = {
                    "displayName": getattr(role_obj, "displayName", "") or "",
                    "description": getattr(role_obj, "description", "") or "",
                    "displayIcon": getattr(role_obj, "displayIcon", "") or "",
                }

            media[agent_name] = {
                "displayIcon":              getattr(agent, "displayIcon",              "") or "",
                "displayIconSmall":         getattr(agent, "displayIconSmall",         "") or "",
                "bustPortrait":             getattr(agent, "bustPortrait",             "") or "",
                "fullPortrait":             getattr(agent, "fullPortrait",             "") or "",
                "fullPortraitV2":           getattr(agent, "fullPortraitV2",           "") or "",
                "killfeedPortrait":         getattr(agent, "killfeedPortrait",         "") or "",
                "background":               getattr(agent, "background",               "") or "",
                "backgroundGradientColors": list(getattr(agent, "backgroundGradientColors", []) or []),
                "isFullPortraitRightFacing": bool(getattr(agent, "isFullPortraitRightFacing", False)),
                "description":              getattr(agent, "description",              "") or "",
                "characterTags":            list(getattr(agent, "characterTags",       []) or []),
                "role":                     role_dict,
            }

        return cls(data, icons, media, descriptions)

    # ── Lookups ───────────────────────────────────────────────────────────────

    def name(self, agent: str, slot: str) -> str:
        """Return the ability display name for *agent* + *slot*.

        Falls back to the uppercased slot key if the agent or slot is unknown.

        Args:
            agent: Agent display name, e.g. ``"Skye"``.
            slot:  One of ``"c"``, ``"q"``, ``"e"``, ``"x"`` (case-insensitive).

        Returns:
            Display name string, e.g. ``"Trailblazer"``.
        """
        return self._data.get(agent, {}).get(slot.lower(), slot.upper())

    def label(self, agent: str, slot: str) -> str:
        """Return a formatted ``"KEY – Ability Name"`` label.

        Example: ``ability_map.label("Skye", "q")`` → ``"Q – Trailblazer"``
        """
        return f"{slot.upper()} – {self.name(agent, slot)}"

    def slots(self, agent: str) -> Dict[str, str]:
        """Return the full ``{slot_key: ability_name}`` mapping for *agent*.

        Returns an empty dict if the agent is unknown.
        """
        return dict(self._data.get(agent, {}))

    def icon(self, agent: str, slot: str) -> str:
        """Return the ability icon URL for *agent* + *slot*.

        Falls back to an empty string if the agent or slot is unknown.

        Args:
            agent: Agent display name, e.g. ``"Skye"``.
            slot:  One of ``"c"``, ``"q"``, ``"e"``, ``"x"`` (case-insensitive).

        Returns:
            Icon URL string, or ``""`` if not available.
        """
        return self._icons.get(agent, {}).get(slot.lower(), "")

    def icon_slots(self, agent: str) -> Dict[str, str]:
        """Return the full ``{slot_key: icon_url}`` mapping for *agent*.

        Returns an empty dict if the agent is unknown.
        Consumers should include this as ``ability_icons`` alongside
        ``ability_names`` in any serialised per-agent loadout dict so that
        the front-end can render ability icons in the util CPR column and
        accordion detail panel without extra API calls.

        Example output JSON shape::

            "loadout": {
                "utility":       {"c": 0.72, "q": 0.41, "e": 0.88, "x": 0.31},
                "ability_names": {"c": "Updraft", "q": "Cloudburst", ...},
                "ability_icons": {"c": "<url>", "q": "<url>", ...},
                ...
            }
        """
        return dict(self._icons.get(agent, {}))

    def description(self, agent: str, slot: str) -> str:
        """Return the ability description text for *agent* + *slot*.

        Falls back to an empty string if the agent or slot is unknown.

        Args:
            agent: Agent display name, e.g. ``"Skye"``.
            slot:  One of ``"c"``, ``"q"``, ``"e"``, ``"x"`` (case-insensitive).

        Returns:
            Description string, or ``""`` if not available.
        """
        return self._descriptions.get(agent, {}).get(slot.lower(), "")

    def description_slots(self, agent: str) -> Dict[str, str]:
        """Return the full ``{slot_key: description_text}`` mapping for *agent*.

        Returns an empty dict if the agent is unknown.
        Consumers should include this as ``ability_descriptions`` alongside
        ``ability_names`` and ``ability_icons`` in any serialised per-agent
        loadout dict.

        Example output JSON shape::

            "loadout": {
                "utility":              {"c": 0.72, "q": 0.41, "e": 0.88, "x": 0.31},
                "ability_names":        {"c": "Updraft", "q": "Cloudburst", ...},
                "ability_icons":        {"c": "<url>", "q": "<url>", ...},
                "ability_descriptions": {"c": "INSTANTLY propel Jett...", ...},
                ...
            }
        """
        return dict(self._descriptions.get(agent, {}))

    def agent_media(self, agent: str) -> Dict[str, Any]:
        """Return all agent-level media fields for *agent*.

        Returns a dict with the following keys (all strings default to ``""``
        when the agent is unknown or the field was absent from the API)::

            {
                "displayIcon":              "<url>",   # square icon
                "displayIconSmall":         "<url>",   # small variant
                "bustPortrait":             "<url>",   # cropped bust shot
                "fullPortrait":             "<url>",   # full-body art
                "fullPortraitV2":           "<url>",   # updated full-body art
                "killfeedPortrait":         "<url>",   # killfeed icon
                "background":               "<url>",   # card background image
                "backgroundGradientColors": ["<hex>", ...],  # palette list
                "isFullPortraitRightFacing": bool,
                "description":              "...",     # agent bio
                "characterTags":            [...],     # flavor tags
                "role": {
                    "displayName": "Duelist",
                    "description": "...",
                    "displayIcon": "<url>",
                },
            }

        Args:
            agent: Agent display name, e.g. ``"Jett"``.

        Returns:
            Media dict, or a dict of empty defaults if the agent is unknown.
        """
        return dict(self._media.get(agent, {
            "displayIcon": "", "displayIconSmall": "", "bustPortrait": "",
            "fullPortrait": "", "fullPortraitV2": "", "killfeedPortrait": "",
            "background": "", "backgroundGradientColors": [],
            "isFullPortraitRightFacing": False,
            "description": "", "characterTags": [],
            "role": {"displayName": "", "description": "", "displayIcon": ""},
        }))

    def all_agents(self) -> List[str]:
        """Return the list of all agent names in the map."""
        return list(self._data.keys())


# ---------------------------------------------------------------------------
# Coercion helpers
# ---------------------------------------------------------------------------

def _int(value: Any, default: int = 0) -> int:
    """Coerce *value* to ``int``, returning *default* for ``None`` or ``UNSET``.

    The JsonInjester returns the ``UNSET`` singleton (a truthy non-None object)
    for missing fields, so the usual ``value or 0`` guard silently passes UNSET
    through.  This helper catches it explicitly.
    """
    if value is None or isinstance(value, _UNSET_CLS):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _str(value: Any, default: str = "") -> str:
    """Coerce *value* to ``str``, returning *default* for ``None`` or ``UNSET``."""
    if value is None or isinstance(value, _UNSET_CLS):
        return default
    return str(value)


# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

#: A weapon/shield distribution row: {"count": int, "buy_rate_pct": float}
DistRow = Dict[str, Union[int, float]]

#: weapon/shield name → DistRow
Distribution = Dict[str, DistRow]

#: dimension (phase/agent/map/band) → Distribution
DistByDimension = Dict[str, Distribution]

#: slot label → CPR value
CastRates = Dict[str, float]

#: dimension → CastRates
CastRatesByDimension = Dict[str, CastRates]

#: economy stats row returned by SpendReport methods
SpendStats = Dict[str, Union[int, float]]

#: dimension → SpendStats
SpendByDimension = Dict[str, SpendStats]

#: puuid → per-player match totals dict
PlayerTotals = Dict[str, Dict[str, Union[str, int]]]


# ---------------------------------------------------------------------------
# Buy-phase classification
# ---------------------------------------------------------------------------

#: Round indices (0-based) that are always treated as Pistol rounds regardless
#: of loadout value.  Index 0 = round 1 (attack-side open), 12 = second-half opener.
PISTOL_ROUND_INDICES: Set[int] = {0, 12}


@dataclass
class BuyPhaseThresholds:
    """Credit thresholds for auto-classifying a round's buy type.

    Applied to ``loadout_value`` (the total credit value of what a player
    carries into a round — weapon + armor + any purchased ability charges).

    Classification order:
      1. ``round_index`` in ``PISTOL_ROUND_INDICES``        → **Pistol**
      2. ``loadout_value <= eco_max``                        → **Eco**
      3. ``eco_max < loadout_value <= force_max``            → **Force**
      4. ``loadout_value > force_max``                       → **Full Buy**

    Defaults reflect common community conventions.  Adjust to taste.
    """
    eco_max:   int = 2000   # ≤ 2000 cr  → Eco
    force_max: int = 3999   # ≤ 3999 cr  → Force  (4000+ → Full Buy)


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------

@dataclass
class AnalysisFilters:
    """Declarative filters applied to the round-record dataset before aggregation.

    All fields default to ``None`` (= no restriction on that dimension).
    Multiple non-None fields are combined with AND logic.

    Attributes:
        maps:       Include only rounds played on these maps (case-insensitive).
        agents:     Include only rounds where the player used one of these agents.
        min_tier:   Minimum competitive tier, inclusive (1 = Iron 1, 27 = Radiant).
        max_tier:   Maximum competitive tier, inclusive.
        buy_phases: Include only rounds whose buy phase is in this list.
                    Valid values: ``"Pistol"``, ``"Eco"``, ``"Force"``, ``"Full Buy"``.
        puuids:     Restrict to specific player UUIDs.
        acts:       Include only rounds from these act/season IDs (case-insensitive).
                    Values match ``match.metadata.season_id``.  ``None`` = all acts.
    """
    maps:       Optional[List[str]] = None
    agents:     Optional[List[str]] = None
    min_tier:   Optional[int]       = None
    max_tier:   Optional[int]       = None
    buy_phases: Optional[List[str]] = None
    puuids:     Optional[List[str]] = None
    acts:       Optional[List[str]] = None
    sides:      Optional[List[str]] = None  # e.g. ["attack"] or ["defense"]


# ---------------------------------------------------------------------------
# Internal flat record — one player × one round
# ---------------------------------------------------------------------------

@dataclass
class _RoundRecord:
    """Flattened snapshot of one player's data for one round.

    This is the atomic unit of analysis.  All report classes operate on a
    filtered list of these records.

    Match-level totals (``match_*`` fields) are denormalised onto every record
    so that per-match aggregation remains correct even after round-level
    filtering (e.g. filtering to Full Buy rounds still gives you the player's
    full-match spend total for reference).
    """
    # ── Identity ─────────────────────────────────────────────────────────────
    match_id:    str
    round_index: int    # 0-based
    puuid:       str
    player_name: str    # display name + tag, e.g. "Henrik3#EUW3"
    agent:       str    # e.g. "Sova"
    tier:        int    # currenttier at match time (1–27; 0 = unranked/unknown)
    map_name:    str    # e.g. "Ascent"
    act:         str    # Season/act ID from match metadata, e.g. "e9a1"; "" = unknown
    side:        str    # "attack" | "defense" — derived from player_team + round_index

    # ── Round economy ─────────────────────────────────────────────────────────
    buy_phase:     str  # "Pistol" | "Eco" | "Force" | "Full Buy"
    loadout_value: int  # total credit value of loadout entering this round
    spent:         int  # credits spent this round (gun + armor + util charges combined)
    remaining:     int  # credits remaining after buying

    # ── Loadout ───────────────────────────────────────────────────────────────
    weapon_name: str    # e.g. "Vandal", "Ghost"; "" = none / classic (free)
    armor_name:  str    # e.g. "Heavy Shields", "Light Shields"; "" = none

    # ── Match-level ability cast totals (from Player object, denormalised per round) ──
    # NOTE: The HenrikDev API does not populate per-round cast counts —
    # MatchRoundsItemPlayerStatsItemAbilityCasts fields are always None.
    # Only PlayerAbilityCasts (match-level totals) are populated.
    # CPR is computed in UtilityReport as match_total / match_rounds_total.
    match_c_total:      int  # total C casts across the whole match
    match_q_total:      int  # total Q casts
    match_e_total:      int  # total E casts
    match_x_total:      int  # total X (ultimate) casts
    match_rounds_total: int  # total rounds this player appeared in for this match

    # ── Match-level economy totals (from Player object, denormalised per round) ──
    match_spent_overall:   int  # total credits spent across the whole match
    match_loadout_overall: int  # sum of all round loadout values for the match


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _pct(numerator: int, denominator: int) -> float:
    """Safe percentage rounded to 2 decimal places."""
    return round(100.0 * numerator / denominator, 2) if denominator else 0.0


def _avg(values: List[int]) -> float:
    """Mean of a list of ints, rounded to 2 decimal places."""
    return round(sum(values) / len(values), 2) if values else 0.0


def _group_by(
    records: List[_RoundRecord],
    key_fn:  Callable[[_RoundRecord], str],
) -> Dict[str, List[_RoundRecord]]:
    """Group *records* by the string key returned by *key_fn*."""
    groups: Dict[str, List[_RoundRecord]] = defaultdict(list)
    for r in records:
        groups[key_fn(r)].append(r)
    return groups


RANK_BANDS: List[Tuple[str, int, int]] = [
    ("Iron",       3,  5),
    ("Bronze",     6,  8),
    ("Silver",     9, 11),
    ("Gold",      12, 14),
    ("Platinum",  15, 17),
    ("Diamond",   18, 20),
    ("Ascendant", 21, 23),
    ("Immortal",  24, 26),
    ("Radiant",   27, 27),
]

#: Maps the human-readable rank-band label returned by ``_rank_band()`` to the
#: lowercase abbreviated key used by the agents-screen frontend rank badges.
#: These must match the ``data-rank`` values set on ``.rank-badge`` elements.
_RANK_BAND_KEY: Dict[str, str] = {
    "Iron":       "iron",
    "Bronze":     "bronze",
    "Silver":     "silver",
    "Gold":       "gold",
    "Platinum":   "plat",
    "Diamond":    "diamond",
    "Ascendant":  "ascend",
    "Immortal":   "immortal",
    "Radiant":    "radiant",
    "Unranked":   "unranked",
}


def _rank_band(tier: int) -> str:
    """Map a numeric tier (1–27) to a rank-band label."""
    for name, lo, hi in RANK_BANDS:
        if lo <= tier <= hi:
            return name
    return "Unranked"


# ---------------------------------------------------------------------------
# WeaponReport
# ---------------------------------------------------------------------------

class WeaponReport:
    """Weapon pick-rate analysis.

    All methods return dicts keyed by weapon name with ``count`` and
    ``buy_rate_pct`` fields, sorted by count descending.

    Note: ``weapon_name`` reflects the weapon the player *carried into* the
    round (from the API's economy snapshot at round start), which includes
    weapons survived from previous rounds — not only fresh purchases.
    """

    def __init__(self, records: List[_RoundRecord]) -> None:
        self._records: List[_RoundRecord] = records

    def _total(self) -> int:
        return len(self._records)

    def distribution(self) -> Distribution:
        """Pick rate of every weapon across all records, sorted by frequency.

        Returns::

            {
                "Vandal":           {"count": 412, "buy_rate_pct": 42.04},
                "Phantom":          {"count": 308, "buy_rate_pct": 31.43},
                "(none / classic)": {"count":  92, "buy_rate_pct":  9.39},
                ...
            }
        """
        counts: Dict[str, int] = defaultdict(int)
        for r in self._records:
            counts[r.weapon_name or "Classic"] += 1
        total: int = self._total()
        return {
            name: {"count": cnt, "buy_rate_pct": _pct(cnt, total)}
            for name, cnt in sorted(counts.items(), key=lambda x: -x[1])
        }

    def buy_rate(self, weapon: str) -> Dict[str, Union[str, int, float]]:
        """Pick rate for a single named weapon (case-insensitive).

        Example::

            report.weapons.buy_rate("Ghost")
            # → {"weapon": "Ghost", "count": 142, "total_rounds": 980,
            #    "buy_rate_pct": 14.49}
        """
        weapon_lower: str = weapon.lower()
        count: int = sum(1 for r in self._records if r.weapon_name.lower() == weapon_lower)
        return {
            "weapon":       weapon,
            "count":        count,
            "total_rounds": self._total(),
            "buy_rate_pct": _pct(count, self._total()),
        }

    def by_buy_phase(self) -> DistByDimension:
        """Weapon distribution broken down by buy phase.

        Useful for: "what rifles dominate Full Buy?" or "what do players
        run on Eco — Ghost, Shorty, or nothing?"
        """
        return {
            phase: WeaponReport(recs).distribution()
            for phase, recs in sorted(_group_by(self._records, lambda r: r.buy_phase).items())
        }

    def by_agent(self) -> DistByDimension:
        """Weapon distribution broken down by agent.

        Useful for: "do Jett players prefer the Operator?" or
        "what does Sova typically run?"
        """
        return {
            agent: WeaponReport(recs).distribution()
            for agent, recs in sorted(_group_by(self._records, lambda r: r.agent).items())
        }

    def by_map(self) -> DistByDimension:
        """Weapon distribution broken down by map."""
        return {
            m: WeaponReport(recs).distribution()
            for m, recs in sorted(_group_by(self._records, lambda r: r.map_name).items())
        }

    def by_rank_band(self) -> DistByDimension:
        """Weapon distribution broken down by rank band (Iron → Radiant)."""
        return {
            band: WeaponReport(recs).distribution()
            for band, recs in _group_by(self._records, lambda r: _rank_band(r.tier)).items()
        }

    def by_act(self) -> DistByDimension:
        """Weapon distribution broken down by act / season.

        Keys are act ID strings (e.g. ``"e9a1"``), matching
        ``match.metadata.season_id``.  Useful for tracking meta shifts across
        patches — e.g. did the Vandal/Phantom split change after an act update?
        """
        return {
            act: WeaponReport(recs).distribution()
            for act, recs in sorted(_group_by(self._records, lambda r: r.act).items())
            if act
        }

    def by_act_by_rank(self) -> Dict[str, Dict[str, Distribution]]:
        """Weapon distribution nested as ``{act → {rank_band → distribution}}``.

        Powers the Act + Rank compound filter on the meta/agents screens.
        Each leaf is the same shape as :meth:`distribution`.

        Example::

            data = report.weapons.by_act_by_rank()
            data["e9a2"]["diamond"]
            # → {"Vandal": {"count": 88, "buy_rate_pct": 54.0}, ...}
        """
        result: Dict[str, Dict[str, Distribution]] = {}
        for act, act_recs in sorted(_group_by(self._records, lambda r: r.act).items()):
            if not act:
                continue
            result[act] = {
                _RANK_BAND_KEY.get(band, band.lower()): WeaponReport(recs).distribution()
                for band, recs in _group_by(act_recs, lambda r: _rank_band(r.tier)).items()
            }
        return result

    def by_act_by_rank_by_map(self) -> Dict[str, Dict[str, Dict[str, Distribution]]]:
        """Weapon distribution nested as ``{act → {rank → {map → distribution}}}``.

        The deepest pre-computed slice available.  The meta_precompute flat-key
        approach is generally preferred for frontend lookups; use this method
        for Python-side analytics or one-off drill-downs.

        Example::

            data = report.weapons.by_act_by_rank_by_map()
            data["e9a2"]["diamond"]["Ascent"]
            # → {"Vandal": {"count": 42, "buy_rate_pct": 61.8}, ...}
        """
        result: Dict[str, Dict[str, Dict[str, Distribution]]] = {}
        for act, act_recs in sorted(_group_by(self._records, lambda r: r.act).items()):
            if not act:
                continue
            by_rank: Dict[str, Dict[str, Distribution]] = {}
            for band, band_recs in _group_by(act_recs, lambda r: _rank_band(r.tier)).items():
                rank_key = _RANK_BAND_KEY.get(band, band.lower())
                by_rank[rank_key] = {
                    m: WeaponReport(map_recs).distribution()
                    for m, map_recs in sorted(
                        _group_by(band_recs, lambda r: r.map_name).items()
                    )
                    if m
                }
            result[act] = by_rank
        return result

    def by_act_by_rank_by_map_by_agent(
        self,
    ) -> Dict[str, Dict[str, Dict[str, Dict[str, Distribution]]]]:
        """Weapon distribution nested as ``{act → {rank → {map → {agent → distribution}}}}``.

        The most granular pre-computed slice.  Because the result can be large
        (acts × ranks × maps × agents), prefer the flat ``meta_precompute``
        index for frontend lookups and use this method only when you need the
        full cross-product in Python.

        Example::

            data = report.weapons.by_act_by_rank_by_map_by_agent()
            data["e9a2"]["diamond"]["Ascent"]["Jett"]
            # → {"Operator": {"count": 12, "buy_rate_pct": 63.2}, ...}
        """
        result: Dict[str, Dict[str, Dict[str, Dict[str, Distribution]]]] = {}
        for act, act_recs in sorted(_group_by(self._records, lambda r: r.act).items()):
            if not act:
                continue
            by_rank: Dict[str, Dict[str, Dict[str, Distribution]]] = {}
            for band, band_recs in _group_by(act_recs, lambda r: _rank_band(r.tier)).items():
                rank_key = _RANK_BAND_KEY.get(band, band.lower())
                by_map: Dict[str, Dict[str, Distribution]] = {}
                for m, map_recs in sorted(
                    _group_by(band_recs, lambda r: r.map_name).items()
                ):
                    if not m:
                        continue
                    by_map[m] = {
                        agent: WeaponReport(agent_recs).distribution()
                        for agent, agent_recs in sorted(
                            _group_by(map_recs, lambda r: r.agent).items()
                        )
                        if agent
                    }
                by_rank[rank_key] = by_map
            result[act] = by_rank
        return result


# ---------------------------------------------------------------------------
# ShieldReport
# ---------------------------------------------------------------------------

class ShieldReport:
    """Armor / shield purchase analysis.

    Shield names from the API are human-readable strings:
    ``"Heavy Shields"`` (1000 cr), ``"Light Shields"`` (400 cr), or ``""``
    (no armor equipped — shown as ``"(none)"`` in output).

    API names are normalised to the display labels used by the JS frontend
    via ``_SHIELD_NAME_MAP`` so that keys always match ``SHIELD_KEYS`` /
    ``SHIELD_STYLE`` in agents.html.
    """

    #: Maps Henrik API armor name → JS display label.
    #: "Light Shields" → "Regen Shield" because Light Shields partially
    #: regenerate between rounds and that is the label used in the UI.
    _SHIELD_NAME_MAP: Dict[str, str] = {
        "Heavy Shields": "Heavy Armor",
        "Light Shields": "Regen Shield",
    }

    @classmethod
    def _normalise(cls, armor_name: str) -> str:
        """Return the JS-facing display label for an API armor name."""
        return cls._SHIELD_NAME_MAP.get(armor_name, armor_name) if armor_name else "(none)"

    def __init__(self, records: List[_RoundRecord]) -> None:
        self._records: List[_RoundRecord] = records

    def _total(self) -> int:
        return len(self._records)

    def distribution(self) -> Distribution:
        """Frequency and buy-rate of each shield type.

        API armor names are normalised via ``_SHIELD_NAME_MAP`` so the
        returned keys match the JS ``SHIELD_KEYS`` / ``SHIELD_STYLE`` table.

        Returns::

            {
                "Heavy Armor":  {"count": 510, "buy_rate_pct": 52.04},
                "Regen Shield": {"count": 280, "buy_rate_pct": 28.57},
                "(none)":       {"count": 190, "buy_rate_pct": 19.39},
            }
        """
        counts: Dict[str, int] = defaultdict(int)
        for r in self._records:
            counts[self._normalise(r.armor_name)] += 1
        total: int = self._total()
        return {
            name: {"count": cnt, "buy_rate_pct": _pct(cnt, total)}
            for name, cnt in sorted(counts.items(), key=lambda x: -x[1])
        }

    def by_buy_phase(self) -> DistByDimension:
        """Shield distribution broken down by buy phase.

        Useful for: "do players always buy Heavy on Full Buy?" or
        "how often do Force-buy players skip armor entirely?"
        """
        return {
            phase: ShieldReport(recs).distribution()
            for phase, recs in sorted(_group_by(self._records, lambda r: r.buy_phase).items())
        }

    def by_agent(self) -> DistByDimension:
        """Shield distribution broken down by agent."""
        return {
            agent: ShieldReport(recs).distribution()
            for agent, recs in sorted(_group_by(self._records, lambda r: r.agent).items())
        }

    def by_map(self) -> DistByDimension:
        """Shield distribution broken down by map."""
        return {
            m: ShieldReport(recs).distribution()
            for m, recs in sorted(_group_by(self._records, lambda r: r.map_name).items())
        }

    def by_rank_band(self) -> DistByDimension:
        """Shield distribution broken down by rank band."""
        return {
            band: ShieldReport(recs).distribution()
            for band, recs in _group_by(self._records, lambda r: _rank_band(r.tier)).items()
        }

    def by_act(self) -> DistByDimension:
        """Shield distribution broken down by act / season.

        Keys are act ID strings (e.g. ``"e9a1"``).  Useful for tracking
        economy-convention shifts across patches.
        """
        return {
            act: ShieldReport(recs).distribution()
            for act, recs in sorted(_group_by(self._records, lambda r: r.act).items())
            if act
        }

    def by_act_by_rank(self) -> Dict[str, Dict[str, Distribution]]:
        """Shield distribution nested as ``{act → {rank_band → distribution}}``.

        Example::

            data = report.shields.by_act_by_rank()
            data["e9a2"]["diamond"]
            # → {"Heavy Shields": {"count": 70, "buy_rate_pct": 62.5}, ...}
        """
        result: Dict[str, Dict[str, Distribution]] = {}
        for act, act_recs in sorted(_group_by(self._records, lambda r: r.act).items()):
            if not act:
                continue
            result[act] = {
                _RANK_BAND_KEY.get(band, band.lower()): ShieldReport(recs).distribution()
                for band, recs in _group_by(act_recs, lambda r: _rank_band(r.tier)).items()
            }
        return result

    def by_act_by_rank_by_map(self) -> Dict[str, Dict[str, Dict[str, Distribution]]]:
        """Shield distribution nested as ``{act → {rank → {map → distribution}}}``.

        Example::

            data = report.shields.by_act_by_rank_by_map()
            data["e9a2"]["diamond"]["Ascent"]
            # → {"Heavy Shields": {"count": 40, "buy_rate_pct": 71.4}, ...}
        """
        result: Dict[str, Dict[str, Dict[str, Distribution]]] = {}
        for act, act_recs in sorted(_group_by(self._records, lambda r: r.act).items()):
            if not act:
                continue
            by_rank: Dict[str, Dict[str, Distribution]] = {}
            for band, band_recs in _group_by(act_recs, lambda r: _rank_band(r.tier)).items():
                rank_key = _RANK_BAND_KEY.get(band, band.lower())
                by_rank[rank_key] = {
                    m: ShieldReport(map_recs).distribution()
                    for m, map_recs in sorted(
                        _group_by(band_recs, lambda r: r.map_name).items()
                    )
                    if m
                }
            result[act] = by_rank
        return result

    def by_act_by_rank_by_map_by_agent(
        self,
    ) -> Dict[str, Dict[str, Dict[str, Dict[str, Distribution]]]]:
        """Shield distribution nested as ``{act → {rank → {map → {agent → distribution}}}}``.

        Example::

            data = report.shields.by_act_by_rank_by_map_by_agent()
            data["e9a2"]["diamond"]["Ascent"]["Reyna"]
            # → {"Heavy Shields": {"count": 8, "buy_rate_pct": 80.0}, ...}
        """
        result: Dict[str, Dict[str, Dict[str, Dict[str, Distribution]]]] = {}
        for act, act_recs in sorted(_group_by(self._records, lambda r: r.act).items()):
            if not act:
                continue
            by_rank: Dict[str, Dict[str, Dict[str, Distribution]]] = {}
            for band, band_recs in _group_by(act_recs, lambda r: _rank_band(r.tier)).items():
                rank_key = _RANK_BAND_KEY.get(band, band.lower())
                by_map: Dict[str, Dict[str, Distribution]] = {}
                for m, map_recs in sorted(
                    _group_by(band_recs, lambda r: r.map_name).items()
                ):
                    if not m:
                        continue
                    by_map[m] = {
                        agent: ShieldReport(agent_recs).distribution()
                        for agent, agent_recs in sorted(
                            _group_by(map_recs, lambda r: r.agent).items()
                        )
                        if agent
                    }
                by_rank[rank_key] = by_map
            result[act] = by_rank
        return result


# ---------------------------------------------------------------------------
# UtilityReport  — cast rate analysis (separate from credits)
# ---------------------------------------------------------------------------

class UtilityReport:
    """Ability cast-rate analysis.

    All rates are expressed as **casts-per-round (CPR)** — the average number
    of times an ability was activated per round played.

    **Data source:** The HenrikDev API only populates cast counts at the
    *match* level (``PlayerAbilityCasts``).  Per-round cast fields
    (``MatchRoundsItemPlayerStatsItemAbilityCasts``) are always ``None``.
    CPR is therefore computed as ``match_total_casts / rounds_in_match``
    per player-match, then averaged across all player-matches in the slice.

    Consequence: slicing by ``buy_phase`` changes *which rounds* are included
    and thus *which player-matches* appear in the slice, but cannot change the
    CPR values themselves (the match totals are fixed).  Buy-phase breakdowns
    are therefore most useful for comparing CPR across different subsets of
    matches (e.g. "Skye's CPR in matches that were mostly Full Buy") rather
    than literally "CPR during Eco rounds".

    Interpretation guide:
      - CPR ≈ 1.0  → ability used nearly every round
      - CPR < 1.0  → hoarding, early death, or not owning the ability
      - CPR > 1.0  → possible for multi-charge abilities (e.g. Skye Q, 2 charges)

    Slot mapping (VALORANT default keybinds):
      - **C** → Signature ability  (usually free or discounted)
      - **Q** → Basic ability       (has a credit cost per charge)
      - **E** → Secondary ability   (varies by agent)
      - **X** → Ultimate            (costs ultimate points, not credits)
    """

    def __init__(
        self,
        records: List[_RoundRecord],
        ability_map: Optional[AgentAbilityMap] = None,
    ) -> None:
        self._records: List[_RoundRecord] = records
        self._ability_map: Optional[AgentAbilityMap] = ability_map

    def _cpr(self, slot: str) -> float:
        """Average casts-per-round for *slot* across all records.

        Because the API only provides match-level cast totals (not per-round),
        CPR is computed per player-match as ``match_total / match_rounds_total``,
        then averaged across all player-match pairs in the record set.

        A player-match with ``match_rounds_total == 0`` is skipped (disconnected
        player with no rounds on record).
        """
        total_attr: str = f"match_{slot}_total"
        cprs: List[float] = []

        # De-duplicate to one entry per (puuid, match_id) so we don't
        # count the same match's total N times (once per round record).
        seen: Dict[Tuple[str, str], _RoundRecord] = {}
        for r in self._records:
            key: Tuple[str, str] = (r.puuid, r.match_id)
            if key not in seen:
                seen[key] = r

        for r in seen.values():
            rounds: int = r.match_rounds_total
            if rounds > 0:
                cprs.append(getattr(r, total_attr) / rounds)

        return round(sum(cprs) / len(cprs), 4) if cprs else 0.0

    def _rates_for(
        self,
        records: List[_RoundRecord],
        agent: Optional[str] = None,
    ) -> CastRates:
        """Build a :data:`CastRates` dict for an arbitrary record slice.

        When *agent* is supplied and an :class:`AgentAbilityMap` is attached,
        keys are formatted as ``"KEY – Ability Name"`` (e.g. ``"Q – Trailblazer"``).
        Otherwise the generic ``"C_cpr"`` / ``"Q_cpr"`` / ``"E_cpr"`` / ``"X_cpr"``
        keys are used — appropriate for multi-agent slices where a single name
        would be misleading.
        """
        rep = UtilityReport(records, self._ability_map)
        if self._ability_map and agent:
            return {
                self._ability_map.label(agent, "c"): rep._cpr("c"),
                self._ability_map.label(agent, "q"): rep._cpr("q"),
                self._ability_map.label(agent, "e"): rep._cpr("e"),
                self._ability_map.label(agent, "x"): rep._cpr("x"),
            }
        return {
            "C_cpr": rep._cpr("c"),
            "Q_cpr": rep._cpr("q"),
            "E_cpr": rep._cpr("e"),
            "X_cpr": rep._cpr("x"),
        }

    def cast_rates(self) -> CastRates:
        """Overall CPR for each ability slot across all records.

        Returns::

            {
                "C_cpr": 0.8200,   # signature
                "Q_cpr": 0.6100,   # basic / purchasable
                "E_cpr": 0.9400,   # secondary
                "X_cpr": 0.1700,   # ultimate
            }
        """
        return self._rates_for(self._records)

    def cast_rate_by_agent(self, agent: Optional[str] = None) -> CastRatesByDimension:
        """CPR broken down by agent.

        When an :class:`AgentAbilityMap` is attached to this analyser, each
        agent's dict is keyed by real ability names instead of generic slot
        keys.  For example::

            # Without ability_map:
            {"Skye": {"C_cpr": 0.91, "Q_cpr": 1.73, "E_cpr": 0.88, "X_cpr": 0.19}}

            # With ability_map:
            {"Skye": {"C – Guiding Light": 0.91, "Q – Trailblazer": 1.73,
                      "E – Regrowth": 0.88, "X – Seekers": 0.19}}

        Args:
            agent: If provided, filters to just that agent (case-insensitive).
                   If ``None``, returns all agents.
        """
        groups: Dict[str, List[_RoundRecord]] = _group_by(self._records, lambda r: r.agent)
        if agent:
            agent_lower: str = agent.lower()
            groups = {k: v for k, v in groups.items() if k.lower() == agent_lower}
        return {
            ag: self._rates_for(recs, agent=ag)
            for ag, recs in sorted(groups.items())
        }

    def cast_rate_by_map(self) -> CastRatesByDimension:
        """CPR broken down by map.

        Useful for discovering whether certain maps encourage heavier utility
        usage (e.g. maps with long, covered chokes may see more smoke/flash usage).

        Keys use generic slot labels (``"C_cpr"`` etc.) because each map slice
        spans multiple agents.
        """
        return {
            m: self._rates_for(recs)
            for m, recs in sorted(_group_by(self._records, lambda r: r.map_name).items())
        }

    def cast_rate_by_buy_phase(self) -> CastRatesByDimension:
        """CPR broken down by buy phase.

        Note: because cast counts are only available at match level (not per
        round), this groups player-matches by the *dominant* buy phase seen in
        the filtered record set rather than isolating individual round behavior.
        Useful for comparing CPR across matches with different economy profiles.
        """
        return {
            phase: self._rates_for(recs)
            for phase, recs in sorted(_group_by(self._records, lambda r: r.buy_phase).items())
        }

    def cast_rate_by_rank_band(self) -> CastRatesByDimension:
        """CPR broken down by rank band.

        Answers: "Do higher-ranked players deploy their ultimates more consistently?"
        or "Do lower-ranked players hoard their utility?"
        """
        return {
            band: self._rates_for(recs)
            for band, recs in _group_by(self._records, lambda r: _rank_band(r.tier)).items()
        }

    def cast_rate_by_act(self) -> CastRatesByDimension:
        """CPR broken down by act / season.

        Keys are act ID strings (e.g. ``"e9a1"``), matching
        ``match.metadata.season_id``.  Useful for tracking whether utility
        usage patterns shift after ability balance patches.

        Keys use generic slot labels (``"C_cpr"`` etc.) because each act slice
        spans multiple agents.
        """
        return {
            act: self._rates_for(recs)
            for act, recs in sorted(_group_by(self._records, lambda r: r.act).items())
            if act
        }

    def cast_rate_by_act_by_rank(self) -> Dict[str, Dict[str, CastRates]]:
        """CPR nested as ``{act → {rank_band → CastRates}}``.

        Example::

            data = report.utility.cast_rate_by_act_by_rank()
            data["e9a2"]["diamond"]
            # → {"C_cpr": 0.88, "Q_cpr": 0.72, "E_cpr": 0.95, "X_cpr": 0.21}
        """
        result: Dict[str, Dict[str, CastRates]] = {}
        for act, act_recs in sorted(_group_by(self._records, lambda r: r.act).items()):
            if not act:
                continue
            result[act] = {
                _RANK_BAND_KEY.get(band, band.lower()): self._rates_for(recs)
                for band, recs in _group_by(act_recs, lambda r: _rank_band(r.tier)).items()
            }
        return result

    def cast_rate_by_act_by_rank_by_map(self) -> Dict[str, Dict[str, Dict[str, CastRates]]]:
        """CPR nested as ``{act → {rank → {map → CastRates}}}``.

        Example::

            data = report.utility.cast_rate_by_act_by_rank_by_map()
            data["e9a2"]["diamond"]["Ascent"]
            # → {"C_cpr": 0.91, "Q_cpr": 0.68, "E_cpr": 0.97, "X_cpr": 0.19}
        """
        result: Dict[str, Dict[str, Dict[str, CastRates]]] = {}
        for act, act_recs in sorted(_group_by(self._records, lambda r: r.act).items()):
            if not act:
                continue
            by_rank: Dict[str, Dict[str, CastRates]] = {}
            for band, band_recs in _group_by(act_recs, lambda r: _rank_band(r.tier)).items():
                rank_key = _RANK_BAND_KEY.get(band, band.lower())
                by_rank[rank_key] = {
                    m: self._rates_for(map_recs)
                    for m, map_recs in sorted(
                        _group_by(band_recs, lambda r: r.map_name).items()
                    )
                    if m
                }
            result[act] = by_rank
        return result

    def cast_rate_by_act_by_rank_by_map_by_agent(
        self,
    ) -> Dict[str, Dict[str, Dict[str, Dict[str, CastRates]]]]:
        """CPR nested as ``{act → {rank → {map → {agent → CastRates}}}}``.

        When an :class:`AgentAbilityMap` is attached, each agent's leaf dict is
        keyed by real ability names (``"Q – Trailblazer"`` etc.) rather than
        generic slot labels.

        Example::

            data = report.utility.cast_rate_by_act_by_rank_by_map_by_agent()
            data["e9a2"]["diamond"]["Ascent"]["Skye"]
            # → {"C – Guiding Light": 0.91, "Q – Trailblazer": 1.73,
            #    "E – Regrowth": 0.88, "X – Seekers": 0.19}
        """
        result: Dict[str, Dict[str, Dict[str, Dict[str, CastRates]]]] = {}
        for act, act_recs in sorted(_group_by(self._records, lambda r: r.act).items()):
            if not act:
                continue
            by_rank: Dict[str, Dict[str, Dict[str, CastRates]]] = {}
            for band, band_recs in _group_by(act_recs, lambda r: _rank_band(r.tier)).items():
                rank_key = _RANK_BAND_KEY.get(band, band.lower())
                by_map: Dict[str, Dict[str, CastRates]] = {}
                for m, map_recs in sorted(
                    _group_by(band_recs, lambda r: r.map_name).items()
                ):
                    if not m:
                        continue
                    by_map[m] = {
                        agent: self._rates_for(agent_recs, agent=agent)
                        for agent, agent_recs in sorted(
                            _group_by(map_recs, lambda r: r.agent).items()
                        )
                        if agent
                    }
                by_rank[rank_key] = by_map
            result[act] = by_rank
        return result

    def agents_by_slot_usage(self, slot: str = "x") -> List[Tuple[str, float]]:
        """Agents ranked by CPR for a given ability slot, descending.

        Args:
            slot: One of ``"c"``, ``"q"``, ``"e"``, ``"x"`` (case-insensitive).

        Raises:
            ValueError: If *slot* is not one of the four valid values.

        Examples::

            report.utility.agents_by_slot_usage("x")   # ultimate-hungry agents
            report.utility.agents_by_slot_usage("q")   # which agents spam Q most?
            report.utility.agents_by_slot_usage("c")   # signature usage ranking
        """
        slot = slot.lower()
        if slot not in ("c", "q", "e", "x"):
            raise ValueError(f"Invalid slot '{slot}'. Must be one of: c, q, e, x")
        groups: Dict[str, List[_RoundRecord]] = _group_by(self._records, lambda r: r.agent)
        ranked: List[Tuple[str, float]] = [
            (ag, UtilityReport(recs, self._ability_map)._cpr(slot)) for ag, recs in groups.items()
        ]
        return sorted(ranked, key=lambda x: -x[1])


# ---------------------------------------------------------------------------
# SpendReport  — credit economy analysis (separate from cast counts)
# ---------------------------------------------------------------------------

class SpendReport:
    """Credit economy analysis — spend, loadout value, and remaining credits.

    Intentionally kept separate from :class:`UtilityReport` because the API
    bundles all round spend (weapon + armor + ability charges) into a single
    ``spent`` field with **no per-item breakdown**.

    What you *can* infer from spend data:
      - Comparing average ``spent`` between agents running similar weapon/armor
        choices can surface which agents invest more in ability charges (e.g.
        Skye's Q costs 250 cr/charge; Sova's Recon Bolt costs 200 cr/charge).
      - Comparing ``spent`` vs ``loadout_value`` reveals how much of a player's
        loadout is freshly bought vs carried over from surviving previous rounds.
      - High ``remaining`` after buying = saving credits for future rounds.
      - ``match_totals_by_player()`` gives lifetime credit flows per player
        across all matches in the dataset.
    """

    def __init__(self, records: List[_RoundRecord]) -> None:
        self._records: List[_RoundRecord] = records

    def _stats_for(self, recs: List[_RoundRecord]) -> SpendStats:
        """Build a :data:`SpendStats` dict for an arbitrary record slice."""
        if not recs:
            return {
                "avg_spent": 0, "avg_loadout_value": 0,
                "avg_remaining": 0, "sample_rounds": 0,
            }
        return {
            "avg_spent":         _avg([r.spent         for r in recs]),
            "avg_loadout_value": _avg([r.loadout_value for r in recs]),
            "avg_remaining":     _avg([r.remaining     for r in recs]),
            "sample_rounds":     len(recs),
        }

    def buy_phase_distribution(self) -> Dict[str, Dict[str, Union[int, float]]]:
        """Count and percentage of rounds in each buy phase.

        Returns::

            {
                "Pistol":   {"count":  96, "pct":  8.33},
                "Eco":      {"count": 210, "pct": 18.23},
                "Force":    {"count": 185, "pct": 16.06},
                "Full Buy": {"count": 661, "pct": 57.38},
            }
        """
        total: int = len(self._records)
        counts: Dict[str, int] = defaultdict(int)
        for r in self._records:
            counts[r.buy_phase] += 1
        return {
            phase: {"count": counts.get(phase, 0), "pct": _pct(counts.get(phase, 0), total)}
            for phase in ("Pistol", "Eco", "Force", "Full Buy")
        }

    def average_by_phase(self) -> SpendByDimension:
        """Average economy stats per buy phase.

        Answers: "How much do players typically spend on a Force buy?" or
        "What's the average loadout value on Full Buy rounds?"
        """
        return {
            phase: self._stats_for(recs)
            for phase, recs in sorted(_group_by(self._records, lambda r: r.buy_phase).items())
        }

    def average_by_agent(self) -> SpendByDimension:
        """Average economy stats per agent.

        Because ability charge costs are bundled into ``spent``, agents whose
        abilities cost more (e.g. Skye Q at 250 cr/charge, Sova Shock Dart at
        150 cr/charge) will show higher average spend than agents with free
        abilities, *even when holding the same weapon and armor*.  This makes
        ``average_by_agent()`` a useful proxy for "which agents invest most in
        utility charges?" when controlling for buy phase.
        """
        return {
            ag: self._stats_for(recs)
            for ag, recs in sorted(_group_by(self._records, lambda r: r.agent).items())
        }

    def average_by_map(self) -> SpendByDimension:
        """Average economy stats per map.

        Differences across maps may reflect map-specific buy conventions
        or side-economy patterns captured in the dataset.
        """
        return {
            m: self._stats_for(recs)
            for m, recs in sorted(_group_by(self._records, lambda r: r.map_name).items())
        }

    def by_rank_band(self) -> SpendByDimension:
        """Average economy stats per rank band.

        Answers: "Do higher-ranked players spend their credits more aggressively?"
        or "Do lower-ranked players leave more credits on the table?"
        """
        return {
            band: self._stats_for(recs)
            for band, recs in _group_by(self._records, lambda r: _rank_band(r.tier)).items()
        }

    def average_by_act(self) -> SpendByDimension:
        """Average economy stats per act / season.

        Keys are act ID strings (e.g. ``"e9a1"``).  Useful for tracking
        whether economy conventions (e.g. force-buy thresholds, full-buy rates)
        shift across patches or as the player base evolves.
        """
        return {
            act: self._stats_for(recs)
            for act, recs in sorted(_group_by(self._records, lambda r: r.act).items())
            if act
        }

    def average_by_act_by_rank(self) -> Dict[str, Dict[str, SpendStats]]:
        """Economy stats nested as ``{act → {rank_band → SpendStats}}``.

        Example::

            data = report.spend.average_by_act_by_rank()
            data["e9a2"]["diamond"]
            # → {"avg_spent": 3400.0, "avg_loadout_value": 5100.0,
            #    "avg_remaining": 1700.0, "sample_rounds": 410}
        """
        result: Dict[str, Dict[str, SpendStats]] = {}
        for act, act_recs in sorted(_group_by(self._records, lambda r: r.act).items()):
            if not act:
                continue
            result[act] = {
                _RANK_BAND_KEY.get(band, band.lower()): self._stats_for(recs)
                for band, recs in _group_by(act_recs, lambda r: _rank_band(r.tier)).items()
            }
        return result

    def average_by_act_by_rank_by_map(self) -> Dict[str, Dict[str, Dict[str, SpendStats]]]:
        """Economy stats nested as ``{act → {rank → {map → SpendStats}}}``.

        Example::

            data = report.spend.average_by_act_by_rank_by_map()
            data["e9a2"]["diamond"]["Ascent"]
            # → {"avg_spent": 3550.0, "avg_loadout_value": 5300.0,
            #    "avg_remaining": 1750.0, "sample_rounds": 88}
        """
        result: Dict[str, Dict[str, Dict[str, SpendStats]]] = {}
        for act, act_recs in sorted(_group_by(self._records, lambda r: r.act).items()):
            if not act:
                continue
            by_rank: Dict[str, Dict[str, SpendStats]] = {}
            for band, band_recs in _group_by(act_recs, lambda r: _rank_band(r.tier)).items():
                rank_key = _RANK_BAND_KEY.get(band, band.lower())
                by_rank[rank_key] = {
                    m: self._stats_for(map_recs)
                    for m, map_recs in sorted(
                        _group_by(band_recs, lambda r: r.map_name).items()
                    )
                    if m
                }
            result[act] = by_rank
        return result

    def average_by_act_by_rank_by_map_by_agent(
        self,
    ) -> Dict[str, Dict[str, Dict[str, Dict[str, SpendStats]]]]:
        """Economy stats nested as ``{act → {rank → {map → {agent → SpendStats}}}}``.

        Because ability charge costs are bundled into ``spent``, the per-agent
        leaf values at this depth serve as a proxy for how much each agent
        invests in ability charges at a given rank/map/act combination.

        Example::

            data = report.spend.average_by_act_by_rank_by_map_by_agent()
            data["e9a2"]["diamond"]["Ascent"]["Skye"]
            # → {"avg_spent": 3750.0, "avg_loadout_value": 5400.0,
            #    "avg_remaining": 1650.0, "sample_rounds": 14}
        """
        result: Dict[str, Dict[str, Dict[str, Dict[str, SpendStats]]]] = {}
        for act, act_recs in sorted(_group_by(self._records, lambda r: r.act).items()):
            if not act:
                continue
            by_rank: Dict[str, Dict[str, Dict[str, SpendStats]]] = {}
            for band, band_recs in _group_by(act_recs, lambda r: _rank_band(r.tier)).items():
                rank_key = _RANK_BAND_KEY.get(band, band.lower())
                by_map: Dict[str, Dict[str, SpendStats]] = {}
                for m, map_recs in sorted(
                    _group_by(band_recs, lambda r: r.map_name).items()
                ):
                    if not m:
                        continue
                    by_map[m] = {
                        agent: self._stats_for(agent_recs)
                        for agent, agent_recs in sorted(
                            _group_by(map_recs, lambda r: r.agent).items()
                        )
                        if agent
                    }
                by_rank[rank_key] = by_map
            result[act] = by_rank
        return result

    def match_totals_by_player(self) -> PlayerTotals:
        """Per-player match-level spend and cast totals across all matches.

        De-duplicates by (puuid, match_id) before summing, so round-level
        filtering does not inflate match totals.

        Returns a dict keyed by puuid::

            {
                "abc-123": {
                    "player_name":           "Henrik3#EUW3",
                    "matches_played":        12,
                    "total_spent_overall":   714000,
                    "total_loadout_overall": 890000,
                    "total_c_casts":         312,
                    "total_q_casts":         188,
                    "total_e_casts":         345,
                    "total_x_casts":          47,
                },
                ...
            }
        """
        seen: Dict[Tuple[str, str], _RoundRecord] = {}
        for r in self._records:
            key: Tuple[str, str] = (r.puuid, r.match_id)
            if key not in seen:
                seen[key] = r

        player_matches: Dict[str, List[_RoundRecord]] = defaultdict(list)
        for (puuid, _), r in seen.items():
            player_matches[puuid].append(r)

        return {
            puuid: {
                "player_name":           recs[0].player_name,
                "matches_played":        len(recs),
                "total_spent_overall":   sum(r.match_spent_overall   for r in recs),
                "total_loadout_overall": sum(r.match_loadout_overall  for r in recs),
                "total_c_casts":         sum(r.match_c_total          for r in recs),
                "total_q_casts":         sum(r.match_q_total          for r in recs),
                "total_e_casts":         sum(r.match_e_total          for r in recs),
                "total_x_casts":         sum(r.match_x_total          for r in recs),
                "total_rounds":          sum(r.match_rounds_total      for r in recs),
            }
            for puuid, recs in player_matches.items()
        }


# ---------------------------------------------------------------------------
# AnalysisReport  — top-level container
# ---------------------------------------------------------------------------

@dataclass
class AnalysisReport:
    """Container returned by :meth:`MatchAnalyser.analyse`.

    Attributes:
        weapons: Weapon pick-rate analysis (:class:`WeaponReport`).
        shields: Armor purchase analysis (:class:`ShieldReport`).
        utility: Ability cast-rate analysis — CPR per slot (:class:`UtilityReport`).
        spend:   Credit economy analysis — total spend, loadout, remaining
                 (:class:`SpendReport`).  Intentionally separate from ``utility``
                 because the API does not break out spend per ability.
        meta:    Dataset-level counts (matches, rounds, players, agents, maps).
    """
    weapons: WeaponReport
    shields: ShieldReport
    utility: UtilityReport
    spend:   SpendReport
    meta:    Dict[str, Any]

    def summary(self) -> str:
        """Human-readable one-page summary of the dataset."""
        lines: List[str] = [
            "=" * 62,
            " VALORANT MATCH ANALYSIS  —  SUMMARY",
            "=" * 62,
            f"  Matches   : {self.meta['matches']:,}",
            f"  Rounds    : {self.meta['rounds']:,}  (player-rounds)",
            f"  Players   : {self.meta['players']:,}",
            f"  Agents    : {self.meta['agents']:,}",
            f"  Maps      : {', '.join(sorted(self.meta['maps']))}",
            "",
            "── BUY PHASE DISTRIBUTION ───────────────────────────────────",
        ]
        for phase, s in self.spend.buy_phase_distribution().items():
            lines.append(f"  {phase:<12} {s['count']:>7,} rounds   {s['pct']:>5.1f}%")

        lines += ["", "── TOP 10 WEAPONS ───────────────────────────────────────────"]
        for i, (weapon, s) in enumerate(self.weapons.distribution().items()):
            if i >= 10:
                break
            lines.append(f"  {weapon:<24} {s['count']:>6,} rounds   {s['buy_rate_pct']:>5.1f}%")

        lines += ["", "── SHIELD DISTRIBUTION ──────────────────────────────────────"]
        for shield, s in self.shields.distribution().items():
            lines.append(f"  {shield:<24} {s['count']:>6,} rounds   {s['buy_rate_pct']:>5.1f}%")

        lines += [
            "",
            "── UTILITY CAST RATES (casts-per-round) ─────────────────────",
            "   Note: cast counts — credit spend is in the economy section.",
        ]
        cpr: CastRates = self.utility.cast_rates()
        lines.append(f"  C (signature)    {cpr['C_cpr']:>7.4f} CPR")
        lines.append(f"  Q (basic/buy)    {cpr['Q_cpr']:>7.4f} CPR")
        lines.append(f"  E (secondary)    {cpr['E_cpr']:>7.4f} CPR")
        lines.append(f"  X (ultimate)     {cpr['X_cpr']:>7.4f} CPR")

        lines += ["", "── TOP 5 ULTIMATE USERS (X-slot CPR) ───────────────────────"]
        for agent, cpr_val in self.utility.agents_by_slot_usage("x")[:5]:
            ult_name: str = (
                self.utility._ability_map.name(agent, "x")
                if self.utility._ability_map else "X"
            )
            lines.append(f"  {agent:<22} ({ult_name:<20}) {cpr_val:.4f} CPR")

        lines.append("=" * 62)
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# MatchAnalyser  — entry point
# ---------------------------------------------------------------------------

class MatchAnalyser:
    """Analyses a list of ``Match`` objects from api_henrik.py.

    The analyser flattens all match → round → player_stats data into
    ``_RoundRecord`` objects on first use.  All subsequent ``analyse()``
    calls reuse those records — only filtering and aggregation re-runs,
    making repeated sliced queries fast.

    Args:
        matches:     List of :class:`Match` instances to analyse.
        thresholds:  Buy-phase credit thresholds.  Defaults to community-standard
                     values; pass a custom :class:`BuyPhaseThresholds` to override.
        ability_map: Optional :class:`AgentAbilityMap` built from ``ValAssetApi``.
                     When provided, per-agent utility breakdowns are keyed by real
                     ability names (e.g. ``"Q – Trailblazer"``) instead of generic
                     slot labels (``"Q_cpr"``).  Multi-agent slices always use
                     generic labels regardless.  Defaults to ``None`` (generic labels).

    Example::

        from api_valorant_assets import ValAssetApi
        from loadout_lens import MatchAnalyser, AgentAbilityMap, AnalysisFilters

        ability_map = AgentAbilityMap.build()   # one HTTP call; fully cached after
        analyser    = MatchAnalyser(matches, ability_map=ability_map)

        # Full dataset summary
        report = analyser.analyse()
        print(report.summary())

        # Narrow to Diamond+ on Ascent, Full Buy rounds
        report = analyser.analyse(AnalysisFilters(
            maps       = ["Ascent"],
            min_tier   = 16,
            buy_phases = ["Full Buy"],
        ))

        # Named ability keys when ability_map is attached
        print(report.utility.cast_rate_by_agent("Skye"))
        # → {"Skye": {"C – Guiding Light": 0.91, "Q – Trailblazer": 1.73,
        #             "E – Regrowth": 0.88, "X – Seekers": 0.19}}

        # Generic slot keys for multi-agent slices
        print(report.utility.cast_rate_by_map())
        # → {"Ascent": {"C_cpr": 0.80, "Q_cpr": 0.61, ...}, ...}

        # Custom aggregation on raw records
        for rec in analyser.iter_records(AnalysisFilters(buy_phases=["Eco"])):
            print(rec.agent, rec.weapon_name, rec.spent)
    """

    def __init__(
        self,
        matches:          List[Any],
        thresholds:       BuyPhaseThresholds = BuyPhaseThresholds(),
        ability_map:      Optional[AgentAbilityMap] = None,
        excluded_puuids:  Optional[Set[str]] = None,
    ) -> None:
        self._matches:         List[Match]                = matches
        self._thresholds:      BuyPhaseThresholds         = thresholds
        self._ability_map:     Optional[AgentAbilityMap]  = ability_map
        self._excluded_puuids: Set[str]                   = excluded_puuids or set()
        self._records:         List[_RoundRecord]         = []
        self._built:           bool                       = False

    # ── Public interface ──────────────────────────────────────────────────────

    def analyse(self, filters: Optional[AnalysisFilters] = None) -> AnalysisReport:
        """Run the analysis pipeline and return an :class:`AnalysisReport`.

        The first call builds the internal record list (one-time cost, linear
        in total player-rounds).  Subsequent calls with different filters reuse
        the same records and are fast.

        Args:
            filters: Optional :class:`AnalysisFilters`.  ``None`` = full dataset.

        Returns:
            :class:`AnalysisReport` with weapon, shield, utility, and spend sub-reports.
        """
        self._ensure_built()
        records: List[_RoundRecord] = self._apply_filters(self._records, filters)
        return self._build_report(records, ability_map=self._ability_map)

    def iter_records(
        self,
        filters: Optional[AnalysisFilters] = None,
    ) -> Iterator[_RoundRecord]:
        """Yield flat ``_RoundRecord`` objects, optionally filtered.

        Use this when you need a custom aggregation that the built-in report
        classes don't cover — you get the raw atoms without re-implementing
        the parsing and flattening logic.

        Example::

            for rec in analyser.iter_records(AnalysisFilters(agents=["Jett"])):
                print(rec.round_index, rec.weapon_name, rec.x_casts)
        """
        self._ensure_built()
        yield from self._apply_filters(self._records, filters)

    # ── Internal: lazy build ──────────────────────────────────────────────────

    def _ensure_built(self) -> None:
        if not self._built:
            self._build_records()
            self._built = True
            
    def _build_records(self) -> None:
        """Flatten matches → rounds → player_stats into ``_RoundRecord`` objects.

        Performance improvements over the original:

        1. **Per-player match-level data is pre-extracted once** (into
           ``player_cache``) before the round loop, rather than re-resolving
           the same attribute chains on every round the player appears in
           (typically 20–30 rounds per player per match).

        2. **Single round pass** — the original iterated rounds twice: once to
           build ``rounds_per_player``, then again for record building.  Here
           we count appearances during the main pass and back-fill the final
           total after the inner loops complete, eliminating the redundant
           deserialization pass.

        3. **``rounds_list()`` instead of the generator** — avoids re-entering
           the generator and ensures the raw-dict deserialization only happens
           once per round.

        4. **Bound-method caching** — ``self._records.append`` and
           ``self._classify_buy`` are cached as locals before the hot loop to
           skip repeated attribute resolution on every iteration.
        """
        # (match_id, puuid) → final round count for that player in that match.
        # Built during the main pass; used to back-fill match_rounds_total after.
        final_round_counts: dict[tuple[str, str], int] = {}

        # Index of the first record appended during this call, so the back-fill
        # loop below only touches records written here (safe for incremental use).
        start_index: int = len(self._records)

        records_append = self._records.append   # avoid repeated attr lookup in hot loop
        _classify_buy  = self._classify_buy

        for match in self._matches:
            # ── Map + match ID ──────────────────────────────────────────────
            map_name: str = ""
            match_id: str = ""
            act:      str = ""
            try:
                map_name = _str(match.metadata.map)
                match_id = _str(match.metadata.match_id)
                act      = _str(getattr(match.metadata, "season_id", None))
            except Exception:
                pass

            # ── Pre-extract all per-player match-level data ─────────────────
            # Resolving every attribute chain here (once per player per match)
            # avoids repeating it inside the round loop — which iterates over
            # every player for every round (10 players × ~25 rounds = ~250×).

            # Determine which team(s) to exclude: any team that contains an
            # excluded puuid.  This mirrors _get_nonexcluded_players() in
            # agent_stats.py — we exclude the *entire allied team*, not just
            # the single tracked player, so loadout records only represent
            # opponent players (consistent with pick/win/KDA stats).
            excluded_team_ids: set[str] = set()
            for p in match.players:
                if p.puuid in self._excluded_puuids:
                    team_id = getattr(p, "team_id", None)
                    if team_id:
                        excluded_team_ids.add(team_id)

            player_cache: dict[str, tuple] = {}
            for p in match.players:
                puuid = p.puuid

                # Skip the tracked player and every teammate on the same side.
                if puuid in self._excluded_puuids:
                    continue
                team_id = getattr(p, "team_id", None)
                if team_id and team_id in excluded_team_ids:
                    continue

                agent: str = _str(getattr(p, "character",   None))
                tier:  int = _int(getattr(p, "currenttier", None))

                # Exclude unranked / unrated players (tier 0–2).
                # These are not a valid rank band and must not appear anywhere
                # in the tree — mirroring the rank-band definitions in
                # agent_stats._RANK_BANDS which start at tier 3 (Iron).
                if tier <= 2:
                    continue

                match_spent_overall:   int = 0
                match_loadout_overall: int = 0
                match_econ = getattr(p, "economy", None)
                if match_econ is not None:
                    spent_obj   = getattr(match_econ, "spent",         None)
                    loadout_obj = getattr(match_econ, "loadout_value", None)
                    if spent_obj is not None:
                        match_spent_overall   = _int(getattr(spent_obj,   "overall", None))
                    if loadout_obj is not None:
                        match_loadout_overall = _int(getattr(loadout_obj, "overall", None))

                match_c: int = 0
                match_q: int = 0
                match_e: int = 0
                match_x: int = 0
                ability_totals = getattr(p, "ability_casts", None)
                if ability_totals is not None:
                    match_c = _int(getattr(ability_totals, "c_cast", None))
                    match_q = _int(getattr(ability_totals, "q_cast", None))
                    match_e = _int(getattr(ability_totals, "e_cast", None))
                    match_x = _int(getattr(ability_totals, "x_cast", None))

                player_cache[puuid] = (
                    agent, tier,
                    match_spent_overall, match_loadout_overall,
                    match_c, match_q, match_e, match_x,
                )

            # ── Round loop (single pass) ────────────────────────────────────
            # rounds_list() returns the fully-unpacked list, deserialising each
            # MatchRoundsItem only once.  Using enumerate() directly is faster
            # than maintaining a manual counter.
            for round_index, round_obj in enumerate(match.rounds_list):
                for ps in round_obj.player_stats:
                    puuid = ps.player_puuid

                    # Count this player's appearances for match_rounds_total.
                    count_key = (match_id, puuid)
                    if count_key in final_round_counts:
                        final_round_counts[count_key] += 1
                    else:
                        final_round_counts[count_key] = 1

                    # Unpack pre-extracted match-level data (no attribute chains).
                    # player_cache only contains opponent players — excluded player
                    # and their entire allied team were intentionally omitted.
                    # Any puuid absent from the cache must be skipped here too.
                    cached = player_cache.get(puuid)
                    if cached is None:
                        continue
                    (agent, tier,
                     match_spent_overall, match_loadout_overall,
                     match_c, match_q, match_e, match_x) = cached

                    # Round-level economy
                    loadout_value: int = 0
                    spent:         int = 0
                    remaining:     int = 0
                    weapon_name:   str = ""
                    armor_name:    str = ""
                    econ = getattr(ps, "economy", None)
                    if econ is not None:
                        loadout_value = _int(getattr(econ, "loadout_value", None))
                        spent         = _int(getattr(econ, "spent",         None))
                        remaining     = _int(getattr(econ, "remaining",     None))
                        weapon_obj = getattr(econ, "weapon", None)
                        armor_obj  = getattr(econ, "armor",  None)
                        if weapon_obj is not None:
                            weapon_name = _str(getattr(weapon_obj, "name", None))
                        if armor_obj is not None:
                            armor_name  = _str(getattr(armor_obj,  "name", None))

                    # Derive attack/defense side.
                    # In standard Valorant: rounds 0–11 = first half (Red attacks),
                    # rounds 12–23 = second half (Blue attacks).
                    # Overtime (24+) alternates starting with Red attacking again.
                    player_team_str: str = _str(getattr(ps, "player_team", None)).lower()
                    if round_index < 12:
                        _attacking_team = "red"
                    elif round_index < 24:
                        _attacking_team = "blue"
                    else:
                        # Overtime: even OT rounds → Red attacks, odd → Blue attacks
                        _attacking_team = "red" if (round_index - 24) % 2 == 0 else "blue"
                    _side: str = "attack" if player_team_str == _attacking_team else "defense"

                    # match_rounds_total is written as a placeholder (1) here
                    # and back-filled with the correct final count below.
                    
                    records_append(_RoundRecord(
                        match_id      = match_id,
                        round_index   = round_index,
                        puuid         = puuid,
                        player_name   = _str(getattr(ps, "player_display_name", None)),
                        agent         = agent,
                        tier          = tier,
                        map_name      = map_name,
                        act           = act,
                        side          = _side,
                        buy_phase     = _classify_buy(round_index, loadout_value),
                        loadout_value = loadout_value,
                        spent         = spent,
                        remaining     = remaining,
                        weapon_name   = weapon_name,
                        armor_name    = armor_name,
                        match_c_total         = match_c,
                        match_q_total         = match_q,
                        match_e_total         = match_e,
                        match_x_total         = match_x,
                        match_rounds_total    = 1,          # placeholder — fixed below
                        match_spent_overall   = match_spent_overall,
                        match_loadout_overall = match_loadout_overall,
                    ))

        # ── Back-fill match_rounds_total ────────────────────────────────────
        # Now that final_round_counts is fully populated, stamp the correct
        # total onto every record we just appended.  This replaces the original
        # pre-pass (which iterated all rounds a second time just to count).
        for record in self._records[start_index:]:
            count = final_round_counts.get((record.match_id, record.puuid))
            if count is not None:
                record.match_rounds_total = count

    # ── Internal: buy-phase classification ────────────────────────────────────

    def _classify_buy(self, round_index: int, loadout_value: int) -> str:
        """Return the buy-phase label for a given round index and loadout value."""
        if round_index in PISTOL_ROUND_INDICES:
            return "Pistol"
        if loadout_value <= self._thresholds.eco_max:
            return "Eco"
        if loadout_value <= self._thresholds.force_max:
            return "Force"
        return "Full Buy"

    # ── Internal: filtering ───────────────────────────────────────────────────

    @staticmethod
    def _apply_filters(
        records: List[_RoundRecord],
        filters: Optional[AnalysisFilters],
    ) -> List[_RoundRecord]:
        if filters is None:
            return records

        out: List[_RoundRecord] = records

        if filters.maps:
            allowed_maps: Set[str] = {m.lower() for m in filters.maps}
            out = [r for r in out if r.map_name.lower() in allowed_maps]

        if filters.agents:
            allowed_agents: Set[str] = {a.lower() for a in filters.agents}
            out = [r for r in out if r.agent.lower() in allowed_agents]

        if filters.min_tier is not None:
            out = [r for r in out if r.tier >= filters.min_tier]

        if filters.max_tier is not None:
            out = [r for r in out if r.tier <= filters.max_tier]

        if filters.buy_phases:
            allowed_phases: Set[str] = {p.lower() for p in filters.buy_phases}
            out = [r for r in out if r.buy_phase.lower() in allowed_phases]

        if filters.puuids:
            allowed_puuids: Set[str] = set(filters.puuids)
            out = [r for r in out if r.puuid in allowed_puuids]

        if filters.acts:
            allowed_acts: Set[str] = {a.lower() for a in filters.acts}
            out = [r for r in out if r.act.lower() in allowed_acts]

        if filters.sides:
            allowed_sides: Set[str] = {s.lower() for s in filters.sides}
            out = [r for r in out if r.side in allowed_sides]

        return out

    # ── Public: per-agent map / rank slices for the agents screen ────────────

    def agent_loadout_slices(
        self,
        filters: Optional[AnalysisFilters] = None,
    ) -> Dict[str, Dict[str, Any]]:
        """Return per-agent ``by_map``, ``by_rank``, and ``by_act`` loadout breakdowns.

        Produces the slice data consumed by the agents-screen frontend to
        power its Map, Rank, and Act filter sidebar controls.  Each slice has the
        same shape as a ``by_phase`` entry — ``weapons`` and ``shields`` —
        so the existing rendering helpers work without changes.

        Args:
            filters: Optional base filters applied before slicing (e.g. to
                     restrict to a specific buy phase before breaking down
                     by map / rank / act).  ``None`` = full dataset.

        Returns:
            A dict keyed by agent name.  Each value is::

                {
                    "by_map": {
                        "Ascent":  {"weapons": {...}, "shields": {...}},
                        "Bind":    {"weapons": {...}, "shields": {...}},
                        ...
                    },
                    "by_rank": {
                        "iron":    {"weapons": {...}, "shields": {...}},
                        "diamond": {"weapons": {...}, "shields": {...}},
                        ...
                    },
                    "by_act": {
                        "e9a1":   {"weapons": {...}, "shields": {...}},
                        "e9a2":   {"weapons": {...}, "shields": {...}},
                        ...
                    },
                }

        Example::

            slices = analyser.agent_loadout_slices()
            chamber_on_ascent = slices["Chamber"]["by_map"]["Ascent"]
            # → {"weapons": {...}, "shields": {...}}

            diamond_chamber = slices["Chamber"]["by_rank"]["diamond"]
            # → {"weapons": {...}, "shields": {...}}

            chamber_e9a2 = slices["Chamber"]["by_act"]["e9a2"]
            # → {"weapons": {...}, "shields": {...}}

        Typical usage when building ``report_data.js``::

            slices = analyser.agent_loadout_slices()
            for agent_entry in agents_list:
                name = agent_entry["name"]
                if name in slices:
                    agent_entry["by_map"]  = slices[name]["by_map"]
                    agent_entry["by_rank"] = slices[name]["by_rank"]
                    agent_entry["by_act"]  = slices[name]["by_act"]
        """
        self._ensure_built()
        base: List[_RoundRecord] = self._apply_filters(self._records, filters)

        # Group all records by agent first so we only iterate once.
        by_agent: Dict[str, List[_RoundRecord]] = _group_by(base, lambda r: r.agent)

        result: Dict[str, Dict[str, Any]] = {}
        for agent_name, agent_records in by_agent.items():
            if not agent_name:
                continue

            # ── by_map ───────────────────────────────────────────────────────
            by_map: Dict[str, Any] = {}
            for map_name, map_recs in _group_by(agent_records, lambda r: r.map_name).items():
                if not map_name:
                    continue
                by_map[map_name] = self._loadout_slice(map_recs)

            # ── by_rank ──────────────────────────────────────────────────────
            by_rank: Dict[str, Any] = {}
            for band, band_recs in _group_by(agent_records, lambda r: _rank_band(r.tier)).items():
                # Normalise to the same lowercase keys the frontend uses
                # (iron, bronze, silver, gold, plat, diamond, ascend,
                #  immortal, radiant, unranked).
                band_key = _RANK_BAND_KEY.get(band, band.lower())
                by_rank[band_key] = self._loadout_slice(band_recs)

            # ── by_act ───────────────────────────────────────────────────────
            by_act: Dict[str, Any] = {}
            for act_name, act_recs in _group_by(agent_records, lambda r: r.act).items():
                if not act_name:
                    continue
                by_act[act_name] = self._loadout_slice(act_recs)

            result[agent_name] = {"by_map": by_map, "by_rank": by_rank, "by_act": by_act}

        return result

    def agent_nested_tree(
        self,
        filters: Optional[AnalysisFilters] = None,
        match_stats: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Build the single unified ``agent → map → rank`` tree with ALL statistics.

        Every rank node contains every statistic in one place — no second tree
        to cross-reference.  The shape at each rank node is::

            {
                # Match-level stats (picks, win rate, KDA, etc.)
                # Merged from match_stats if supplied; omitted otherwise.
                "picks":                int,
                "win_rate":             float,
                "non_mirror_win_rate":  float | null,
                "kda":                  float,
                "teams":                int,
                "team_percentage":      float,
                "matches_seen":         int,
                "matches_seen_percentage": float,
                "non_mirror_picks":     int,

                # Utility CPR — at rank level because the API only exposes
                # match-level cast totals; CPR is identical across all phases
                # of the same player-match so repeating it per-phase is wrong.
                "utility": { "c": float, "q": float, "e": float, "x": float },

                # Per-phase loadout breakdown.
                # weapons / shields ARE meaningfully phase-specific.
                "by_phase": {
                    "Pistol":   { "weapons": {...}, "shields": {...},
                                  "sample_rounds": int },
                    "Eco":      { ... },
                    "Force":    { ... },
                    "Full Buy": { ... },
                }
            }

        Map nodes carry an ``overall`` pre-roll (all ranks × all phases for
        this agent × map) plus ``by_rank``.  Agent nodes carry ``overall``
        (all maps × ranks × phases) plus ``by_map``.

        Waterfall examples::

            # Exact leaf — Jett, Force, Bind, Gold:
            tree["Jett"]["by_map"]["Bind"]["by_rank"]["gold"]["by_phase"]["Force"]

            # Roll up ranks — Jett, Force, Bind, all ranks:
            avg(tree["Jett"]["by_map"]["Bind"]["by_rank"][r]["by_phase"]["Force"]
                for r in tree["Jett"]["by_map"]["Bind"]["by_rank"])

            # Roll up maps + ranks — Jett, Force, all maps:
            avg(tree["Jett"]["by_map"][m]["by_rank"][r]["by_phase"]["Force"]
                for m in tree["Jett"]["by_map"]
                for r in tree["Jett"]["by_map"][m]["by_rank"])

            # Pre-rolled shortcut — Jett on Bind regardless of rank/phase:
            tree["Jett"]["by_map"]["Bind"]["overall"]

        Args:
            filters:     Optional base filters (e.g. restrict puuids/acts).
            match_stats: Output of ``build_agent_stats_nested()``.  When
                         supplied, pick/win/KDA fields are merged into every
                         rank node and overall node.

        Returns:
            Nested dict as described.  Empty intersections are omitted.
        """
        
        self._ensure_built()
        base: List[_RoundRecord] = self._apply_filters(self._records, filters)

        PHASES: List[str] = ["Pistol", "Eco", "Force", "Full Buy"]

        # Convenience: pull match-stat dicts by path so merge code is readable.
        ms_by_agent: Dict[str, Any] = (match_stats or {}).get("by_agent", {})

        def _utility(records: List[_RoundRecord]) -> Dict[str, float]:
            rep = UtilityReport(records, self._ability_map)
            return {
                "c": rep._cpr("c"), "q": rep._cpr("q"),
                "e": rep._cpr("e"), "x": rep._cpr("x"),
            }

        # def _overall_node(
        #     records: List[_RoundRecord],
        #     ms: Optional[Dict[str, Any]] = None,
        # ) -> Dict[str, Any]:
        #     """Aggregate node: all metrics, no phase breakdown."""
        #     node: Dict[str, Any] = {
        #         "weapons":       WeaponReport(records).distribution(),
        #         "shields":       ShieldReport(records).distribution(),
        #         "utility":       _utility(records),
        #         "sample_rounds": len(records),
        #     }
        #     if ms:
        #         node.update(ms)
        #     return node

        def _phase_leaf(records: List[_RoundRecord]) -> Dict[str, Any]:
            """Phase leaf: weapons + shields + spend (utility lives on rank node)."""
            spend = SpendReport(records)
            return {
                "weapons":       WeaponReport(records).distribution(),
                "shields":       ShieldReport(records).distribution(),
                "sample_rounds": len(records),
                "spend":         spend._stats_for(records),
            }

        by_agent_recs: Dict[str, List[_RoundRecord]] = _group_by(base, lambda r: r.agent)
        tree: Dict[str, Any] = {}

        for agent_name, agent_recs in by_agent_recs.items():
            if not agent_name:
                continue

            # Only build a tree entry for agents that have pick data in
            # match_stats.  If match_stats was not supplied we fall through
            # (legacy / no-exclusion usage); when it *is* supplied, an absent
            # entry means this agent appeared only on the allied team (which
            # _build_records now filters out) and should produce no output.
            if ms_by_agent and agent_name not in ms_by_agent:
                continue

            ms_agent = ms_by_agent.get(agent_name, {})

            by_map_node: Dict[str, Any] = {}
            for map_name, map_recs in _group_by(agent_recs, lambda r: r.map_name).items():
                if not map_name:
                    continue

                ms_map = ms_agent.get("by_map", {}).get(map_name, {})

                by_rank_node: Dict[str, Any] = {}
                for band, band_recs in _group_by(map_recs, lambda r: _rank_band(r.tier)).items():
                    rank_key = _RANK_BAND_KEY.get(band, band.lower())

                    ms_rank = ms_map.get("by_rank", {}).get(rank_key, {})

                    by_phase_node: Dict[str, Any] = {}
                    for phase in PHASES:
                        phase_recs = [r for r in band_recs if r.buy_phase == phase]
                        if phase_recs:
                            by_phase_node[phase] = _phase_leaf(phase_recs)

                    if by_phase_node:
                        rank_node: Dict[str, Any] = {
                            "utility":  _utility(band_recs),
                            "by_phase": by_phase_node,
                        }
                        if ms_rank:
                            rank_node.update(ms_rank)
                        by_rank_node[rank_key] = rank_node

                if by_rank_node:
                    by_map_node[map_name] = {
                        # "overall": _overall_node(map_recs, ms_map.get("overall")),
                        "by_rank": by_rank_node,
                    }

            if by_map_node:
                tree[agent_name] = {
                    # "overall": _overall_node(agent_recs, ms_agent.get("overall")),
                    "by_map":  by_map_node,
                }

        return tree

    @staticmethod
    def _loadout_slice(records: List[_RoundRecord]) -> Dict[str, Any]:
        """Build a ``{weapons, shields}`` slice dict for *records*.

        This is the same shape as each entry in an agent's ``by_phase`` dict,
        so the agents-screen rendering helpers work unchanged for map / rank
        slices too.
        """
        return {
            "weapons": WeaponReport(records).distribution(),
            "shields": ShieldReport(records).distribution(),
        }

    # ── Internal: report construction ─────────────────────────────────────────

    def meta_precompute(
        self,
        filters: Optional[AnalysisFilters] = None,
    ) -> Dict[str, Any]:
        """Pre-compute all analysis dimensions sliced by map, rank, act, and phase.

        Returns a nested dict that the ``report_data.js`` frontend can look up at
        render time without round-tripping to Python.  All combinations are
        pre-built so the JS simply indexes into the structure with the current
        filter state.

        The returned dict has this shape::

            {
                "maps":  ["Ascent", "Bind", ...],          # sorted list of all maps
                "ranks": ["iron", "bronze", ..., "radiant"],
                "acts":  ["e9a1", "e9a2", ...],            # season_id strings, sorted
                "phases": ["Pistol", "Eco", "Force", "Full Buy"],
                "weapons": {
                    "<map>|<rank>|<act>|<phase>": {
                        "Vandal": {"count": 412, "buy_rate_pct": 42.0}, ...
                    },
                    ...
                },
                "shields": {
                    "<map>|<rank>|<act>|<phase>": {
                        "Heavy Armor": {"count": 200, "buy_rate_pct": 55.0}, ...
                    },
                    ...
                },
                "utility": {
                    "<map>|<rank>|<act>|<phase>": {
                        "C_cpr": 0.82, "Q_cpr": 0.61, "E_cpr": 0.94, "X_cpr": 0.17
                    },
                    ...
                },
            }

        The special sentinel ``"__all__"`` is used for "no filter on this dimension",
        so the key ``"__all__|diamond|__all__|Full Buy"`` means Diamond rank, all maps,
        all acts, Full Buy phase.

        Args:
            filters: Optional base filters (e.g. restrict to specific puuids) applied
                     before slicing.  ``None`` = full dataset.

        Example (Python consumer)::

            pre = analyser.meta_precompute()
            key = "Ascent|diamond|__all__|Full Buy"
            print(pre["weapons"][key])
            print(pre["utility"][key])

        Example (JS consumer using the generated report_data.js)::

            function metaKey(map, rank, act, phase) {
                return [map||'__all__', rank||'__all__', act||'__all__', phase||'Full Buy'].join('|');
            }
            var weapons = metaData.precompute.weapons[metaKey(metaMap, activeRank, metaAct, metaPhase)];
            var utility = metaData.precompute.utility[metaKey(metaMap, activeRank, metaAct, metaPhase)];
        """
        self._ensure_built()
        base: List[_RoundRecord] = self._apply_filters(self._records, filters)

        ALL = "__all__"

        maps_present:  List[str] = sorted({r.map_name for r in base if r.map_name})
        ranks_present: List[str] = sorted(
            {_RANK_BAND_KEY.get(_rank_band(r.tier), "unranked") for r in base},
            key=lambda k: ["iron","bronze","silver","gold","plat","diamond","ascend","immortal","radiant","unranked"].index(k)
                if k in ["iron","bronze","silver","gold","plat","diamond","ascend","immortal","radiant","unranked"] else 99,
        )
        acts_present:  List[str] = sorted({r.act for r in base if r.act})
        phases:        List[str] = ["Pistol", "Eco", "Force", "Full Buy"]

        # Dimension value lists to iterate — always include ALL sentinel
        map_vals  = [ALL] + maps_present
        rank_vals = [ALL] + ranks_present
        act_vals  = [ALL] + acts_present

        weapons_index: Dict[str, Any] = {}
        shields_index: Dict[str, Any] = {}
        utility_index: Dict[str, Any] = {}

        for map_val in map_vals:
            map_recs = base if map_val == ALL else [r for r in base if r.map_name == map_val]
            for rank_val in rank_vals:
                rank_recs = map_recs if rank_val == ALL else [
                    r for r in map_recs
                    if _RANK_BAND_KEY.get(_rank_band(r.tier), "unranked") == rank_val
                ]
                for act_val in act_vals:
                    act_recs = rank_recs if act_val == ALL else [r for r in rank_recs if r.act == act_val]
                    for phase in phases:
                        phase_recs = [r for r in act_recs if r.buy_phase == phase]
                        key = f"{map_val}|{rank_val}|{act_val}|{phase}"
                        weapons_index[key] = WeaponReport(phase_recs).distribution()
                        shields_index[key] = ShieldReport(phase_recs).distribution()
                        utility_index[key] = UtilityReport(phase_recs, self._ability_map)._rates_for(phase_recs)

        return {
            "maps":    maps_present,
            "ranks":   ranks_present,
            "acts":    acts_present,
            "phases":  phases,
            "weapons": weapons_index,
            "shields": shields_index,
            "utility": utility_index,
        }

    @staticmethod
    def _build_report(
        records: List[_RoundRecord],
        ability_map: Optional[AgentAbilityMap] = None,
    ) -> AnalysisReport:
        meta: Dict[str, Any] = {
            "matches": len({r.match_id  for r in records}),
            "rounds":  len(records),
            "players": len({r.puuid     for r in records}),
            "agents":  len({r.agent     for r in records}),
            "maps":    {r.map_name      for r in records},
            "acts":    sorted({r.act    for r in records if r.act}),
        }
        return AnalysisReport(
            weapons = WeaponReport(records),
            shields = ShieldReport(records),
            utility = UtilityReport(records, ability_map=ability_map),
            spend   = SpendReport(records),
            meta    = meta,
        )