from concurrent.futures import ThreadPoolExecutor
import multiprocessing
from pathlib import Path
import threading
from typing import Dict, Optional, List, Union
from enum import Enum
import json
import math

import time
from datetime import datetime, timedelta
import requests

from api_cache import Cache
from jsoninjest import UNSET, JsonInjester

from api_request_logger import RequestLogger

CONFIG_FILE  = Path("config.json")

def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {"api_key": ""}

API_KEY = load_config().get("api_key", None)
REQUESTS_LOG = 'logs/unofficial-api-requests.log'
# CACHE_FILE = 'caches/api-cache.pkl'
CACHE_FILE = 'caches/henrik/chunks.manifest'

class ShortDatetime(datetime):
    def __repr__(self):
        return f'<datetime: {self.strftime("%Y-%m-%d %H:%M")}>'

# === ENUMS ===
class AffinitiesEnum(Enum):
    EU = 'eu'
    NA = 'na'
    LATAM = 'latam'
    BR = 'br'
    AP = 'ap'
    KR = 'kr'

class ModesApiEnum(Enum):
    COMPETITIVE = 'competitive'
    CUSTOM = 'custom'
    DEATHMATCH = 'deathmatch'
    ESCALATION = 'escalation'
    TEAMDEATHMATCH = 'teamdeathmatch'
    NEWMAP = 'newmap'
    REPLICATION = 'replication'
    SNOWBALLFIGHT = 'snowballfight'
    SPIKERUSH = 'spikerush'
    SWIFTPLAY = 'swiftplay'
    UNRATED = 'unrated'

class MapsEnum(Enum):
    ASCENT = 'Ascent'
    SPLIT = 'Split'
    FRACTURE = 'Fracture'
    BIND = 'Bind'
    BREEZE = 'Breeze'
    DISTRICT = 'District'
    KASBAH = 'Kasbah'
    PIAZZA = 'Piazza'
    LOTUS = 'Lotus'
    PEARL = 'Pearl'
    ICEBOX = 'Icebox'
    HAVEN = 'Haven'

class SeasonsEnum(Enum):
    E1A1 = 'e1a1'
    E1A2 = 'e1a2'
    E1A3 = 'e1a3'
    E2A1 = 'e2a1'
    E2A2 = 'e2a2'
    E2A3 = 'e2a3'
    E3A1 = 'e3a1'
    E3A2 = 'e3a2'
    E3A3 = 'e3a3'
    E4A1 = 'e4a1'
    E4A2 = 'e4a2'
    E4A3 = 'e4a3'
    E5A1 = 'e5a1'
    E5A2 = 'e5a2'
    E5A3 = 'e5a3'
    E6A1 = 'e6a1'
    E6A2 = 'e6a2'
    E6A3 = 'e6a3'
    E7A1 = 'e7a1'
    E7A2 = 'e7a2'
    E7A3 = 'e7a3'

class TiersEnum(Enum):
    UNRATED = 'Unrated'
    UNKNOWN_1 = 'Unknown 1'
    UNKNOWN_2 = 'Unknown 2'
    IRON_1 = 'Iron 1'
    IRON_2 = 'Iron 2'
    IRON_3 = 'Iron 3'
    BRONZE_1 = 'Bronze 1'
    BRONZE_2 = 'Bronze 2'
    BRONZE_3 = 'Bronze 3'
    SILVER_1 = 'Silver 1'
    SILVER_2 = 'Silver 2'
    SILVER_3 = 'Silver 3'
    GOLD_1 = 'Gold 1'
    GOLD_2 = 'Gold 2'
    GOLD_3 = 'Gold 3'
    PLATINUM_1 = 'Platinum 1'
    PLATINUM_2 = 'Platinum 2'
    PLATINUM_3 = 'Platinum 3'
    DIAMOND_1 = 'Diamond 1'
    DIAMOND_2 = 'Diamond 2'
    DIAMOND_3 = 'Diamond 3'
    ASCENDANT_1 = 'Ascendant 1'
    ASCENDANT_2 = 'Ascendant 2'
    ASCENDANT_3 = 'Ascendant 3'
    IMMORTAL_1 = 'Immortal 1'
    IMMORTAL_2 = 'Immortal 2'
    IMMORTAL_3 = 'Immortal 3'
    RADIANT = 'Radiant'

class ModesEnum(Enum):
    COMPETITIVE = 'Competitive'
    CUSTOM_GAME = 'Custom Game'
    DEATHMATCH = 'Deathmatch'
    ESCALATION = 'Escalation'
    TEAM_DEATHMATCH = 'Team Deathmatch'
    NEW_MAP = 'New Map'
    REPLICATION = 'Replication'
    SNOWBALL_FIGHT = 'Snowball Fight'
    SPIKE_RUSH = 'Spike Rush'
    SWIFTPLAY = 'Swiftplay'
    UNRATED = 'Unrated'

class ModeIdsEnum(Enum):
    COMPETITIVE = 'competitive'
    CUSTOM = 'custom'
    DEATHMATCH = 'deathmatch'
    GGTEAM = 'ggteam'
    HURM = 'hurm'
    NEWMAP = 'newmap'
    ONEFA = 'onefa'
    SNOWBALL = 'snowball'
    SPIKERUSH = 'spikerush'
    SWIFTPLAY = 'swiftplay'
    UNRATED = 'unrated'

class RegionsEnum(Enum):
    EU = 'eu'
    NA = 'na'
    AP = 'ap'
    KR = 'kr'

class PlatformsEnum(Enum):
    PC = 'PC'
    CONSOLE = 'Console'


def build_url(url: str, required_params: dict, optional_params: dict) -> str:
    extract_enum = lambda e: e.value if isinstance(e, Enum) else e
    required_params = {k: extract_enum(v) for (k, v) in required_params.items()}
    optional_params = {k: extract_enum(v) for (k, v) in optional_params.items()}
    
    query_seen = False
    for param, value in optional_params.items():
        if value is not None:
            if not query_seen:
                url += f"?{param}={value}"
                query_seen = True
            else:
                url += f"&{param}={value}"
    
    url = url.format(**required_params)
    return url

def select_from_dict(data: Dict, keys: Dict[Union[str, tuple], List[str]], group:Dict=None) -> Dict:
    def select_keys(data: Dict, keys: Union[str, List[str]]) -> Dict:
        if keys == '*':
            return data
        else:
            view = {}
            for k in keys:
                view[k] = data[k]
            return view
    
    group = group or {}
    view = {k: {} for k in group.values()}
    for path, subkeys in keys.items():
        if isinstance(path, tuple):
            sub_view = data
            for k in path:
                sub_view = sub_view[k]
            path = k
        else:
            sub_view = data[path]
        
        for collection in group.keys():
            if path in collection:
                namespace = group[collection]
                view[namespace][path] = select_keys(sub_view, subkeys)
                break
        else:
            view[path] = select_keys(sub_view, subkeys)
     
    return view


def singleton(cls):
    instances = {}

    def get_instance(*args, **kwargs):
        if cls not in instances:
            instances[cls] = cls(*args, **kwargs)
            if hasattr(instances[cls], '__post_init__'):
                instances[cls].__post_init__()
        return instances[cls]

    return get_instance

RATE_LIMIT = 90 # requests
RATE_PER = 60 # seconds

class RateException(Exception):
    pass

@singleton
class UnofficialApi:
    
    def __init__(self, api_key = API_KEY):
        self.__api_key = api_key
        self.__swagger_schemas = 'https://app.swaggerhub.com/apiproxy/registry/Henrik-3/HenrikDev-API/3.0.0'
        self.__base_uri = 'https://api.henrikdev.xyz'
        self.cache = Cache(CACHE_FILE)
        self.logger = RequestLogger(REQUESTS_LOG)
    
    @property
    def is_rate_limit_reached(self):
        return self.quota_usage > 0.9 # Deliberately use less than the full rate limit
    
    @property
    def quota_usage(self):
        return len(self.logger.get_logs_from_last_seconds(RATE_PER)) / RATE_LIMIT
    
    @property
    def time_until_limit_reset(self):       
        recent_requests = self.logger.get_logs_from_last_seconds(RATE_PER)
        if len(recent_requests) == 0:
            return 0
    
        oldest_request_ts: datetime = datetime.fromtimestamp(recent_requests[0].timestamp)
        target_time = oldest_request_ts + timedelta(seconds=RATE_PER)
        
        time_to_wait = (target_time - datetime.now()).total_seconds()
        
        return time_to_wait
    
    def wait_until_limit_reset(self):       
        time.sleep(self.time_until_limit_reset + 1)
        
    def uncached_fetch(self, uri: str, payload: Optional[dict]=None) -> requests.Response:
        if self.is_rate_limit_reached:
            self.wait_until_limit_reset()
            # raise RateException(f"Rate limit reached! You must wait {self.time_until_limit_reset} seconds.")
        
        headers = {
            'Authorization': self.__api_key
        }
        
        for try_count in range(3):
            response = requests.get(uri, headers=headers, json=payload)
            status = response.status_code

            if status == 429:
                wait_time = max(self.time_until_limit_reset, 3*try_count)
                time.sleep(wait_time)
                continue
            elif status == 200:
                break
            # response.raise_for_status()
        if status == 429:
            raise RateException(f"Rate limit reached! {response.json()}")

        self.logger.log(uri, response.status_code)
        return response
    
    def uncached_post(self, uri: str, payload: Optional[dict]=None) -> requests.Response:
        if self.is_rate_limit_reached:
            raise RateException(f"Rate limit reached! You must wait {self.time_until_limit_reset} seconds.")
        
        headers = {
            'Authorization': self.__api_key
        }
        
        response = requests.post(uri, headers=headers, json=payload)
        response.raise_for_status()
        self.logger.log(uri, response.status_code)
        return response
    
    def fetch(self, uri: str, expiry: int, force_update: bool=False) -> dict:
        if self.cache.has(uri):
            record = self.cache.get(uri)
            if record.is_data_stale or force_update:
                try:
                    data = self.uncached_fetch(uri).json()
                    self.cache.update(uri, data)
                except (requests.exceptions.ConnectionError, requests.RequestException) as e:
                    return record.data
            else:
                data = record.data
        else:
            data = self.uncached_fetch(uri).json()
            self.cache.store(uri, data, expiry)
        
        return data

    # === MANUALLY ADDED ===    
    # def get_match_history(self, puuid: str, mode=ModesApiEnum.COMPETITIVE) -> List['MatchReference']:
    #     INCREMENT_SIZE = 20
    #     def inner__get_match_ids(puuid: str, begin: int=0, end: int=INCREMENT_SIZE):
    #         endpoint = 'https://api.henrikdev.xyz/valorant/v1/raw'
    #         payload = {
    #             "type": "matchhistory",
    #             "value": puuid,
    #             "region": 'na',
    #             'queries': f'?queue={mode.value}&startIndex={begin}&endIndex={end}'
    #         }
    #         response = self.uncached_post(endpoint, payload)
    #         response.raise_for_status()
    #         return response.json()

    #     history = []

    #     data = inner__get_match_ids(puuid)
    #     history.extend(map(MatchReference, data['History']))
    #     total = data['Total']

    #     index = INCREMENT_SIZE
    #     while len(history) != total:
    #         data = inner__get_match_ids(puuid, index, index+INCREMENT_SIZE)
    #         history.extend(map(MatchReference, data['History']))
    #         index += INCREMENT_SIZE
        
    #     return sorted(history, key=lambda e: e.timestamp)

    def get_match_history(self, puuid: str, mode=ModesApiEnum.COMPETITIVE, region: 'AffinitiesEnum'='na') -> List['MatchReference']:
        INCREMENT_SIZE = 25
        NUM_THREADS = 3  # Adjust as needed

        def inner__get_match_ids(puuid: str, begin: int=0, end: int=INCREMENT_SIZE):
            endpoint = 'https://api.henrikdev.xyz/valorant/v1/raw'
            payload = {
                "type": "matchhistory",
                "value": puuid,
                "region": region,
                'queries': f'?queue={mode.value}&startIndex={begin}&endIndex={end}'
            }
            response = self.uncached_post(endpoint, payload)
            response.raise_for_status()
            return response.json()['data']

        history = dict()
        total = float('inf')  # Set an initial large value for total to ensure the loop runs at least once

        def fetch_matches(start_index: int, event: threading.Event):
            nonlocal history, total
            
            try:
                data = inner__get_match_ids(puuid, start_index, start_index + INCREMENT_SIZE)
            except requests.HTTPError as e:
                if e.response.status_code == 400:
                    event.set()
                    return None
                else:
                    raise e
            
            for match_data in data['History']:
                match = MatchReference(match_data)  # Assuming MatchReference constructor accepts match data
                history[match.match_id] = match
                
            total = data['Total']
            if len(history) >= total:
                event.set()  # Signal that enough records have been collected

        event = threading.Event()
        with ThreadPoolExecutor(max_workers=NUM_THREADS) as executor:
            start_index = 0
            futures = []
            for _ in range(2):  # Queue 2 initial calls
                futures.append(executor.submit(fetch_matches, start_index, event))
                start_index += INCREMENT_SIZE
            for f in futures:
                if not event.is_set():
                    f.result()
            
            if not event.is_set():
                futures = [executor.submit(fetch_matches, index, event) for index in range(start_index, total, INCREMENT_SIZE)]
                event.wait()  # Wait for all records to be collected
                
        return sorted(history.values(), key=lambda e: e.timestamp)

    # === API Resources ===
    def get_account_by_name(self, name: str, tag: str, force: Optional[bool]=None) -> 'V1Account':
        '''
        Get account details

        Parameters:
            - name (str)
            - tag (str)
            - force (bool): Force data update

        Returns:
            - V1Account: Account details
        '''
        
        if not force:
            alt_resource = '/valorant/v1/by-puuid/account/'
            
            cached_uris = self.cache.storage.records.keys()
            for uri in cached_uris:
                if alt_resource not in uri:
                    continue
                
                record = self.cache.get(uri)
                account = V1Account(record.data)
                is_same_account = account.name == name and account.tag == tag
                
                if is_same_account and record.is_data_stale:
                    # Too bad, still have to fetch the account
                    break
                elif is_same_account:
                    return account
        
        required_params = {'name': name, 'tag': tag}
        optional_params = {'force': force}
        resource = self.__base_uri + build_url('/valorant/v1/account/{name}/{tag}', required_params, optional_params)
        data: dict = self.fetch(resource, expiry=math.inf, force_update=(force or False))
        return V1Account(data)

    def get_account_by_puuid(self, puuid: str, force: Optional[bool]=None) -> 'V1Account':
        '''
        Get account details

        Parameters:
            - puuid (str)
            - force (bool): Force data update

        Returns:
            - V1Account: Account details
        '''
        
        if not force:
            alt_resource = '/valorant/v1/account/'
            
            cached_uris = self.cache.storage.records.keys()
            for uri in cached_uris:
                if alt_resource not in uri:
                    continue
                
                record = self.cache.get(uri)
                account = V1Account(record.data)
                is_same_account = account.puuid == puuid
                
                if is_same_account and record.is_data_stale:
                    # Too bad, still have to fetch the account
                    break
                elif is_same_account:
                    return account
        
        required_params = {'puuid': puuid}
        optional_params = {'force': force}
        resource = self.__base_uri + build_url('/valorant/v1/by-puuid/account/{puuid}', required_params, optional_params)
        data: dict = self.fetch(resource, expiry=math.inf, force_update=(force or False))
        return V1Account(data)

    def get_recent_matches_by_puuid(self, puuid: str, affinity: 'AffinitiesEnum'='na', mode: Optional['ModesApiEnum']=None, map: Optional['MapsEnum']=None, page: Optional[int]=None, size: Optional[int]=None) -> 'V1LifetimeMatches':
        '''
        Get lifetime matches
        (apparently, these are the matches that would be visible from within the client)

        Parameters:
            - puuid (str): PUUID of the user
            - affinity (AffinitiesEnum): Choose from ap, br, eu, kr, latam, na (br and latam will be internally converted to na)
            - mode (ModesApiEnum)
            - map (MapsEnum)
            - page (int): The page used in pagination (if this is used, the size query parameter also have to exist)
            - size (int): The amount of returned matches

        Returns:
            - V1LifetimeMatches: Account details
        '''
        
        required_params = {'puuid': puuid, 'affinity': affinity}
        optional_params = {'mode': mode, 'map': map, 'page': page, 'size': size}
        resource = self.__base_uri + build_url('/valorant/v1/by-puuid/lifetime/matches/{affinity}/{puuid}', required_params, optional_params)
        data: dict = self.fetch(resource, expiry=3600.0)
        return V1LifetimeMatches(data)

    def get_recent_mmr_history_by_puuid(self, puuid: str, region: 'AffinitiesEnum'='na', page: Optional[int]=None, size: Optional[int]=None) -> 'V1LifetimeMmrHistory':
        '''
        Get lifetime mmr history

        Parameters:
            - puuid (str): PUUID of the user
            - region (AffinitiesEnum): Choose from ap, br, eu, kr, latam, na (br and latam will be internally converted to na)
            - page (int): The page used in pagination (if this is used, the size query parameter also have to exist)
            - size (int): The amount of mmr changes

        Returns:
            - V1LifetimeMmrHistory: MMR History Data
        '''
        
        required_params = {'puuid': puuid, 'region': region}
        optional_params = {'page': page, 'size': size}
        resource = self.__base_uri + build_url('/valorant/v1/by-puuid/lifetime/mmr-history/{region}/{puuid}', required_params, optional_params)
        data: dict = self.fetch(resource, expiry=3600.0)
        return V1LifetimeMmrHistory(data)

    # def get_matches_by_puuid(self, puuid: str, affinity: 'AffinitiesEnum'='na', mode: Optional['ModesApiEnum']=None, map: Optional['MapsEnum']=None, size: Optional[int]=None) -> 'V3matches':
    #     '''
    #     Get account details

    #     Parameters:
    #         - puuid (str): PUUID of the user
    #         - affinity (AffinitiesEnum): Choose from ap, br, eu, kr, latam, na (br and latam will be internally converted to na)
    #         - mode (ModesApiEnum)
    #         - map (MapsEnum): Available for v3 matches
    #         - size (int): Available for v3 matches (how many matches should be returned)

    #     Returns:
    #         - V3matches: Account details
    #     '''
        
    #     required_params = {'puuid': puuid, 'affinity': affinity}
    #     optional_params = {'mode': mode, 'map': map, 'size': size}
    #     resource = self.__base_uri + build_url('/valorant/v3/by-puuid/matches/{affinity}/{puuid}', required_params, optional_params)
    #     data: dict = self.fetch(resource, expiry=3600.0)
    #     return V3matches(data)

    # def get_mmr_by_name(self, name: str, tag: str, affinity: 'AffinitiesEnum'='na') -> 'V1mmr':
    #     '''
    #     Get mmr details

    #     Parameters:
    #         - name (str)
    #         - tag (str)
    #         - affinity (AffinitiesEnum): Choose from ap, br, eu, kr, latam, na (br and latam will be internally converted to na)

    #     Returns:
    #         - V1mmr: MMR Details
    #     '''
        
    #     required_params = {'name': name, 'tag': tag, 'affinity': affinity}
    #     optional_params = {}
    #     resource = self.__base_uri + build_url('/valorant/v1/mmr/{affinity}/{name}/{tag}', required_params, optional_params)
    #     data: dict = self.fetch(resource, expiry=3600.0)
    #     return V1mmr(data)

    # def get_mmr_by_puuid(self, puuid: str, affinity: 'AffinitiesEnum'='na') -> 'V1mmr':
    #     '''
    #     Get MMR Details

    #     Parameters:
    #         - puuid (str): PUUID of the user
    #         - affinity (AffinitiesEnum): Choose from ap, br, eu, kr, latam, na (br and latam will be internally converted to na)

    #     Returns:
    #         - V1mmr: MMR Details
    #     '''
        
    #     required_params = {'puuid': puuid, 'affinity': affinity}
    #     optional_params = {}
    #     resource = self.__base_uri + build_url('/valorant/v1/by-puuid/mmr/{affinity}/{puuid}', required_params, optional_params)
    #     data: dict = self.fetch(resource, expiry=3600.0)
    #     return V1mmr(data)

    def get_act_performance_by_puuid(self, puuid: str, affinity: 'AffinitiesEnum'='na', season: Optional['SeasonsEnum']=None) -> 'V2mmr':
        '''
        Get MMR Details

        Parameters:
            - puuid (str): PUUID of the user
            - affinity (AffinitiesEnum): Choose from ap, br, eu, kr, latam, na (br and latam will be internally converted to na)
            - season (SeasonsEnum): Available for v2 MMR

        Returns:
            - V2mmr: MMR Details
        '''
        
        required_params = {'puuid': puuid, 'affinity': affinity}
        optional_params = {'season': season}
        resource = self.__base_uri + build_url('/valorant/v2/by-puuid/mmr/{affinity}/{puuid}', required_params, optional_params)
        data: dict = self.fetch(resource, expiry=3600.0)
        return V2mmr(data)

    # def get_mmr_history_by_puuid(self, puuid: str, affinity: 'AffinitiesEnum'='na') -> 'V1mmrh':
    #     '''
    #     Get mmr history

    #     Parameters:
    #         - puuid (str): PUUID of the user
    #         - affinity (AffinitiesEnum): Choose from ap, br, eu, kr, latam, na (br and latam will be internally converted to na)

    #     Returns:
    #         - V1mmrh: mmr history
    #     '''
        
    #     required_params = {'puuid': puuid, 'affinity': affinity}
    #     optional_params = {}
    #     resource = self.__base_uri + build_url('/valorant/v1/by-puuid/mmr-history/{affinity}/{puuid}', required_params, optional_params)
    #     data: dict = self.fetch(resource, expiry=3600.0)
    #     return V1mmrh(data)

    def get_recent_matches_by_name(self, name: str, tag: str, affinity: 'AffinitiesEnum'='na', mode: Optional['ModesApiEnum']=None, map: Optional['MapsEnum']=None, page: Optional[int]=None, size: Optional[int]=None) -> 'V1LifetimeMatches':
        '''
        Get lifetime matches
        (apparently, these are the matches that would be visible from within the client)

        Parameters:
            - name (str)
            - tag (str)
            - affinity (AffinitiesEnum): Choose from ap, br, eu, kr, latam, na (br and latam will be internally converted to na)
            - mode (ModesApiEnum)
            - map (MapsEnum)
            - page (int): The page used in pagination (if this is used, the size query parameter also have to exist)
            - size (int): The amount of returned matches

        Returns:
            - V1LifetimeMatches: Account details
        '''
        
        required_params = {'name': name, 'tag': tag, 'affinity': affinity}
        optional_params = {'mode': mode, 'map': map, 'page': page, 'size': size}
        resource = self.__base_uri + build_url('/valorant/v1/lifetime/matches/{affinity}/{name}/{tag}', required_params, optional_params)
        data: dict = self.fetch(resource, expiry=3600.0)
        return V1LifetimeMatches(data)

    def get_recent_mmr_history_by_name(self, name: str, tag: str, affinity: 'AffinitiesEnum'='na', page: Optional[int]=None, size: Optional[int]=None) -> 'V1LifetimeMmrHistory':
        '''
        Get lifetime mmr changes

        Parameters:
            - name (str)
            - tag (str)
            - affinity (AffinitiesEnum): Choose from ap, br, eu, kr, latam, na (br and latam will be internally converted to na)
            - page (int): The page used in pagination (if this is used, the size query parameter also have to exist)
            - size (int): The amount of returned mmr changes

        Returns:
            - V1LifetimeMmrHistory: MMR History Data
        '''
        
        required_params = {'name': name, 'tag': tag, 'affinity': affinity}
        optional_params = {'page': page, 'size': size}
        resource = self.__base_uri + build_url('/valorant/v1/lifetime/mmr-history/{affinity}/{name}/{tag}', required_params, optional_params)
        data: dict = self.fetch(resource, expiry=3600.0)
        return V1LifetimeMmrHistory(data)

    # def get_matches_by_name(self, name: str, tag: str, affinity: 'AffinitiesEnum'='na') -> 'GetMatchesByNameResponse':
    #     '''
    #     Get Match History

    #     Parameters:
    #         - name (str)
    #         - tag (str)
    #         - affinity (AffinitiesEnum)

    #     Returns:
    #         - GetMatchesByNameResponse: Array of Match data
    #     '''
        
    #     required_params = {'name': name, 'tag': tag, 'affinity': affinity}
    #     optional_params = {}
    #     resource = self.__base_uri + build_url('/valorant/v3/matches/{affinity}/{name}/{tag}', required_params, optional_params)
    #     data: dict = self.fetch(resource, expiry=3600.0)
    #     return GetMatchesByNameResponse(data)

    def get_match_by_id(self, matchId: str) -> Optional['Match']:
        '''
        Get Match Deatils

        Parameters:
            - matchId (str)

        Returns:
            - Match: Match data
        '''
        
        def select_key_fields_from_match(match: dict) -> dict:
            selection = {
                'metadata': '*',
                'players': ['all_players'],
                ('teams', 'red'): ['has_won', 'rounds_won', 'rounds_lost'],
                ('teams', 'blue'): ['has_won', 'rounds_won', 'rounds_lost'],
                'rounds': '*',
                'observers': '*',
                'coaches': '*'
            }

            group = {
                ('blue', 'red'): 'teams'
            }

            return select_from_dict(match, selection, group)
            
        required_params = {'matchId': matchId}
        optional_params = {}
        resource = self.__base_uri + build_url('/valorant/v2/match/{matchId}', required_params, optional_params)
        
        new_match = not self.cache.has(resource)

        data: dict = self.fetch(resource, expiry=math.inf)
        if 'errors' in data.keys():
            return None

        if new_match:
            selection = select_key_fields_from_match(data['data'])
            match = Match(selection)
            data['data'] = selection
            self.cache.update(resource, data)
            return match
        else:
            return Match(data['data'])

    def get_act_performance_by_name(self, name: str, tag: str, affinity: 'AffinitiesEnum'='na', season: Optional['SeasonsEnum']=None) -> 'V2mmr':
        '''
        Get mmr details

        Parameters:
            - name (str)
            - tag (str)
            - affinity (AffinitiesEnum): Choose from ap, br, eu, kr, latam, na (br and latam will be internally converted to na)
            - season (SeasonsEnum): Available for v2 mmr only

        Returns:
            - V2mmr: MMR Details
        '''
        
        required_params = {'name': name, 'tag': tag, 'affinity': affinity}
        optional_params = {'season': season}
        resource = self.__base_uri + build_url('/valorant/v2/mmr/{affinity}/{name}/{tag}', required_params, optional_params)
        data: dict = self.fetch(resource, expiry=3600.0)
        return V2mmr(data)

    def get_available_queues(self, affinity: 'AffinitiesEnum'='na') -> 'V1QueueStatus':
        '''
        Get a list of all available queues and their metadata

        Parameters:
            - affinity (AffinitiesEnum)

        Returns:
            - V1QueueStatus: Queue metadata
        '''
        
        required_params = {'affinity': affinity}
        optional_params = {}
        resource = self.__base_uri + build_url('/valorant/v1/queue-status/{affinity}', required_params, optional_params)
        data: dict = self.fetch(resource, expiry=3600.0)
        return V1QueueStatus(data)

    def get_api_version(self, affinity: 'AffinitiesEnum'='na') -> 'GetApiVersionResponse':
        '''
        No description.

        Parameters:
            - affinity (AffinitiesEnum)

        Returns:
            - GetApiVersionResponse: Versioning for the VALORANT API
        '''
        
        required_params = {'affinity': affinity}
        optional_params = {}
        resource = self.__base_uri + build_url('/valorant/v1/version/{affinity}', required_params, optional_params)
        data: dict = self.fetch(resource, expiry=86400.0)
        return GetApiVersionResponse(data)


# === MODELS ===
class V1Account:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.puuid: str = ji.get('data.puuid')
        self.region: str = ji.get('data.region')
        self.account_level: int = ji.get('data.account_level')
        self.name: Optional[str] = ji.get('data.name')
        self.tag: Optional[str] = ji.get('data.tag')
        self.card: 'V1AccountCard' = ji.get('data.card', cast=V1AccountCard)
        self.last_update: str = ji.get('data.last_update')
        self.last_update_raw: int = ji.get('data.last_update_raw')
    def as_dict(self):
        return self.__data

class V1AccountCard:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.small: str = ji.get('small')
        self.large: str = ji.get('large')
        self.wide: str = ji.get('wide')
        self.id: str = ji.get('id')
    def as_dict(self):
        return self.__data

class V1LifetimeMatchesItem:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.meta: 'V1LifetimeMatchesItemMeta' = ji.get('meta', cast=V1LifetimeMatchesItemMeta)
        self.stats: 'V1LifetimeMatchesItemStats' = ji.get('stats', cast=V1LifetimeMatchesItemStats)
        self.teams: 'V1LifetimeMatchesItemTeams' = ji.get('teams', cast=V1LifetimeMatchesItemTeams)
    def as_dict(self):
        return self.__data

class V1LifetimeMatchesItemStats:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.puuid: str = ji.get('puuid')
        self.team: str = ji.get('team')
        self.level: float = ji.get('level')
        self.character: 'V1LifetimeMatchesItemStatsCharacter' = ji.get('character', cast=V1LifetimeMatchesItemStatsCharacter)
        self.tier: float = ji.get('tier')
        self.score: float = ji.get('score')
        self.kills: float = ji.get('kills')
        self.deaths: float = ji.get('deaths')
        self.assists: float = ji.get('assists')
        self.shots: 'V1LifetimeMatchesItemStatsShots' = ji.get('shots', cast=V1LifetimeMatchesItemStatsShots)
        self.damage: 'V1LifetimeMatchesItemStatsDamage' = ji.get('damage', cast=V1LifetimeMatchesItemStatsDamage)
    def as_dict(self):
        return self.__data

class V1LifetimeMatchesItemStatsShots:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.head: float = ji.get('head')
        self.body: float = ji.get('body')
        self.leg: float = ji.get('leg')
    def as_dict(self):
        return self.__data

class V1LifetimeMatchesItemStatsCharacter:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.id: str = ji.get('id')
        self.name: Optional[str] = ji.get('name')
    def as_dict(self):
        return self.__data

class V1LifetimeMatchesItemStatsDamage:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.made: float = ji.get('made')
        self.received: float = ji.get('received')
    def as_dict(self):
        return self.__data

class V1LifetimeMatchesItemMeta:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.id: str = ji.get('id')
        self.map: 'V1LifetimeMatchesItemMetaMap' = ji.get('map', cast=V1LifetimeMatchesItemMetaMap)
        self.version: str = ji.get('version')
        self.mode: str = ji.get('mode')
        self.started_at: str = ji.get('started_at')
        self.season: 'V1LifetimeMatchesItemMetaSeason' = ji.get('season', cast=V1LifetimeMatchesItemMetaSeason)
        self.region: Optional[str] = ji.get('region')
        self.cluster: Optional[str] = ji.get('cluster')
    def as_dict(self):
        return self.__data

class V1LifetimeMatchesItemMetaSeason:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.id: str = ji.get('id')
        self.short: Optional[str] = ji.get('short')
    def as_dict(self):
        return self.__data

class V1LifetimeMatchesItemMetaMap:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.id: str = ji.get('id')
        self.name: Optional[str] = ji.get('name')
    def as_dict(self):
        return self.__data

class V1LifetimeMatchesItemTeams:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.red: Optional[float] = ji.get('red')
        self.blue: Optional[float] = ji.get('blue')
    def as_dict(self):
        return self.__data

class V1LifetimeMatchesResults:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.total: float = ji.get('total')
        self.returned: float = ji.get('returned')
        self.before: float = ji.get('before')
        self.after: float = ji.get('after')
    def as_dict(self):
        return self.__data

class V1LifetimeMatches:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.name: str = ji.get('name')
        self.tag: str = ji.get('tag')
        self.results: 'V1LifetimeMatchesResults' = ji.get('results', cast=V1LifetimeMatchesResults)
        self.data: List['V1LifetimeMatchesItem'] = [V1LifetimeMatchesItem(e) for e in ji.get('data', [])]
    def as_dict(self):
        return self.__data

class V1LifetimeMmrHistoryResults:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.total: float = ji.get('total')
        self.returned: float = ji.get('returned')
        self.before: float = ji.get('before')
        self.after: float = ji.get('after')
    def as_dict(self):
        return self.__data

class V1LifetimeMmrHistory:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.name: str = ji.get('name')
        self.tag: str = ji.get('tag')
        self.results: 'V1LifetimeMmrHistoryResults' = ji.get('results', cast=V1LifetimeMmrHistoryResults)
        self.data: List['V1LifetimeMmrHistoryItem'] = [V1LifetimeMmrHistoryItem(e) for e in ji.get('data', [])]
    def as_dict(self):
        return self.__data

class V1LifetimeMmrHistoryItem:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.match_id: str = ji.get('match_id')
        self.tier: 'V1LifetimeMmrHistoryItemTier' = ji.get('tier', cast=V1LifetimeMmrHistoryItemTier)
        self.map: 'V1LifetimeMmrHistoryItemMap' = ji.get('map', cast=V1LifetimeMmrHistoryItemMap)
        self.season: 'V1LifetimeMmrHistoryItemSeason' = ji.get('season', cast=V1LifetimeMmrHistoryItemSeason)
        self.ranking_in_tier: float = ji.get('ranking_in_tier')
        self.last_mmr_change: float = ji.get('last_mmr_change')
        self.elo: float = ji.get('elo')
        self.date: str = ji.get('date')
    @property
    def datetime(self) -> datetime:
        # Replace 'Z' with '+00:00' to be fully compliant with fromisoformat's expected timezone format
        date_string_adjusted = self.date[:-1] + '+00:00'
        return ShortDatetime.fromisoformat(date_string_adjusted).astimezone()
    def as_dict(self):
        return self.__data

class V1LifetimeMmrHistoryItemMap:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.id: str = ji.get('id')
        self.name: 'MapsEnum' = ji.get('name')
    def as_dict(self):
        return self.__data

class V1LifetimeMmrHistoryItemSeason:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.id: str = ji.get('id')
        self.short: 'SeasonsEnum' = ji.get('short')
    def as_dict(self):
        return self.__data

class V1LifetimeMmrHistoryItemTier:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.id: float = ji.get('id')
        self.name: 'TiersEnum' = ji.get('name')
    def as_dict(self):
        return self.__data

class V3matches:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.data: List['Match'] = [Match(e) for e in ji.get('data', [])]
    def as_dict(self):
        return self.__data
    
class Match:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.metadata: 'MatchMetadata' = ji.get('metadata', cast=MatchMetadata)
        self.players = [Player(e) for e in ji.get('players.all_players', [])]
        self.observers: List['Observer'] = [Observer(e) for e in ji.get('observers', [])]
        self.coaches: List['Coach'] = [Coach(e) for e in ji.get('coaches', [])]
        self.teams: 'MatchTeams' = ji.get('teams', cast=MatchTeams)
        
        self.__rounds_data = ji.get('rounds', [])
        self.__rounds_unpacked = []
        
    def as_dict(self):
        return self.__data
        
    def get_round(self, index) -> 'MatchRoundsItem':
        # Motivation: initializing MatchRoundsItem objects is very expensive and so we want to maximize on lazy loading        
        if index < len(self.__rounds_unpacked):
            return self.__rounds_unpacked[index]
        else:
            index_unpacked = len(self.__rounds_unpacked)
            packed_items = self.__rounds_data[index_unpacked:]

            for idx, each in enumerate(packed_items):
                idx += index_unpacked
                unpacked_item = MatchRoundsItem(each)
                self.__rounds_unpacked.append(unpacked_item)
                
                if idx == index:
                    return unpacked_item
        
        raise IndexError(f"No Round #{index} for this match!")
    
    @property
    def rounds_list(self) -> List['MatchRoundsItem']:
        unpacked = len(self.__rounds_data) == len(self.__rounds_unpacked)
        if unpacked:
            return self.__rounds_unpacked
        else:
            return list(self.rounds)
                
    @property
    def number_of_rounds(self):
        return len(self.__rounds_data)
        
    @property
    def rounds(self):
        unpacked = len(self.__rounds_data) == len(self.__rounds_unpacked)
        
        if not unpacked:
            # with multiprocessing.Pool() as pool:
            #     self.__rounds_unpacked = pool.map(MatchRoundsItem, self.__rounds_data)
            for each in self.__rounds_data:
                unpacked_item = MatchRoundsItem(each)
                self.__rounds_unpacked.append(unpacked_item)
                yield unpacked_item
        else:
            for each in self.__rounds_unpacked:
                yield each
    

# class MatchPlayers:
#     def __init__(self, data: dict):
#         self.__data = data
#         ji = JsonInjester(data)
#         self.all_players: List['Player'] = [Player(e) for e in ji.get('all_players', [])]
#         self.red: List['Player'] = [Player(e) for e in ji.get('red', [])]
#         self.blue: List['Player'] = [Player(e) for e in ji.get('blue', [])]
#     def as_dict(self):
#         return self.__data

class Player:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.session_playtime: 'PlayerSessionPlaytime' = ji.get('session_playtime', cast=PlayerSessionPlaytime)
        self.assets: 'PlayerAssets' = ji.get('assets', cast=PlayerAssets)
        self.behavior: 'PlayerBehavior' = ji.get('behavior', cast=PlayerBehavior)
        self.platform: 'PlayerPlatform' = ji.get('platform', cast=PlayerPlatform)
        self.ability_casts: 'PlayerAbilityCasts' = ji.get('ability_casts', cast=PlayerAbilityCasts)
        self.stats: 'PlayerStats' = ji.get('stats', cast=PlayerStats)
        self.economy: 'PlayerEconomy' = ji.get('economy', cast=PlayerEconomy)
        self.puuid: str = ji.get('puuid')
        '''Example: 54942ced-1967-5f66-8a16-1e0dae875641'''

        self.name: str = ji.get('name')
        '''Example: Henrik3'''

        self.tag: str = ji.get('tag')
        '''Example: EUW3'''

        self.team_id: str = ji.get('team')
        '''Example: Red'''

        self.level: int = ji.get('level')
        '''Example: 104'''

        self.character: str = ji.get('character')
        '''Example: Sova'''

        self.currenttier: int = ji.get('currenttier')
        '''Example: 12'''

        self.currenttier_patched: str = ji.get('currenttier_patched')
        '''Example: Gold 1'''

        self.player_card: str = ji.get('player_card')
        '''Example: 8edf22c5-4489-ab41-769a-07adb4c454d6'''

        self.player_title: str = ji.get('player_title')
        '''Example: e3ca05a4-4e44-9afe-3791-7d96ca8f71fa'''

        self.party_id: str = ji.get('party_id')
        '''Example: b7590bd4-e2c9-4dd3-8cbf-05f04158375e'''

        self.damage_made: int = ji.get('damage_made')
        '''Example: 3067'''

        self.damage_received: int = ji.get('damage_received')
        '''Example: 3115'''

    def as_dict(self):
        return self.__data

class PlayerStats:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.score: int = ji.get('score')
        '''Example: 4869'''

        self.kills: int = ji.get('kills')
        '''Example: 18'''

        self.deaths: int = ji.get('deaths')
        '''Example: 18'''

        self.assists: int = ji.get('assists')
        '''Example: 5'''

        self.bodyshots: int = ji.get('bodyshots')
        '''Example: 48'''

        self.headshots: int = ji.get('headshots')
        '''Example: 9'''

        self.legshots: int = ji.get('legshots')
        '''Example: 5'''

    def as_dict(self):
        return self.__data

class PlayerAssets:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.card: 'PlayerAssetsCard' = ji.get('card', cast=PlayerAssetsCard)
        self.agent: 'PlayerAssetsAgent' = ji.get('agent', cast=PlayerAssetsAgent)
    def as_dict(self):
        return self.__data

class PlayerAssetsCard:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.small: str = ji.get('small')
        '''Example: https://media.valorant-api.com/playercards/8edf22c5-4489-ab41-769a-07adb4c454d6/smallart.png'''

        self.large: str = ji.get('large')
        '''Example: https://media.valorant-api.com/playercards/8edf22c5-4489-ab41-769a-07adb4c454d6/largeart.png'''

        self.wide: str = ji.get('wide')
        '''Example: https://media.valorant-api.com/playercards/8edf22c5-4489-ab41-769a-07adb4c454d6/wideart.png'''

    def as_dict(self):
        return self.__data

class PlayerAssetsAgent:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.small: str = ji.get('small')
        '''Example: https://media.valorant-api.com/agents/320b2a48-4d9b-a075-30f1-1f93a9b638fa/displayicon.png'''

        self.full: str = ji.get('full')
        '''Example: https://media.valorant-api.com/agents/320b2a48-4d9b-a075-30f1-1f93a9b638fa/fullportrait.png'''

        self.bust: str = ji.get('bust')
        '''Example: https://media.valorant-api.com/agents/320b2a48-4d9b-a075-30f1-1f93a9b638fa/bustportrait.png'''

        self.killfeed: str = ji.get('killfeed')
        '''Example: https://media.valorant-api.com/agents/320b2a48-4d9b-a075-30f1-1f93a9b638fa/killfeedportrait.png'''

    def as_dict(self):
        return self.__data

class PlayerEconomy:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.spent: 'PlayerEconomySpent' = ji.get('spent', cast=PlayerEconomySpent)
        self.loadout_value: 'PlayerEconomyLoadoutValue' = ji.get('loadout_value', cast=PlayerEconomyLoadoutValue)
    def as_dict(self):
        return self.__data

class PlayerEconomyLoadoutValue:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.overall: int = ji.get('overall')
        '''Example: 71700'''

        self.average: int = ji.get('average')
        '''Example: 3117'''

    def as_dict(self):
        return self.__data

class PlayerEconomySpent:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.overall: int = ji.get('overall')
        '''Example: 59750'''

        self.average: int = ji.get('average')
        '''Example: 2598'''

    def as_dict(self):
        return self.__data

class PlayerSessionPlaytime:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.minutes: int = ji.get('minutes')
        '''Example: 26'''

        self.seconds: int = ji.get('seconds')
        '''Example: 1560'''

        self.milliseconds: int = ji.get('milliseconds')
        '''Example: 1560000'''

    def as_dict(self):
        return self.__data

class PlayerBehavior:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.friendly_fire: 'PlayerBehaviorFriendlyFire' = ji.get('friendly_fire', cast=PlayerBehaviorFriendlyFire)
        self.afk_rounds: int = ji.get('afk_rounds')
        '''Example: 0'''

        self.rounds_in_spawn: int = ji.get('rounds_in_spawn')
        '''Example: 0'''

    def as_dict(self):
        return self.__data

class PlayerBehaviorFriendlyFire:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.incoming: int = ji.get('incoming')
        '''Example: 0'''

        self.outgoing: int = ji.get('outgoing')
        '''Example: 0'''

    def as_dict(self):
        return self.__data

class PlayerAbilityCasts:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.c_cast: Optional[int] = ji.get('c_cast')
        '''Example: 16'''

        self.q_cast: Optional[int] = ji.get('q_cast')
        '''Example: 5'''

        self.e_cast: Optional[int] = ji.get('e_cast')
        '''Example: 26'''

        self.x_cast: Optional[int] = ji.get('x_cast')
        '''Example: 0'''

    def as_dict(self):
        return self.__data

class PlayerPlatform:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.os: 'PlayerPlatformOs' = ji.get('os', cast=PlayerPlatformOs)
        self.type: str = ji.get('type')
        '''Example: PC'''

    def as_dict(self):
        return self.__data

class PlayerPlatformOs:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.name: str = ji.get('name')
        '''Example: Windows'''

        self.version: str = ji.get('version')
        '''Example: 10.0.22000.1.768.64bit'''

    def as_dict(self):
        return self.__data

class Coach:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.puuid: str = ji.get('puuid')
        self.team: str = ji.get('team')
    def as_dict(self):
        return self.__data

class MatchRoundsItem:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.plant_events: 'MatchRoundsItemPlantEvents' = ji.get('plant_events', cast=MatchRoundsItemPlantEvents)
        self.defuse_events: 'MatchRoundsItemDefuseEvents' = ji.get('defuse_events', cast=MatchRoundsItemDefuseEvents)
        self.player_stats: List['MatchRoundsItemPlayerStatsItem'] = [MatchRoundsItemPlayerStatsItem(e) for e in ji.get('player_stats', [])]
        self.winning_team: str = ji.get('winning_team')
        '''Example: Red'''

        self.end_type: str = ji.get('end_type')
        '''Example: Eliminated'''

        self.bomb_planted: Optional[bool] = ji.get('bomb_planted')
        '''Example: True'''

        self.bomb_defused: Optional[bool] = ji.get('bomb_defused')
        '''Example: False'''

    def as_dict(self):
        return self.__data

class MatchRoundsItemPlayerStatsItem:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.ability_casts: 'MatchRoundsItemPlayerStatsItemAbilityCasts' = ji.get('ability_casts', cast=MatchRoundsItemPlayerStatsItemAbilityCasts)
        self.damage_events: List['MatchRoundsItemPlayerStatsItemDamageEventsItem'] = [MatchRoundsItemPlayerStatsItemDamageEventsItem(e) for e in ji.get('damage_events', [])]
        self.kill_events: List['MatchRoundsItemPlayerStatsItemKillEventsItem'] = [MatchRoundsItemPlayerStatsItemKillEventsItem(e) for e in ji.get('kill_events', [])]
        self.economy: 'MatchRoundsItemPlayerStatsItemEconomy' = ji.get('economy', cast=MatchRoundsItemPlayerStatsItemEconomy)
        self.player_puuid: str = ji.get('player_puuid')
        '''Example: 54942ced-1967-5f66-8a16-1e0dae875641'''

        self.player_display_name: str = ji.get('player_display_name')
        '''Example: Henrik3#EUW3'''

        self.player_team: str = ji.get('player_team')
        '''Example: Red'''

        self.damage: int = ji.get('damage')
        '''Example: 282'''

        self.bodyshots: int = ji.get('bodyshots')
        '''Example: 7'''

        self.headshots: int = ji.get('headshots')
        '''Example: 1'''

        self.legshots: int = ji.get('legshots')
        '''Example: 1'''

        self.kills: int = ji.get('kills')
        '''Example: 2'''

        self.score: int = ji.get('score')
        '''Example: 430'''

        self.was_afk: bool = ji.get('was_afk')
        '''Example: False'''

        self.was_penalized: bool = ji.get('was_penalized')
        '''Example: False'''

        self.stayed_in_spawn: bool = ji.get('stayed_in_spawn')
        '''Example: False'''

    def as_dict(self):
        return self.__data

class MatchRoundsItemPlayerStatsItemKillEventsItem:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.victim_death_location: 'MatchRoundsItemPlayerStatsItemKillEventsItemVictimDeathLocation' = ji.get('victim_death_location', cast=MatchRoundsItemPlayerStatsItemKillEventsItemVictimDeathLocation)
        self.damage_weapon_assets: 'MatchRoundsItemPlayerStatsItemKillEventsItemDamageWeaponAssets' = ji.get('damage_weapon_assets', cast=MatchRoundsItemPlayerStatsItemKillEventsItemDamageWeaponAssets)
        self.player_locations_on_kill: List['MatchRoundsItemPlayerStatsItemKillEventsItemPlayerLocationsOnKillItem'] = [MatchRoundsItemPlayerStatsItemKillEventsItemPlayerLocationsOnKillItem(e) for e in ji.get('player_locations_on_kill', [])]
        self.assistants: List['MatchRoundsItemPlayerStatsItemKillEventsItemAssistantsItem'] = [MatchRoundsItemPlayerStatsItemKillEventsItemAssistantsItem(e) for e in ji.get('assistants', [])]
        self.kill_time_in_round: int = ji.get('kill_time_in_round')
        '''Example: 43163'''

        self.kill_time_in_match: int = ji.get('kill_time_in_match')
        '''Example: 890501'''

        self.killer_puuid: str = ji.get('killer_puuid')
        '''Example: 54942ced-1967-5f66-8a16-1e0dae875641'''

        self.killer_display_name: str = ji.get('killer_display_name')
        '''Example: Henrik3#EUW3'''

        self.killer_team: str = ji.get('killer_team')
        '''Example: Red'''

        self.victim_puuid: str = ji.get('victim_puuid')
        '''Example: 54942ced-1967-5f66-8a16-1e0dae875641'''

        self.victim_display_name: str = ji.get('victim_display_name')
        '''Example: Henrik3#EUW3'''

        self.victim_team: str = ji.get('victim_team')
        '''Example: Red'''

        self.damage_weapon_id: str = ji.get('damage_weapon_id')
        '''Example: 9C82E19D-4575-0200-1A81-3EACF00CF872'''

        self.damage_weapon_name: str = ji.get('damage_weapon_name')
        '''Example: Vandal'''

        self.secondary_fire_mode: bool = ji.get('secondary_fire_mode')
        '''Example: False'''

    def as_dict(self):
        return self.__data

class MatchRoundsItemPlayerStatsItemKillEventsItemAssistantsItem:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.assistant_puuid: str = ji.get('assistant_puuid')
        '''Example: 54942ced-1967-5f66-8a16-1e0dae875641'''

        self.assistant_display_name: str = ji.get('assistant_display_name')
        '''Example: Henrik3#EUW3'''

        self.assistant_team: str = ji.get('assistant_team')
        '''Example: Red'''

    def as_dict(self):
        return self.__data

class MatchRoundsItemPlayerStatsItemKillEventsItemDamageWeaponAssets:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.display_icon: str = ji.get('display_icon')
        '''Example: https://media.valorant-api.com/weapons/9c82e19d-4575-0200-1a81-3eacf00cf872/displayicon.png'''

        self.killfeed_icon: str = ji.get('killfeed_icon')
        '''Example: https://media.valorant-api.com/weapons/9c82e19d-4575-0200-1a81-3eacf00cf872/killstreamicon.png'''

    def as_dict(self):
        return self.__data

class MatchRoundsItemPlayerStatsItemKillEventsItemVictimDeathLocation:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.x: int = ji.get('x')
        '''Example: 7266'''

        self.y: int = ji.get('y')
        '''Example: -5096'''

    def as_dict(self):
        return self.__data

class MatchRoundsItemPlayerStatsItemKillEventsItemPlayerLocationsOnKillItem:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.location: 'MatchRoundsItemPlayerStatsItemKillEventsItemPlayerLocationsOnKillItemLocation' = ji.get('location', cast=MatchRoundsItemPlayerStatsItemKillEventsItemPlayerLocationsOnKillItemLocation)
        self.player_puuid: str = ji.get('player_puuid')
        '''Example: 54942ced-1967-5f66-8a16-1e0dae875641'''

        self.player_display_name: str = ji.get('player_display_name')
        '''Example: Henrik3#EUW3'''

        self.player_team: str = ji.get('player_team')
        '''Example: Red'''

        self.view_radians: float = ji.get('view_radians')
        '''Example: 0.5277854'''

    def as_dict(self):
        return self.__data

class MatchRoundsItemPlayerStatsItemKillEventsItemPlayerLocationsOnKillItemLocation:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.x: int = ji.get('x')
        '''Example: 5177'''

        self.y: int = ji.get('y')
        '''Example: -8908'''

    def as_dict(self):
        return self.__data

class MatchRoundsItemPlayerStatsItemAbilityCasts:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.c_casts: Optional[int] = ji.get('c_casts')
        '''Example: 2'''

        self.q_casts: Optional[int] = ji.get('q_casts')
        '''Example: 5'''

        self.e_casts: Optional[int] = ji.get('e_cast')
        '''Example: 20'''

        self.x_casts: Optional[int] = ji.get('x_cast')
        '''Example: 1'''

    def as_dict(self):
        return self.__data

class MatchRoundsItemPlayerStatsItemDamageEventsItem:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.receiver_puuid: str = ji.get('receiver_puuid')
        '''Example: 54942ced-1967-5f66-8a16-1e0dae875641'''

        self.receiver_display_name: str = ji.get('receiver_display_name')
        '''Example: Henrik3#EUW3'''

        self.receiver_team: str = ji.get('receiver_team')
        '''Example: Red'''

        self.bodyshots: int = ji.get('bodyshots')
        '''Example: 3'''

        self.damage: int = ji.get('damage')
        '''Example: 156'''

        self.headshots: int = ji.get('headshots')
        '''Example: 1'''

        self.legshots: int = ji.get('legshots')
        '''Example: 0'''

    def as_dict(self):
        return self.__data

class MatchRoundsItemPlayerStatsItemEconomy:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.weapon: 'MatchRoundsItemPlayerStatsItemEconomyWeapon' = ji.get('weapon', cast=MatchRoundsItemPlayerStatsItemEconomyWeapon)
        self.armor: 'MatchRoundsItemPlayerStatsItemEconomyArmor' = ji.get('armor', cast=MatchRoundsItemPlayerStatsItemEconomyArmor)
        self.loadout_value: int = ji.get('loadout_value')
        '''Example: 3900'''

        self.remaining: int = ji.get('remaining')
        '''Example: 5300'''

        self.spent: int = ji.get('spent')
        '''Example: 1550'''

    def as_dict(self):
        return self.__data

class MatchRoundsItemPlayerStatsItemEconomyWeapon:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.assets: 'MatchRoundsItemPlayerStatsItemEconomyWeaponAssets' = ji.get('assets', cast=MatchRoundsItemPlayerStatsItemEconomyWeaponAssets)
        self.id: str = ji.get('id')
        '''Example: 462080D1-4035-2937-7C09-27AA2A5C27A7'''

        self.name: str = ji.get('name')
        '''Example: Spectre'''

    def as_dict(self):
        return self.__data

class MatchRoundsItemPlayerStatsItemEconomyWeaponAssets:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.display_icon: str = ji.get('display_icon')
        '''Example: https://media.valorant-api.com/weapons/462080d1-4035-2937-7c09-27aa2a5c27a7/displayicon.png'''

        self.killfeed_icon: str = ji.get('killfeed_icon')
        '''Example: https://media.valorant-api.com/weapons/462080d1-4035-2937-7c09-27aa2a5c27a7/killstreamicon.png'''

    def as_dict(self):
        return self.__data

class MatchRoundsItemPlayerStatsItemEconomyArmor:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.assets: 'MatchRoundsItemPlayerStatsItemEconomyArmorAssets' = ji.get('assets', cast=MatchRoundsItemPlayerStatsItemEconomyArmorAssets)
        self.id: str = ji.get('id')
        '''Example: 822BCAB2-40A2-324E-C137-E09195AD7692'''

        self.name: str = ji.get('name')
        '''Example: Heavy Shields'''

    def as_dict(self):
        return self.__data

class MatchRoundsItemPlayerStatsItemEconomyArmorAssets:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.display_icon: str = ji.get('display_icon')
        '''Example: https://media.valorant-api.com/gear/822bcab2-40a2-324e-c137-e09195ad7692/displayicon.png'''

    def as_dict(self):
        return self.__data

class MatchRoundsItemDefuseEvents:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.defuse_location: 'MatchRoundsItemDefuseEventsDefuseLocation' = ji.get('defuse_location', cast=MatchRoundsItemDefuseEventsDefuseLocation)
        self.defused_by: 'MatchRoundsItemDefuseEventsDefusedBy' = ji.get('defused_by', cast=MatchRoundsItemDefuseEventsDefusedBy)
        self.player_locations_on_defuse: List['MatchRoundsItemDefuseEventsPlayerLocationsOnDefuseItem'] = [MatchRoundsItemDefuseEventsPlayerLocationsOnDefuseItem(e) for e in ji.get('player_locations_on_defuse', [])]
        self.defuse_time_in_round: Optional[int] = ji.get('defuse_time_in_round')
        '''Example: 26345'''

    def as_dict(self):
        return self.__data

class MatchRoundsItemDefuseEventsPlayerLocationsOnDefuseItem:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.location: 'MatchRoundsItemDefuseEventsPlayerLocationsOnDefuseItemLocation' = ji.get('location', cast=MatchRoundsItemDefuseEventsPlayerLocationsOnDefuseItemLocation)
        self.player_puuid: str = ji.get('player_puuid')
        '''Example: 54942ced-1967-5f66-8a16-1e0dae875641'''

        self.player_display_name: str = ji.get('player_display_name')
        '''Example: Henrik3#EUW3'''

        self.player_team: str = ji.get('player_team')
        '''Example: Red'''

        self.view_radians: float = ji.get('view_radians')
        '''Example: 0.5277854'''

    def as_dict(self):
        return self.__data

class MatchRoundsItemDefuseEventsPlayerLocationsOnDefuseItemLocation:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.x: int = ji.get('x')
        '''Example: 5177'''

        self.y: int = ji.get('y')
        '''Example: -8908'''

    def as_dict(self):
        return self.__data

class MatchRoundsItemDefuseEventsDefusedBy:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.puuid: str = ji.get('puuid')
        '''Example: 54942ced-1967-5f66-8a16-1e0dae875641'''

        self.display_name: str = ji.get('display_name')
        '''Example: Henrik3#EUW3'''

        self.team: str = ji.get('team')
        '''Example: Red'''

    def as_dict(self):
        return self.__data

class MatchRoundsItemDefuseEventsDefuseLocation:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.x: int = ji.get('x')
        '''Example: -1325'''

        self.y: int = ji.get('y')
        '''Example: -1325'''

    def as_dict(self):
        return self.__data

class MatchRoundsItemPlantEvents:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.plant_location: 'MatchRoundsItemPlantEventsPlantLocation' = ji.get('plant_location', cast=MatchRoundsItemPlantEventsPlantLocation)
        self.planted_by: 'MatchRoundsItemPlantEventsPlantedBy' = ji.get('planted_by', cast=MatchRoundsItemPlantEventsPlantedBy)
        self.player_locations_on_plant: List['MatchRoundsItemPlantEventsPlayerLocationsOnPlantItem'] = [MatchRoundsItemPlantEventsPlayerLocationsOnPlantItem(e) for e in ji.get('player_locations_on_plant', [])]
        self.plant_site: Optional[str] = ji.get('plant_site')
        '''Example: A'''

        self.plant_time_in_round: Optional[int] = ji.get('plant_time_in_round')
        '''Example: 26345'''

    def as_dict(self):
        return self.__data

class MatchRoundsItemPlantEventsPlayerLocationsOnPlantItem:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.location: 'MatchRoundsItemPlantEventsPlayerLocationsOnPlantItemLocation' = ji.get('location', cast=MatchRoundsItemPlantEventsPlayerLocationsOnPlantItemLocation)
        self.player_puuid: str = ji.get('player_puuid')
        '''Example: 54942ced-1967-5f66-8a16-1e0dae875641'''

        self.player_display_name: str = ji.get('player_display_name')
        '''Example: Henrik3#EUW3'''

        self.player_team: str = ji.get('player_team')
        '''Example: Red'''

        self.view_radians: float = ji.get('view_radians')
        '''Example: 0.5277854'''

    def as_dict(self):
        return self.__data

class MatchRoundsItemPlantEventsPlayerLocationsOnPlantItemLocation:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.x: int = ji.get('x')
        '''Example: 5177'''

        self.y: int = ji.get('y')
        '''Example: -8908'''

    def as_dict(self):
        return self.__data

class MatchRoundsItemPlantEventsPlantedBy:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.puuid: str = ji.get('puuid')
        '''Example: 54942ced-1967-5f66-8a16-1e0dae875641'''

        self.display_name: str = ji.get('display_name')
        '''Example: Henrik3#EUW3'''

        self.team: str = ji.get('team')
        '''Example: Red'''

    def as_dict(self):
        return self.__data

class MatchRoundsItemPlantEventsPlantLocation:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.x: int = ji.get('x')
        '''Example: -1325'''

        self.y: int = ji.get('y')
        '''Example: -1325'''

    def as_dict(self):
        return self.__data

class MatchTeams:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.red: 'Team' = ji.get('red', cast=Team)
        self.blue: 'Team' = ji.get('blue', cast=Team)
    def as_dict(self):
        return self.__data

class Team:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        # self.roster: 'TeamRoster' = ji.get('roster', cast=TeamRoster)
        self.has_won: Optional[bool] = ji.get('has_won')
        '''Example: True'''

        self.rounds_won: Optional[int] = ji.get('rounds_won')
        '''Example: 13'''

        self.rounds_lost: Optional[int] = ji.get('rounds_lost')
        '''Example: 10'''

    def as_dict(self):
        return self.__data

class TeamRoster:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.members: List[str] = [e for e in ji.get('members', [])]
        self.name: str = ji.get('name')
        self.tag: str = ji.get('tag')
        self.customization: 'TeamRosterCustomization' = ji.get('customization', cast=TeamRosterCustomization)
    def as_dict(self):
        return self.__data

class TeamRosterCustomization:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.icon: str = ji.get('icon')
        self.image: str = ji.get('image')
        self.primary: str = ji.get('primary')
        self.secondary: str = ji.get('secondary')
        self.tertiary: str = ji.get('tertiary')
    def as_dict(self):
        return self.__data

class Observer:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.puuid: str = ji.get('puuid')
        self.name: str = ji.get('name')
        self.tag: str = ji.get('tag')
        self.platform: 'ObserverPlatform' = ji.get('platform', cast=ObserverPlatform)
        self.session_playtime: 'ObserverSessionPlaytime' = ji.get('session_playtime', cast=ObserverSessionPlaytime)
        self.team: str = ji.get('team')
        self.level: float = ji.get('level')
        self.player_card: str = ji.get('player_card')
        self.player_title: str = ji.get('player_title')
        self.party_id: str = ji.get('party_id')
    def as_dict(self):
        return self.__data

class ObserverPlatform:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.os: 'ObserverPlatformOs' = ji.get('os', cast=ObserverPlatformOs)
        self.type: str = ji.get('type')
        '''Example: PC'''

    def as_dict(self):
        return self.__data

class ObserverPlatformOs:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.name: str = ji.get('name')
        '''Example: Windows'''

        self.version: str = ji.get('version')
        '''Example: 10.0.22000.1.768.64bit'''

    def as_dict(self):
        return self.__data

class ObserverSessionPlaytime:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.minutes: int = ji.get('minutes')
        '''Example: 26'''

        self.seconds: int = ji.get('seconds')
        '''Example: 1560'''

        self.milliseconds: int = ji.get('milliseconds')
        '''Example: 1560000'''

    def as_dict(self):
        return self.__data

class MatchMetadata:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.map: 'MapsEnum' = ji.get('map')
        self.mode: 'ModesEnum' = ji.get('mode')
        self.mode_id: 'ModeIdsEnum' = ji.get('mode_id')
        self.season_id: str = ji.get('season_id')
        self.match_id: str = ji.get('matchid')
        self.premier_info: 'MatchMetadataPremierInfo' = ji.get('premier_info', cast=MatchMetadataPremierInfo)
        self.region: 'RegionsEnum' = ji.get('region')
        self.game_version: str = ji.get('game_version')
        '''Example: release-03.12-shipping-16-649370'''

        self.game_length: int = ji.get('game_length')
        '''Example: 2356581'''

        self.game_start: int = ji.get('game_start')
        '''Example: 1641934366'''

        self.game_start_patched: str = ji.get('game_start_patched')
        '''Example: Tuesday, January 11, 2022 9:52 PM'''

        self.rounds_played: int = ji.get('rounds_played')
        '''Example: 23'''

        self.queue: str = ji.get('queue')
        '''Example: Standard'''

        self.platform: str = ji.get('platform')
        '''Example: PC'''

        self.cluster: str = ji.get('cluster')
        '''Example: London'''
    
    @property
    def datetime(self) -> datetime:
        return ShortDatetime.fromtimestamp(self.game_start)
    
    @property
    def url(self) -> str:
        return f"https://vtl.lol/match/{self.match_id}"

    def as_dict(self):
        return self.__data

class MatchMetadataPremierInfo:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.tournament_id: Optional[str] = ji.get('tournament_id')
        self.matchup_id: Optional[str] = ji.get('matchup_id')
    def as_dict(self):
        return self.__data

class V1mmrImages:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.small: Optional[str] = ji.get('small')
        self.large: Optional[str] = ji.get('large')
        self.triangle_down: Optional[str] = ji.get('triangle_down')
        self.triangle_up: Optional[str] = ji.get('triangle_up')
    def as_dict(self):
        return self.__data

class V1mmr:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.images: 'V1mmrImages' = ji.get('data.images', cast=V1mmrImages)
        self.currenttier: Optional[int] = ji.get('data.currenttier')
        '''Example: 12'''

        self.currenttier_patched: Optional[str] = ji.get('data.currenttier_patched')
        '''Example: Gold 1'''

        self.ranking_in_tier: Optional[int] = ji.get('data.ranking_in_tier')
        '''Example: 20'''

        self.mmr_change_to_last_game: Optional[int] = ji.get('data.mmr_change_to_last_game')
        '''Example: -16'''

        self.elo: Optional[int] = ji.get('data.elo')
        '''Example: 920'''

        self.name: Optional[str] = ji.get('data.name')
        '''Example: Henrik3'''

        self.tag: Optional[str] = ji.get('data.tag')
        '''Example: EUW3'''

        self.old: bool = ji.get('data.old')
        '''Example: True'''

    def as_dict(self):
        return self.__data

class V2mmrHighestRank:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.old: bool = ji.get('old')
        '''Example: False'''

        self.tier: Optional[int] = ji.get('tier')
        '''Example: 19'''

        self.patched_tier: Optional[str] = ji.get('patched_tier')
        '''Example: Diamond 2'''

        self.season: Optional[str] = ji.get('season')
        '''Example: e5a3'''

    def as_dict(self):
        return self.__data

class V2mmrBySeason:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.e6a3: 'BySeason' = ji.get('e6a3', cast=BySeason)
        self.e6a2: 'BySeason' = ji.get('e6a2', cast=BySeason)
        self.e6a1: 'BySeason' = ji.get('e6a1', cast=BySeason)
        self.e5a3: 'BySeason' = ji.get('e5a3', cast=BySeason)
        self.e5a2: 'BySeason' = ji.get('e5a2', cast=BySeason)
        self.e5a1: 'BySeason' = ji.get('e5a1', cast=BySeason)
        self.e4a3: 'BySeason' = ji.get('e4a3', cast=BySeason)
        self.e4a2: 'BySeason' = ji.get('e4a2', cast=BySeason)
        self.e4a1: 'BySeason' = ji.get('e4a1', cast=BySeason)
        self.e3a3: 'BySeason' = ji.get('e3a3', cast=BySeason)
        self.e3a2: 'BySeason' = ji.get('e3a2', cast=BySeason)
        self.e3a1: 'BySeason' = ji.get('e3a1', cast=BySeason)
        self.e2a3: 'BySeason' = ji.get('e2a3', cast=BySeason)
        self.e2a2: 'BySeason' = ji.get('e2a2', cast=BySeason)
        self.e2a1: 'BySeason' = ji.get('e2a1', cast=BySeason)
        self.e1a3: 'BySeason' = ji.get('e1a3', cast=BySeason)
        self.e1a2: 'BySeason' = ji.get('e1a2', cast=BySeason)
        self.e1a1: 'BySeason' = ji.get('e1a1', cast=BySeason)
    def as_dict(self):
        return self.__data

class BySeason:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.act_rank_wins: List['BySeasonActRankWinsItem'] = [BySeasonActRankWinsItem(e) for e in ji.get('act_rank_wins', [])]
        self.error: Optional[bool] = ji.get('error')
        '''Example: False'''

        self.wins: int = ji.get('wins')
        '''Example: 12'''

        self.number_of_games: int = ji.get('number_of_games')
        '''Example: 24'''

        self.final_rank: int = ji.get('final_rank')
        '''Example: 12'''

        self.final_rank_patched: str = ji.get('final_rank_patched')
        '''Example: Gold 1'''

        self.old: bool = ji.get('old')
        '''Example: True'''

    def as_dict(self):
        return self.__data

class BySeasonActRankWinsItem:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.patched_tier: str = ji.get('patched_tier')
        '''Example: Gold 1'''

        self.tier: int = ji.get('tier')
        '''Example: 12'''

    def as_dict(self):
        return self.__data

class V2mmrCurrentData:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.images: 'V2mmrCurrentDataImages' = ji.get('images', cast=V2mmrCurrentDataImages)
        self.currenttier: Optional[int] = ji.get('currenttier')
        '''Example: 12'''

        self.currenttier_patched: Optional[str] = ji.get('currenttierpatched')
        '''Example: Gold 1'''

        self.ranking_in_tier: Optional[int] = ji.get('ranking_in_tier')
        '''Example: 20'''

        self.mmr_change_to_last_game: Optional[int] = ji.get('mmr_change_to_last_game')
        '''Example: -16'''

        self.elo: Optional[int] = ji.get('elo')
        '''Example: 920'''

        self.old: bool = ji.get('old')
        '''Example: True'''

    def as_dict(self):
        return self.__data

class V2mmrCurrentDataImages:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.small: Optional[str] = ji.get('small')
        self.large: Optional[str] = ji.get('large')
        self.triangle_down: Optional[str] = ji.get('triangle_down')
        self.triangle_up: Optional[str] = ji.get('triangle_up')
    def as_dict(self):
        return self.__data

class V2mmr:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.current_data: 'V2mmrCurrentData' = ji.get('data.current_data', cast=V2mmrCurrentData)
        self.highest_rank: 'V2mmrHighestRank' = ji.get('data.highest_rank', cast=V2mmrHighestRank)
        self.by_season: 'V2mmrBySeason' = ji.get('data.by_season', cast=V2mmrBySeason)
        self.name: Optional[str] = ji.get('data.name')
        '''Example: Henrik3'''

        self.tag: Optional[str] = ji.get('data.tag')
        '''Example: EUW3'''

    def as_dict(self):
        return self.__data

class V1mmrh:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.data: List['V1mmrhDataItem'] = [V1mmrhDataItem(e) for e in ji.get('data', [])]
        self.name: str = ji.get('name')
        '''Example: Henrik3'''

        self.tag: str = ji.get('tag')
        '''Example: EUW3'''

    def as_dict(self):
        return self.__data

class V1mmrhDataItem:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.images: 'V1mmrhDataItemImages' = ji.get('images', cast=V1mmrhDataItemImages)
        self.map: 'V1mmrhDataItemMap' = ji.get('map', cast=V1mmrhDataItemMap)
        self.currenttier: int = ji.get('currenttier')
        '''Example: 12'''

        self.currenttier_patched: str = ji.get('currenttier_patched')
        '''Example: Gold 1'''

        self.match_id: str = ji.get('match_id')
        '''Example: e5a3301c-c8e5-43bc-be94-a5c0d5275fd4'''

        self.season_id: str = ji.get('season_id')
        '''Example: 34093c29-4306-43de-452f-3f944bde22be'''

        self.ranking_in_tier: int = ji.get('ranking_in_tier')
        '''Example: 20'''

        self.mmr_change_to_last_game: int = ji.get('mmr_change_to_last_game')
        '''Example: -16'''

        self.elo: int = ji.get('elo')
        '''Example: 920'''

        self.date: str = ji.get('date')
        '''Example: Tuesday, January 11, 2022 9:52 PM'''

        self.date_raw: int = ji.get('date_raw')
        '''Example: 1641934366'''

    def as_dict(self):
        return self.__data

class V1mmrhDataItemMap:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.name: str = ji.get('name')
        '''Example: Icebox'''

        self.id: str = ji.get('id')
        '''Example: e2ad5c54-4114-a870-9641-8ea21279579a'''

    def as_dict(self):
        return self.__data

class V1mmrhDataItemImages:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.small: str = ji.get('small')
        self.large: str = ji.get('large')
        self.triangle_down: str = ji.get('triangle_down')
        self.triangle_up: str = ji.get('triangle_up')
    def as_dict(self):
        return self.__data

class GetMatchesByNameResponse:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.data: List['Match'] = [Match(e) for e in ji.get('data', [])]
    def as_dict(self):
        return self.__data

class V1QueueStatusDataItem:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.mode: 'ModesEnum' = ji.get('mode')
        self.mode_id: 'ModeIdsEnum' = ji.get('mode_id')
        self.enabled: bool = ji.get('enabled')
        self.team_size: float = ji.get('team_size')
        self.number_of_teams: float = ji.get('number_of_teams')
        self.party_size: 'V1QueueStatusDataItemPartySize' = ji.get('party_size', cast=V1QueueStatusDataItemPartySize)
        self.high_skill: 'V1QueueStatusDataItemHighSkill' = ji.get('high_skill', cast=V1QueueStatusDataItemHighSkill)
        self.ranked: bool = ji.get('ranked')
        self.tournament: bool = ji.get('tournament')
        self.skill_disparity: List['V1QueueStatusDataItemSkillDisparityItem'] = [V1QueueStatusDataItemSkillDisparityItem(e) for e in ji.get('skill_disparity', [])]
        self.required_account_level: float = ji.get('required_account_level')
        self.game_rules: 'V1QueueStatusDataItemGameRules' = ji.get('game_rules', cast=V1QueueStatusDataItemGameRules)
        self.platforms: List['PlatformsEnum'] = [e for e in ji.get('platforms', [])]
        self.maps: List['V1QueueStatusDataItemMapsItem'] = [V1QueueStatusDataItemMapsItem(e) for e in ji.get('maps', [])]
    def as_dict(self):
        return self.__data

class V1QueueStatusDataItemGameRules:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.overtime_win_by_two: bool = ji.get('overtime_win_by_two')
        self.allow_lenient_surrender: bool = ji.get('allow_lenient_surrender')
        self.allow_drop_out: bool = ji.get('allow_drop_out')
        self.assign_random_agents: bool = ji.get('assign_random_agents')
        self.skip_pregame: bool = ji.get('skip_pregame')
        self.allow_overtime_draw_vote: bool = ji.get('allow_overtime_draw_vote')
        self.overtime_win_by_two_capped: bool = ji.get('overtime_win_by_two_capped')
        self.premier_mode: bool = ji.get('premier_mode')
    def as_dict(self):
        return self.__data

class V1QueueStatusDataItemHighSkill:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.max_party_size: float = ji.get('max_party_size')
        self.min_tier: float = ji.get('min_tier')
        self.max_tier: float = ji.get('max_tier')
    def as_dict(self):
        return self.__data

class V1QueueStatusDataItemSkillDisparityItem:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.tier: float = ji.get('tier')
        self.name: 'TiersEnum' = ji.get('name')
        self.max_tier: 'V1QueueStatusDataItemSkillDisparityItemMaxTier' = ji.get('max_tier', cast=V1QueueStatusDataItemSkillDisparityItemMaxTier)
    def as_dict(self):
        return self.__data

class V1QueueStatusDataItemSkillDisparityItemMaxTier:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.id: float = ji.get('id')
        self.name: 'TiersEnum' = ji.get('name')
    def as_dict(self):
        return self.__data

class V1QueueStatusDataItemPartySize:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.max: float = ji.get('max')
        self.min: float = ji.get('min')
        self.invalid: List[float] = [e for e in ji.get('invalid', [])]
        self.full_party_bypass: bool = ji.get('full_party_bypass')
    def as_dict(self):
        return self.__data

class V1QueueStatusDataItemMapsItem:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.map: 'V1QueueStatusDataItemMapsItemMap' = ji.get('map', cast=V1QueueStatusDataItemMapsItemMap)
        self.enabled: bool = ji.get('enabled')
    def as_dict(self):
        return self.__data

class V1QueueStatusDataItemMapsItemMap:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.id: str = ji.get('id')
        self.name: 'MapsEnum' = ji.get('name')
    def as_dict(self):
        return self.__data

class V1QueueStatus:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.data: List['V1QueueStatusDataItem'] = [V1QueueStatusDataItem(e) for e in ji.get('data', [])]
    def as_dict(self):
        return self.__data

class GetApiVersionResponse:
    def __init__(self, data: dict):
        self.__data = data
        ji = JsonInjester(data)
        self.build_ver: str = ji.get('data.build_ver')
        '''Example: 04.00.00.655657'''

        self.build_date: str = ji.get('data.build_date')
        '''Example: Apr  2 2024'''

        self.version: str = ji.get('data.version')
        '''Example: 15'''

        self.version_for_api: str = ji.get('data.version_for_api')
        '''Example: release-04.00-shipping-20-655657'''

        self.branch: str = ji.get('data.branch')
        '''Example: release-04.00'''

        self.region: str = ji.get('data.region')
        '''Example: EU'''

    def as_dict(self):
        return self.__data

# === MANUALLY CREATED ===
class MatchReference:
    def __init__(self, match_info: dict):
        def format_timestamp_as_str(millis) -> str:
            # Convert milliseconds to seconds
            timestamp_seconds = millis / 1000
            # Convert to datetime object
            dt_object = datetime.fromtimestamp(timestamp_seconds)
            # Format the datetime object to a string in a readable format
            readable_time = dt_object.strftime('%Y-%m-%d %H:%M:%S')
            return readable_time
        
        self.__dict = match_info
        self.match_id = match_info['MatchID']
        self.timestamp = match_info['GameStartTime']
        self.timestamp_str = format_timestamp_as_str(self.timestamp)
        self.gamemode = ModesApiEnum(match_info['QueueID'])
    def as_dict(self) -> dict:
        return self.__dict
        