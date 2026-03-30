from urllib.error import HTTPError
from api_henrik import AffinitiesEnum, Match, UnofficialApi, V1LifetimeMmrHistoryItem
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

from db_valorant import PlayerProfile, ValorantDB

import time

class MatchHistoryProcessor:

    def __init__(self, puuid: str, acts_of_interest: List[Tuple[str, str]], db: ValorantDB, match_count_max: int = 200, timespan: int = 60):
        """
        Initialize the match history processor.
        
        Args:
            puuid: Player unique identifier
            acts_of_interest: List of season/act identifiers to filter by
            db: ValorantDB instance
            match_count_max: Maximum number of matches to include
            timespan: Number of days to look back for matches
        """

        self.api = UnofficialApi()
        self.db = db
        
        self.db.update_match_history_for_puuid(puuid)
        self.my_account: PlayerProfile = self.db.get_profile_by_puuid(puuid)
        
        self.timespan = timespan
        self.match_count_max = match_count_max
                
        episode_act_formatter = lambda e, a: f'e{e}a{a}'
        self.acts_of_interest: List[str] = [episode_act_formatter(*act) for act in acts_of_interest]
        
        # Processed data attributes
        self.seasonal_history: Dict[str, Dict[datetime, V1LifetimeMmrHistoryItem]] = defaultdict(dict)
        self._recent_history: Dict[datetime, V1LifetimeMmrHistoryItem] = {}
        self.recent_matches: List[Match] = []
        self.recent_matches_by_id: Dict[str, Match] = {}
        self.recent_mmr: Dict[str, V1LifetimeMmrHistoryItem] = {}
        self.recent_season_labels: Dict[str, str] = {}
        self.recent_is_placement: Dict[str, bool] = {}
        self.recent_previous_match: Dict[str, Optional[str]] = {}
        
        # Process the data
        self._process()
    
    # Accessor methods for seasonal_history
    def get_seasonal_history(self, season: Optional[str] = None) -> Dict:
        """
        Get seasonal history, optionally filtered by season.
        
        Args:
            season: Optional season identifier to filter by
            
        Returns:
            Full seasonal history dict or history for specific season
        """
        if season:
            return dict(self.seasonal_history.get(season, {}))
        return {k: dict(v) for k, v in self.seasonal_history.items()}
    
    def get_seasons(self) -> List[str]:
        """Get list of all seasons in the history."""
        return list(self.seasonal_history.keys())
    
    # Accessor methods for recent_history
    def get_recent_history(self) -> Dict[datetime, V1LifetimeMmrHistoryItem]:
        """Get all recent match history entries."""
        return dict(self._recent_history)
    
    def get_recent_history_by_date(self, date: datetime) -> Optional[V1LifetimeMmrHistoryItem]:
        """Get a specific match history entry by date."""
        return self._recent_history.get(date)
    
    # Accessor methods for recent_matches
    def get_recent_matches(self) -> List[Match]:
        """Get list of recent matches in chronological order."""
        return list(self.recent_matches)
    
    def get_match_count(self) -> int:
        """Get the number of recent matches."""
        return len(self.recent_matches)
    
    def get_match_at_index(self, index: int) -> Optional[Match]:
        """Get a match by its index in the recent matches list."""
        if 0 <= index < len(self.recent_matches):
            return self.recent_matches[index]
        return None
    
    # Accessor methods for recent_matches_by_id
    def get_match_by_id(self, match_id: str) -> Optional[Match]:
        """Get a match by its match ID."""
        return self.recent_matches_by_id.get(match_id)
    
    def get_all_match_ids(self) -> List[str]:
        """Get list of all recent match IDs."""
        return list(self.recent_matches_by_id.keys())
    
    def has_match(self, match_id: str) -> bool:
        """Check if a match ID exists in recent matches."""
        return match_id in self.recent_matches_by_id
    
    # Accessor methods for recent_mmr
    def get_mmr_by_match_id(self, match_id: str) -> Optional[V1LifetimeMmrHistoryItem]:
        """Get MMR data for a specific match."""
        return self.recent_mmr.get(match_id)
    
    def get_all_mmr_data(self) -> Dict[str, V1LifetimeMmrHistoryItem]:
        """Get all MMR data for recent matches."""
        return dict(self.recent_mmr)
    
    # Accessor methods for recent_season_labels
    def get_season_label(self, match_id: str) -> Optional[str]:
        """Get the season label for a specific match."""
        return self.recent_season_labels.get(match_id)
    
    def get_all_season_labels(self) -> Dict[str, str]:
        """Get season labels for all recent matches."""
        return dict(self.recent_season_labels)
    
    def get_matches_by_season(self, season: str) -> List[str]:
        """Get list of match IDs for a specific season."""
        return [
            match_id 
            for match_id, label in self.recent_season_labels.items() 
            if label == season
        ]
    
    # Accessor methods for recent_is_placement
    def is_placement_match(self, match_id: str) -> Optional[bool]:
        """Check if a specific match is a placement match."""
        return self.recent_is_placement.get(match_id)
    
    def get_placement_status(self) -> Dict[str, bool]:
        """Get placement status for all recent matches."""
        return dict(self.recent_is_placement)
    
    def get_placement_matches(self) -> List[str]:
        """Get list of match IDs that are placement matches."""
        return [
            match_id 
            for match_id, is_placement in self.recent_is_placement.items() 
            if is_placement
        ]
    
    def get_ranked_matches(self) -> List[str]:
        """Get list of match IDs that are not placement matches."""
        return [
            match_id 
            for match_id, is_placement in self.recent_is_placement.items() 
            if not is_placement
        ]
    
    # Accessor methods for recent_previous_match
    def get_previous_match_id(self, match_id: str) -> Optional[str]:
        """Get the match ID of the previous match."""
        return self.recent_previous_match.get(match_id)
    
    def get_next_match_id(self, match_id: str) -> Optional[str]:
        """Get the match ID of the next match (chronologically)."""
        for curr_id, prev_id in self.recent_previous_match.items():
            if prev_id == match_id:
                return curr_id
        return None
    
    def get_match_chain(self, match_id: str, direction: str = 'backward') -> List[str]:
        """
        Get a chain of matches starting from the given match.
        
        Args:
            match_id: Starting match ID
            direction: 'backward' for previous matches, 'forward' for next matches
            
        Returns:
            List of match IDs in chronological order
        """
        chain = [match_id]
        
        if direction == 'backward':
            current = match_id
            while True:
                prev = self.get_previous_match_id(current)
                if prev is None:
                    break
                chain.append(prev)
                current = prev
        else:  # forward
            current = match_id
            while True:
                next_id = self.get_next_match_id(current)
                if next_id is None:
                    break
                chain.insert(0, next_id)
                current = next_id
        
        return chain
    
    # Additional utility accessors
    def get_match_summary(self, match_id: str) -> Optional[Dict]:
        """Get a summary of all data for a specific match."""
        if not self.has_match(match_id):
            return None
        
        return {
            'match': self.get_match_by_id(match_id),
            'mmr': self.get_mmr_by_match_id(match_id),
            'season': self.get_season_label(match_id),
            'is_placement': self.is_placement_match(match_id),
            'previous_match_id': self.get_previous_match_id(match_id),
            'next_match_id': self.get_next_match_id(match_id)
        }
    
    def get_date_range(self) -> Optional[tuple[datetime, datetime]]:
        """Get the date range of recent matches (oldest, newest)."""
        if not self._recent_history:
            return None
        dates = list(self._recent_history.keys())
        return (min(dates), max(dates))
    
    # Processing methods
    def _build_seasonal_history(self):
        """Build seasonal history grouped by season/act."""
        for game in self.my_account.match_history.values():
            self.seasonal_history[game.season.short].update({game.datetime: game})
    
    def _get_threshold(self) -> datetime:
        """Calculate the threshold datetime for filtering matches."""
        return datetime.now(timezone.utc) - timedelta(days=self.timespan)
    
    def _select_matches(self, threshold: datetime):
        """Select matches from acts of interest within the timespan."""
        matches_selection = [
            (datecode, game)
            for act in self.acts_of_interest
            for (datecode, game) in self.seasonal_history[act].items()
        ]
        
        self._recent_history = dict(
            sorted(
                ((datecode, game) for datecode, game in matches_selection if datecode >= threshold),
                key=lambda x: x[0],
                reverse=True
            )[:self.match_count_max]
        )
    
    def _build_match_data(self):
        # """Build match data structures from recent history."""
        # self.recent_matches = [
        #     self.db.get_match(match.match_id) # error handling
        #     for match in self._recent_history.values()
        # ]

        """Build match data structures from recent history."""
        self.recent_matches = []

        for match in self._recent_history.values():
            match_data = self.db.get_match(match.match_id)
            if match_data is None:
                continue
            self.recent_matches.append(match_data)
        
        
        self.recent_matches_by_id = {
            g.metadata.match_id: g 
            for g in self.recent_matches
        }
        
        self.recent_mmr = {
            game.match_id: game 
            for game in self._recent_history.values()
        }
        
        self.recent_season_labels = {
            g.metadata.match_id: self.recent_mmr[g.metadata.match_id].season.short 
            for g in self.recent_matches
        }
        
        self.recent_is_placement = {
            g.metadata.match_id: self.recent_mmr[g.metadata.match_id].tier.name == 'Unrated' 
            for g in self.recent_matches
        }
        
        self.recent_previous_match = {
            self.recent_matches[i].metadata.match_id: 
                self.recent_matches[i+1].metadata.match_id if i < len(self.recent_matches) - 1 else None
            for i in range(len(self.recent_matches))
        }
    
    def _process(self):
        """Execute the full processing pipeline."""
        self._build_seasonal_history()
        threshold = self._get_threshold()
        self._select_matches(threshold)
        self._build_match_data()
    
    def refresh(self):
        """Refresh all processed data with current account data."""
        self.seasonal_history.clear()
        self._process()