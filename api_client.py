"""
api_client.py

Provides a Valorant in-client API wrapper (ClientApi) that interfaces with
Riot's local Riot Client lockfile and remote PVP/GLZ endpoints, as well as a
Player data class that enriches raw player response objects with rank, name,
and tracker info.
"""

import time
from typing import List, Union

import requests
import urllib3
from urllib.parse import quote
import certifi
import base64
import os

from db_valorant import ValorantDB
from utils import singleton
from prettytable import PrettyTable
from agent_name_enum import AgentName


# Suppress SSL warnings from unverified local requests (Riot Client uses self-signed certs)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

db = ValorantDB()


class Player:
    """
    Represents a Valorant player in the current match lobby.

    Populated from a raw player dict returned by the pregame/coregame API,
    enriched with rank, display name, and a tracker URL.
    """

    def __init__(self, player_data_response_obj: dict):
        player = player_data_response_obj
        api = ClientApi()

        self.puuid: str = player["Subject"]
        self.agent: str = api.get_agent_name_from_id(player["CharacterID"])
        self.streamer_mode: bool = player["PlayerIdentity"]["Incognito"]
        self.username, self.tagline = api.get_name_from_puuid(self.puuid)
        self.tag = self.tagline  # Alias for tagline; used in display/fallback logic
        self.level: int = player["PlayerIdentity"]["AccountLevel"]
        self.rank, self.rr = api.get_current_rank_from_puuid(self.puuid)
        self.tracker: str = f"https://vtl.lol/id/{quote(self.username)}_{quote(self.tagline)}"

        # TeamID is only present in coregame responses, not pregame
        self.team: str | None = player.get("TeamID")


@singleton
class ClientApi:
    """
    Singleton wrapper around Riot's local and remote Valorant APIs.

    On first access the lockfile is read to obtain the local port/password,
    and auth headers are lazily initialised from the local entitlements endpoint.

    Regions map to the following base URLs:
      - GLZ  : https://glz-<region>-1.<region>.a.pvp.net  (game/lobby)
      - PD   : https://pd.<region>.a.pvp.net               (player data/MMR)
      - USW2 : https://usw2.pp.sgp.pvp.net                 (party/social graph)
    """

    # All Valorant competitive tiers in rank order (indices 0–2 reserved for Unranked)
    VALORANT_RANKS = [
        "Unranked", "Unranked", "Unranked",
        "Iron 1",     "Iron 2",     "Iron 3",
        "Bronze 1",   "Bronze 2",   "Bronze 3",
        "Silver 1",   "Silver 2",   "Silver 3",
        "Gold 1",     "Gold 2",     "Gold 3",
        "Platinum 1", "Platinum 2", "Platinum 3",
        "Diamond 1",  "Diamond 2",  "Diamond 3",
        "Ascendant 1","Ascendant 2","Ascendant 3",
        "Immortal 1", "Immortal 2", "Immortal 3",
        "Radiant",
    ]

    def __init__(self, region: str = 'na'):
        self.region = region
        self.glz_url  = f"https://glz-{region}-1.{region}.a.pvp.net"
        self.pd_url   = f"https://pd.{region}.a.pvp.net"
        self.usw2_url = "https://usw2.pp.sgp.pvp.net"

        # Private backing fields — accessed via properties to enable lazy init
        self.__lockfile   = None
        self.__headers    = None
        self.__puuid      = None
        self.__agents_map = None

        # Pre-compute the Basic auth token from the lockfile password once
        self.auth_token = base64.b64encode(
            f"riot:{self.lockfile['password']}".encode()
        ).decode()

        # Simple in-memory request cache; keyed by logical resource name
        self.cache: dict = {}

    # ------------------------------------------------------------------
    # Cache helpers
    # ------------------------------------------------------------------

    def clear_cache(self):
        """Invalidate all cached responses (e.g. between matches)."""
        self.cache = {}

    # ------------------------------------------------------------------
    # Lockfile / auth properties (lazily initialised)
    # ------------------------------------------------------------------

    @property
    def lockfile(self) -> dict:
        """
        Parse the Riot Client lockfile and return its fields as a dict.

        Keys: name, PID, port, password, protocol
        Raises an Exception if the lockfile cannot be found (client not running).
        """
        if self.__lockfile is None:
            lockfile_path = os.path.join(
                os.getenv('LOCALAPPDATA'),
                R'Riot Games\Riot Client\Config\lockfile'
            )
            try:
                with open(lockfile_path) as f:
                    keys = ['name', 'PID', 'port', 'password', 'protocol']
                    self.__lockfile = dict(zip(keys, f.read().split(':')))
            except OSError:
                raise Exception(
                    "Lockfile not found — is the Riot Client running?"
                )
        return self.__lockfile

    @property
    def current_version(self) -> str:
        """Fetch the current Valorant client version string from the public API."""
        data = requests.get('https://valorant-api.com/v1/version').json()['data']
        return f"{data['branch']}-shipping-{data['buildVersion']}-{data['version'].split('.')[3]}"

    @property
    def rso_token(self) -> str:
        """Return the RSO (OAuth2) access token, cached after first fetch."""
        if 'rso' not in self.cache:
            self.cache['rso'] = (
                self.handle_local_request('/entitlements/v2/token')
                    .json()['authorization']['accessToken']['token']
            )
        return self.cache['rso']

    @property
    def pas_token(self) -> str:
        """Return the PAS (geo-routing) token, cached after first fetch."""
        if 'pas' not in self.cache:
            self.cache['pas'] = requests.get(
                "https://riot-geo.pas.si.riotgames.com/pas/v1/service/chat",
                headers=self.headers,
            ).content.decode()
        return self.cache['pas']

    @property
    def headers(self) -> dict:
        """
        Build and cache the auth headers required by remote PVP endpoints.

        Fetches the entitlements token from the local Riot Client and constructs:
          - Authorization (Bearer RSO token)
          - X-Riot-Entitlements-JWT
          - X-Riot-ClientPlatform (static base64-encoded platform info)
          - X-Riot-ClientVersion
        """
        if self.__headers is None:
            uri = f"https://127.0.0.1:{self.lockfile['port']}/entitlements/v1/token"
            entitlements = requests.get(
                uri,
                headers={'Authorization': f'Basic {self.auth_token}'},
                verify=False,
            ).json()

            self.__headers = {
                'Authorization': f"Bearer {entitlements['accessToken']}",
                'X-Riot-Entitlements-JWT': entitlements['token'],
                # Static base64 payload encoding platform metadata (PC / Windows)
                'X-Riot-ClientPlatform': (
                    "ew0KCSJwbGF0Zm9ybVR5cGUiOiAiUEMiLA0KCSJwbGF0Zm9ybU9TIjogIldpbmRvd3MiLA0K"
                    "CSJwbGF0Zm9ybU9TVmVyc2lvbiI6ICIxMC4wLjE5MDQyLjEuMjU2LjY0Yml0IiwNCgkicGxh"
                    "dGZvcm1DaGlwc2V0IjogIlVua25vd24iDQp9"
                ),
                'X-Riot-ClientVersion': self.current_version,
            }
        return self.__headers

    @property
    def my_puuid(self) -> str:
        """Return the local player's PUUID, cached after first fetch."""
        if self.__puuid is None:
            uri = f"https://127.0.0.1:{self.lockfile['port']}/entitlements/v1/token"
            entitlements = requests.get(
                uri,
                headers={'Authorization': f'Basic {self.auth_token}'},
                verify=False,
            ).json()
            self.__puuid = entitlements['subject']
        return self.__puuid

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def handle_local_request(self, endpoint: str):
        """
        Send a GET request to the local Riot Client HTTP server.

        Uses Basic auth derived from the lockfile password; SSL verification
        is disabled because the client uses a self-signed certificate.
        """
        url = f"https://127.0.0.1:{self.lockfile['port']}{endpoint}"
        return requests.get(
            url,
            headers={'Authorization': f'Basic {self.auth_token}'},
            verify=False,
        )

    def handle_remote_request(self, endpoint: str, basetype: str = 'glz', payload=None):
        """
        Send an authenticated GET request to a remote PVP endpoint.

        Args:
            endpoint:  Path component appended to the chosen base URL.
            basetype:  One of 'glz' (default), 'pd', or 'usw2'.
            payload:   Unused for GET; kept for API consistency.
        """
        baseurl = self._resolve_base_url(basetype)
        return requests.get(baseurl + endpoint, headers=self.headers, verify=False)

    def handle_remote_request_post(self, endpoint: str, payload: dict, basetype: str = 'glz'):
        """
        Send an authenticated POST request to a remote PVP endpoint.

        Args:
            endpoint:  Path component appended to the chosen base URL.
            payload:   JSON body to send.
            basetype:  One of 'glz' (default), 'pd', or 'usw2'.
        """
        baseurl = self._resolve_base_url(basetype)
        return requests.post(
            baseurl + endpoint,
            json=payload,
            headers=self.headers,
            verify=certifi.where(),
        )

    def _resolve_base_url(self, basetype: str) -> str:
        """Map a basetype string to its corresponding base URL."""
        if basetype == 'usw2':
            return self.usw2_url
        return self.glz_url if basetype == 'glz' else self.pd_url

    # ------------------------------------------------------------------
    # Agent helpers
    # ------------------------------------------------------------------

    @property
    def agents_map(self) -> dict:
        """
        Return a dict mapping agent UUID → display name.

        Fetched once from valorant-api.com and cached for the lifetime
        of the singleton.
        """
        if self.__agents_map is None:
            agents = requests.get("https://valorant-api.com/v1/agents").json()['data']
            self.__agents_map = {
                agent['uuid']: agent['displayName'] for agent in agents
            }
        return self.__agents_map

    def get_agent_name_from_id(self, char_id: str) -> str:
        """Resolve a CharacterID UUID to a human-readable agent name."""
        return self.agents_map.get(char_id, "N/A")

    # ------------------------------------------------------------------
    # Player / rank lookups
    # ------------------------------------------------------------------

    def get_current_rank_from_puuid(self, puuid: str) -> tuple[str, int]:
        """
        Return (rank_name, ranked_rating) for a player's most recent competitive match.

        Falls back to ("Unranked", 0) if the player has no competitive history.
        """
        endpoint = (
            self.pd_url
            + f"/mmr/v1/players/{puuid}/competitiveupdates"
            + "?startIndex=0&endIndex=20&queue=competitive"
        )
        response = requests.get(endpoint, headers=self.headers, verify=False).json()

        if not response["Matches"]:
            return ("Unranked", 0)

        latest = response["Matches"][0]
        rank_name = self.VALORANT_RANKS[latest["TierAfterUpdate"]]
        rr = latest["RankedRatingAfterUpdate"]
        return (rank_name, rr)

    def get_name_from_puuid(self, puuid: str) -> tuple[str, str]:
        """Return (GameName, TagLine) for the given PUUID via the name-service."""
        endpoint = self.pd_url + "/name-service/v2/players"
        data = requests.put(
            endpoint, headers=self.headers, json=[puuid], verify=False
        ).json()[0]
        return (data["GameName"], data["TagLine"])

    # ------------------------------------------------------------------
    # Party management
    # ------------------------------------------------------------------

    def get_my_party_id(self) -> str:
        """Return the local player's current party ID."""
        endpoint = self.glz_url + f"/parties/v1/players/{self.my_puuid}"
        return requests.get(endpoint, headers=self.headers, verify=False).json()['CurrentPartyID']

    def get_current_party_from_id(self, party_id: str) -> dict:
        """Return the full party object for the given party ID."""
        endpoint = self.glz_url + f"/parties/v1/parties/{party_id}"
        return requests.get(endpoint, headers=self.headers, verify=False).json()

    def change_queue(self, index: int):
        """
        Switch the party's selected queue to the Nth eligible queue.

        Args:
            index: 1-based index into the party's EligibleQueues list.
        """
        party_id = self.get_my_party_id()
        available_queues = self.get_current_party_from_id(party_id)["EligibleQueues"]
        endpoint = self.glz_url + f"/parties/v1/parties/{party_id}/queue"
        return requests.post(
            endpoint,
            json={"queueID": available_queues[index - 1]},
            headers=self.headers,
            verify=certifi.where(),
        )

    def join_queue(self):
        """Start matchmaking for the local player's party."""
        party_id = self.get_my_party_id()
        endpoint = self.glz_url + f"/parties/v1/parties/{party_id}/matchmaking/join"
        return requests.post(endpoint, headers=self.headers, verify=certifi.where())

    def leave_queue(self):
        """Cancel matchmaking for the local player's party."""
        party_id = self.get_my_party_id()
        endpoint = self.glz_url + f"/parties/v1/parties/{party_id}/matchmaking/leave"
        return requests.post(endpoint, headers=self.headers, verify=certifi.where())

    # ------------------------------------------------------------------
    # Match ID helpers
    # ------------------------------------------------------------------

    def get_match_id(self, is_pregame: bool = False) -> str | None:
        """
        Return the MatchID for the player's current pregame or core-game session.

        Returns None and prints a message if no match is found.
        """
        match_type = "pregame" if is_pregame else "core-game"
        endpoint = self.glz_url + f"/{match_type}/v1/players/{self.my_puuid}"
        try:
            return requests.get(
                endpoint, headers=self.headers, verify=certifi.where()
            ).json()['MatchID']
        except KeyError:
            print(f"No {match_type} match id found.")

    def get_pregame_match_id(self) -> str | None:
        """Convenience wrapper — returns the current pregame match ID."""
        return self.get_match_id(is_pregame=True)

    def get_coregame_match_id(self) -> str | None:
        """Convenience wrapper — returns the current core-game match ID."""
        return self.get_match_id(is_pregame=False)

    # ------------------------------------------------------------------
    # Blocking wait helpers
    # ------------------------------------------------------------------

    def wait_until_match_found(self):
        """Block until a pregame MatchID appears (polls every 5 s)."""
        response = {}
        while 'MatchID' not in response:
            endpoint = self.glz_url + f"/pregame/v1/players/{self.my_puuid}"
            response = requests.get(
                endpoint, headers=self.headers, verify=certifi.where()
            ).json()
            time.sleep(5)

    def wait_until_match_started(self):
        """Block until a core-game MatchID appears (polls every 5 s)."""
        response = {}
        while 'MatchID' not in response:
            endpoint = self.glz_url + f"/core-game/v1/players/{self.my_puuid}"
            response = requests.get(
                endpoint, headers=self.headers, verify=certifi.where()
            ).json()
            time.sleep(5)

    # ------------------------------------------------------------------
    # Agent select
    # ------------------------------------------------------------------

    def select_pregame_agent(self, agent_name: Union[AgentName, str]):
        """
        Hover (soft-select) an agent during agent select.

        Accepts either an AgentName enum value or a plain string (case-insensitive).
        """
        if isinstance(agent_name, AgentName):
            agent_name = agent_name.value

        agents_by_name = {name.lower(): uuid for uuid, name in self.agents_map.items()}
        agent_id = agents_by_name[agent_name.lower()]

        pregame_id = self.get_pregame_match_id()
        endpoint = self.glz_url + f"/pregame/v1/matches/{pregame_id}/select/{agent_id}"
        return requests.post(endpoint, headers=self.headers, verify=certifi.where())

    def lock_pregame_agent(self, agent_name: str):
        """
        Lock (confirm) the selected agent during agent select.

        Note: there is a double-lookup bug in the original code preserved here;
        review before use in production.
        """
        agents_by_name = {name.lower(): uuid for uuid, name in self.__agents_map.items()}
        agent_id = agents_by_name[agents_by_name[agent_name.lower()]]  # NOTE: double-lookup

        pregame_id = self.get_pregame_match_id()
        endpoint = self.glz_url + f"/pregame/v1/matches/{pregame_id}/lock/{agent_id}"
        return requests.post(endpoint, headers=self.headers, verify=certifi.where())

    # ------------------------------------------------------------------
    # Lobby / match data
    # ------------------------------------------------------------------

    def _fetch_pregame_response(self) -> dict:
        """
        Fetch and cache the pregame match response.

        Raises BaseException if the API returns an HTTP error status.
        """
        if 'pregame_response' not in self.cache:
            endpoint = self.glz_url + f"/pregame/v1/matches/{self.get_pregame_match_id()}"
            response = requests.get(endpoint, headers=self.headers, verify=False).json()
            if 'httpStatus' in response:
                raise BaseException(response['message'])
            self.cache['pregame_response'] = response
        return self.cache['pregame_response']

    def _fetch_coregame_response(self) -> dict:
        """
        Fetch and cache the core-game match response.

        Raises BaseException if the API returns an HTTP error status.
        """
        if 'coregame_response' not in self.cache:
            endpoint = self.glz_url + f"/core-game/v1/matches/{self.get_coregame_match_id()}"
            response = requests.get(endpoint, headers=self.headers, verify=certifi.where()).json()
            if 'httpStatus' in response:
                raise BaseException(response['message'])
            self.cache['coregame_response'] = response
        return self.cache['coregame_response']

    def get_players_in_pre_lobby(self) -> List[Player]:
        """Return a list of Player objects for all allies in the pregame lobby."""
        if 'pregame_players' not in self.cache:
            response = self._fetch_pregame_response()
            self.cache['pregame_players'] = [
                Player(p) for p in response["AllyTeam"]["Players"]
            ]
        return self.cache['pregame_players']

    def get_players_in_core_lobby(self) -> List[Player]:
        """Return a list of Player objects for all players in the active match."""
        if 'coregame_players' not in self.cache:
            response = self._fetch_coregame_response()
            self.cache['coregame_players'] = [
                Player(p) for p in response["Players"]
            ]
        return self.cache['coregame_players']

    def get_pregame_map_name(self) -> str:
        """Return the MapID string for the current pregame match."""
        return self._fetch_pregame_response()['MapID']

    def get_pregame_chat_id(self) -> str:
        """Return the MUC chat room ID for the pregame lobby."""
        return self._fetch_pregame_response()['MUCName']

    def get_coregame_chat_id(self) -> str:
        """Return the team MUC chat room ID for the active match."""
        return self._fetch_coregame_response()['TeamMUCName']

    def get_coregame_allchat_id(self) -> str:
        """Return the all-chat MUC room ID for the active match."""
        return self._fetch_coregame_response()['AllMUCName']

    # ------------------------------------------------------------------
    # Chat
    # ------------------------------------------------------------------

    def send_message(self, message: str, cid: str, chat_type: str = "groupchat"):
        """
        Send a chat message to a given MUC room via the local Riot Client API.

        Args:
            message:   The text to send.
            cid:       The MUC channel ID (from get_*_chat_id helpers).
            chat_type: Chat type string; defaults to "groupchat".
        """
        endpoint = f"https://127.0.0.1:{self.lockfile['port']}/chat/v6/messages"
        return requests.post(
            endpoint,
            json={"message": message, "cid": cid, "type": chat_type},
            headers={'Authorization': f'Basic {self.auth_token}'},
            verify=False,
        )

    # ------------------------------------------------------------------
    # Misc / experimental
    # ------------------------------------------------------------------

    def play_replay(self):
        """
        Attempt to start a solo replay experience for a hard-coded match ID.

        NOTE: This is experimental / incomplete — the match ID is hard-coded.
        """
        match_id = "9d101bde-77bc-56f3-99e0-6487921d846e"
        endpoint = self.glz_url + f"/parties/v1/players/{match_id}/startsoloexperience"
        return requests.post(endpoint, headers=self.headers, verify=certifi.where())


# ----------------------------------------------------------------------
# CLI entry point
# ----------------------------------------------------------------------

if __name__ == "__main__":
    from api_henrik import AffinitiesEnum, UnofficialApi, Match
    import sys

    henrik = UnofficialApi()

    def print_tablurized(obj_list, fields, title, sortby=0, filters={}):
        """
        Print a PrettyTable of objects, selecting attributes by field name.

        Args:
            obj_list: Iterable of objects whose attributes match `fields`.
            fields:   Column names (matched case-insensitively to object attrs).
            title:    Table title string.
            sortby:   Either a field name string or an int index into `fields`.
            filters:  Dict mapping lowercase field name → value transform function.
        """
        table = PrettyTable()
        for obj in obj_list:
            row = []
            for attr in fields:
                attr_lower = attr.lower()
                value = getattr(obj, attr_lower)
                if attr_lower in filters:
                    value = filters[attr_lower](value)
                row.append(str(value))
            table.add_rows([row])

        table.title = title
        table.field_names = fields
        # Accept either a column name string or a positional index
        table.sortby = sortby if isinstance(sortby, str) else fields[sortby]
        print(table)

    # Default to --core if no argument is provided
    arg = "--core"
    if len(sys.argv) > 1:
        arg = sys.argv[1]

    api = ClientApi()

    # Display filters: convert team IDs to coloured emoji indicators
    filters = {
        "team": lambda team: '🔵' if team == "Blue" else '🔴',
    }

    def _fill_missing_names(players: List[Player]):
        """
        Fall back to the Henrik unofficial API for players in streamer mode
        whose username/tagline were returned as empty strings.
        """
        for player in players:
            if player.tag == '' or player.username == '':
                account = henrik.get_account_by_puuid(player.puuid)
                player.username = account.name
                player.tag = account.tag
                player.tagline = account.tag
                player.tracker = (
                    f"https://vtl.lol/id/{quote(player.username)}_{quote(player.tagline)}"
                )

    if arg.lower() == "--pre":
        pregame_data = api.get_players_in_pre_lobby()
        _fill_missing_names(pregame_data)
        fields = ["Username", "Tag", "Agent", "Rank", "RR", "Level", "Tracker"]
        print_tablurized(pregame_data, fields, "Valorant status: Pregame", sortby="Rank", filters=filters)

    elif arg.lower() == "--core":
        coregame_data = api.get_players_in_core_lobby()
        _fill_missing_names(coregame_data)
        fields = ["Team", "Username", "Tag", "Agent", "Rank", "RR", "Level", "Tracker"]
        print_tablurized(coregame_data, fields, "Valorant status: Coregame", sortby="Team", filters=filters)

    else:
        python_exe = sys.executable.split('\\')[-1]
        print(
            "This script must be executed with one of the following arguments:\n"
            "  --pre   Gather stats on your team during agent select\n"
            "  --core  Gather stats on everyone in a currently running match\n\n"
            f"Example: {python_exe} {sys.argv[0]} --pre"
        )