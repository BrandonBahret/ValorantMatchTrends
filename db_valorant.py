from concurrent.futures import ThreadPoolExecutor
from functools import cached_property as lazy_property
import inspect
import time
from typing import Dict, List, Optional, Tuple
from collections import defaultdict

from tqdm import tqdm

from api_cache import Cache
from jsoninjest import UNSET

from api_henrik import AffinitiesEnum, Match, UnofficialApi, V1AccountCard
from api_henrik import MatchReference, V1LifetimeMmrHistoryItem
from api_henrik import BySeason, BySeasonActRankWinsItem
import rank_utils



class SeasonPerformance:
    def __init__(self, by_season_item: Tuple[str, dict]):
        episode_act_str, season_data = by_season_item
        season = BySeason(season_data)
        self.has_data = season.error == UNSET
        
        if not self.has_data:
            return None
        
        self.season_str = episode_act_str
        act_index = episode_act_str.index('a')
        self.episode = int(episode_act_str[1:act_index])
        self.act = int(episode_act_str[act_index+1:])
        self.season_index = (self.episode-1) * 3 + self.act
        '''Formula (episode-1) * 3 + act: e1a1->1 ... e8a2->23'''
        
        self.wins = season.wins
        self.games_played = season.number_of_games
        self.win_rate = season.wins / season.number_of_games
        
        placements: Dict[int, BySeasonActRankWinsItem] = {}
        # should_skip_unrated = len(season.act_rank_wins) > 1
        for each in season.act_rank_wins:
            # if should_skip_unrated and each.patched_tier == 'Unrated':
            #     continue
            placements[each.tier] = each
        placements: List[Tuple[int, BySeasonActRankWinsItem]] = sorted(placements.items(), reverse=True)
        
        self.peak_act_rank = placements[0][1].patched_tier
        '''Peak placement for the act'''
        
        self.starting_act_rank = placements[-1][1].patched_tier
        '''First known placement for the rank'''
        
        self.rank_at_end = season.final_rank_patched
        '''Placement at the very end of the act'''
        
        # We're looking for the first placement here
        if self.starting_act_rank == 'Unrated':
            if len(placements) > 2:
                self.starting_act_rank = placements[-2][1].patched_tier
       
class PlayerIdentification:
    def __init__(self, puuid: str):
        api = UnofficialApi()
        account = api.get_account_by_puuid(puuid)
        self.puuid = account.puuid
        self.name = account.name
        self.tag = account.tag
        self.full_name = f"{account.name}#{account.tag}"

class PlayerCurrentTier:
    def __init__(self, puuid: str):
        api = UnofficialApi()
        account = api.get_account_by_puuid(puuid)
        perf = api.get_act_performance_by_puuid(puuid, account.region)
        
        self.peak_rank = perf.highest_rank.patched_tier
        self.rank = perf.current_data.currenttier_patched
        self.rank_rating = perf.current_data.ranking_in_tier
        self.rank_images = perf.current_data.images
        self.elo = perf.current_data.elo
        self.level = account.account_level

class PlayerProfile:
    def __init__(self, puuid: str, history: Dict[str, Optional['V1LifetimeMmrHistoryItem']]):        
        self.api = UnofficialApi()
        self._puuid = puuid
        self.match_history = history # List of matches this player participated in
        
    @lazy_property
    def identity(self) -> PlayerIdentification:
        identity = PlayerIdentification(self.puuid)
        return identity
    
    @lazy_property
    def current(self) -> PlayerCurrentTier:
        current = PlayerCurrentTier(self.puuid)
        return current
        
    @lazy_property
    def artwork(self) -> V1AccountCard:
        account = self.api.get_account_by_puuid(self.puuid)
        return account.card

    @lazy_property
    def region(self) -> str:
        account = self.api.get_account_by_puuid(self.puuid)
        return account.region
    
    @lazy_property
    def seasonal_performances(self) -> List[SeasonPerformance]:
        seasonal_performances: List[SeasonPerformance] = []
        perf = self.api.get_act_performance_by_puuid(self.puuid, self.region)
        labeled_seasonal_performance_details = perf.by_season.as_dict().items()
        for each in labeled_seasonal_performance_details:
            performance = SeasonPerformance(each)
            if performance.has_data:
                seasonal_performances.append(performance)
        seasonal_performances = sorted(seasonal_performances, key=lambda e: e.season_index, reverse=True)        
        return seasonal_performances
    
    @property
    def puuid(self) -> str:
        return self._puuid
    
    @property
    def peak_rank(self) -> str:
        seasonal_peaks = [e.peak_act_rank for e in self.seasonal_performances]
        seasonal_peaks_floats = map(rank_utils.map_rank_to_float, seasonal_peaks)
        return rank_utils.reverse_map_valorant_rank(max(seasonal_peaks_floats), include_rr=False)
    
    @property
    def match_ids(self):
        return list(self.match_history.keys())

    def get_match_mmr(self, match_id: str) -> Optional['V1LifetimeMmrHistoryItem']:
        return self.match_history[match_id]


def get_lazy_properties(cls):
    lazy_properties = []
    for name, attr in inspect.getmembers(cls):
        if isinstance(attr, lazy_property):
            lazy_properties.append(name)
    return lazy_properties

class ValorantDB:
    def __init__(self, region: 'AffinitiesEnum' = 'na'):
        self.region = region
        self._dao: Cache = Cache('data/analysis.json')

    def invalidate_lazy_props(self):
        cached_props = get_lazy_properties(ValorantDB)
        for prop in cached_props:
            if prop in self.__dict__:
                del self.__dict__[prop]
    
    def invalidate_property(self, prop: str):
        cached_props = get_lazy_properties(ValorantDB)
        if prop not in cached_props:
            raise KeyError(f"{prop!r} is not a lazy loaded property!")
        if prop not in self.__dict__:
            del self.__dict__[prop]

    @lazy_property
    def available_matches(self) -> Dict[str, MatchReference]:
        matches: dict = self._dao.get_object('matches', {})
        matches = {k: MatchReference(v) for (k,v) in matches.items()}
        return matches
    
    @lazy_property
    def available_matches_list(self) -> Dict[str, MatchReference]:
        return list(self.available_matches.keys())

    @lazy_property
    def players(self) -> Dict[str, Dict[str, Optional['V1LifetimeMmrHistoryItem']]]:
        players: dict = self._dao.get_object('players', {})
        players = defaultdict(dict, players)
        for puuid in players.keys():
            history = players[puuid]
            for match_id in history:
                each = history[match_id]
                
                if each['mmr'] is None:
                    continue
                if isinstance(each['mmr'], V1LifetimeMmrHistoryItem):
                    continue
                else:
                    each['mmr'] = V1LifetimeMmrHistoryItem(each['mmr'])
        
        return players
    
    @lazy_property
    def available_players(self) -> List[str]:
        players: dict = self._dao.get_object('players', {})
        return list(players.keys())
       
    def get_match(self, match_id: str) -> 'Match':
        api = UnofficialApi()
        return api.get_match_by_id(match_id)
    
    def get_profile_by_name(self, name: str, tag: str) -> 'PlayerProfile':
        api = UnofficialApi()
        
        account = api.get_account_by_name(name, tag)
        puuid = account.puuid
        if puuid not in self.available_players:
            self.update_match_history_for_puuid(puuid)
        
        # history = {match_id: V1LifetimeMmrHistoryItem(v['mmr']) for (match_id,v) in self.players[puuid].items()}
        history = {
            match_id: (
                v['mmr']
                if isinstance(v['mmr'], V1LifetimeMmrHistoryItem)
                else V1LifetimeMmrHistoryItem(v['mmr'])
            )
            for match_id, v in self.players[puuid].items()
            if v['mmr'] is not None
        }
        return PlayerProfile(puuid, history)
    
    def get_profile_by_puuid(self, puuid: str) -> 'PlayerProfile':        
        if puuid not in self.players:
            self.update_match_history_for_puuid(puuid)
        
        # history = {match_id: V1LifetimeMmrHistoryItem(v['mmr']) for (match_id,v) in self.players[puuid].items() if v['mmr'] is not None}
        history = {
            match_id: (
                v['mmr']
                if isinstance(v['mmr'], V1LifetimeMmrHistoryItem)
                else V1LifetimeMmrHistoryItem(v['mmr'])
            )
            for match_id, v in self.players[puuid].items()
            if v['mmr'] is not None
        }
        
        return PlayerProfile(puuid, history)
    
    def update_match_history_for_puuid(self, puuid: str):
        '''
        This method will search the api for recent matches completed by a given player.
        Consequently, members refrenced before this method was invoked should to be assumed out-of-date.
        '''
        def save_players_dict(players: dict):
            for puuid, history in players.items():
                for match_id, metadata in history.items():
                    if isinstance(metadata['mmr'], V1LifetimeMmrHistoryItem):
                        metadata['mmr'] = metadata['mmr'].as_dict()
        
            self._dao.store('players', players, cast=dict)
        
        def save_matches_dict(history: Dict[str, MatchReference]):
            matches = {}
            for match_id in history.keys():
                matches[match_id] = history[match_id].as_dict()

            self._dao.store('matches', matches, cast=dict)
        
        api = UnofficialApi()
        
        players = self.players # Load current players dict from cache
        matches = self.available_matches # Load current match history from cache
        
        # Get the recent match history for this player
        history = api.get_match_history(puuid, region=self.region)
        for ref in history:
            matches[ref.match_id] = ref
            if ref.match_id not in players[puuid]: # Don't overwrite metadata for match
                players[puuid][ref.match_id] = {'mmr': None}

        # Search mmr history for metadata to add
        history = api.get_recent_mmr_history_by_puuid(puuid, region=self.region)
        for mmr in history.data:
            players[puuid][mmr.match_id] = {'mmr': mmr}

        save_players_dict(players)
        save_matches_dict(matches)

    def download_missing_match_data(self):
        NUM_THREADS = 10
        CHUNK_SIZE = 100
        
        api = UnofficialApi()
        
        # Split available matches into chunks
        match_count = len(self.available_matches_list)
        chunks = (self.available_matches_list[i:i+CHUNK_SIZE] for i in range(0, match_count, CHUNK_SIZE))

        def fetch_match(match_id):
            return api.get_match_by_id(match_id)

        with ThreadPoolExecutor(max_workers=NUM_THREADS) as executor:
            for chunk in chunks:
                queue = {
                    match_id: executor.submit(fetch_match, match_id)
                    for match_id in chunk
                }
                
                for match_id in tqdm(chunk):
                    future = queue[match_id]
                    m = future.result()
                    
                    # Process the fetched match 'm' as needed
    
    def update_match_history_for_my_profile(self):
        '''
        Collects information for recent matches and their participants.
        Additionaly, collects mmr change information for my matches.
        '''
        
        api = UnofficialApi()
        
        profile = self.get_profile_by_name('bbahret', '001')
        self.update_match_history_for_puuid(profile.puuid)
        
        for match_id in profile.match_ids:
            self.invalidate_lazy_props()
            current_known_players = self.players
            match = api.get_match_by_id(match_id)
            for player in match.players:
                if player.puuid in current_known_players:
                    continue
                self.update_match_history_for_puuid(player.puuid)
