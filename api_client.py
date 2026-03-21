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


urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
db = ValorantDB()


class Player:
    def __init__(self, player_data_response_obj):
        player = player_data_response_obj
        api = ClientApi()
    
        self.puuid = player["Subject"]
        # self.profile = db.get_profile_by_puuid(self.puuid)
        self.agent = api.get_agent_name_from_id(player["CharacterID"])
        self.streamer_mode = player["PlayerIdentity"]["Incognito"]
        self.username, self.tagline = api.get_name_from_puuid(self.puuid)
        self.tag = self.tagline
        self.level = player["PlayerIdentity"]["AccountLevel"]
        self.rank, self.rr = api.get_current_rank_from_puuid(self.puuid) 
        # self.peak_rank = self.profile.current.peak_rank
        # self.party_id = api.get_party_id_from_puuid(self.puuid)
        self.tracker = f"https://vtl.lol/id/{quote(self.username)}_{quote(self.tagline)}"

        if "TeamID" in player.keys():
            self.team = player["TeamID"]
        else:
            self.team = None

@singleton
class ClientApi:

    def __init__(self, region='na'):
        self.local_api_documentation = "https://riotclient.kebs.dev/"
        self.region = region
        self.glz_url = f"https://glz-{self.region}-1.{self.region}.a.pvp.net"
        self.pd_url = f"https://pd.{self.region}.a.pvp.net"
        
        self.usw2_url = "https://usw2.pp.sgp.pvp.net"

        self.__lockfile = None
        self.__headers = None
        self.__puuid = None
        self.__agents_map = None
        self.__rso = None
        self.__pas = None
        self.auth_token = base64.b64encode(f"riot:{self.lockfile['password']}".encode()).decode()
        
        self.cache = {}

    def clear_cache(self):
        self.cache = {}
        
    @property
    def lockfile(self):
        if self.__lockfile is None:
            try:
                with open(os.path.join(os.getenv('LOCALAPPDATA'), R'Riot Games\Riot Client\Config\lockfile')) as lockfile:
                    data = lockfile.read().split(':')
                    keys = ['name', 'PID', 'port', 'password', 'protocol']
                    self.__lockfile = dict(zip(keys, data))
            except:
                raise Exception("Lockfile not found")
        
        return self.__lockfile

    def handle_local_request(self, endpoint):
        baseurl = f"https://127.0.0.1:{self.lockfile['port']}"
        resource = baseurl + endpoint
        token = {'Authorization': f'Basic {self.auth_token}'}
        return requests.get(resource, headers=token, verify=False)

    def handle_remote_request(self, endpoint, basetype='glz', payload = None):
        if basetype == "usw2":
            baseurl = self.usw2_url
        else:
            baseurl = self.glz_url if basetype == 'glz' else self.pd_url
            
        resource = baseurl + endpoint
        return requests.get(resource, headers=self.headers, verify=False)
    
    def handle_remote_request_post(self, endpoint, payload, basetype='glz'):
        if basetype == "usw2":
            baseurl = self.usw2_url
        else:
            baseurl = self.glz_url if basetype == 'glz' else self.pd_url
            
        resource = baseurl + endpoint
        return requests.post(resource, json=payload, headers=self.headers, verify=certifi.where())

    @property
    def current_version(self):
        data = requests.get('https://valorant-api.com/v1/version')
        data = data.json()['data']
        version = f"{data['branch']}-shipping-{data['buildVersion']}-{data['version'].split('.')[3]}"
        return version
    
    @property
    def rso_token(self):
        if 'rso' not in self.cache:
            self.cache['rso'] = self.handle_local_request(f'/entitlements/v2/token').json()['authorization']['accessToken']['token']
        
        return self.cache['rso']
    
    @property
    def pas_token(self):
        if 'pas' not in self.cache:
            self.cache['pas'] = requests.get("https://riot-geo.pas.si.riotgames.com/pas/v1/service/chat", headers=self.headers).content.decode()
        
        return self.cache['pas']

    @property
    def headers(self):
        if self.__headers is None:
            response = requests.get(f"https://127.0.0.1:{self.lockfile['port']}/entitlements/v1/token", headers={'Authorization': f'Basic {self.auth_token}'}, verify=False)

            entitlements = response.json()
            self.__rso = entitlements['accessToken']
            self.__headers = {
                'Authorization': f"Bearer {entitlements['accessToken']}",
                'X-Riot-Entitlements-JWT': entitlements['token'],
                'X-Riot-ClientPlatform': "ew0KCSJwbGF0Zm9ybVR5cGUiOiAiUEMiLA0KCSJwbGF0Zm9ybU9TIjogIldpbmRvd3MiLA0KCSJwbGF0Zm9ybU9TVmVyc2lvbiI6ICIxMC4wLjE5MDQyLjEuMjU2LjY0Yml0IiwNCgkicGxhdGZvcm1DaGlwc2V0IjogIlVua25vd24iDQp9",
                'X-Riot-ClientVersion': self.current_version
            }

        return self.__headers

    @property
    def my_puuid(self):
        if self.__puuid is None:
            local_headers = {}
            local_headers['Authorization'] = f'Basic {self.auth_token}'
            
            uri = f"https://127.0.0.1:{self.lockfile['port']}/entitlements/v1/token"
            entitlements = requests.get(uri, headers=local_headers, verify=False).json()
            self.__puuid = entitlements['subject']

        return self.__puuid

    def get_current_rank_from_puuid(self, puuid):
        VALORANT_RANKS = ["Iron 1", "Iron 2", "Iron 3", "Bronze 1", "Bronze 2", "Bronze 3", "Silver 1", "Silver 2", "Silver 3",
                  "Gold 1", "Gold 2", "Gold 3", "Platinum 1", "Platinum 2", "Platinum 3", "Diamond 1", "Diamond 2", "Diamond 3",
                  "Ascendant 1", "Ascendant 2", "Ascendant 3", "Immortal 1", "Immortal 2", "Immortal 3", "Radiant"]
        
        endpoint = self.pd_url + f"/mmr/v1/players/{puuid}/competitiveupdates?startIndex=0&endIndex=20&queue=competitive"
        response = requests.get(endpoint, headers=self.headers, verify=False).json()
        ranks = ["Unranked", "Unranked", "Unranked"] + VALORANT_RANKS

        if len(response["Matches"]) == 0:
            return (ranks[1], 0)
        else:
            rank_idx = response["Matches"][0]["TierAfterUpdate"]
            rr = response["Matches"][0]["RankedRatingAfterUpdate"]
            return (ranks[rank_idx], rr)

    def get_name_from_puuid(self, puuid):
        endpoint = self.pd_url + f"/name-service/v2/players"
        response = requests.put(endpoint, headers=self.headers, json=[puuid], verify=False)
        tagline = response.json()[0]["TagLine"]
        username = response.json()[0]["GameName"]
        return (username, tagline)  
    
    def get_my_party_id(self):
        endpoint = self.glz_url + f"/parties/v1/players/{self.my_puuid}"
        response = requests.get(endpoint, headers=self.headers, verify=False)
        return response.json()['CurrentPartyID']

    def get_current_party_from_id(self, party_id):
        endpoint = self.glz_url + f"/parties/v1/parties/{party_id}"
        response = requests.get(endpoint, headers=self.headers, verify=False)
        return response.json()
    
    @property
    def agents_map(self):
        if self.__agents_map is None:
            agents = requests.get("https://valorant-api.com/v1/agents").json()['data']
            self.__agents_map = {
                agent['uuid'] : agent['displayName'] for agent in agents
            }
        
        return self.__agents_map
    
    def get_agent_name_from_id(self, char_id):
        if self.__agents_map is None:
            agents = requests.get("https://valorant-api.com/v1/agents").json()['data']
            self.__agents_map = {
                agent['uuid'] : agent['displayName'] for agent in agents
            }

        agent_name = "N/A"
        if char_id in self.__agents_map.keys():
            agent_name = self.__agents_map[char_id]
        
        return agent_name
    

    def play_replay(self):
        matchID = "9d101bde-77bc-56f3-99e0-6487921d846e"
        endpoint = self.glz_url + f"/parties/v1/players/{matchID}/startsoloexperience"        
        
        return requests.post(endpoint, headers=self.headers, verify=certifi.where())
    
    def change_queue(self, index):
        party_id = self.get_my_party_id()
        available_queues = self.get_current_party_from_id(party_id)["EligibleQueues"]
        
        endpoint = self.glz_url + f"/parties/v1/parties/{party_id}/queue"
        payload = {"queueID": available_queues[index-1]}
        
        return requests.post(endpoint, json=payload, headers=self.headers, verify=certifi.where())

    def join_queue(self):
        party_id = self.get_my_party_id()
        
        endpoint = self.glz_url + f"/parties/v1/parties/{party_id}/matchmaking/join"
        return requests.post(endpoint, headers=self.headers, verify=certifi.where())

    def leave_queue(self):
        party_id = self.get_my_party_id()
        
        endpoint = self.glz_url + f"/parties/v1/parties/{party_id}/matchmaking/leave"
        return requests.post(endpoint, headers=self.headers, verify=certifi.where())
    
    def select_pregame_agent(self, agent_name: Union[AgentName|str]):
        if isinstance(agent_name, AgentName):
            agent_name = agent_name.value
        
        agents = {str.lower(v):k for k,v in self.agents_map.items()}
        agent_id = agents[str.lower(agent_name)]

        pregame_id = self.get_pregame_match_id()
        endpoint = self.glz_url + f"/pregame/v1/matches/{pregame_id}/select/{agent_id}"
        return requests.post(endpoint, headers=self.headers, verify=certifi.where())

    def lock_pregame_agent(self, agent_name):
        agents = {str.lower(v):k for k,v in self.__agents_map.items()}
        agent_id = agents[agents[str.lower(agent_name)]]

        pregame_id = self.get_pregame_match_id()
        endpoint = self.glz_url + f"/pregame/v1/matches/{pregame_id}/lock/{agent_id}"
        return requests.post(endpoint, headers=self.headers, verify=certifi.where())

    def get_match_id(self, is_pregame=False):
        match_type = "pregame" if is_pregame else "core-game"
        try:
            endpoint = self.glz_url + f"/{match_type}/v1/players/{self.my_puuid}"
            response = requests.get(endpoint, headers=self.headers, verify=certifi.where()).json()
            return response['MatchID']
        except KeyError:
            print(f"No {match_type} match id found.")

    def get_coregame_match_id(self):
        return self.get_match_id(is_pregame=False)
    
    def wait_until_match_found(self):
        response = {}
        
        while 'MatchID' not in response:
            endpoint = self.glz_url + f"/pregame/v1/players/{self.my_puuid}"
            response = requests.get(endpoint, headers=self.headers, verify=certifi.where()).json()
            time.sleep(5)
            
    def wait_until_match_started(self):
        response = {}
        
        while 'MatchID' not in response:
            endpoint = self.glz_url + f"/core-game/v1/players/{self.my_puuid}"
            response = requests.get(endpoint, headers=self.headers, verify=certifi.where()).json()
            time.sleep(5)
        
    def get_pregame_match_id(self):
        return self.get_match_id(is_pregame=True)

    def get_players_in_pre_lobby(self) -> List[Player]:
        if 'pregame_response' not in self.cache: 
            endpoint = self.glz_url + f"/pregame/v1/matches/{self.get_pregame_match_id()}"
            response = requests.get(endpoint, headers=self.headers, verify=False).json()
            self.cache['pregame_response'] = response

            if 'httpStatus' in response:
                raise BaseException(response['message'])

            self.cache['pregame_players'] = [Player(p) for p in response["AllyTeam"]["Players"]]
        
        return self.cache['pregame_players']
    
    def get_pregame_chat_id(self):
        if 'pregame_response' not in self.cache: 
            endpoint = self.glz_url + f"/pregame/v1/matches/{self.get_pregame_match_id()}"
            response = requests.get(endpoint, headers=self.headers, verify=False).json()

            if 'httpStatus' in response:
                raise BaseException(response['message'])
            else:
                self.cache['pregame_response'] = response
        
        response = self.cache['pregame_response']
        return response['MUCName']

    def get_coregame_chat_id(self):
        if 'coregame_response' not in self.cache: 
            endpoint = self.glz_url + f"/core-game/v1/matches/{self.get_coregame_match_id()}"
            response = requests.get(endpoint, headers=self.headers, verify=certifi.where()).json()

            if 'httpStatus' in response:
                raise BaseException(response['message'])
            else:
                self.cache['coregame_response'] = response
        
        response = self.cache['coregame_response']
        return response['TeamMUCName']
    
    def get_coregame_allchat_id(self):
        if 'coregame_response' not in self.cache: 
            endpoint = self.glz_url + f"/core-game/v1/matches/{self.get_coregame_match_id()}"
            response = requests.get(endpoint, headers=self.headers, verify=False).json()

            if 'httpStatus' in response:
                raise BaseException(response['message'])
            else:
                self.cache['coregame_response'] = response
        
        response = self.cache['coregame_response']
        return response['AllMUCName']

    def send_message(self, message, cid, chat_type="groupchat"):
        # endpoint = self.pd_url + f"/chat/v5/messages"
        endpoint = f"https://127.0.0.1:{self.lockfile['port']}/chat/v6/messages"
        token = {'Authorization': f'Basic {self.auth_token}'}
        return requests.post(endpoint, json={"message": message, "cid": cid, "type": chat_type}, headers=token, verify=False)
    
    def get_pregame_map_name(self):
        if 'pregame_response' not in self.cache: 
            endpoint = self.glz_url + f"/pregame/v1/matches/{self.get_pregame_match_id()}"
            response = requests.get(endpoint, headers=self.headers, verify=False).json()

            if 'httpStatus' in response:
                raise BaseException(response['message'])
            else:
                self.cache['pregame_response'] = response
        
        response = self.cache['pregame_response']
        return response['MapID']    
    
    def get_players_in_core_lobby(self) -> List[Player]:
        if 'coregame_response' not in self.cache:
            endpoint = self.glz_url + f"/core-game/v1/matches/{self.get_coregame_match_id()}"
            response = requests.get(endpoint, headers=self.headers, verify=False).json()
            self.cache['coregame_response'] = response

            if 'httpStatus' in response:
                raise BaseException(response['message'])
            
            self.cache['coregame_players'] = [Player(p) for p in response["Players"]]
        
        return self.cache['coregame_players']

if __name__ == "__main__":
    
    from api_henrik import AffinitiesEnum, UnofficialApi, Match

    import sys

    henrik = UnofficialApi()

    def print_tablurized(obj_list, fields, title, sortby=0, filters={}):
        table = PrettyTable()

        for obj in obj_list:
            row = []
            for attr in fields:
                attr = str.lower(attr)
                value = getattr(obj, attr)
                if attr in filters:
                    value = filters[attr](value)
                row.append(str(value))

            table.add_rows([row])
            
        table.title = title
        table.field_names = fields
        # if sortby is a string, then its probably a keyname, otherwise its an index.
        table.sortby = sortby if isinstance(sortby, str) else fields[sortby]

        print(table)
    
    # def create_party_symbol_dict(players: List[Player]):
    #     party_ids = [p.party_id for p in players]
    #     parties = {
    #         party_id : party_ids.count(party_id)
    #         for party_id in party_ids
    #         if party_ids.count(party_id) > 1
    #     }
    #     labeled_parties = dict(zip(parties.keys(), ["🟧", "🟩", "🟨", "🟪"]))
    #     return labeled_parties

    # arg = "--help"
    arg = "--core"
    if len(sys.argv) > 1:
        arg = sys.argv[1]


    api = ClientApi()

    filters  = {
        "Team": lambda team: '🔵' if team == "Blue" else '🔴',
        # "Party_ID": lambda party_id: party_labels[party_id] if party_id in party_labels else ""
    }

    if str.lower(arg) == "--pre":
        pregame_data = api.get_players_in_pre_lobby()
        for player in pregame_data:
            if player.tag == '' or player.username == '':
                account = henrik.get_account_by_puuid(player.puuid)
                player.username = account.name
                player.tag = account.tag
                player.tagline = account.tag
                player.tracker = f"https://vtl.lol/id/{quote(player.username)}_{quote(player.tagline)}"
                
        # party_labels = create_party_symbol_dict(pregame_data)

        fields = ["Username", "Tag", "Agent", "Rank", "RR", "Level", "Tracker"]
        print_tablurized(pregame_data, fields, "Valorant status: Pregame", sortby="Rank", filters=filters)

    elif str.lower(arg) == "--core":
        coregame_data = api.get_players_in_core_lobby()
        for player in coregame_data:
            if player.tag == '' or player.username == '':
                account = henrik.get_account_by_puuid(player.puuid)
                player.username = account.name
                player.tag = account.tag
                player.tagline = account.tag
                player.tracker = f"https://vtl.lol/id/{quote(player.username)}_{quote(player.tagline)}"
                        
        # party_labels = create_party_symbol_dict(coregame_data)

        fields = ["Team", "Username", "Tag", "Agent", "Rank", "RR", "Level", "Tracker"]
        print_tablurized(coregame_data, fields, "Valorant status: Coregame", sortby="Team", filters=filters)

    else:
        pythonPath = sys.executable.split('\\')[-1]
        print(
            "This script must be executed with two arguments.\n"
            "The first argument is one of:\n"
            "--pre - for gathering stats on everyone in your team during agent select\n"
            "--core - for gathering stats on everyone in a currently running match\n"
            f"{pythonPath} {sys.argv[0]} --pre"
        )