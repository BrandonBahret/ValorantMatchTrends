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
import pyperclip


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

    # Rank → (hex colour, short label) used for badge styling
    RANK_COLOURS = {
        "Unranked":    ("#6b7280", "UNRANKED"),
        "Iron 1":      ("#78716c", "IRON"),
        "Iron 2":      ("#78716c", "IRON"),
        "Iron 3":      ("#78716c", "IRON"),
        "Bronze 1":    ("#b45309", "BRONZE"),
        "Bronze 2":    ("#b45309", "BRONZE"),
        "Bronze 3":    ("#b45309", "BRONZE"),
        "Silver 1":    ("#9ca3af", "SILVER"),
        "Silver 2":    ("#9ca3af", "SILVER"),
        "Silver 3":    ("#9ca3af", "SILVER"),
        "Gold 1":      ("#d97706", "GOLD"),
        "Gold 2":      ("#d97706", "GOLD"),
        "Gold 3":      ("#d97706", "GOLD"),
        "Platinum 1":  ("#0891b2", "PLAT"),
        "Platinum 2":  ("#0891b2", "PLAT"),
        "Platinum 3":  ("#0891b2", "PLAT"),
        "Diamond 1":   ("#7c3aed", "DIAMOND"),
        "Diamond 2":   ("#7c3aed", "DIAMOND"),
        "Diamond 3":   ("#7c3aed", "DIAMOND"),
        "Ascendant 1": ("#059669", "ASC"),
        "Ascendant 2": ("#059669", "ASC"),
        "Ascendant 3": ("#059669", "ASC"),
        "Immortal 1":  ("#dc2626", "IMMORTAL"),
        "Immortal 2":  ("#dc2626", "IMMORTAL"),
        "Immortal 3":  ("#dc2626", "IMMORTAL"),
        "Radiant":     ("#fbbf24", "RADIANT"),
    }

    def _fetch_assets():
        """
        Return (agents_map, tiers_map) dicts keyed by display name / rank name.
        Falls back to empty dicts silently so the page still renders without icons.

        agents_map : { "Reyna": AgentItem, ... }
        tiers_map  : { "Gold 2": TierItem, ... }  keyed by divisionName title-case
        """
        try:
            from api_valorant_assets import ValAssetApi
            assets = ValAssetApi()
            agents_map = assets.get_agents()                     # keyed by displayName
            raw_tiers  = assets.get_competitive_tiers()          # List[TierItem]
            # divisionName looks like "GOLD 2" → normalise to "Gold 2"
            tiers_map  = {t.divisionName.title(): t for t in raw_tiers}
            return agents_map, tiers_map
        except Exception:
            return {}, {}

    def _rank_badge(rank: str, rr: int, tiers_map: dict) -> str:
        """Return an HTML rank badge, using the official rank icon when available."""
        colour, label = RANK_COLOURS.get(rank, ("#6b7280", rank.upper()))
        num     = rank.split()[-1] if rank.split()[-1].isdigit() else ""
        num_html = f'<span class="rank-num">{num}</span>' if num else ""
        tier    = tiers_map.get(rank)
        icon_html = (
            f'<img class="rank-icon" src="{tier.smallIcon}" alt="{rank}">'
            if tier and tier.smallIcon else ""
        )
        return (
            f'<span class="rank-badge" style="--rc:{colour}">'
            f"  {icon_html}"
            f"  <span class='rank-label'>{label}</span>"
            f"  {num_html}"
            f"  <span class='rank-rr'>{rr} RR</span>"
            f"</span>"
        )

    def _gradient_css(colors: list) -> str:
        """Build a CSS linear-gradient string from a list of hex colour strings."""
        if not colors:
            return "#1a1a2e"
        stops = ", ".join(f"#{c}" if not c.startswith("#") else c for c in colors)
        return f"linear-gradient(135deg, {stops})"

    def _player_card(p: "Player", show_team: bool,
                     agents_map: dict, tiers_map: dict,
                     my_puuid: str = "") -> str:
        """Render a single player card div (replaces old <tr> layout)."""
        agent_item  = agents_map.get(p.agent)
        gradient    = _gradient_css(agent_item.backgroundGradientColors if agent_item else [])
        agent_icon  = agent_item.displayIconSmall if agent_item else ""
        rank_html   = _rank_badge(p.rank, p.rr, tiers_map)
        streamer    = "⚠ STREAMER" if p.streamer_mode else ""
        is_me       = my_puuid and p.puuid == my_puuid
        team_class  = ""
        if show_team:
            team_class = "blue-side" if getattr(p, "team", None) == "Blue" else "red-side"
        if is_me:
            team_class = (team_class + " is-me").strip()

        icon_tag = (
            f'<img class="agent-icon" src="{agent_icon}" alt="{p.agent}">'
            if agent_icon else
            f'<div class="agent-icon agent-icon--missing">{p.agent[:2].upper()}</div>'
        )
        streamer_tag = (
            f'<span class="streamer-badge">{streamer}</span>' if streamer else ""
        )
        me_tag = '<span class="me-badge">YOU</span>' if is_me else ""

        return f"""<div class="player-card {team_class}" style="--grad:{gradient}">
  <div class="card-agent-bg"></div>
  <div class="card-agent-icon">{icon_tag}</div>
  <div class="card-body">
    <div class="card-top">
      <span class="player-name">{p.username}</span><span class="player-tag">#{p.tag}</span>
      {me_tag}{streamer_tag}
    </div>
    <div class="card-agent-label">{p.agent if p.agent != "N/A" else "—"}</div>
  </div>
  <div class="card-rank">{rank_html}</div>
  <div class="card-level"><span class="level-num">{p.level}</span><span class="level-lbl">LVL</span></div>
  <div class="card-tracker">
    <a class="tracker-link" href="{p.tracker}" target="_blank">vtl.lol ↗</a>
  </div>
</div>"""

    def _team_section(players, label: str, accent: str,
                      show_team: bool, agents_map: dict, tiers_map: dict,
                      my_puuid: str = "") -> str:
        cards = "\n".join(_player_card(p, show_team, agents_map, tiers_map, my_puuid) for p in players)
        return f"""<div class="team-block">
  <div class="team-header" style="--accent:{accent}">
    <span class="team-label">{label}</span>
    <span class="team-count">{len(players)} players</span>
  </div>
  <div class="cards-stack">{cards}</div>
</div>"""

    HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Barlow+Condensed:ital,wght@0,400;0,600;0,700;0,800;1,700&family=Barlow:wght@400;500&display=swap" rel="stylesheet">
<style>
  :root {{
    --bg:      #09090b;
    --surface: #111113;
    --border:  #27272a;
    --muted:   #52525b;
    --text:    #e4e4e7;
    --subtext: #a1a1aa;
    --blue:    #3b82f6;
    --red:     #ef4444;
    --accent:  #ff4655;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    background: var(--bg);
    color: var(--text);
    font-family: 'Barlow', sans-serif;
    font-size: 14px;
    min-height: 100vh;
    padding: 28px 24px;
  }}
  body::before {{
    content: '';
    position: fixed; inset: 0;
    background: repeating-linear-gradient(
      0deg, transparent, transparent 2px,
      rgba(0,0,0,.055) 2px, rgba(0,0,0,.055) 4px
    );
    pointer-events: none; z-index: 999;
  }}

  /* ── Header ───────────────────────────────────────────────────── */
  .page-header {{
    display: flex; align-items: center; gap: 14px;
    margin-bottom: 24px; padding-bottom: 14px;
    border-bottom: 1px solid var(--border);
  }}
  .logo-svg {{ width: 32px; height: 32px; flex-shrink: 0; }}
  .page-title {{
    font-family: 'Barlow Condensed', sans-serif;
    font-size: 26px; font-weight: 800;
    text-transform: uppercase; letter-spacing: .06em;
    line-height: 1;
  }}
  .page-title span {{ color: var(--accent); }}
  .page-meta {{
    font-size: 11px; color: var(--muted);
    text-transform: uppercase; letter-spacing: .13em;
    margin-left: auto;
  }}

  /* ── Grid ─────────────────────────────────────────────────────── */
  .teams-grid {{ display: grid; gap: 16px; }}
  .teams-grid.dual {{ grid-template-columns: 1fr 1fr; }}

  /* ── Team block ───────────────────────────────────────────────── */
  .team-block {{
    border: 1px solid var(--border);
    border-radius: 6px; overflow: hidden;
    background: var(--surface);
  }}
  .team-header {{
    display: flex; justify-content: space-between; align-items: center;
    padding: 9px 16px;
    background: color-mix(in srgb, var(--accent) 10%, transparent);
    border-bottom: 2px solid var(--accent);
  }}
  .team-label {{
    font-family: 'Barlow Condensed', sans-serif;
    font-size: 13px; font-weight: 700;
    text-transform: uppercase; letter-spacing: .15em;
    color: var(--accent);
  }}
  .team-count {{ font-size: 11px; color: var(--muted); letter-spacing: .08em; }}
  .cards-stack {{ display: flex; flex-direction: column; }}

  /* ── Player card ──────────────────────────────────────────────── */
  .player-card {{
    position: relative; display: flex;
    align-items: center; gap: 0;
    overflow: hidden;
    border-bottom: 1px solid var(--border);
    min-height: 68px;
    transition: filter .15s;
  }}
  .player-card:last-child {{ border-bottom: none; }}
  .player-card:hover {{ filter: brightness(1.06); }}

  /* gradient background from agent colours */
  .card-agent-bg {{
    position: absolute; inset: 0;
    background: var(--grad);
    opacity: .13;
    pointer-events: none;
  }}

  /* team side stripe */
  .player-card.blue-side::before,
  .player-card.red-side::before {{
    content: ''; position: absolute; left: 0; top: 0; bottom: 0;
    width: 3px;
  }}
  .player-card.blue-side::before {{ background: var(--blue); }}
  .player-card.red-side::before  {{ background: var(--red);  }}

  /* ── Agent icon column ────────────────────────────────────────── */
  .card-agent-icon {{
    position: relative; flex-shrink: 0;
    width: 60px; height: 68px;
    display: flex; align-items: flex-end; justify-content: center;
    overflow: hidden;
    background: color-mix(in srgb, var(--grad) 30%, transparent);
  }}
  .agent-icon {{
    width: 56px; height: 56px;
    object-fit: contain;
    filter: drop-shadow(0 2px 6px rgba(0,0,0,.6));
    margin-bottom: 4px;
  }}
  .agent-icon--missing {{
    width: 40px; height: 40px;
    display: flex; align-items: center; justify-content: center;
    font-family: 'Barlow Condensed', sans-serif;
    font-size: 14px; font-weight: 700;
    color: var(--muted);
    background: rgba(255,255,255,.05);
    border-radius: 4px;
    margin-bottom: 10px;
  }}

  /* ── Card body (name + agent label) ──────────────────────────── */
  .card-body {{
    position: relative; flex: 1; min-width: 0;
    padding: 0 12px;
  }}
  .card-top {{ display: flex; align-items: baseline; gap: 2px; flex-wrap: wrap; }}
  .player-name {{
    font-family: 'Barlow Condensed', sans-serif;
    font-size: 17px; font-weight: 700; letter-spacing: .025em;
    color: var(--text); white-space: nowrap;
  }}
  .player-tag {{ font-size: 12px; color: var(--muted); }}
  .streamer-badge {{
    font-size: 9px; font-weight: 700; letter-spacing: .1em;
    color: #f59e0b;
    background: rgba(245,158,11,.11);
    border: 1px solid rgba(245,158,11,.28);
    border-radius: 2px; padding: 1px 5px;
    margin-left: 6px; vertical-align: middle;
  }}
  .card-agent-label {{
    font-family: 'Barlow Condensed', sans-serif;
    font-size: 11px; font-weight: 600; letter-spacing: .1em;
    color: var(--muted); text-transform: uppercase;
    margin-top: 2px;
  }}

  /* ── Rank badge ───────────────────────────────────────────────── */
  .card-rank {{ position: relative; flex-shrink: 0; padding: 0 12px; }}
  .rank-icon {{
    width: 22px; height: 22px;
    object-fit: contain;
  }}
  .rank-label {{
    font-family: 'Barlow Condensed', sans-serif;
    font-size: 12px; font-weight: 700; letter-spacing: .12em;
    color: var(--rc);
  }}
  .rank-num {{
    font-family: 'Barlow Condensed', sans-serif;
    font-size: 12px; font-weight: 400;
    color: color-mix(in srgb, var(--rc) 75%, white);
  }}
  .rank-rr {{
    font-size: 10px; color: var(--muted);
    border-left: 1px solid var(--border);
    padding-left: 6px; margin-left: 1px;
  }}

  /* ── Level ────────────────────────────────────────────────────── */
  .card-level {{
    position: relative; flex-shrink: 0;
    display: flex; flex-direction: column;
    align-items: center; justify-content: center;
    width: 44px;
  }}
  .level-num {{
    font-family: 'Barlow Condensed', sans-serif;
    font-size: 18px; font-weight: 700;
    color: var(--subtext); line-height: 1;
  }}
  .level-lbl {{
    font-size: 9px; color: var(--muted);
    text-transform: uppercase; letter-spacing: .1em;
  }}

  /* ── Tracker ──────────────────────────────────────────────────── */
  .card-tracker {{
    position: relative; flex-shrink: 0;
    padding: 0 14px;
  }}
  .tracker-link {{
    font-size: 10px; font-weight: 600; letter-spacing: .07em;
    color: var(--muted); text-decoration: none;
    text-transform: uppercase;
    border: 1px solid var(--border); border-radius: 2px;
    padding: 3px 8px;
    transition: color .14s, border-color .14s;
  }}
  .tracker-link:hover {{ color: var(--accent); border-color: var(--accent); }}

  /* ── Me highlight ─────────────────────────────────────────────── */
  .player-card.is-me {{
    outline: 2px solid var(--accent);
    outline-offset: -2px;
    z-index: 1;
  }}
  .player-card.is-me .card-agent-bg {{ opacity: .22; }}
  .me-badge {{
    font-size: 9px; font-weight: 700; letter-spacing: .1em;
    color: var(--accent);
    background: rgba(255,70,85,.12);
    border: 1px solid rgba(255,70,85,.35);
    border-radius: 2px; padding: 1px 5px;
    margin-left: 6px; vertical-align: middle;
    text-transform: uppercase;
  }}

  /* ── Rank badge: explicit fallback so it always renders ───────── */
  .rank-badge {{
    display: inline-flex; align-items: center; gap: 5px;
    background: color-mix(in srgb, var(--rc, #6b7280) 13%, transparent);
    border: 1px solid color-mix(in srgb, var(--rc, #6b7280) 35%, transparent);
    border-radius: 3px; padding: 4px 8px; white-space: nowrap;
  }}
</style>
</head>
<body>
<div class="page-header">
  <svg class="logo-svg" viewBox="0 0 32 32" fill="#ff4655" xmlns="http://www.w3.org/2000/svg">
    <path d="M19.8,26.1h-0.2c-2.4,0-4.8,0-7.2,0c-0.3,0-0.5-0.1-0.6-0.3c-2.5-3.2-5.1-6.3-7.6-9.5C4.1,16.1,4,16,4,15.8c0-3.1,0-6.1,0-9.2c0-0.1,0-0.2,0.1-0.2h0.1c5.2,6.5,10.4,13,15.5,19.5c0,0,0,0.1,0.1,0.1L19.8,26.1L19.8,26.1z"/>
    <path d="M27.8,16.3c-0.7,0.9-1.5,1.8-2.2,2.8c-0.2,0.2-0.4,0.3-0.6,0.3c-2.4,0-4.8,0-7.1,0c0,0-0.1,0-0.1,0c-0.1,0-0.2-0.1-0.1-0.2c0,0,0-0.1,0.1-0.1c2.4-3,4.7-5.9,7.1-8.9c1-1.2,2-2.5,2.9-3.7c0-0.1,0.1-0.1,0.2-0.1c0,0,0.1,0,0.1,0c0,0.1,0,0.1,0,0.2c0,3,0,6.1,0,9.1C28,16,27.9,16.2,27.8,16.3L27.8,16.3z"/>
  </svg>
  <div class="page-title">VALORANT</div>
  <div class="page-meta">{subtitle} · {timestamp}</div>
</div>
<div class="teams-grid {grid_class}">
{content}
</div>
</body>
</html>"""

    def render_html(players: List["Player"], mode: str, title: str):
        """Fetch assets, build HTML, and open in the default browser."""
        import tempfile, webbrowser
        from datetime import datetime

        print("⏳ Fetching agent & rank assets…")
        agents_map, tiers_map = _fetch_assets()

        my_puuid  = api.my_puuid
        timestamp = datetime.now().strftime("%H:%M · %d %b %Y")
        subtitle  = "PREGAME" if mode == "pre" else "LIVE MATCH"
        show_team = mode == "core"

        if mode == "core":
            blue = [p for p in players if getattr(p, "team", None) == "Blue"]
            red  = [p for p in players if getattr(p, "team", None) != "Blue"]

            # Determine which side the local player is on
            my_team = next(
                (getattr(p, "team", None) for p in players if p.puuid == my_puuid),
                "Blue"
            )
            if my_team == "Blue":
                ally_players, opp_players = blue, red
                ally_accent, opp_accent   = "#3b82f6", "#ef4444"
            else:
                ally_players, opp_players = red, blue
                ally_accent, opp_accent   = "#ef4444", "#3b82f6"

            content = (
                _team_section(ally_players, "🔵  Ally",     ally_accent, show_team, agents_map, tiers_map, my_puuid) +
                _team_section(opp_players,  "🔴  Opponent", opp_accent,  show_team, agents_map, tiers_map, my_puuid)
            )
            grid_class = "dual"
        else:
            content    = _team_section(players, "Ally", "#ff4655", False, agents_map, tiers_map, my_puuid)
            grid_class = ""

        html = HTML_TEMPLATE.format(
            title=title, subtitle=subtitle,
            timestamp=timestamp, content=content, grid_class=grid_class,
        )

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".html", delete=False, encoding="utf-8"
        ) as f:
            f.write(html)
            path = f.name

        webbrowser.open(f"file://{path}")
        print(f"✔  Opened match overview → {path}")

    def print_tablurized(obj_list, fields, title, sortby=0, filters={}):
        """Render the HUD in the browser; fall back to terminal on error."""
        mode = "core" if "Team" in fields else "pre"
        try:
            render_html(obj_list, mode, title)
        except Exception as e:
            # Graceful terminal fallback
            print(f"[warn] HTML render failed ({e}), falling back to terminal output")
            from prettytable import PrettyTable
            table = PrettyTable()
            table.field_names = fields
            table.sortby = sortby if isinstance(sortby, str) else fields[sortby]
            table.hrules = 1
            for obj in obj_list:
                row = []
                for attr in fields:
                    v = getattr(obj, attr.lower())
                    if attr.lower() in filters:
                        v = filters[attr.lower()](v)
                    row.append(str(v))
                table.add_row(row)
            print(table)

    # Rank abbreviation map for --copy snippets
    RANK_ABV = {
        "Unranked":    "ur",
        "Iron 1": "i1", "Iron 2": "i2", "Iron 3": "i3",
        "Bronze 1": "b1", "Bronze 2": "b2", "Bronze 3": "b3",
        "Silver 1": "s1", "Silver 2": "s2", "Silver 3": "s3",
        "Gold 1": "g1", "Gold 2": "g2", "Gold 3": "g3",
        "Platinum 1": "p1", "Platinum 2": "p2", "Platinum 3": "p3",
        "Diamond 1": "d1", "Diamond 2": "d2", "Diamond 3": "d3",
        "Ascendant 1": "a1", "Ascendant 2": "a2", "Ascendant 3": "a3",
        "Immortal 1": "imm1", "Immortal 2": "imm2", "Immortal 3": "imm3",
        "Radiant": "rad",
    }

    def build_copy_snippet(players: List[Player], mode: str) -> str:
        """
        Build a short, chat-friendly rank summary string ready to paste.

        Format (core):  🔵 Gekko:g2 Reyna:s1 || 🔴 Omen:p3 Jett:g1
        Format (pre):   Gekko:g2 Reyna:s1 Jett:s3
        """
        def fmt(p) -> str:
            abv = RANK_ABV.get(p.rank, p.rank.lower().replace(" ", ""))
            agent = p.agent if p.agent != "N/A" else "?"
            return f"{agent}:{abv}"

        if mode == "core":
            blue = [p for p in players if getattr(p, "team", None) == "Blue"]
            red  = [p for p in players if getattr(p, "team", None) != "Blue"]
            return "(Ally: " + " ".join(fmt(p) for p in blue) + ") (Opponent: " + " ".join(fmt(p) for p in red) + ")"
        else:
            return " ".join(fmt(p) for p in players)

    # Parse args — accept --pre / --core and optional --copy
    args = [a.lower() for a in sys.argv[1:]]
    mode_arg = next((a for a in args if a in ("--pre", "--core")), "--core")
    do_copy  = "--copy" in args

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

    if mode_arg == "--pre":
        pregame_data = api.get_players_in_pre_lobby()
        _fill_missing_names(pregame_data)
        fields = ["Username", "Tag", "Agent", "Rank", "RR", "Level", "Tracker"]
        print_tablurized(pregame_data, fields, "Valorant status: Pregame", sortby="Rank", filters=filters)
        if do_copy:
            snippet = build_copy_snippet(pregame_data, "pre")
            pyperclip.copy(snippet)
            print(f"📋 Copied: {snippet}")

    elif mode_arg == "--core":
        coregame_data = api.get_players_in_core_lobby()
        _fill_missing_names(coregame_data)
        fields = ["Team", "Username", "Tag", "Agent", "Rank", "RR", "Level", "Tracker"]
        print_tablurized(coregame_data, fields, "Valorant status: Coregame", sortby="Team", filters=filters)
        if do_copy:
            snippet = build_copy_snippet(coregame_data, "core")
            pyperclip.copy(snippet)
            print(f"📋 Copied: {snippet}")

    else:
        python_exe = sys.executable.split('\\')[-1]
        print(
            "This script must be executed with one of the following arguments:\n"
            "  --pre    Gather stats on your team during agent select\n"
            "  --core   Gather stats on everyone in a currently running match\n"
            "  --copy   Also copy a short rank summary to the clipboard\n\n"
            f"Example: {python_exe} {sys.argv[0]} --core --copy"
        )