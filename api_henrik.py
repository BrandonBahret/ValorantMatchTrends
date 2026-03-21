"""
api_henrik.py

Wrapper around the HenrikDev unofficial VALORANT API
(https://app.swaggerhub.com/apiproxy/registry/Henrik-3/HenrikDev-API/3.0.0).

Responsibilities:
  - Rate-limit tracking and automatic back-off
  - Persistent disk caching via ``Cache``
  - Request logging via ``RequestLogger``
  - Strongly-typed response models generated from the swagger schema

Typical usage::

    api = UnofficialApi()
    account = api.get_account_by_name("Henrik3", "EUW3")
    mmr     = api.get_act_performance_by_name("Henrik3", "EUW3")
"""

# ---------------------------------------------------------------------------
# Standard-library imports
# ---------------------------------------------------------------------------
import json
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Callable, Dict, Generator, List, Optional, Tuple, Type, TypeVar, Union

# ---------------------------------------------------------------------------
# Third-party imports
# ---------------------------------------------------------------------------
import requests

# ---------------------------------------------------------------------------
# Local imports
# ---------------------------------------------------------------------------
from api_cache import Cache
from api_request_logger import RequestLogger
from jsoninjest import UNSET, JsonInjester  # noqa: F401  (UNSET re-exported for callers)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CONFIG_FILE = Path("config.json")


def load_config() -> dict:
    """Load ``config.json`` from the working directory.

    Returns an empty ``api_key`` entry if the file is absent or malformed.
    """
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE) as fh:
                return json.load(fh)
        except Exception:
            pass
    return {"api_key": ""}


API_KEY: Optional[str] = load_config().get("api_key", None)

REQUESTS_LOG = "logs/unofficial-api-requests.log"
CACHE_FILE   = "caches/henrik/chunks.manifest"

# ---------------------------------------------------------------------------
# Rate-limit constants
# ---------------------------------------------------------------------------

RATE_LIMIT: int = 90   # max requests allowed …
RATE_PER:   int = 60   # … per this many seconds


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class ShortDatetime(datetime):
    """``datetime`` subclass with a compact ``repr``."""

    def __repr__(self) -> str:
        return f"<datetime: {self.strftime('%Y-%m-%d %H:%M')}>"


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class AffinitiesEnum(Enum):
    """Server affinity / shard identifiers used by the HenrikDev API."""
    EU    = "eu"
    NA    = "na"
    LATAM = "latam"
    BR    = "br"
    AP    = "ap"
    KR    = "kr"


class ModesApiEnum(Enum):
    """Game-mode slugs as used in API query parameters."""
    COMPETITIVE    = "competitive"
    CUSTOM         = "custom"
    DEATHMATCH     = "deathmatch"
    ESCALATION     = "escalation"
    TEAMDEATHMATCH = "teamdeathmatch"
    NEWMAP         = "newmap"
    REPLICATION    = "replication"
    SNOWBALLFIGHT  = "snowballfight"
    SPIKERUSH      = "spikerush"
    SWIFTPLAY      = "swiftplay"
    UNRATED        = "unrated"


class MapsEnum(Enum):
    """Human-readable map names returned in match payloads."""
    ASCENT   = "Ascent"
    SPLIT    = "Split"
    FRACTURE = "Fracture"
    BIND     = "Bind"
    BREEZE   = "Breeze"
    DISTRICT = "District"
    KASBAH   = "Kasbah"
    PIAZZA   = "Piazza"
    LOTUS    = "Lotus"
    PEARL    = "Pearl"
    ICEBOX   = "Icebox"
    HAVEN    = "Haven"


class SeasonsEnum(Enum):
    """Episode/Act identifiers (e.g. ``e1a1`` = Episode 1 Act 1)."""
    E1A1 = "e1a1"
    E1A2 = "e1a2"
    E1A3 = "e1a3"
    E2A1 = "e2a1"
    E2A2 = "e2a2"
    E2A3 = "e2a3"
    E3A1 = "e3a1"
    E3A2 = "e3a2"
    E3A3 = "e3a3"
    E4A1 = "e4a1"
    E4A2 = "e4a2"
    E4A3 = "e4a3"
    E5A1 = "e5a1"
    E5A2 = "e5a2"
    E5A3 = "e5a3"
    E6A1 = "e6a1"
    E6A2 = "e6a2"
    E6A3 = "e6a3"
    E7A1 = "e7a1"
    E7A2 = "e7a2"
    E7A3 = "e7a3"


class TiersEnum(Enum):
    """Competitive rank tiers."""
    UNRATED      = "Unrated"
    UNKNOWN_1    = "Unknown 1"
    UNKNOWN_2    = "Unknown 2"
    IRON_1       = "Iron 1"
    IRON_2       = "Iron 2"
    IRON_3       = "Iron 3"
    BRONZE_1     = "Bronze 1"
    BRONZE_2     = "Bronze 2"
    BRONZE_3     = "Bronze 3"
    SILVER_1     = "Silver 1"
    SILVER_2     = "Silver 2"
    SILVER_3     = "Silver 3"
    GOLD_1       = "Gold 1"
    GOLD_2       = "Gold 2"
    GOLD_3       = "Gold 3"
    PLATINUM_1   = "Platinum 1"
    PLATINUM_2   = "Platinum 2"
    PLATINUM_3   = "Platinum 3"
    DIAMOND_1    = "Diamond 1"
    DIAMOND_2    = "Diamond 2"
    DIAMOND_3    = "Diamond 3"
    ASCENDANT_1  = "Ascendant 1"
    ASCENDANT_2  = "Ascendant 2"
    ASCENDANT_3  = "Ascendant 3"
    IMMORTAL_1   = "Immortal 1"
    IMMORTAL_2   = "Immortal 2"
    IMMORTAL_3   = "Immortal 3"
    RADIANT      = "Radiant"


class ModesEnum(Enum):
    """Human-readable game-mode names returned inside match payloads."""
    COMPETITIVE  = "Competitive"
    CUSTOM_GAME  = "Custom Game"
    DEATHMATCH   = "Deathmatch"
    ESCALATION   = "Escalation"
    TEAM_DEATHMATCH = "Team Deathmatch"
    NEW_MAP      = "New Map"
    REPLICATION  = "Replication"
    SNOWBALL_FIGHT = "Snowball Fight"
    SPIKE_RUSH   = "Spike Rush"
    SWIFTPLAY    = "Swiftplay"
    UNRATED      = "Unrated"


class ModeIdsEnum(Enum):
    """Internal game-mode IDs (differ slightly from ``ModesApiEnum``)."""
    COMPETITIVE = "competitive"
    CUSTOM      = "custom"
    DEATHMATCH  = "deathmatch"
    GGTEAM      = "ggteam"
    HURM        = "hurm"
    NEWMAP      = "newmap"
    ONEFA       = "onefa"
    SNOWBALL    = "snowball"
    SPIKERUSH   = "spikerush"
    SWIFTPLAY   = "swiftplay"
    UNRATED     = "unrated"


class RegionsEnum(Enum):
    """Geographic regions supported by the ranked MMR endpoints."""
    EU = "eu"
    NA = "na"
    AP = "ap"
    KR = "kr"


class PlatformsEnum(Enum):
    """Player platform types."""
    PC      = "PC"
    CONSOLE = "Console"


# ---------------------------------------------------------------------------
# URL utilities
# ---------------------------------------------------------------------------

def build_url(
    url: str,
    required_params: Dict[str, Union[str, Enum]],
    optional_params: Dict[str, Optional[Union[str, int, Enum]]],
) -> str:
    """Build a URL with path substitutions and an optional query string.

    Args:
        url: URL template with ``{param}`` placeholders for required params,
             e.g. ``"/valorant/v1/account/{name}/{tag}"``.
        required_params: Mapping of placeholder name → value; substituted
                         directly into the URL path via ``str.format``.
        optional_params: Mapping of query-param name → value; ``None`` values
                         are silently omitted.

    Returns:
        Fully constructed URL string.
    """
    extract_enum: Callable = lambda e: e.value if isinstance(e, Enum) else e

    # Unwrap any Enum values so they can be used in str.format and query strings
    required_params = {k: extract_enum(v) for k, v in required_params.items()}
    optional_params = {k: extract_enum(v) for k, v in optional_params.items()}

    # Append non-None optional params as a query string
    query_started = False
    for param, value in optional_params.items():
        if value is not None:
            separator = "?" if not query_started else "&"
            url += f"{separator}{param}={value}"
            query_started = True

    # Substitute required params into path placeholders
    url = url.format(**required_params)
    return url


def select_from_dict(
    data: Dict,
    keys: Dict[Union[str, Tuple], List[str]],
    group: Optional[Dict] = None,
) -> Dict:
    """Extract a sub-view of *data* and optionally nest fields under group keys.

    Args:
        data: Source dictionary (typically a parsed API response).
        keys: Mapping of path → sub-keys to select.  A path may be:
              - A plain string ``"metadata"`` → ``data["metadata"]``
              - A tuple ``("teams", "red")`` → ``data["teams"]["red"]``
              Sub-keys may be a list of field names or ``"*"`` for the full dict.
        group: Optional mapping of collection → namespace.  Keys within the
               collection are nested under the given namespace in the output.
               Example: ``{('blue', 'red'): 'teams'}`` places both ``blue``
               and ``red`` under ``output["teams"]``.

    Returns:
        Filtered dict with the requested structure.
    """
    def _select_keys(src: Dict, sub_keys: Union[str, List[str]]) -> Dict:
        """Return all or a subset of *src*."""
        if sub_keys == "*":
            return src
        return {k: src[k] for k in sub_keys}

    group = group or {}
    view: Dict = {namespace: {} for namespace in group.values()}

    for path, subkeys in keys.items():
        # Traverse nested path if given as a tuple
        if isinstance(path, tuple):
            sub_view = data
            for k in path:
                sub_view = sub_view[k]
            path = k  # use final key as the local name
        else:
            sub_view = data[path]

        # Check whether this path belongs to a group
        for collection, namespace in group.items():
            if path in collection:
                view[namespace][path] = _select_keys(sub_view, subkeys)
                break
        else:
            view[path] = _select_keys(sub_view, subkeys)

    return view


# ---------------------------------------------------------------------------
# Singleton decorator
# ---------------------------------------------------------------------------
T = TypeVar("T")

def singleton(cls: Type[T]) -> Callable[..., T]:
    """Class decorator that enforces a single shared instance per class.

    If the class defines ``__post_init__``, it is called once after the first
    instantiation (useful for deferred/heavy initialisation).
    """
    instances: Dict[Type, object] = {}

    def get_instance(*args, **kwargs):
        if cls not in instances:
            instances[cls] = cls(*args, **kwargs)
            if hasattr(instances[cls], "__post_init__"):
                instances[cls].__post_init__()
        return instances[cls]

    return get_instance


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class RateException(Exception):
    """Raised when the API rate limit is exceeded and cannot be recovered."""


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------

@singleton
class UnofficialApi:
    """Singleton HTTP client for the HenrikDev VALORANT API.

    Handles:
    - Automatic rate-limit detection and sleep-based back-off
    - Disk-based response caching with configurable TTLs
    - Structured, type-annotated response models

    Args:
        api_key: HenrikDev API key.  Defaults to the value loaded from
                 ``config.json`` at import time.
    """

    def __init__(self, api_key: Optional[str] = API_KEY):
        self.__api_key    = api_key
        self.__base_uri   = "https://api.henrikdev.xyz"
        self.__swagger_url = (
            "https://app.swaggerhub.com/apiproxy/registry/"
            "Henrik-3/HenrikDev-API/3.0.0"
        )
        self.cache  = Cache(CACHE_FILE)
        self.logger = RequestLogger(REQUESTS_LOG)

    # ------------------------------------------------------------------
    # Rate-limit helpers
    # ------------------------------------------------------------------

    @property
    def is_rate_limit_reached(self) -> bool:
        """``True`` when quota usage exceeds 90 % of the allowed rate."""
        return self.quota_usage > 0.9

    @property
    def quota_usage(self) -> float:
        """Fraction of the rate limit consumed within the current window."""
        return len(self.logger.get_logs_from_last_seconds(RATE_PER)) / RATE_LIMIT

    @property
    def time_until_limit_reset(self) -> float:
        """Seconds until the oldest request in the current window expires.

        Returns ``0`` when there are no recent requests.
        """
        recent = self.logger.get_logs_from_last_seconds(RATE_PER)
        if not recent:
            return 0.0

        oldest_dt   = datetime.fromtimestamp(recent[0].timestamp)
        reset_at    = oldest_dt + timedelta(seconds=RATE_PER)
        return (reset_at - datetime.now()).total_seconds()

    def wait_until_limit_reset(self) -> None:
        """Block until the rate-limit window resets (with a 1-second buffer)."""
        time.sleep(self.time_until_limit_reset + 1)

    # ------------------------------------------------------------------
    # Low-level HTTP methods
    # ------------------------------------------------------------------

    def uncached_fetch(
        self,
        uri: str,
        payload: Optional[dict] = None,
    ) -> requests.Response:
        """Make a GET request, retrying up to 3 times on HTTP 429.

        Blocks automatically when the local rate-limit threshold is reached
        before issuing the request.

        Args:
            uri:     Full URL to fetch.
            payload: Optional JSON body (passed as ``json=`` to ``requests``).

        Returns:
            The successful ``requests.Response``.

        Raises:
            RateException: When the server returns 429 on all retry attempts.
        """
        if self.is_rate_limit_reached:
            self.wait_until_limit_reset()

        headers = {"Authorization": self.__api_key}
        status  = None

        for attempt in range(3):
            response = requests.get(uri, headers=headers, json=payload)
            status   = response.status_code

            if status == 429:
                wait_time = max(self.time_until_limit_reset, 3 * attempt)
                time.sleep(wait_time)
                continue
            elif status == 200:
                break

        if status == 429:
            raise RateException(f"Rate limit reached! {response.json()}")

        self.logger.log(uri, response.status_code)
        return response

    def uncached_post(
        self,
        uri: str,
        payload: Optional[dict] = None,
    ) -> requests.Response:
        """Make a POST request.

        Unlike ``uncached_fetch``, this raises immediately if the rate limit
        is reached rather than sleeping (POST calls tend to be mutations).

        Args:
            uri:     Full URL.
            payload: JSON body.

        Returns:
            The successful ``requests.Response``.

        Raises:
            RateException:          If the local quota threshold is reached.
            requests.HTTPError:     On any non-2xx HTTP status.
        """
        if self.is_rate_limit_reached:
            raise RateException(
                f"Rate limit reached! You must wait "
                f"{self.time_until_limit_reset:.1f} seconds."
            )

        headers  = {"Authorization": self.__api_key}
        response = requests.post(uri, headers=headers, json=payload)
        response.raise_for_status()
        self.logger.log(uri, response.status_code)
        return response

    def fetch(
        self,
        uri: str,
        expiry: float,
        force_update: bool = False,
    ) -> dict:
        """Fetch *uri*, serving from the disk cache when fresh.

        Cache miss or stale entries trigger a live HTTP GET.  On connection
        failure for a stale entry, the cached data is returned as a fallback.

        Args:
            uri:          Full URL to fetch.
            expiry:       Cache TTL in seconds.  Use ``math.inf`` for permanent.
            force_update: Bypass the freshness check and always refetch.

        Returns:
            Parsed JSON response as a dict.
        """
        if self.cache.has(uri):
            record = self.cache.get(uri)
            if record.is_data_stale or force_update:
                try:
                    data = self.uncached_fetch(uri).json()
                    self.cache.update(uri, data)
                except (requests.exceptions.ConnectionError, requests.RequestException):
                    # Network unavailable — return stale data rather than crashing
                    return record.data
            else:
                data = record.data
        else:
            data = self.uncached_fetch(uri).json()
            self.cache.store(uri, data, expiry)

        return data

    # ------------------------------------------------------------------
    # Manually implemented endpoint (not generated from swagger)
    # ------------------------------------------------------------------

    def get_match_history(
        self,
        puuid:  str,
        mode:   ModesApiEnum = ModesApiEnum.COMPETITIVE,
        region: Union[AffinitiesEnum, str] = "na",
    ) -> List["MatchReference"]:
        """Fetch the complete competitive match history for a player by PUUID.

        Parallelises requests using a small thread pool so that the full
        history is retrieved faster than serial pagination would allow.

        Args:
            puuid:  Player UUID.
            mode:   Queue/game-mode filter.
            region: Server affinity.

        Returns:
            List of ``MatchReference`` objects sorted oldest → newest.
        """
        INCREMENT_SIZE = 25
        NUM_THREADS    = 3

        def _fetch_page(begin: int, end: int) -> dict:
            """POST to the raw match-history endpoint for a single page."""
            endpoint = f"{self.__base_uri}/valorant/v1/raw"
            payload  = {
                "type":    "matchhistory",
                "value":   puuid,
                "region":  region,
                "queries": f"?queue={mode.value}&startIndex={begin}&endIndex={end}",
            }
            response = self.uncached_post(endpoint, payload)
            response.raise_for_status()
            return response.json()["data"]

        history: Dict[str, MatchReference] = {}
        total   = float("inf")  # unknown until the first response arrives
        done    = threading.Event()

        def fetch_and_store(start_index: int) -> None:
            """Worker: fetch one page and populate *history*."""
            nonlocal total
            try:
                data = _fetch_page(start_index, start_index + INCREMENT_SIZE)
            except requests.HTTPError as exc:
                if exc.response.status_code == 400:
                    # No more pages available
                    done.set()
                    return
                raise

            for match_data in data["History"]:
                match = MatchReference(match_data)
                history[match.match_id] = match

            total = data["Total"]
            if len(history) >= total:
                done.set()

        with ThreadPoolExecutor(max_workers=NUM_THREADS) as executor:
            # Queue the first two pages eagerly to warm the pipeline
            start_index = 0
            initial_futures = []
            for _ in range(2):
                initial_futures.append(executor.submit(fetch_and_store, start_index))
                start_index += INCREMENT_SIZE

            for future in initial_futures:
                if not done.is_set():
                    future.result()

            # Queue any remaining pages now that we know the total
            if not done.is_set():
                remaining = [
                    executor.submit(fetch_and_store, idx)
                    for idx in range(start_index, int(total), INCREMENT_SIZE)
                ]
                done.wait()

        return sorted(history.values(), key=lambda m: m.timestamp)

    # ------------------------------------------------------------------
    # Generated API resource methods
    # ------------------------------------------------------------------

    def get_account_by_name(
        self,
        name:  str,
        tag:   str,
        force: Optional[bool] = None,
    ) -> "V1Account":
        """Return account details for a player identified by name and tag.

        Attempts to serve from a cached by-PUUID record if one matches,
        avoiding a redundant network request.

        Args:
            name:  In-game name (e.g. ``"Henrik3"``).
            tag:   Tagline (e.g. ``"EUW3"``).
            force: When ``True``, bypass the cache and refetch.

        Returns:
            :class:`V1Account`
        """
        if not force:
            # Check if we already hold a by-PUUID record for the same account
            alt_resource = "/valorant/v1/by-puuid/account/"
            for uri in self.cache.storage.records.keys():
                if alt_resource not in uri:
                    continue
                record  = self.cache.get(uri)
                account = V1Account(record.data)
                if account.name == name and account.tag == tag:
                    if record.is_data_stale:
                        break  # must refetch below
                    return account

        required_params = {"name": name, "tag": tag}
        optional_params = {"force": force}
        resource = self.__base_uri + build_url(
            "/valorant/v1/account/{name}/{tag}",
            required_params,
            optional_params,
        )
        data = self.fetch(resource, expiry=math.inf, force_update=bool(force))
        return V1Account(data)

    def get_account_by_puuid(
        self,
        puuid: str,
        force: Optional[bool] = None,
    ) -> "V1Account":
        """Return account details for a player identified by PUUID.

        Attempts to serve from a cached by-name record if one matches.

        Args:
            puuid: Player UUID.
            force: When ``True``, bypass the cache and refetch.

        Returns:
            :class:`V1Account`
        """
        if not force:
            alt_resource = "/valorant/v1/account/"
            for uri in self.cache.storage.records.keys():
                if alt_resource not in uri:
                    continue
                record  = self.cache.get(uri)
                account = V1Account(record.data)
                if account.puuid == puuid:
                    if record.is_data_stale:
                        break
                    return account

        required_params = {"puuid": puuid}
        optional_params = {"force": force}
        resource = self.__base_uri + build_url(
            "/valorant/v1/by-puuid/account/{puuid}",
            required_params,
            optional_params,
        )
        data = self.fetch(resource, expiry=math.inf, force_update=bool(force))
        return V1Account(data)

    def get_recent_matches_by_puuid(
        self,
        puuid:    str,
        affinity: Union[AffinitiesEnum, str] = "na",
        mode:     Optional[ModesApiEnum] = None,
        map:      Optional[MapsEnum] = None,
        page:     Optional[int] = None,
        size:     Optional[int] = None,
    ) -> "V1LifetimeMatches":
        """Return recent (client-visible) matches for a player by PUUID.

        Args:
            puuid:    Player UUID.
            affinity: Server affinity.
            mode:     Filter by game mode.
            map:      Filter by map.
            page:     Pagination page (requires *size*).
            size:     Number of matches to return per page.

        Returns:
            :class:`V1LifetimeMatches`
        """
        required_params = {"puuid": puuid, "affinity": affinity}
        optional_params = {"mode": mode, "map": map, "page": page, "size": size}
        resource = self.__base_uri + build_url(
            "/valorant/v1/by-puuid/lifetime/matches/{affinity}/{puuid}",
            required_params,
            optional_params,
        )
        return V1LifetimeMatches(self.fetch(resource, expiry=3600.0))

    def get_recent_mmr_history_by_puuid(
        self,
        puuid:  str,
        region: Union[AffinitiesEnum, str] = "na",
        page:   Optional[int] = None,
        size:   Optional[int] = None,
    ) -> "V1LifetimeMmrHistory":
        """Return recent MMR history for a player by PUUID.

        Args:
            puuid:  Player UUID.
            region: Server affinity.
            page:   Pagination page (requires *size*).
            size:   Number of MMR changes to return.

        Returns:
            :class:`V1LifetimeMmrHistory`
        """
        required_params = {"puuid": puuid, "region": region}
        optional_params = {"page": page, "size": size}
        resource = self.__base_uri + build_url(
            "/valorant/v1/by-puuid/lifetime/mmr-history/{region}/{puuid}",
            required_params,
            optional_params,
        )
        return V1LifetimeMmrHistory(self.fetch(resource, expiry=3600.0))

    def get_act_performance_by_puuid(
        self,
        puuid:    str,
        affinity: Union[AffinitiesEnum, str] = "na",
        season:   Optional[SeasonsEnum] = None,
    ) -> "V2mmr":
        """Return v2 MMR / act performance for a player by PUUID.

        Args:
            puuid:    Player UUID.
            affinity: Server affinity.
            season:   Optionally filter to a specific act.

        Returns:
            :class:`V2mmr`
        """
        required_params = {"puuid": puuid, "affinity": affinity}
        optional_params = {"season": season}
        resource = self.__base_uri + build_url(
            "/valorant/v2/by-puuid/mmr/{affinity}/{puuid}",
            required_params,
            optional_params,
        )
        return V2mmr(self.fetch(resource, expiry=3600.0))

    def get_match_by_id(self, match_id: str) -> Optional["Match"]:
        """Return full match data for a given match ID.

        On first fetch, the response is trimmed to key fields only (metadata,
        all_players, teams, rounds, observers, coaches) before being stored
        in the cache, reducing cache size significantly.

        Args:
            match_id: VALORANT match UUID.

        Returns:
            :class:`Match`, or ``None`` if the API returned errors.
        """
        def _trim_match(match: dict) -> dict:
            """Keep only the fields we actually use from a raw match payload."""
            selection = {
                "metadata": "*",
                "players":  ["all_players"],
                ("teams", "red"):  ["has_won", "rounds_won", "rounds_lost"],
                ("teams", "blue"): ["has_won", "rounds_won", "rounds_lost"],
                "rounds":    "*",
                "observers": "*",
                "coaches":   "*",
            }
            group = {("blue", "red"): "teams"}
            return select_from_dict(match, selection, group)

        required_params = {"matchId": match_id}
        resource        = self.__base_uri + build_url(
            "/valorant/v2/match/{matchId}",
            required_params,
            {},
        )

        is_new_match = not self.cache.has(resource)
        data: dict   = self.fetch(resource, expiry=math.inf)

        if "errors" in data:
            return None

        if is_new_match:
            # Trim and re-store to keep the cache lean
            trimmed         = _trim_match(data["data"])
            data["data"]    = trimmed
            self.cache.update(resource, data)
            return Match(trimmed)

        return Match(data["data"])

    def get_act_performance_by_name(
        self,
        name:     str,
        tag:      str,
        affinity: Union[AffinitiesEnum, str] = "na",
        season:   Optional[SeasonsEnum] = None,
    ) -> "V2mmr":
        """Return v2 MMR / act performance for a player by name and tag.

        Args:
            name:     In-game name.
            tag:      Tagline.
            affinity: Server affinity.
            season:   Optionally filter to a specific act.

        Returns:
            :class:`V2mmr`
        """
        required_params = {"name": name, "tag": tag, "affinity": affinity}
        optional_params = {"season": season}
        resource = self.__base_uri + build_url(
            "/valorant/v2/mmr/{affinity}/{name}/{tag}",
            required_params,
            optional_params,
        )
        return V2mmr(self.fetch(resource, expiry=3600.0))

    def get_recent_matches_by_name(
        self,
        name:     str,
        tag:      str,
        affinity: Union[AffinitiesEnum, str] = "na",
        mode:     Optional[ModesApiEnum] = None,
        map:      Optional[MapsEnum] = None,
        page:     Optional[int] = None,
        size:     Optional[int] = None,
    ) -> "V1LifetimeMatches":
        """Return recent (client-visible) matches for a player by name and tag.

        Args:
            name:     In-game name.
            tag:      Tagline.
            affinity: Server affinity.
            mode:     Filter by game mode.
            map:      Filter by map.
            page:     Pagination page (requires *size*).
            size:     Number of matches to return.

        Returns:
            :class:`V1LifetimeMatches`
        """
        required_params = {"name": name, "tag": tag, "affinity": affinity}
        optional_params = {"mode": mode, "map": map, "page": page, "size": size}
        resource = self.__base_uri + build_url(
            "/valorant/v1/lifetime/matches/{affinity}/{name}/{tag}",
            required_params,
            optional_params,
        )
        return V1LifetimeMatches(self.fetch(resource, expiry=3600.0))

    def get_recent_mmr_history_by_name(
        self,
        name:     str,
        tag:      str,
        affinity: Union[AffinitiesEnum, str] = "na",
        page:     Optional[int] = None,
        size:     Optional[int] = None,
    ) -> "V1LifetimeMmrHistory":
        """Return recent MMR history for a player by name and tag.

        Args:
            name:     In-game name.
            tag:      Tagline.
            affinity: Server affinity.
            page:     Pagination page (requires *size*).
            size:     Number of MMR changes to return.

        Returns:
            :class:`V1LifetimeMmrHistory`
        """
        required_params = {"name": name, "tag": tag, "affinity": affinity}
        optional_params = {"page": page, "size": size}
        resource = self.__base_uri + build_url(
            "/valorant/v1/lifetime/mmr-history/{affinity}/{name}/{tag}",
            required_params,
            optional_params,
        )
        return V1LifetimeMmrHistory(self.fetch(resource, expiry=3600.0))

    def get_available_queues(
        self,
        affinity: Union[AffinitiesEnum, str] = "na",
    ) -> "V1QueueStatus":
        """Return metadata for all available game queues in a region.

        Args:
            affinity: Server affinity.

        Returns:
            :class:`V1QueueStatus`
        """
        required_params = {"affinity": affinity}
        resource = self.__base_uri + build_url(
            "/valorant/v1/queue-status/{affinity}",
            required_params,
            {},
        )
        return V1QueueStatus(self.fetch(resource, expiry=3600.0))

    def get_api_version(
        self,
        affinity: Union[AffinitiesEnum, str] = "na",
    ) -> "GetApiVersionResponse":
        """Return the current VALORANT client version for a given region.

        Args:
            affinity: Server affinity.

        Returns:
            :class:`GetApiVersionResponse`
        """
        required_params = {"affinity": affinity}
        resource = self.__base_uri + build_url(
            "/valorant/v1/version/{affinity}",
            required_params,
            {},
        )
        return GetApiVersionResponse(self.fetch(resource, expiry=86400.0))


# ===========================================================================
# Response models
# ===========================================================================
# All models below follow the same pattern:
#   - Accept a raw ``dict`` from the API
#   - Deserialise fields via ``JsonInjester``
#   - Expose ``as_dict()`` to retrieve the original payload


class V1Account:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.puuid:           str        = ji.get("data.puuid")
        self.region:          str        = ji.get("data.region")
        self.account_level:   int        = ji.get("data.account_level")
        self.name:            Optional[str] = ji.get("data.name")
        self.tag:             Optional[str] = ji.get("data.tag")
        self.card:            "V1AccountCard" = ji.get("data.card", cast=V1AccountCard)
        self.last_update:     str        = ji.get("data.last_update")
        self.last_update_raw: int        = ji.get("data.last_update_raw")

    def as_dict(self) -> dict:
        return self.__data


class V1AccountCard:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.small: str = ji.get("small")
        self.large: str = ji.get("large")
        self.wide:  str = ji.get("wide")
        self.id:    str = ji.get("id")

    def as_dict(self) -> dict:
        return self.__data


# ---------------------------------------------------------------------------
# Lifetime matches
# ---------------------------------------------------------------------------

class V1LifetimeMatchesItem:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.meta:  "V1LifetimeMatchesItemMeta"  = ji.get("meta",  cast=V1LifetimeMatchesItemMeta)
        self.stats: "V1LifetimeMatchesItemStats" = ji.get("stats", cast=V1LifetimeMatchesItemStats)
        self.teams: "V1LifetimeMatchesItemTeams" = ji.get("teams", cast=V1LifetimeMatchesItemTeams)

    def as_dict(self) -> dict:
        return self.__data


class V1LifetimeMatchesItemStats:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.puuid:     str   = ji.get("puuid")
        self.team:      str   = ji.get("team")
        self.level:     float = ji.get("level")
        self.character: "V1LifetimeMatchesItemStatsCharacter" = ji.get("character", cast=V1LifetimeMatchesItemStatsCharacter)
        self.tier:      float = ji.get("tier")
        self.score:     float = ji.get("score")
        self.kills:     float = ji.get("kills")
        self.deaths:    float = ji.get("deaths")
        self.assists:   float = ji.get("assists")
        self.shots:     "V1LifetimeMatchesItemStatsShots"  = ji.get("shots",  cast=V1LifetimeMatchesItemStatsShots)
        self.damage:    "V1LifetimeMatchesItemStatsDamage" = ji.get("damage", cast=V1LifetimeMatchesItemStatsDamage)

    def as_dict(self) -> dict:
        return self.__data


class V1LifetimeMatchesItemStatsShots:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.head: float = ji.get("head")
        self.body: float = ji.get("body")
        self.leg:  float = ji.get("leg")

    def as_dict(self) -> dict:
        return self.__data


class V1LifetimeMatchesItemStatsCharacter:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.id:   str           = ji.get("id")
        self.name: Optional[str] = ji.get("name")

    def as_dict(self) -> dict:
        return self.__data


class V1LifetimeMatchesItemStatsDamage:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.made:     float = ji.get("made")
        self.received: float = ji.get("received")

    def as_dict(self) -> dict:
        return self.__data


class V1LifetimeMatchesItemMeta:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.id:         str           = ji.get("id")
        self.map:        "V1LifetimeMatchesItemMetaMap"    = ji.get("map",    cast=V1LifetimeMatchesItemMetaMap)
        self.version:    str           = ji.get("version")
        self.mode:       str           = ji.get("mode")
        self.started_at: str           = ji.get("started_at")
        self.season:     "V1LifetimeMatchesItemMetaSeason" = ji.get("season", cast=V1LifetimeMatchesItemMetaSeason)
        self.region:     Optional[str] = ji.get("region")
        self.cluster:    Optional[str] = ji.get("cluster")

    def as_dict(self) -> dict:
        return self.__data


class V1LifetimeMatchesItemMetaSeason:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.id:    str           = ji.get("id")
        self.short: Optional[str] = ji.get("short")

    def as_dict(self) -> dict:
        return self.__data


class V1LifetimeMatchesItemMetaMap:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.id:   str           = ji.get("id")
        self.name: Optional[str] = ji.get("name")

    def as_dict(self) -> dict:
        return self.__data


class V1LifetimeMatchesItemTeams:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.red:  Optional[float] = ji.get("red")
        self.blue: Optional[float] = ji.get("blue")

    def as_dict(self) -> dict:
        return self.__data


class V1LifetimeMatchesResults:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.total:    float = ji.get("total")
        self.returned: float = ji.get("returned")
        self.before:   float = ji.get("before")
        self.after:    float = ji.get("after")

    def as_dict(self) -> dict:
        return self.__data


class V1LifetimeMatches:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.name:    str                          = ji.get("name")
        self.tag:     str                          = ji.get("tag")
        self.results: "V1LifetimeMatchesResults"   = ji.get("results", cast=V1LifetimeMatchesResults)
        self.data:    List["V1LifetimeMatchesItem"] = [V1LifetimeMatchesItem(e) for e in ji.get("data", [])]

    def as_dict(self) -> dict:
        return self.__data


# ---------------------------------------------------------------------------
# Lifetime MMR history
# ---------------------------------------------------------------------------

class V1LifetimeMmrHistoryResults:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.total:    float = ji.get("total")
        self.returned: float = ji.get("returned")
        self.before:   float = ji.get("before")
        self.after:    float = ji.get("after")

    def as_dict(self) -> dict:
        return self.__data


class V1LifetimeMmrHistory:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.name:    str                              = ji.get("name")
        self.tag:     str                              = ji.get("tag")
        self.results: "V1LifetimeMmrHistoryResults"    = ji.get("results", cast=V1LifetimeMmrHistoryResults)
        self.data:    List["V1LifetimeMmrHistoryItem"] = [V1LifetimeMmrHistoryItem(e) for e in ji.get("data", [])]

    def as_dict(self) -> dict:
        return self.__data


class V1LifetimeMmrHistoryItem:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.match_id:       str                             = ji.get("match_id")
        self.tier:           "V1LifetimeMmrHistoryItemTier"  = ji.get("tier",   cast=V1LifetimeMmrHistoryItemTier)
        self.map:            "V1LifetimeMmrHistoryItemMap"   = ji.get("map",    cast=V1LifetimeMmrHistoryItemMap)
        self.season:         "V1LifetimeMmrHistoryItemSeason"= ji.get("season", cast=V1LifetimeMmrHistoryItemSeason)
        self.ranking_in_tier:    float = ji.get("ranking_in_tier")
        self.last_mmr_change:    float = ji.get("last_mmr_change")
        self.elo:                float = ji.get("elo")
        self.date:               str   = ji.get("date")

    @property
    def datetime(self) -> ShortDatetime:
        """Parse the ISO-8601 date string (with trailing ``Z``) to a local datetime."""
        # Replace trailing 'Z' with '+00:00' for full fromisoformat compatibility
        adjusted = self.date[:-1] + "+00:00"
        return ShortDatetime.fromisoformat(adjusted).astimezone()

    def as_dict(self) -> dict:
        return self.__data


class V1LifetimeMmrHistoryItemMap:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.id:   str       = ji.get("id")
        self.name: MapsEnum  = ji.get("name")

    def as_dict(self) -> dict:
        return self.__data


class V1LifetimeMmrHistoryItemSeason:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.id:    str         = ji.get("id")
        self.short: SeasonsEnum = ji.get("short")

    def as_dict(self) -> dict:
        return self.__data


class V1LifetimeMmrHistoryItemTier:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.id:   float     = ji.get("id")
        self.name: TiersEnum = ji.get("name")

    def as_dict(self) -> dict:
        return self.__data


# ---------------------------------------------------------------------------
# Match (v3 list wrapper)
# ---------------------------------------------------------------------------

class V3matches:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.data: List["Match"] = [Match(e) for e in ji.get("data", [])]

    def as_dict(self) -> dict:
        return self.__data


# ---------------------------------------------------------------------------
# Match (full detail)
# ---------------------------------------------------------------------------

class Match:
    """Full match data with lazy-loaded round objects.

    Rounds are expensive to deserialise (many nested objects), so they are
    unpacked on demand via :meth:`get_round` or iterated via :attr:`rounds`.
    """

    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.metadata:  "MatchMetadata"     = ji.get("metadata", cast=MatchMetadata)
        self.players:   List["Player"]      = [Player(e) for e in ji.get("players.all_players", [])]
        self.observers: List["Observer"]    = [Observer(e) for e in ji.get("observers", [])]
        self.coaches:   List["Coach"]       = [Coach(e) for e in ji.get("coaches", [])]
        self.teams:     "MatchTeams"        = ji.get("teams", cast=MatchTeams)

        # Raw round dicts are kept packed; unpacked lazily on access
        self.__rounds_raw:      List[dict]             = ji.get("rounds", [])
        self.__rounds_unpacked: List["MatchRoundsItem"] = []

    def as_dict(self) -> dict:
        return self.__data

    def get_round(self, index: int) -> "MatchRoundsItem":
        """Return the round at *index*, unpacking it (and all preceding rounds)
        if not yet done.

        Args:
            index: Zero-based round index.

        Returns:
            :class:`MatchRoundsItem`

        Raises:
            IndexError: If *index* is out of range.
        """
        if index < len(self.__rounds_unpacked):
            return self.__rounds_unpacked[index]

        # Unpack rounds up to and including *index*
        start = len(self.__rounds_unpacked)
        for i, raw in enumerate(self.__rounds_raw[start:], start=start):
            item = MatchRoundsItem(raw)
            self.__rounds_unpacked.append(item)
            if i == index:
                return item

        raise IndexError(f"No round #{index} for this match (total: {self.number_of_rounds})")

    @property
    def number_of_rounds(self) -> int:
        """Total number of rounds in this match."""
        return len(self.__rounds_raw)

    @property
    def rounds_list(self) -> List["MatchRoundsItem"]:
        """All rounds as a list, fully unpacked."""
        if len(self.__rounds_raw) == len(self.__rounds_unpacked):
            return self.__rounds_unpacked
        return list(self.rounds)

    @property
    def rounds(self) -> Generator["MatchRoundsItem", None, None]:
        """Generator that lazily unpacks and yields each round."""
        if len(self.__rounds_raw) == len(self.__rounds_unpacked):
            yield from self.__rounds_unpacked
        else:
            for raw in self.__rounds_raw:
                item = MatchRoundsItem(raw)
                self.__rounds_unpacked.append(item)
                yield item


# ---------------------------------------------------------------------------
# Player and sub-models
# ---------------------------------------------------------------------------

class Player:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.session_playtime: "PlayerSessionPlaytime" = ji.get("session_playtime", cast=PlayerSessionPlaytime)
        self.assets:           "PlayerAssets"          = ji.get("assets",           cast=PlayerAssets)
        self.behavior:         "PlayerBehavior"        = ji.get("behavior",         cast=PlayerBehavior)
        self.platform:         "PlayerPlatform"        = ji.get("platform",         cast=PlayerPlatform)
        self.ability_casts:    "PlayerAbilityCasts"    = ji.get("ability_casts",    cast=PlayerAbilityCasts)
        self.stats:            "PlayerStats"           = ji.get("stats",            cast=PlayerStats)
        self.economy:          "PlayerEconomy"         = ji.get("economy",          cast=PlayerEconomy)
        self.puuid:                str = ji.get("puuid")           # e.g. "54942ced-1967-5f66-8a16-1e0dae875641"
        self.name:                 str = ji.get("name")            # e.g. "Henrik3"
        self.tag:                  str = ji.get("tag")             # e.g. "EUW3"
        self.team_id:              str = ji.get("team")            # e.g. "Red"
        self.level:                int = ji.get("level")           # e.g. 104
        self.character:            str = ji.get("character")       # e.g. "Sova"
        self.currenttier:          int = ji.get("currenttier")     # e.g. 12
        self.currenttier_patched:  str = ji.get("currenttier_patched")  # e.g. "Gold 1"
        self.player_card:          str = ji.get("player_card")
        self.player_title:         str = ji.get("player_title")
        self.party_id:             str = ji.get("party_id")
        self.damage_made:          int = ji.get("damage_made")     # e.g. 3067
        self.damage_received:      int = ji.get("damage_received") # e.g. 3115

    def as_dict(self) -> dict:
        return self.__data


class PlayerStats:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.score:      int = ji.get("score")      # e.g. 4869
        self.kills:      int = ji.get("kills")      # e.g. 18
        self.deaths:     int = ji.get("deaths")     # e.g. 18
        self.assists:    int = ji.get("assists")    # e.g. 5
        self.bodyshots:  int = ji.get("bodyshots")  # e.g. 48
        self.headshots:  int = ji.get("headshots")  # e.g. 9
        self.legshots:   int = ji.get("legshots")   # e.g. 5

    def as_dict(self) -> dict:
        return self.__data


class PlayerAssets:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.card:  "PlayerAssetsCard"  = ji.get("card",  cast=PlayerAssetsCard)
        self.agent: "PlayerAssetsAgent" = ji.get("agent", cast=PlayerAssetsAgent)

    def as_dict(self) -> dict:
        return self.__data


class PlayerAssetsCard:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.small: str = ji.get("small")
        self.large: str = ji.get("large")
        self.wide:  str = ji.get("wide")

    def as_dict(self) -> dict:
        return self.__data


class PlayerAssetsAgent:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.small:    str = ji.get("small")
        self.full:     str = ji.get("full")
        self.bust:     str = ji.get("bust")
        self.killfeed: str = ji.get("killfeed")

    def as_dict(self) -> dict:
        return self.__data


class PlayerEconomy:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.spent:          "PlayerEconomySpent"        = ji.get("spent",          cast=PlayerEconomySpent)
        self.loadout_value:  "PlayerEconomyLoadoutValue" = ji.get("loadout_value",  cast=PlayerEconomyLoadoutValue)

    def as_dict(self) -> dict:
        return self.__data


class PlayerEconomyLoadoutValue:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.overall: int = ji.get("overall")  # e.g. 71700
        self.average: int = ji.get("average")  # e.g. 3117

    def as_dict(self) -> dict:
        return self.__data


class PlayerEconomySpent:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.overall: int = ji.get("overall")  # e.g. 59750
        self.average: int = ji.get("average")  # e.g. 2598

    def as_dict(self) -> dict:
        return self.__data


class PlayerSessionPlaytime:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.minutes:      int = ji.get("minutes")       # e.g. 26
        self.seconds:      int = ji.get("seconds")       # e.g. 1560
        self.milliseconds: int = ji.get("milliseconds")  # e.g. 1560000

    def as_dict(self) -> dict:
        return self.__data


class PlayerBehavior:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.friendly_fire:    "PlayerBehaviorFriendlyFire" = ji.get("friendly_fire", cast=PlayerBehaviorFriendlyFire)
        self.afk_rounds:       int = ji.get("afk_rounds")       # e.g. 0
        self.rounds_in_spawn:  int = ji.get("rounds_in_spawn")  # e.g. 0

    def as_dict(self) -> dict:
        return self.__data


class PlayerBehaviorFriendlyFire:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.incoming: int = ji.get("incoming")  # e.g. 0
        self.outgoing: int = ji.get("outgoing")  # e.g. 0

    def as_dict(self) -> dict:
        return self.__data


class PlayerAbilityCasts:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.c_cast: Optional[int] = ji.get("c_cast")  # e.g. 16
        self.q_cast: Optional[int] = ji.get("q_cast")  # e.g. 5
        self.e_cast: Optional[int] = ji.get("e_cast")  # e.g. 26
        self.x_cast: Optional[int] = ji.get("x_cast")  # e.g. 0

    def as_dict(self) -> dict:
        return self.__data


class PlayerPlatform:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.os:   "PlayerPlatformOs" = ji.get("os", cast=PlayerPlatformOs)
        self.type: str = ji.get("type")  # e.g. "PC"

    def as_dict(self) -> dict:
        return self.__data


class PlayerPlatformOs:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.name:    str = ji.get("name")    # e.g. "Windows"
        self.version: str = ji.get("version") # e.g. "10.0.22000.1.768.64bit"

    def as_dict(self) -> dict:
        return self.__data


class Coach:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.puuid: str = ji.get("puuid")
        self.team:  str = ji.get("team")

    def as_dict(self) -> dict:
        return self.__data


# ---------------------------------------------------------------------------
# Round models
# ---------------------------------------------------------------------------

class MatchRoundsItem:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.plant_events:  "MatchRoundsItemPlantEvents"  = ji.get("plant_events",  cast=MatchRoundsItemPlantEvents)
        self.defuse_events: "MatchRoundsItemDefuseEvents" = ji.get("defuse_events", cast=MatchRoundsItemDefuseEvents)
        self.player_stats:  List["MatchRoundsItemPlayerStatsItem"] = [
            MatchRoundsItemPlayerStatsItem(e) for e in ji.get("player_stats", [])
        ]
        self.winning_team:  str            = ji.get("winning_team")   # e.g. "Red"
        self.end_type:      str            = ji.get("end_type")       # e.g. "Eliminated"
        self.bomb_planted:  Optional[bool] = ji.get("bomb_planted")
        self.bomb_defused:  Optional[bool] = ji.get("bomb_defused")

    def as_dict(self) -> dict:
        return self.__data


class MatchRoundsItemPlayerStatsItem:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.ability_casts:    "MatchRoundsItemPlayerStatsItemAbilityCasts" = ji.get("ability_casts", cast=MatchRoundsItemPlayerStatsItemAbilityCasts)
        self.damage_events:    List["MatchRoundsItemPlayerStatsItemDamageEventsItem"] = [
            MatchRoundsItemPlayerStatsItemDamageEventsItem(e) for e in ji.get("damage_events", [])
        ]
        self.kill_events:      List["MatchRoundsItemPlayerStatsItemKillEventsItem"] = [
            MatchRoundsItemPlayerStatsItemKillEventsItem(e) for e in ji.get("kill_events", [])
        ]
        self.economy:          "MatchRoundsItemPlayerStatsItemEconomy" = ji.get("economy", cast=MatchRoundsItemPlayerStatsItemEconomy)
        self.player_puuid:         str  = ji.get("player_puuid")
        self.player_display_name:  str  = ji.get("player_display_name")   # e.g. "Henrik3#EUW3"
        self.player_team:          str  = ji.get("player_team")           # e.g. "Red"
        self.damage:               int  = ji.get("damage")
        self.bodyshots:            int  = ji.get("bodyshots")
        self.headshots:            int  = ji.get("headshots")
        self.legshots:             int  = ji.get("legshots")
        self.kills:                int  = ji.get("kills")
        self.score:                int  = ji.get("score")
        self.was_afk:              bool = ji.get("was_afk")
        self.was_penalized:        bool = ji.get("was_penalized")
        self.stayed_in_spawn:      bool = ji.get("stayed_in_spawn")

    def as_dict(self) -> dict:
        return self.__data


class MatchRoundsItemPlayerStatsItemKillEventsItem:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.victim_death_location: "MatchRoundsItemPlayerStatsItemKillEventsItemVictimDeathLocation" = ji.get(
            "victim_death_location", cast=MatchRoundsItemPlayerStatsItemKillEventsItemVictimDeathLocation
        )
        self.damage_weapon_assets: "MatchRoundsItemPlayerStatsItemKillEventsItemDamageWeaponAssets" = ji.get(
            "damage_weapon_assets", cast=MatchRoundsItemPlayerStatsItemKillEventsItemDamageWeaponAssets
        )
        self.player_locations_on_kill: List["MatchRoundsItemPlayerStatsItemKillEventsItemPlayerLocationsOnKillItem"] = [
            MatchRoundsItemPlayerStatsItemKillEventsItemPlayerLocationsOnKillItem(e)
            for e in ji.get("player_locations_on_kill", [])
        ]
        self.assistants: List["MatchRoundsItemPlayerStatsItemKillEventsItemAssistantsItem"] = [
            MatchRoundsItemPlayerStatsItemKillEventsItemAssistantsItem(e)
            for e in ji.get("assistants", [])
        ]
        self.kill_time_in_round:  int  = ji.get("kill_time_in_round")   # ms from round start
        self.kill_time_in_match:  int  = ji.get("kill_time_in_match")   # ms from match start
        self.killer_puuid:        str  = ji.get("killer_puuid")
        self.killer_display_name: str  = ji.get("killer_display_name")
        self.killer_team:         str  = ji.get("killer_team")
        self.victim_puuid:        str  = ji.get("victim_puuid")
        self.victim_display_name: str  = ji.get("victim_display_name")
        self.victim_team:         str  = ji.get("victim_team")
        self.damage_weapon_id:    str  = ji.get("damage_weapon_id")
        self.damage_weapon_name:  str  = ji.get("damage_weapon_name")   # e.g. "Vandal"
        self.secondary_fire_mode: bool = ji.get("secondary_fire_mode")

    def as_dict(self) -> dict:
        return self.__data


class MatchRoundsItemPlayerStatsItemKillEventsItemAssistantsItem:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.assistant_puuid:        str = ji.get("assistant_puuid")
        self.assistant_display_name: str = ji.get("assistant_display_name")
        self.assistant_team:         str = ji.get("assistant_team")

    def as_dict(self) -> dict:
        return self.__data


class MatchRoundsItemPlayerStatsItemKillEventsItemDamageWeaponAssets:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.display_icon:  str = ji.get("display_icon")
        self.killfeed_icon: str = ji.get("killfeed_icon")

    def as_dict(self) -> dict:
        return self.__data


class MatchRoundsItemPlayerStatsItemKillEventsItemVictimDeathLocation:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.x: int = ji.get("x")
        self.y: int = ji.get("y")

    def as_dict(self) -> dict:
        return self.__data


class MatchRoundsItemPlayerStatsItemKillEventsItemPlayerLocationsOnKillItem:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.location: "MatchRoundsItemPlayerStatsItemKillEventsItemPlayerLocationsOnKillItemLocation" = ji.get(
            "location",
            cast=MatchRoundsItemPlayerStatsItemKillEventsItemPlayerLocationsOnKillItemLocation,
        )
        self.player_puuid:        str   = ji.get("player_puuid")
        self.player_display_name: str   = ji.get("player_display_name")
        self.player_team:         str   = ji.get("player_team")
        self.view_radians:        float = ji.get("view_radians")

    def as_dict(self) -> dict:
        return self.__data


class MatchRoundsItemPlayerStatsItemKillEventsItemPlayerLocationsOnKillItemLocation:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.x: int = ji.get("x")
        self.y: int = ji.get("y")

    def as_dict(self) -> dict:
        return self.__data


class MatchRoundsItemPlayerStatsItemAbilityCasts:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.c_casts: Optional[int] = ji.get("c_casts")
        self.q_casts: Optional[int] = ji.get("q_casts")
        self.e_casts: Optional[int] = ji.get("e_cast")   # note: API key inconsistency
        self.x_casts: Optional[int] = ji.get("x_cast")   # note: API key inconsistency

    def as_dict(self) -> dict:
        return self.__data


class MatchRoundsItemPlayerStatsItemDamageEventsItem:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.receiver_puuid:        str = ji.get("receiver_puuid")
        self.receiver_display_name: str = ji.get("receiver_display_name")
        self.receiver_team:         str = ji.get("receiver_team")
        self.bodyshots: int = ji.get("bodyshots")
        self.damage:    int = ji.get("damage")
        self.headshots: int = ji.get("headshots")
        self.legshots:  int = ji.get("legshots")

    def as_dict(self) -> dict:
        return self.__data


class MatchRoundsItemPlayerStatsItemEconomy:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.weapon:        "MatchRoundsItemPlayerStatsItemEconomyWeapon" = ji.get("weapon", cast=MatchRoundsItemPlayerStatsItemEconomyWeapon)
        self.armor:         "MatchRoundsItemPlayerStatsItemEconomyArmor"  = ji.get("armor",  cast=MatchRoundsItemPlayerStatsItemEconomyArmor)
        self.loadout_value: int = ji.get("loadout_value")
        self.remaining:     int = ji.get("remaining")
        self.spent:         int = ji.get("spent")

    def as_dict(self) -> dict:
        return self.__data


class MatchRoundsItemPlayerStatsItemEconomyWeapon:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.assets: "MatchRoundsItemPlayerStatsItemEconomyWeaponAssets" = ji.get("assets", cast=MatchRoundsItemPlayerStatsItemEconomyWeaponAssets)
        self.id:   str = ji.get("id")
        self.name: str = ji.get("name")  # e.g. "Spectre"

    def as_dict(self) -> dict:
        return self.__data


class MatchRoundsItemPlayerStatsItemEconomyWeaponAssets:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.display_icon:  str = ji.get("display_icon")
        self.killfeed_icon: str = ji.get("killfeed_icon")

    def as_dict(self) -> dict:
        return self.__data


class MatchRoundsItemPlayerStatsItemEconomyArmor:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.assets: "MatchRoundsItemPlayerStatsItemEconomyArmorAssets" = ji.get("assets", cast=MatchRoundsItemPlayerStatsItemEconomyArmorAssets)
        self.id:   str = ji.get("id")
        self.name: str = ji.get("name")  # e.g. "Heavy Shields"

    def as_dict(self) -> dict:
        return self.__data


class MatchRoundsItemPlayerStatsItemEconomyArmorAssets:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.display_icon: str = ji.get("display_icon")

    def as_dict(self) -> dict:
        return self.__data


# ---------------------------------------------------------------------------
# Defuse / plant event models
# ---------------------------------------------------------------------------

class MatchRoundsItemDefuseEvents:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.defuse_location: "MatchRoundsItemDefuseEventsDefuseLocation" = ji.get(
            "defuse_location", cast=MatchRoundsItemDefuseEventsDefuseLocation
        )
        self.defused_by: "MatchRoundsItemDefuseEventsDefusedBy" = ji.get(
            "defused_by", cast=MatchRoundsItemDefuseEventsDefusedBy
        )
        self.player_locations_on_defuse: List["MatchRoundsItemDefuseEventsPlayerLocationsOnDefuseItem"] = [
            MatchRoundsItemDefuseEventsPlayerLocationsOnDefuseItem(e)
            for e in ji.get("player_locations_on_defuse", [])
        ]
        self.defuse_time_in_round: Optional[int] = ji.get("defuse_time_in_round")

    def as_dict(self) -> dict:
        return self.__data


class MatchRoundsItemDefuseEventsPlayerLocationsOnDefuseItem:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.location: "MatchRoundsItemDefuseEventsPlayerLocationsOnDefuseItemLocation" = ji.get(
            "location", cast=MatchRoundsItemDefuseEventsPlayerLocationsOnDefuseItemLocation
        )
        self.player_puuid:        str   = ji.get("player_puuid")
        self.player_display_name: str   = ji.get("player_display_name")
        self.player_team:         str   = ji.get("player_team")
        self.view_radians:        float = ji.get("view_radians")

    def as_dict(self) -> dict:
        return self.__data


class MatchRoundsItemDefuseEventsPlayerLocationsOnDefuseItemLocation:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.x: int = ji.get("x")
        self.y: int = ji.get("y")

    def as_dict(self) -> dict:
        return self.__data


class MatchRoundsItemDefuseEventsDefusedBy:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.puuid:        str = ji.get("puuid")
        self.display_name: str = ji.get("display_name")
        self.team:         str = ji.get("team")

    def as_dict(self) -> dict:
        return self.__data


class MatchRoundsItemDefuseEventsDefuseLocation:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.x: int = ji.get("x")
        self.y: int = ji.get("y")

    def as_dict(self) -> dict:
        return self.__data


class MatchRoundsItemPlantEvents:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.plant_location: "MatchRoundsItemPlantEventsPlantLocation" = ji.get(
            "plant_location", cast=MatchRoundsItemPlantEventsPlantLocation
        )
        self.planted_by: "MatchRoundsItemPlantEventsPlantedBy" = ji.get(
            "planted_by", cast=MatchRoundsItemPlantEventsPlantedBy
        )
        self.player_locations_on_plant: List["MatchRoundsItemPlantEventsPlayerLocationsOnPlantItem"] = [
            MatchRoundsItemPlantEventsPlayerLocationsOnPlantItem(e)
            for e in ji.get("player_locations_on_plant", [])
        ]
        self.plant_site:          Optional[str] = ji.get("plant_site")           # e.g. "A"
        self.plant_time_in_round: Optional[int] = ji.get("plant_time_in_round")

    def as_dict(self) -> dict:
        return self.__data


class MatchRoundsItemPlantEventsPlayerLocationsOnPlantItem:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.location: "MatchRoundsItemPlantEventsPlayerLocationsOnPlantItemLocation" = ji.get(
            "location", cast=MatchRoundsItemPlantEventsPlayerLocationsOnPlantItemLocation
        )
        self.player_puuid:        str   = ji.get("player_puuid")
        self.player_display_name: str   = ji.get("player_display_name")
        self.player_team:         str   = ji.get("player_team")
        self.view_radians:        float = ji.get("view_radians")

    def as_dict(self) -> dict:
        return self.__data


class MatchRoundsItemPlantEventsPlayerLocationsOnPlantItemLocation:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.x: int = ji.get("x")
        self.y: int = ji.get("y")

    def as_dict(self) -> dict:
        return self.__data


class MatchRoundsItemPlantEventsPlantedBy:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.puuid:        str = ji.get("puuid")
        self.display_name: str = ji.get("display_name")
        self.team:         str = ji.get("team")

    def as_dict(self) -> dict:
        return self.__data


class MatchRoundsItemPlantEventsPlantLocation:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.x: int = ji.get("x")
        self.y: int = ji.get("y")

    def as_dict(self) -> dict:
        return self.__data


# ---------------------------------------------------------------------------
# Team models
# ---------------------------------------------------------------------------

class MatchTeams:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.red:  "Team" = ji.get("red",  cast=Team)
        self.blue: "Team" = ji.get("blue", cast=Team)

    def as_dict(self) -> dict:
        return self.__data


class Team:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.has_won:     Optional[bool] = ji.get("has_won")
        self.rounds_won:  Optional[int]  = ji.get("rounds_won")
        self.rounds_lost: Optional[int]  = ji.get("rounds_lost")

    def as_dict(self) -> dict:
        return self.__data


class TeamRoster:
    """Premier team roster data (currently unused in ``Team``)."""

    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.members:       List[str]              = list(ji.get("members", []))
        self.name:          str                    = ji.get("name")
        self.tag:           str                    = ji.get("tag")
        self.customization: "TeamRosterCustomization" = ji.get("customization", cast=TeamRosterCustomization)

    def as_dict(self) -> dict:
        return self.__data


class TeamRosterCustomization:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.icon:      str = ji.get("icon")
        self.image:     str = ji.get("image")
        self.primary:   str = ji.get("primary")
        self.secondary: str = ji.get("secondary")
        self.tertiary:  str = ji.get("tertiary")

    def as_dict(self) -> dict:
        return self.__data


# ---------------------------------------------------------------------------
# Observer models
# ---------------------------------------------------------------------------

class Observer:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.puuid:            str                    = ji.get("puuid")
        self.name:             str                    = ji.get("name")
        self.tag:              str                    = ji.get("tag")
        self.platform:         "ObserverPlatform"     = ji.get("platform",         cast=ObserverPlatform)
        self.session_playtime: "ObserverSessionPlaytime" = ji.get("session_playtime", cast=ObserverSessionPlaytime)
        self.team:             str                    = ji.get("team")
        self.level:            float                  = ji.get("level")
        self.player_card:      str                    = ji.get("player_card")
        self.player_title:     str                    = ji.get("player_title")
        self.party_id:         str                    = ji.get("party_id")

    def as_dict(self) -> dict:
        return self.__data


class ObserverPlatform:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.os:   "ObserverPlatformOs" = ji.get("os", cast=ObserverPlatformOs)
        self.type: str = ji.get("type")  # e.g. "PC"

    def as_dict(self) -> dict:
        return self.__data


class ObserverPlatformOs:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.name:    str = ji.get("name")    # e.g. "Windows"
        self.version: str = ji.get("version") # e.g. "10.0.22000.1.768.64bit"

    def as_dict(self) -> dict:
        return self.__data


class ObserverSessionPlaytime:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.minutes:      int = ji.get("minutes")
        self.seconds:      int = ji.get("seconds")
        self.milliseconds: int = ji.get("milliseconds")

    def as_dict(self) -> dict:
        return self.__data


# ---------------------------------------------------------------------------
# Match metadata
# ---------------------------------------------------------------------------

class MatchMetadata:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.map:                 MapsEnum     = ji.get("map")
        self.mode:                ModesEnum    = ji.get("mode")
        self.mode_id:             ModeIdsEnum  = ji.get("mode_id")
        self.season_id:           str          = ji.get("season_id")
        self.match_id:            str          = ji.get("matchid")
        self.premier_info:        "MatchMetadataPremierInfo" = ji.get("premier_info", cast=MatchMetadataPremierInfo)
        self.region:              RegionsEnum  = ji.get("region")
        self.game_version:        str          = ji.get("game_version")    # e.g. "release-03.12-shipping-16-649370"
        self.game_length:         int          = ji.get("game_length")     # milliseconds
        self.game_start:          int          = ji.get("game_start")      # Unix timestamp (seconds)
        self.game_start_patched:  str          = ji.get("game_start_patched")
        self.rounds_played:       int          = ji.get("rounds_played")
        self.queue:               str          = ji.get("queue")           # e.g. "Standard"
        self.platform:            str          = ji.get("platform")        # e.g. "PC"
        self.cluster:             str          = ji.get("cluster")         # e.g. "London"

    @property
    def datetime(self) -> ShortDatetime:
        """Game start time as a :class:`ShortDatetime`."""
        return ShortDatetime.fromtimestamp(self.game_start)

    @property
    def url(self) -> str:
        """VTL tracker URL for this match."""
        return f"https://vtl.lol/match/{self.match_id}"

    def as_dict(self) -> dict:
        return self.__data


class MatchMetadataPremierInfo:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.tournament_id: Optional[str] = ji.get("tournament_id")
        self.matchup_id:    Optional[str] = ji.get("matchup_id")

    def as_dict(self) -> dict:
        return self.__data


# ---------------------------------------------------------------------------
# MMR models
# ---------------------------------------------------------------------------

class V1mmrImages:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.small:         Optional[str] = ji.get("small")
        self.large:         Optional[str] = ji.get("large")
        self.triangle_down: Optional[str] = ji.get("triangle_down")
        self.triangle_up:   Optional[str] = ji.get("triangle_up")

    def as_dict(self) -> dict:
        return self.__data


class V1mmr:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.images:                 "V1mmrImages"  = ji.get("data.images", cast=V1mmrImages)
        self.currenttier:            Optional[int]  = ji.get("data.currenttier")           # e.g. 12
        self.currenttier_patched:    Optional[str]  = ji.get("data.currenttier_patched")   # e.g. "Gold 1"
        self.ranking_in_tier:        Optional[int]  = ji.get("data.ranking_in_tier")
        self.mmr_change_to_last_game:Optional[int]  = ji.get("data.mmr_change_to_last_game")
        self.elo:                    Optional[int]  = ji.get("data.elo")
        self.name:                   Optional[str]  = ji.get("data.name")
        self.tag:                    Optional[str]  = ji.get("data.tag")
        self.old:                    bool           = ji.get("data.old")

    def as_dict(self) -> dict:
        return self.__data


class V2mmrHighestRank:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.old:          bool           = ji.get("old")
        self.tier:         Optional[int]  = ji.get("tier")           # e.g. 19
        self.patched_tier: Optional[str]  = ji.get("patched_tier")   # e.g. "Diamond 2"
        self.season:       Optional[str]  = ji.get("season")         # e.g. "e5a3"

    def as_dict(self) -> dict:
        return self.__data


class V2mmrBySeason:
    """Per-act MMR data for all tracked acts (e1a1 through e6a3)."""

    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        # Acts are listed newest-first for convenience
        self.e6a3: "BySeason" = ji.get("e6a3", cast=BySeason)
        self.e6a2: "BySeason" = ji.get("e6a2", cast=BySeason)
        self.e6a1: "BySeason" = ji.get("e6a1", cast=BySeason)
        self.e5a3: "BySeason" = ji.get("e5a3", cast=BySeason)
        self.e5a2: "BySeason" = ji.get("e5a2", cast=BySeason)
        self.e5a1: "BySeason" = ji.get("e5a1", cast=BySeason)
        self.e4a3: "BySeason" = ji.get("e4a3", cast=BySeason)
        self.e4a2: "BySeason" = ji.get("e4a2", cast=BySeason)
        self.e4a1: "BySeason" = ji.get("e4a1", cast=BySeason)
        self.e3a3: "BySeason" = ji.get("e3a3", cast=BySeason)
        self.e3a2: "BySeason" = ji.get("e3a2", cast=BySeason)
        self.e3a1: "BySeason" = ji.get("e3a1", cast=BySeason)
        self.e2a3: "BySeason" = ji.get("e2a3", cast=BySeason)
        self.e2a2: "BySeason" = ji.get("e2a2", cast=BySeason)
        self.e2a1: "BySeason" = ji.get("e2a1", cast=BySeason)
        self.e1a3: "BySeason" = ji.get("e1a3", cast=BySeason)
        self.e1a2: "BySeason" = ji.get("e1a2", cast=BySeason)
        self.e1a1: "BySeason" = ji.get("e1a1", cast=BySeason)

    def as_dict(self) -> dict:
        return self.__data


class BySeason:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.act_rank_wins:     List["BySeasonActRankWinsItem"] = [
            BySeasonActRankWinsItem(e) for e in ji.get("act_rank_wins", [])
        ]
        self.error:             Optional[bool] = ji.get("error")
        self.wins:              int  = ji.get("wins")
        self.number_of_games:   int  = ji.get("number_of_games")
        self.final_rank:        int  = ji.get("final_rank")
        self.final_rank_patched:str  = ji.get("final_rank_patched")  # e.g. "Gold 1"
        self.old:               bool = ji.get("old")

    def as_dict(self) -> dict:
        return self.__data


class BySeasonActRankWinsItem:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.patched_tier: str = ji.get("patched_tier")  # e.g. "Gold 1"
        self.tier:         int = ji.get("tier")          # e.g. 12

    def as_dict(self) -> dict:
        return self.__data


class V2mmrCurrentData:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.images:                  "V2mmrCurrentDataImages" = ji.get("images", cast=V2mmrCurrentDataImages)
        self.currenttier:             Optional[int] = ji.get("currenttier")
        self.currenttier_patched:     Optional[str] = ji.get("currenttierpatched")  # note: API omits underscore
        self.ranking_in_tier:         Optional[int] = ji.get("ranking_in_tier")
        self.mmr_change_to_last_game: Optional[int] = ji.get("mmr_change_to_last_game")
        self.elo:                     Optional[int] = ji.get("elo")
        self.old:                     bool          = ji.get("old")

    def as_dict(self) -> dict:
        return self.__data


class V2mmrCurrentDataImages:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.small:         Optional[str] = ji.get("small")
        self.large:         Optional[str] = ji.get("large")
        self.triangle_down: Optional[str] = ji.get("triangle_down")
        self.triangle_up:   Optional[str] = ji.get("triangle_up")

    def as_dict(self) -> dict:
        return self.__data


class V2mmr:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.current_data: "V2mmrCurrentData" = ji.get("data.current_data", cast=V2mmrCurrentData)
        self.highest_rank: "V2mmrHighestRank" = ji.get("data.highest_rank", cast=V2mmrHighestRank)
        self.by_season:    "V2mmrBySeason"    = ji.get("data.by_season",    cast=V2mmrBySeason)
        self.name:         Optional[str]      = ji.get("data.name")  # e.g. "Henrik3"
        self.tag:          Optional[str]      = ji.get("data.tag")   # e.g. "EUW3"

    def as_dict(self) -> dict:
        return self.__data


# ---------------------------------------------------------------------------
# Legacy MMR history (v1)
# ---------------------------------------------------------------------------

class V1mmrh:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.data: List["V1mmrhDataItem"] = [V1mmrhDataItem(e) for e in ji.get("data", [])]
        self.name: str = ji.get("name")  # e.g. "Henrik3"
        self.tag:  str = ji.get("tag")   # e.g. "EUW3"

    def as_dict(self) -> dict:
        return self.__data


class V1mmrhDataItem:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.images:                  "V1mmrhDataItemImages" = ji.get("images", cast=V1mmrhDataItemImages)
        self.map:                     "V1mmrhDataItemMap"    = ji.get("map",    cast=V1mmrhDataItemMap)
        self.currenttier:             int = ji.get("currenttier")
        self.currenttier_patched:     str = ji.get("currenttier_patched")   # e.g. "Gold 1"
        self.match_id:                str = ji.get("match_id")
        self.season_id:               str = ji.get("season_id")
        self.ranking_in_tier:         int = ji.get("ranking_in_tier")
        self.mmr_change_to_last_game: int = ji.get("mmr_change_to_last_game")
        self.elo:                     int = ji.get("elo")
        self.date:                    str = ji.get("date")      # e.g. "Tuesday, January 11, 2022 9:52 PM"
        self.date_raw:                int = ji.get("date_raw")  # Unix timestamp

    def as_dict(self) -> dict:
        return self.__data


class V1mmrhDataItemMap:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.name: str = ji.get("name")  # e.g. "Icebox"
        self.id:   str = ji.get("id")

    def as_dict(self) -> dict:
        return self.__data


class V1mmrhDataItemImages:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.small:         str = ji.get("small")
        self.large:         str = ji.get("large")
        self.triangle_down: str = ji.get("triangle_down")
        self.triangle_up:   str = ji.get("triangle_up")

    def as_dict(self) -> dict:
        return self.__data


# ---------------------------------------------------------------------------
# Queue status
# ---------------------------------------------------------------------------

class GetMatchesByNameResponse:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.data: List["Match"] = [Match(e) for e in ji.get("data", [])]

    def as_dict(self) -> dict:
        return self.__data


class V1QueueStatusDataItem:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.mode:                   ModesEnum    = ji.get("mode")
        self.mode_id:                ModeIdsEnum  = ji.get("mode_id")
        self.enabled:                bool         = ji.get("enabled")
        self.team_size:              float        = ji.get("team_size")
        self.number_of_teams:        float        = ji.get("number_of_teams")
        self.party_size:             "V1QueueStatusDataItemPartySize"    = ji.get("party_size",   cast=V1QueueStatusDataItemPartySize)
        self.high_skill:             "V1QueueStatusDataItemHighSkill"    = ji.get("high_skill",   cast=V1QueueStatusDataItemHighSkill)
        self.ranked:                 bool         = ji.get("ranked")
        self.tournament:             bool         = ji.get("tournament")
        self.skill_disparity:        List["V1QueueStatusDataItemSkillDisparityItem"] = [
            V1QueueStatusDataItemSkillDisparityItem(e) for e in ji.get("skill_disparity", [])
        ]
        self.required_account_level: float        = ji.get("required_account_level")
        self.game_rules:             "V1QueueStatusDataItemGameRules"    = ji.get("game_rules",   cast=V1QueueStatusDataItemGameRules)
        self.platforms:              List[PlatformsEnum] = list(ji.get("platforms", []))
        self.maps:                   List["V1QueueStatusDataItemMapsItem"] = [
            V1QueueStatusDataItemMapsItem(e) for e in ji.get("maps", [])
        ]

    def as_dict(self) -> dict:
        return self.__data


class V1QueueStatusDataItemGameRules:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.overtime_win_by_two:         bool = ji.get("overtime_win_by_two")
        self.allow_lenient_surrender:     bool = ji.get("allow_lenient_surrender")
        self.allow_drop_out:              bool = ji.get("allow_drop_out")
        self.assign_random_agents:        bool = ji.get("assign_random_agents")
        self.skip_pregame:                bool = ji.get("skip_pregame")
        self.allow_overtime_draw_vote:    bool = ji.get("allow_overtime_draw_vote")
        self.overtime_win_by_two_capped:  bool = ji.get("overtime_win_by_two_capped")
        self.premier_mode:                bool = ji.get("premier_mode")

    def as_dict(self) -> dict:
        return self.__data


class V1QueueStatusDataItemHighSkill:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.max_party_size: float = ji.get("max_party_size")
        self.min_tier:       float = ji.get("min_tier")
        self.max_tier:       float = ji.get("max_tier")

    def as_dict(self) -> dict:
        return self.__data


class V1QueueStatusDataItemSkillDisparityItem:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.tier:     float      = ji.get("tier")
        self.name:     TiersEnum  = ji.get("name")
        self.max_tier: "V1QueueStatusDataItemSkillDisparityItemMaxTier" = ji.get(
            "max_tier", cast=V1QueueStatusDataItemSkillDisparityItemMaxTier
        )

    def as_dict(self) -> dict:
        return self.__data


class V1QueueStatusDataItemSkillDisparityItemMaxTier:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.id:   float     = ji.get("id")
        self.name: TiersEnum = ji.get("name")

    def as_dict(self) -> dict:
        return self.__data


class V1QueueStatusDataItemPartySize:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.max:               float       = ji.get("max")
        self.min:               float       = ji.get("min")
        self.invalid:           List[float] = list(ji.get("invalid", []))
        self.full_party_bypass: bool        = ji.get("full_party_bypass")

    def as_dict(self) -> dict:
        return self.__data


class V1QueueStatusDataItemMapsItem:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.map:     "V1QueueStatusDataItemMapsItemMap" = ji.get("map", cast=V1QueueStatusDataItemMapsItemMap)
        self.enabled: bool = ji.get("enabled")

    def as_dict(self) -> dict:
        return self.__data


class V1QueueStatusDataItemMapsItemMap:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.id:   str      = ji.get("id")
        self.name: MapsEnum = ji.get("name")

    def as_dict(self) -> dict:
        return self.__data


class V1QueueStatus:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.data: List["V1QueueStatusDataItem"] = [
            V1QueueStatusDataItem(e) for e in ji.get("data", [])
        ]

    def as_dict(self) -> dict:
        return self.__data


class GetApiVersionResponse:
    def __init__(self, data: dict) -> None:
        self.__data = data
        ji = JsonInjester(data)
        self.build_ver:       str = ji.get("data.build_ver")        # e.g. "04.00.00.655657"
        self.build_date:      str = ji.get("data.build_date")       # e.g. "Apr  2 2024"
        self.version:         str = ji.get("data.version")          # e.g. "15"
        self.version_for_api: str = ji.get("data.version_for_api")  # e.g. "release-04.00-shipping-20-655657"
        self.branch:          str = ji.get("data.branch")           # e.g. "release-04.00"
        self.region:          str = ji.get("data.region")           # e.g. "EU"

    def as_dict(self) -> dict:
        return self.__data


# ===========================================================================
# Manually created helper class
# ===========================================================================

class MatchReference:
    """Lightweight match reference extracted from raw match-history payloads.

    Used internally by :meth:`UnofficialApi.get_match_history` to build the
    sorted history list without deserialising full match objects.

    Args:
        match_info: Raw dict from the ``History`` array in a match-history
                    response (keys follow PascalCase Riot API conventions).
    """

    def __init__(self, match_info: dict) -> None:
        self.__dict_data  = match_info
        self.match_id:    str          = match_info["MatchID"]
        self.timestamp:   int          = match_info["GameStartTime"]   # Unix ms
        self.timestamp_str: str        = self._format_timestamp(self.timestamp)
        self.gamemode:    ModesApiEnum = ModesApiEnum(match_info["QueueID"])

    @staticmethod
    def _format_timestamp(millis: int) -> str:
        """Convert a millisecond Unix timestamp to a human-readable string."""
        return datetime.fromtimestamp(millis / 1000).strftime("%Y-%m-%d %H:%M:%S")

    def as_dict(self) -> dict:
        return self.__dict_data