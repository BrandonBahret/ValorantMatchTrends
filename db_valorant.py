"""
db_valorant.py

High-level data layer for Valorant player/match analytics.
Wraps the unofficial Henrik API and a local JSON cache (api_cache.Cache)
to provide profile lookups, seasonal performance summaries, and
incremental match-history updates.
"""

from __future__ import annotations

import inspect
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from functools import cached_property as lazy_property
from typing import Dict, Iterator, List, Optional, Tuple

from tqdm import tqdm

import rank_utils
from api_cache import Cache
from api_henrik import (
    AffinitiesEnum,
    BySeason,
    BySeasonActRankWinsItem,
    Match,
    MatchReference,
    UnofficialApi,
    V1AccountCard,
    V1LifetimeMmrHistoryItem,
)
from jsoninjest import UNSET


# ---------------------------------------------------------------------------
# Season / act helpers
# ---------------------------------------------------------------------------

class SeasonPerformance:
    """
    Wraps a single season entry from the ``by_season`` API payload and
    exposes convenient rank/win-rate properties.

    The season string follows the pattern ``e<episode>a<act>``
    (e.g. ``"e5a2"`` → Episode 5, Act 2).

    Season index formula: ``(episode - 1) * 3 + act``
    Maps e1a1 → 1, e1a2 → 2, …, e8a2 → 23.
    """

    has_data: bool
    season_str: str
    episode: int
    act: int
    season_index: int
    wins: int
    games_played: int
    win_rate: float
    peak_act_rank: str
    starting_act_rank: str
    rank_at_end: str

    def __init__(self, by_season_item: Tuple[str, dict]) -> None:
        episode_act_str, season_data = by_season_item
        season = BySeason(season_data)

        self.has_data = season.error == UNSET
        if not self.has_data:
            return

        # Parse season identifier (e.g. "e5a2")
        self.season_str = episode_act_str
        act_index = episode_act_str.index("a")
        self.episode = int(episode_act_str[1:act_index])
        self.act = int(episode_act_str[act_index + 1:])
        self.season_index = (self.episode - 1) * 3 + self.act

        self.wins = season.wins
        self.games_played = season.number_of_games
        self.win_rate = season.wins / season.number_of_games

        # Build a tier → placement map, then sort descending by tier number.
        placements: Dict[int, BySeasonActRankWinsItem] = {
            entry.tier: entry for entry in season.act_rank_wins
        }
        sorted_placements: List[Tuple[int, BySeasonActRankWinsItem]] = sorted(
            placements.items(), reverse=True
        )

        self.peak_act_rank = sorted_placements[0][1].patched_tier
        """Highest rank reached during this act."""

        self.starting_act_rank = sorted_placements[-1][1].patched_tier
        """First known placement for this act."""

        self.rank_at_end = season.final_rank_patched
        """Rank at the very end of the act."""

        # If the lowest observed placement was Unrated, use the next one up
        # (index -2) so we report a meaningful starting rank when available.
        if self.starting_act_rank == "Unrated" and len(sorted_placements) > 2:
            self.starting_act_rank = sorted_placements[-2][1].patched_tier


# ---------------------------------------------------------------------------
# Player identification & current rank
# ---------------------------------------------------------------------------

class PlayerIdentification:
    """Resolves a PUUID to a human-readable Riot ID (name + tag)."""

    puuid: str
    name: str
    tag: str
    full_name: str  # "Name#TAG"

    def __init__(self, puuid: str) -> None:
        api = UnofficialApi()
        account = api.get_account_by_puuid(puuid)

        self.puuid = account.puuid
        self.name = account.name
        self.tag = account.tag
        self.full_name = f"{account.name}#{account.tag}"


class PlayerCurrentTier:
    """Fetches a player's live rank, RR, ELO, and account level."""

    peak_rank: str
    rank: str
    rank_rating: int
    rank_images: object  # vendor type from api_henrik
    elo: int
    level: int

    def __init__(self, puuid: str) -> None:
        api = UnofficialApi()
        account = api.get_account_by_puuid(puuid)
        perf = api.get_act_performance_by_puuid(puuid, account.region)

        self.peak_rank = perf.highest_rank.patched_tier
        self.rank = perf.current_data.currenttier_patched
        self.rank_rating = perf.current_data.ranking_in_tier
        self.rank_images = perf.current_data.images
        self.elo = perf.current_data.elo
        self.level = account.account_level


# ---------------------------------------------------------------------------
# Player profile (lazy-loaded aggregate)
# ---------------------------------------------------------------------------

class PlayerProfile:
    """
    Aggregated view of a single player built from cached match/MMR data.

    Heavy properties (``identity``, ``current``, ``artwork``, ``region``,
    ``seasonal_performances``) are resolved lazily on first access so we
    avoid unnecessary API calls.

    Parameters
    ----------
    puuid:
        The player's Riot PUUID.
    history:
        Mapping of ``match_id → V1LifetimeMmrHistoryItem`` for every match
        stored in the local cache for this player.
    """

    def __init__(
        self,
        puuid: str,
        history: Dict[str, Optional[V1LifetimeMmrHistoryItem]],
    ) -> None:
        self.api: UnofficialApi = UnofficialApi()
        self._puuid = puuid
        self.match_history = history

    # -- Lazy properties (resolved once, then cached on the instance) --------

    @lazy_property
    def identity(self) -> PlayerIdentification:
        """Riot ID (name + tag) for this player."""
        return PlayerIdentification(self._puuid)

    @lazy_property
    def current(self) -> PlayerCurrentTier:
        """Live rank, RR, ELO, and account level."""
        return PlayerCurrentTier(self._puuid)

    @lazy_property
    def artwork(self) -> V1AccountCard:
        """Player card artwork associated with the account."""
        account = self.api.get_account_by_puuid(self._puuid)
        return account.card

    @lazy_property
    def region(self) -> str:
        """The server region the account is registered on (e.g. ``"na"``)."""
        account = self.api.get_account_by_puuid(self._puuid)
        return account.region

    @lazy_property
    def seasonal_performances(self) -> List[SeasonPerformance]:
        """
        Season performances sorted newest-first.

        Only seasons that contain valid data (``has_data=True``) are included.
        """
        perf = self.api.get_act_performance_by_puuid(self._puuid, self.region)
        raw_seasons = perf.by_season.as_dict().items()

        performances = [
            sp
            for entry in raw_seasons
            if (sp := SeasonPerformance(entry)).has_data
        ]
        return sorted(performances, key=lambda sp: sp.season_index, reverse=True)

    # -- Regular properties --------------------------------------------------

    @property
    def puuid(self) -> str:
        """The player's Riot PUUID."""
        return self._puuid

    @property
    def peak_rank(self) -> str:
        """
        All-time peak rank derived from seasonal peak placements.

        Converts each season's peak act rank to a float via ``rank_utils``,
        takes the maximum, then maps back to a rank string.
        """
        season_peaks = [sp.peak_act_rank for sp in self.seasonal_performances]
        peak_floats = map(rank_utils.map_rank_to_float, season_peaks)
        return rank_utils.reverse_map_valorant_rank(max(peak_floats), include_rr=False)

    @property
    def match_ids(self) -> List[str]:
        """Ordered list of match IDs present in this player's local history."""
        return list(self.match_history.keys())

    # -- Methods -------------------------------------------------------------

    def get_match_mmr(self, match_id: str) -> Optional[V1LifetimeMmrHistoryItem]:
        """Return the cached MMR snapshot for a specific match, or ``None``."""
        return self.match_history[match_id]


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def _get_lazy_property_names(cls: type) -> List[str]:
    """
    Return the names of all ``cached_property`` (``lazy_property``) attributes
    defined on *cls*.  Used to invalidate the instance cache selectively.
    """
    return [
        name
        for name, attr in inspect.getmembers(cls)
        if isinstance(attr, lazy_property)
    ]


# ---------------------------------------------------------------------------
# Main database / orchestration class
# ---------------------------------------------------------------------------

class ValorantDB:
    """
    Central data access object for Valorant analytics.

    Persists player and match metadata to a local JSON file via ``Cache``.
    Lazy properties cache deserialized objects in memory; call
    ``invalidate_lazy_props()`` after any write operation to keep state
    consistent.

    Parameters
    ----------
    region:
        Riot region code used when querying the API (default ``"na"``).
    """

    def __init__(self, region: AffinitiesEnum = "na") -> None:
        self.region: AffinitiesEnum = region
        self._dao: Cache = Cache("data/analysis.json")

    # -- Cache management ----------------------------------------------------

    def invalidate_lazy_props(self) -> None:
        """Evict all cached lazy properties so the next access re-fetches them."""
        for prop in _get_lazy_property_names(ValorantDB):
            self.__dict__.pop(prop, None)

    def invalidate_property(self, prop: str) -> None:
        """
        Evict a single cached lazy property by name.

        Raises ``KeyError`` if *prop* is not a lazy property on this class.
        """
        if prop not in _get_lazy_property_names(ValorantDB):
            raise KeyError(f"{prop!r} is not a lazy-loaded property on ValorantDB.")
        self.__dict__.pop(prop, None)

    # -- Lazy data accessors -------------------------------------------------

    @lazy_property
    def available_matches(self) -> Dict[str, MatchReference]:
        """All matches stored in the local cache, keyed by match ID."""
        raw: dict = self._dao.get_object("matches", {})
        return {k: MatchReference(v) for k, v in raw.items()}

    @lazy_property
    def available_matches_list(self) -> List[str]:
        """Ordered list of match IDs present in the local cache."""
        return list(self.available_matches.keys())

    @lazy_property
    def players(self) -> Dict[str, Dict[str, Optional[V1LifetimeMmrHistoryItem]]]:
        """
        Player → match history mapping loaded from cache.

        Raw ``dict`` payloads for ``mmr`` entries are promoted to
        ``V1LifetimeMmrHistoryItem`` objects in-place on first access.
        """
        raw: dict = self._dao.get_object("players", {})
        players: Dict[str, dict] = defaultdict(dict, raw)

        for history in players.values():
            for metadata in history.values():
                mmr = metadata.get("mmr")
                if mmr is not None and not isinstance(mmr, V1LifetimeMmrHistoryItem):
                    metadata["mmr"] = V1LifetimeMmrHistoryItem(mmr)

        return players

    @lazy_property
    def available_players(self) -> List[str]:
        """PUUIDs of all players present in the local cache."""
        raw: dict = self._dao.get_object("players", {})
        return list(raw.keys())

    # -- Read helpers --------------------------------------------------------

    def get_match(self, match_id: str) -> Match:
        """Fetch full match data from the API for the given *match_id*."""
        return UnofficialApi().get_match_by_id(match_id)

    def get_profile_by_name(self, name: str, tag: str) -> PlayerProfile:
        """
        Look up a player by their Riot ID (``name#tag``).

        If the player is not yet in the local cache, their match history is
        fetched and persisted before the profile is returned.
        """
        api = UnofficialApi()
        account = api.get_account_by_name(name, tag)
        puuid = account.puuid

        if puuid not in self.available_players:
            self.update_match_history_for_puuid(puuid)

        history = self._build_mmr_history(puuid)
        return PlayerProfile(puuid, history)

    def get_profile_by_puuid(self, puuid: str) -> PlayerProfile:
        """
        Look up a player by their PUUID.

        If the player is not yet in the local cache, their match history is
        fetched and persisted before the profile is returned.
        """
        if puuid not in self.players:
            self.update_match_history_for_puuid(puuid)

        history = self._build_mmr_history(puuid)
        return PlayerProfile(puuid, history)

    def _build_mmr_history(
        self, puuid: str
    ) -> Dict[str, V1LifetimeMmrHistoryItem]:
        """
        Build the ``match_id → MMR item`` dict passed to ``PlayerProfile``.

        Skips entries where ``mmr`` is ``None`` (match was recorded but MMR
        data was never fetched for it).
        """
        return {
            match_id: (
                meta["mmr"]
                if isinstance(meta["mmr"], V1LifetimeMmrHistoryItem)
                else V1LifetimeMmrHistoryItem(meta["mmr"])
            )
            for match_id, meta in self.players[puuid].items()
            if meta["mmr"] is not None
        }

    # -- Write / update operations -------------------------------------------

    def update_match_history_for_puuid(self, puuid: str) -> None:
        """
        Fetch recent matches and MMR history for *puuid* from the API and
        merge them into the local cache.

        Any data loaded via lazy properties before this call should be
        treated as stale; call ``invalidate_lazy_props()`` to refresh them.
        """
        api = UnofficialApi()

        # Load current state from cache (may use already-hot lazy props)
        players = self.players
        matches = self.available_matches

        # --- Merge match references -----------------------------------------
        for ref in api.get_match_history(puuid, region=self.region):
            matches[ref.match_id] = ref
            # Preserve any existing metadata; only create a stub if absent.
            players[puuid].setdefault(ref.match_id, {"mmr": None})

        # --- Merge MMR snapshots --------------------------------------------
        for mmr in api.get_recent_mmr_history_by_puuid(puuid, region=self.region).data:
            players[puuid][mmr.match_id] = {"mmr": mmr}

        self._save_players(players)
        self._save_matches(matches)

    def download_missing_match_data(self) -> None:
        """
        Download full match data for every match ID in the local cache using a
        thread pool.

        Processes matches in chunks of ``CHUNK_SIZE`` to limit peak memory
        usage.  Results are currently fetched but not persisted — extend this
        method to store or process ``m`` as needed.
        """
        NUM_THREADS = 10
        CHUNK_SIZE = 100

        api = UnofficialApi()
        match_ids = self.available_matches_list

        def fetch_match(match_id: str) -> Match:
            return api.get_match_by_id(match_id)

        # Chunk the list so we don't submit all futures at once
        chunks: Iterator[List[str]] = (
            match_ids[i: i + CHUNK_SIZE]
            for i in range(0, len(match_ids), CHUNK_SIZE)
        )

        with ThreadPoolExecutor(max_workers=NUM_THREADS) as executor:
            for chunk in chunks:
                futures = {mid: executor.submit(fetch_match, mid) for mid in chunk}
                for match_id in tqdm(chunk):
                    match: Match = futures[match_id].result()
                    # TODO: persist or analyse `match` here

    def update_match_history_for_my_profile(self) -> None:
        """
        Convenience method that refreshes match history for the author's
        account (``bbahret#001``) and then fetches history for every opponent
        or teammate encountered in those matches who is not already cached.

        Lazy properties are invalidated between players to ensure each lookup
        uses fresh data.
        """
        api = UnofficialApi()
        profile = self.get_profile_by_name("bbahret", "001")
        self.update_match_history_for_puuid(profile.puuid)

        for match_id in profile.match_ids:
            self.invalidate_lazy_props()
            known_players = self.players
            match = api.get_match_by_id(match_id)

            for player in match.players:
                if player.puuid not in known_players:
                    self.update_match_history_for_puuid(player.puuid)

    # -- Private persistence helpers -----------------------------------------

    def _save_players(
        self, players: Dict[str, Dict[str, dict]]
    ) -> None:
        """Serialize ``V1LifetimeMmrHistoryItem`` objects and write to cache."""
        for history in players.values():
            for metadata in history.values():
                if isinstance(metadata["mmr"], V1LifetimeMmrHistoryItem):
                    metadata["mmr"] = metadata["mmr"].as_dict()

        self._dao.store("players", players, cast=dict)

    def _save_matches(
        self, matches: Dict[str, MatchReference]
    ) -> None:
        """Serialize ``MatchReference`` objects and write to cache."""
        serialized = {mid: ref.as_dict() for mid, ref in matches.items()}
        self._dao.store("matches", serialized, cast=dict)