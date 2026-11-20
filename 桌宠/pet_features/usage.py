"""只存数字用量，未知用量不推算。"""
import datetime as dt
import hashlib
import json
import sqlite3
import time

FIELDS = ('input_tokens', 'cached_input_tokens', 'output_tokens', 'reasoning_output_tokens')


def normalize(usage):
    if not isinstance(usage, dict) or not any(k in usage for k in FIELDS):
        return None
    values = {}
    for key in FIELDS:
        value = usage.get(key, 0)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        values[key] = value
    return values


class UsageTracker:
    def __init__(self, db_path):
        self.db = sqlite3.connect(str(db_path))
        self.db.execute('CREATE TABLE IF NOT EXISTS usage (id TEXT PRIMARY KEY, source TEXT, '
                        'created REAL, day TEXT, data TEXT)')

    def record(self, usage, request_id, source='agent'):
        values = normalize(usage)
        if values is None or not request_id:
            return False
        now = usage.get('created', time.time())
        if not isinstance(now, (int, float)) or not 0 <= now <= 253402214400:
            now = time.time()
        key = hashlib.sha256((str(source) + ':' + str(request_id)).encode()).hexdigest()
        day = dt.datetime.fromtimestamp(now).date().isoformat()
        cursor = self.db.execute('INSERT OR IGNORE INTO usage VALUES (?,?,?,?,?)',
                                 (key, str(source)[:40], now, day, json.dumps(values)))
        self.db.commit()
        return bool(cursor.rowcount)

    def ingest(self, records, source='agent'):
        return sum(self.record(row, row.get('request_id'), row.get('source', source))
                   for row in records if isinstance(row, dict))

    def report(self):
        today = dt.date.today().isoformat()
        result = {'totals': dict.fromkeys(FIELDS, 0), 'today': dict.fromkeys(FIELDS, 0),
                  'current': dict.fromkeys(FIELDS, 0), 'requests': 0,
                  'by_source': {}, 'current_is_last_record': True}
        for source, day, data in self.db.execute('SELECT source,day,data FROM usage ORDER BY created,rowid'):
            values = json.loads(data)
            result['requests'] += 1
            result['current'] = values
            bucket = result['by_source'].setdefault(source, dict.fromkeys(FIELDS, 0))
            for field in FIELDS:
                bucket[field] += values[field]
                result['totals'][field] += values[field]
                if day == today:
                    result['today'][field] += values[field]
        return result

    def close(self):
        self.db.close()
