"""
api_valorant_assets.py

Wrapper around the unofficial Valorant Assets API (https://valorant-api.com).
Provides typed model classes for game assets (agents, maps, seasons, etc.)
and a singleton API client with built-in caching and rate-limit handling.
"""

import inspect
import math
import time
from datetime import datetime
from enum import Enum
from functools import cached_property as lazy_property
from typing import Dict, List, Optional

import requests

from agent_name_enum import AgentName
from api_cache import Cache
from api_request_logger import RequestLogger
from jsoninjest import JsonInjester
from utils import singleton


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

REQUESTS_LOG: str = "logs/assets-api-requests.log"
CACHE_FILE: str = "caches/assets-api-cache.pkl"

RATE_LIMIT: int = 90   # maximum requests allowed …
RATE_PER: int = 60     # … per this many seconds


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def build_url(url: str, required_params: dict, optional_params: dict) -> str:
    """
    Build a full URL by interpolating required path params and appending
    non-None optional params as a query string.

    Enum values are automatically unwrapped to their `.value` before use.

    Args:
        url: URL template that may contain `{key}` placeholders for required params.
        required_params: Mapping of placeholder names to values; inserted into the path.
        optional_params: Mapping of query-string keys to values; None values are skipped.

    Returns:
        The fully-formed URL string.
    """
    extract_enum = lambda e: e.value if isinstance(e, Enum) else e

    required_params = {k: extract_enum(v) for k, v in required_params.items()}
    optional_params = {k: extract_enum(v) for k, v in optional_params.items()}

    query_started = False
    for param, value in optional_params.items():
        if value is not None:
            separator = "?" if not query_started else "&"
            url += f"{separator}{param}={value}"
            query_started = True

    return url.format(**required_params)


def get_lazy_properties(cls) -> List[str]:
    """Return the names of all `cached_property` members defined on *cls*."""
    return [
        name
        for name, attr in inspect.getmembers(cls)
        if isinstance(attr, lazy_property)
    ]


class RateException(Exception):
    """Raised when the API rate limit has been reached."""


# ---------------------------------------------------------------------------
# API Client
# ---------------------------------------------------------------------------

@singleton
class ValAssetApi:
    """
    Singleton client for the Valorant Assets API.

    Handles request caching, rate-limit enforcement, and version staleness
    detection. Lazy-loaded properties (agents, maps, etc.) are computed once
    and cached in-process; call `invalidate_lazy_props()` to force a refresh.
    """

    def __init__(
        self,
        language: Optional[str] = "en-US",
        cache_filepath: Optional[str] = None,
        requests_log: Optional[str] = None,
    ) -> None:
        self.__base_uri = "https://valorant-api.com"

        self.cache = Cache(cache_filepath or CACHE_FILE)
        self.logger = RequestLogger(requests_log or REQUESTS_LOG)
        self.language = language

        # Sorted list of valid agent display names (used for validation elsewhere).
        self.agent_names: List[str] = sorted([e.value for e in AgentName])

        # Fetch and store the current game/API version.
        version_data = self.fetch(self.__base_uri + "/v1/version", expiry=math.inf)
        self.version = AssetsApiVersioning(version_data["data"])

        # If the remote version has changed since the cache was written, start fresh.
        if self.is_data_stale():
            self.cache.completely_erase_cache()

    # ------------------------------------------------------------------
    # Cache / rate-limit helpers
    # ------------------------------------------------------------------

    def invalidate_lazy_props(self) -> None:
        """Delete all in-process cached_property values, forcing re-computation."""
        for prop in get_lazy_properties(ValAssetApi):
            self.__dict__.pop(prop, None)

    @property
    def is_rate_limit_reached(self) -> bool:
        """True if the number of requests in the current window equals the cap."""
        return len(self.logger.get_logs_from_last_seconds(RATE_PER)) >= RATE_LIMIT

    @property
    def quota_usage(self) -> float:
        """Fraction of the rate-limit window currently consumed (0.0 – 1.0)."""
        return len(self.logger.get_logs_from_last_seconds(RATE_PER)) / RATE_LIMIT

    @property
    def time_until_limit_reset(self) -> float:
        """Seconds remaining until the oldest in-window request ages out."""
        recent = self.logger.get_logs_from_last_seconds(RATE_PER)
        if not recent:
            return 0.0

        oldest_ts: datetime = recent[0][0]
        target = oldest_ts + datetime.timedelta(seconds=RATE_PER)
        return (target - datetime.now()).total_seconds()

    def wait_until_limit_reset(self) -> None:
        """Block until the rate-limit window resets (with a 1-second buffer)."""
        time.sleep(self.time_until_limit_reset + 1)

    # ------------------------------------------------------------------
    # Low-level HTTP
    # ------------------------------------------------------------------

    def uncached_fetch(self, uri: str, payload: Optional[dict] = None) -> requests.Response:
        """
        Perform a GET request, bypassing the cache.

        Raises:
            RateException: If the rate limit has been reached.
            HTTPError: If the response status is 4xx or 5xx.
        """
        if self.is_rate_limit_reached:
            raise RateException(
                f"Rate limit reached! You must wait {self.time_until_limit_reset:.1f} seconds."
            )
        response = requests.get(uri, json=payload)
        response.raise_for_status()
        self.logger.log(uri, response.status_code)
        return response

    def uncached_post(self, uri: str, payload: Optional[dict] = None) -> requests.Response:
        """
        Perform a POST request, bypassing the cache.

        Raises:
            RateException: If the rate limit has been reached.
            HTTPError: If the response status is 4xx or 5xx.
        """
        if self.is_rate_limit_reached:
            raise RateException(
                f"Rate limit reached! You must wait {self.time_until_limit_reset:.1f} seconds."
            )
        response = requests.post(uri, json=payload)
        response.raise_for_status()
        self.logger.log(uri, response.status_code)
        return response

    def fetch(self, uri: str, expiry: int, force_update: bool = False) -> dict:
        """
        Fetch *uri*, serving from cache when the cached record is still fresh.

        Args:
            uri: The full resource URL.
            expiry: Time-to-live in seconds for new cache entries.
                    Pass `math.inf` for entries that should never expire
                    (invalidated instead by version checks).
            force_update: If True, re-fetch even when a valid cached entry exists.

        Returns:
            The parsed JSON response as a dict.
        """
        if self.cache.has(uri):
            record = self.cache.get(uri)
            if record.is_data_stale or force_update:
                data = self.uncached_fetch(uri).json()
                self.cache.update(uri, data)
            else:
                data = record.data
        else:
            data = self.uncached_fetch(uri).json()
            self.cache.store(uri, data, expiry)

        return data

    # ------------------------------------------------------------------
    # Resource endpoints
    # ------------------------------------------------------------------

    def get_maps(self) -> List["MapItem"]:
        """Return all playable maps."""
        data = self.fetch(self.__base_uri + "/v1/maps", expiry=math.inf)
        return [MapItem(e) for e in data["data"]]

    def get_competitive_tiers(self) -> List["TierItem"]:
        """
        Return competitive rank tiers for the current act.

        Only returns tiers that are actively used (filters out 'Unused' placeholders).
        """
        data = self.fetch(self.__base_uri + "/v1/competitivetiers", expiry=math.inf)
        # The API returns one entry per act; the last entry is the most recent.
        tiers = CompTierItem(data["data"][-1]).tiers
        return [t for t in tiers if "Unused" not in t.divisionName]

    def get_agents(self) -> Dict[str, "AgentItem"]:
        """Return all playable agents, keyed by display name."""
        resource = self.__base_uri + build_url(
            "/v1/agents",
            required_params={},
            optional_params={"language": self.language, "isPlayableCharacter": True},
        )
        data = self.fetch(resource, expiry=math.inf)
        return {agent["displayName"]: AgentItem(agent) for agent in data["data"]}

    def get_agent_by_name(self, name: str) -> "AgentItem":
        """Look up a single agent by display name. Uses the lazy-cached agent dict."""
        return self.agents[name]

    def get_agent_by_uuid(self, uuid: str) -> "AgentItem":
        """Look up a single agent by UUID. Uses the lazy-cached agent dict."""
        return {a.uuid: a for a in self.agents.values()}[uuid]

    def get_gamemodes(self) -> Dict[str, "Gamemode"]:
        """Return all game modes, keyed by display name."""
        resource = self.__base_uri + build_url(
            "/v1/gamemodes",
            required_params={},
            optional_params={"language": self.language},
        )
        data = self.fetch(resource, expiry=math.inf)
        return {e["displayName"]: Gamemode(e) for e in data["data"]}

    def get_season_index_from_uuid(self, uuid: str) -> int:
        """
        Return the 1-based sequential index of the season identified by *uuid*.

        Only 'CompetitiveSeason' acts (those whose asset path includes
        'CompetitiveSeason', 'Episode', and 'Act') are counted.
        """
        resource = self.__base_uri + build_url(
            "/v1/seasons",
            required_params={},
            optional_params={"language": "en-US"},
        )
        data = self.fetch(resource, expiry=math.inf)

        seasons = [Season(e) for e in data["data"]]
        competitive_only = [
            s for s in seasons
            if all(token in s.assetPath for token in ("CompetitiveSeason", "Episode", "Act"))
        ]
        index_by_uuid = {s.uuid: n + 1 for n, s in enumerate(competitive_only)}
        return index_by_uuid[uuid]

    def get_seasons(self) -> Dict[str, "Season"]:
        """Return all seasons, keyed by UUID."""
        resource = self.__base_uri + build_url(
            "/v1/seasons",
            required_params={},
            optional_params={"language": self.language},
        )
        data = self.fetch(resource, expiry=math.inf)
        return {e["uuid"]: Season(e) for e in data["data"]}

    def is_data_stale(self) -> bool:
        """
        Compare the cached build date against the live API version.

        Returns True if the remote build date differs from the cached one,
        indicating that cached asset data should be invalidated.
        """
        response = self.uncached_fetch(self.__base_uri + "/v1/version")
        response.raise_for_status()
        current_version = AssetsApiVersioning(response.json()["data"])
        return self.version.buildDate != current_version.buildDate

    # ------------------------------------------------------------------
    # Lazy-loaded properties (cached after first access)
    # ------------------------------------------------------------------

    @lazy_property
    def agents(self) -> Dict[str, "AgentItem"]:
        """All playable agents, keyed by display name."""
        return self.get_agents()

    @lazy_property
    def gamemodes(self) -> Dict[str, "Gamemode"]:
        """All game modes, keyed by display name."""
        return self.get_gamemodes()

    @lazy_property
    def maps(self) -> List["MapItem"]:
        """All playable maps."""
        return self.get_maps()

    @lazy_property
    def seasons(self) -> Dict[str, "Season"]:
        """All seasons, keyed by UUID."""
        return self.get_seasons()


# ---------------------------------------------------------------------------
# Model helpers
# ---------------------------------------------------------------------------

def _is_generic_alias(obj) -> bool:
    """Return True if *obj* is a generic type alias (e.g. List[str])."""
    return type(obj).__name__ == "_GenericAlias"


def apimodel(cls):
    """
    Class decorator that auto-generates `__init__` and `as_dict` for API models.

    `__init__` reads annotated fields from the supplied *data* dict via
    JsonInjester, casting values to their annotated types.  Generic container
    annotations (e.g. `List[SomeModel]`) cause each element to be cast to the
    inner type.  The raw dict is stored as `_Initial__Data` for `as_dict`.
    """

    def __init__(self, data: dict) -> None:
        ji = JsonInjester(data)
        object.__setattr__(self, "_Initial__Data", data)

        for field, cast in self.__annotations__.items():
            if isinstance(cast, type):
                setattr(self, field, ji.get(field, cast=cast))

            elif _is_generic_alias(cast):
                container_type = cast.__origin__
                item_cast = cast.__args__[0] if cast.__args__ else object

                if container_type is list:
                    setattr(self, field, [item_cast(e) for e in ji.get(field, [])])

    def as_dict(self) -> dict:
        """Return the original raw dict that was used to construct this model."""
        return self._Initial__Data  # type: ignore[attr-defined]

    cls.__init__ = __init__
    cls.as_dict = as_dict
    return cls


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

@apimodel
class AssetsApiVersioning:
    """Versioning metadata returned by /v1/version."""
    manifestId: str
    branch: str
    version: str
    buildVersion: str
    engineVersion: str
    riotClientVersion: str
    riotClientBuild: str
    buildDate: str


@apimodel
class Coordinate:
    """2-D coordinate used for map callout positions."""
    x: float
    y: float


@apimodel
class MapCallout:
    """A named region on a map (e.g. 'A Site', 'Mid')."""
    regionName: str       # localized
    superRegionName: str  # localized
    location: Coordinate


@apimodel
class MapItem:
    """Full metadata for a single playable map."""
    uuid: str
    displayName: str
    narrativeDescription: str
    tacticalDescription: str
    coordinates: str
    displayIcon: str
    listViewIcon: str
    listViewIconTall: str
    splash: str
    stylizedBackgroundImage: str
    premierBackgroundImage: str
    assetPath: str
    mapUrl: str
    xMultiplier: float
    yMultiplier: float
    xScalarToAdd: float
    yScalarToAdd: float
    callouts: List[MapCallout]


@apimodel
class AgentRole:
    """One of the four agent roles (Duelist, Initiator, Controller, Sentinel)."""
    uuid: str
    displayName: str  # localized
    description: str  # localized
    displayIcon: str
    assetPath: str

    def __repr__(self) -> str:
        return f"<role:{self.displayName}>"


@apimodel
class AgentAbilities:
    """A single ability belonging to an agent."""
    slot: str          # e.g. 'Ability1', 'Ultimate'
    displayName: str   # localized
    description: str   # localized
    displayIcon: str

    def __repr__(self) -> str:
        return f"<ability:{self.displayName}>"


@apimodel
class AgentItem:
    """Full metadata for a single playable agent."""
    uuid: str
    displayName: str              # localized
    description: str              # localized
    developerName: str
    characterTags: List[str]      # localized
    displayIcon: str
    displayIconSmall: str
    bustPortrait: str
    fullPortrait: str
    fullPortraitV2: str
    killfeedPortrait: str
    background: str
    backgroundGradientColors: List[str]
    assetPath: str
    isFullPortraitRightFacing: bool
    isPlayableCharacter: bool
    isAvailableForTest: bool
    isBaseContent: bool
    role: AgentRole
    milestoneThreshold: int
    useLevelVpCostOverride: bool
    levelVpCostOverride: int
    startDate: str
    endDate: str
    abilities: List[AgentAbilities]

    def __repr__(self) -> str:
        return f"<agent:{self.displayName}>"


@apimodel
class TierItem:
    """A single rank tier within a competitive season (e.g. Gold 2)."""
    tier: int
    tierName: str        # localized  e.g. 'Gold'
    division: str        # e.g. 'GOLD'
    divisionName: str    # localized  e.g. 'GOLD 2'
    color: str
    backgroundColor: str
    smallIcon: str
    largeIcon: str
    rankTriangleDownIcon: str
    rankTriangleUpIcon: str

    @lazy_property
    def promotion_level(self) -> int:
        """
        Numeric sub-division within the tier (1, 2, or 3).

        Returns 0 for UNRANKED and placeholder 'Unused' tiers.
        """
        if self.divisionName in ("UNRANKED", "Unused1", "Unused2"):
            return 0
        return int(self.divisionName.replace(self.division, "").strip())

    def __repr__(self) -> str:
        return f"<tier:{self.tierName.capitalize()}>"


@apimodel
class CompTierItem:
    """Container for all rank tiers in a single competitive act."""
    uuid: str
    assetObjectName: str
    tiers: List[TierItem]
    assetPath: str


@apimodel
class FeatureOverride:
    """A boolean override for a named game feature."""
    featureName: str
    state: bool


@apimodel
class RuleOverride:
    """A boolean override for a named game rule."""
    ruleName: str
    state: bool


@apimodel
class Gamemode:
    """Metadata for a single game mode (e.g. Unrated, Competitive, Spike Rush)."""
    uuid: str
    displayName: str    # localized
    duration: str       # localized
    economyType: str
    allowsMatchTimeouts: bool
    isTeamVoiceAllowed: bool
    isMinimapHidden: bool
    orbCount: int
    roundsPerHalf: int
    teamRoles: List[str]
    displayIcon: str
    listViewIconTall: str
    assetPath: str
    gameFeatureOverrides: List[FeatureOverride]
    gameRuleBoolOverrides: List[RuleOverride]

    def __repr__(self) -> str:
        return f"<gamemode:{self.displayName}>"


@apimodel
class SeasonBorder:
    """A ranked border reward for reaching a certain win count in a season."""
    uuid: str
    winsRequired: int
    level: int
    displayIcon: str
    smallIcon: str
    assetPath: str


@apimodel
class Season:
    """Metadata for a single act or episode."""
    uuid: str
    displayName: str
    startTime: str
    endTime: str
    borders: List[SeasonBorder]   # NOTE: was missing type annotation in original
    assetPath: str

    def __repr__(self) -> str:
        return f"<season:{self.displayName}>"