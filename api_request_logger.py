from datetime import datetime
import math
import threading
from typing import Dict, List

from pathlib import Path
import time
import json

from utils import ensure_dirs_exist


LOG_FILENAME = 'api_logfile.log'


UNSET = object()
class LogRecord:
    def __init__(self, record: dict):
        self.__record_dict = record
        self.timestamp: float = record['timestamp']
        self.data: dict = {'uri': record['uri'], 'status': int(record['status'])}
    
    def as_dict(self):
        return self.__record_dict
    
    def __repr__(self):
        ts = datetime.fromtimestamp(self.timestamp)
        ts_str = ts.strftime('%d-%m-%Y %I:%M:%S,%f %p')
        return f"{ts_str} - {self.data!r}"

class RequestLogger:
    def __init__(self, filepath=None):
        self.lock = threading.Lock()
        filepath = filepath or LOG_FILENAME
        self.filepath = filepath

        ensure_dirs_exist(filepath)
        Path(filepath).touch(exist_ok=True)
        with open(filepath, 'r') as f:
            content: str = f.read()
            if content.strip() == '':
                content = '[]'
            records: list = json.loads(content)
        
        self.records: List[LogRecord] = [LogRecord(v) for v in records]

    def get_logs_from_last_seconds(self, seconds:int=60) -> List[LogRecord]:
        # Calculate the time 60 seconds ago
        current_time = datetime.now()

        # Read log file and filter lines within the last 60 seconds
        recent_logs: List[LogRecord] = []
        for log in self.records:
            request_timestamp = datetime.fromtimestamp(log.timestamp)
            # request_timestamp = datetime.strptime(ts, '%Y-%m-%d %H:%M:%S,%f')
            time_since_request = (current_time - request_timestamp).total_seconds()
            
            if time_since_request <= seconds:
                recent_logs.append(log)

        recent_logs.sort(key=lambda log: log.timestamp, reverse=False)
        return recent_logs

    def __save(self):
        with self.lock:
            data = self.as_list()
            content = json.dumps(data, indent=2, separators=(",", ": "))
            
            with open(self.filepath, 'w') as fp:
                fp.write(content)
        
    def as_list(self):
        return [v.as_dict() for v in self.records]
    
    def log(self, uri: str, status: int):
        with self.lock:
            new_record = {'uri': uri, 'status': status, 'timestamp': time.time()}
            self.records.append(LogRecord(new_record))
            
        self.__save()
