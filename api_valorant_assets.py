import inspect
from typing import Dict, Optional, List
from enum import Enum
from agent_name_enum import AgentName

import requests
from datetime import datetime
from functools import cached_property as lazy_property
import time
import math

from jsoninjest import JsonInjester
from api_cache import Cache
from api_request_logger import RequestLogger
from utils import singleton


REQUESTS_LOG = 'logs/assets-api-requests.log'
CACHE_FILE = 'caches/assets-api-cache.pkl'

RATE_LIMIT = 90 # requests
RATE_PER = 60 # seconds

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


def get_lazy_properties(cls):
    lazy_properties = []
    for name, attr in inspect.getmembers(cls):
        if isinstance(attr, lazy_property):
            lazy_properties.append(name)
    return lazy_properties

class RateException(Exception):
    pass

@singleton
class ValAssetApi:
    
    def __init__(self, language: Optional[str]='en-US', cache_filepath=None, requests_log=None):
        self.__base_uri = 'https://valorant-api.com'
        cache_filepath = cache_filepath or CACHE_FILE
        self.cache = Cache(cache_filepath)
        log_filepath = requests_log or REQUESTS_LOG
        self.logger = RequestLogger(log_filepath)
        self.agent_names = sorted([e.value for e in AgentName])
        
        self.language = language

        # Store versioning information
        resource = self.__base_uri + '/v1/version'
        data = self.fetch(resource, math.inf)
        self.version = AssetsApiVersioning(data['data'])
        
        if self.is_data_stale():
            self.cache.completely_erase_cache()

    def invalidate_lazy_props(self):
        cached_props = get_lazy_properties(ValAssetApi)
        for prop in cached_props:
            if prop in self.__dict__:
                del self.__dict__[prop]
    
    @property
    def is_rate_limit_reached(self):
        recent_requests = self.logger.get_logs_from_last_seconds(RATE_PER)
        return len(recent_requests) >= RATE_LIMIT
    
    @property
    def quota_usage(self):
        return len(self.logger.get_logs_from_last_seconds(RATE_PER)) / RATE_LIMIT
    
    @property
    def time_until_limit_reset(self):       
        recent_requests = self.logger.get_logs_from_last_seconds(RATE_PER)
        if len(recent_requests) == 0:
            return 0
    
        oldest_request_ts: datetime = recent_requests[0][0]
        target_time = oldest_request_ts + datetime.timedelta(seconds=RATE_PER)

        time_to_wait = (target_time - datetime.now()).total_seconds()
        
        return time_to_wait
    
    def wait_until_limit_reset(self):       
        time.sleep(self.time_until_limit_reset + 1)
        
    def uncached_fetch(self, uri: str, payload: Optional[dict]=None) -> requests.Response:
        if self.is_rate_limit_reached:
            raise RateException(f"Rate limit reached! You must wait {self.time_until_limit_reset} seconds.")
        
        response = requests.get(uri, json=payload)
        response.raise_for_status()
        self.logger.log(uri, response.status_code)
        return response
    
    def uncached_post(self, uri: str, payload: Optional[dict]=None) -> requests.Response:
        if self.is_rate_limit_reached:
            raise RateException(f"Rate limit reached! You must wait {self.time_until_limit_reset} seconds.")
        
        response = requests.post(uri, json=payload)
        response.raise_for_status()
        self.logger.log(uri, response.status_code)
        return response
    
    def fetch(self, uri: str, expiry: int, force_update: bool=False) -> dict:
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
    
    # === RESOURCES ===

    def get_maps(self) -> List['MapItem']:
        endpoint = '/v1/maps'
        resource = self.__base_uri + endpoint
        
        data = self.fetch(resource, math.inf)
        return [MapItem(e) for e in data['data']]
    
    def get_competitive_tiers(self) -> List['TierItem']:
        resource = self.__base_uri + '/v1/competitivetiers'
        data = self.fetch(resource, math.inf)
        # return [CompTierItem(each) for each in data['data']]
        tiers = CompTierItem(data['data'][-1]).tiers
        return [t for t in tiers if 'Unused' not in t.divisionName]
    
    @lazy_property
    def agents(self):
        return self.get_agents()

    @lazy_property
    def gamemodes(self):
        return self.get_gamemodes()

    @lazy_property
    def maps(self):
        return self.get_maps()
    
    @lazy_property
    def seasons(self):
        return self.get_seasons()
    
    def get_agents(self) -> Dict[str, 'AgentItem']:
        required_params = {}
        optional_params = {'language': self.language, 'isPlayableCharacter': True}
        resource = self.__base_uri + build_url('/v1/agents', required_params, optional_params)
        data = self.fetch(resource, math.inf)
        
        return {agent['displayName']: AgentItem(agent) for agent in data['data']}
    
    def get_agent_by_name(self, name: str) -> 'AgentItem':
        return self.agents[name]
    
    def get_agent_by_uuid(self, uuid: str) -> 'AgentItem':
        return {a.uuid: a for a in self.agents.values()}[uuid]

    def get_gamemodes(self) -> Dict[str, 'Gamemode']:
        required_params = {}
        optional_params = {'language': self.language}
        resource = self.__base_uri + build_url('/v1/gamemodes', required_params, optional_params)
        data = self.fetch(resource, math.inf)
        
        return {e['displayName']: Gamemode(e) for e in data['data']}
    
    def get_season_index_from_uuid(self, uuid: str) -> int:
        required_params = {}
        optional_params = {'language': 'en-US'}
        resource = self.__base_uri + build_url('/v1/seasons', required_params, optional_params)
        data = self.fetch(resource, math.inf)

        seasons = [Season(e) for e in data['data']]
        predicate = lambda s: all([itm in s for itm in ['CompetitiveSeason', 'Episode', 'Act']])
        seasons = [s for s in seasons if predicate(s.assetPath)]
        season_id_to_index = {s.uuid:n+1 for n,s in enumerate(seasons)}
        return season_id_to_index[uuid]
    
    def get_seasons(self) -> Dict[str, 'Season']:
        required_params = {}
        optional_params = {'language': self.language}
        resource = self.__base_uri + build_url('/v1/seasons', required_params, optional_params)
        data = self.fetch(resource, math.inf)

        return {e['uuid']: Season(e) for e in data['data']}
    
    def is_data_stale(self):
        resource = self.__base_uri + '/v1/version'
        response = self.uncached_fetch(resource)
        response.raise_for_status()
        data = response.json()
        current_version = AssetsApiVersioning(data['data'])
        return self.version.buildDate == current_version.buildDate

# === MODELS ===
def is_generic_alias(obj):
    return type(obj).__name__ == '_GenericAlias'

def apimodel(cls):
    def init(self, data: dict):
        ji = JsonInjester(data)
        setattr(self, '_Initial__Data', data)
        fields = self.__annotations__
        for member in fields:
            cast = fields[member]
            if isinstance(cast, type):
                value = ji.get(member, cast=cast)
                setattr(self, member, value)
            elif is_generic_alias(cast):
                container_type = cast.__origin__
                item_cast = object
                if len(cast.__args__) == 1:
                    item_cast = cast.__args__[0]
                if container_type == list:
                    value = [item_cast(e) for e in ji.get(member, [])]
                    setattr(self, member, value)
                    
    def as_dict(self):
        return self._Initial__Data
    
    cls.__init__ = init
    cls.as_dict = as_dict
    return cls

@apimodel
class AssetsApiVersioning:
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
    x: float
    y: float

@apimodel
class MapCallout:
    regionName: str # (localized)
    superRegionName: str # (localized)
    location: Coordinate

@apimodel
class MapItem:
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
    uuid: str
    displayName: str # (localized)
    description: str # (localized)
    displayIcon: str
    assetPath: str
    
    def __repr__(self):
        return f'<role:{self.displayName}>'
    
@apimodel
class AgentAbilities:
    slot: str
    displayName: str # (localized)
    description: str # (localized)
    displayIcon: str
    
    def __repr__(self):
        return f'<ability:{self.displayName}>'
    
@apimodel
class AgentItem:
    uuid: str
    displayName: str # (localized)
    description: str # (localized)
    developerName: str
    characterTags: List[str] # (localized)
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
    
    def __repr__(self):
        return f'<agent:{self.displayName}>'

@apimodel
class TierItem:
    tier: int
    tierName: str # (localized)
    division: str
    divisionName: str # (localized)
    color: str
    backgroundColor: str
    smallIcon: str
    largeIcon: str
    rankTriangleDownIcon: str
    rankTriangleUpIcon: str
    
    @lazy_property
    def promotion_level(self):
        if self.divisionName in ['UNRANKED', 'Unused1', 'Unused2']:
            return 0
        
        return int(self.divisionName.replace(self.division, '').strip())
    
    def __repr__(self):
        return f'<tier:{self.tierName.capitalize()}>'

@apimodel
class CompTierItem:
    uuid: str
    assetObjectName: str
    tiers: List[TierItem]
    assetPath: str


@apimodel
class FeatureOverride:
    featureName: str
    state: bool

@apimodel
class RuleOverride:
    ruleName: str
    state: bool

@apimodel
class Gamemode:
    uuid: str
    displayName: str # (localized)
    duration: str # (localized)
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

    def __repr__(self):
        return f'<gamemode:{self.displayName}>'

@apimodel
class SeasonBorder:
    uuid: str
    winsRequired: int
    level: int
    displayIcon: str
    smallIcon: str
    assetPath: str

@apimodel
class Season:
    uuid: str
    displayName: str
    startTime: str
    endTime: str
    borders = List[SeasonBorder]
    assetPath: str

    def __repr__(self):
        return f'<season:{self.displayName}>'