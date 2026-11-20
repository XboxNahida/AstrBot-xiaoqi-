"""48小时观察日志与北京时间当日答疑，和旧汇报独立保存。"""
import json
import math
import sqlite3
import time
import uuid


class Records:
    def __init__(self, db, clock=time.time):
        self.db, self.clock = db, clock
        db.row_factory = sqlite3.Row
        db.executescript('''
        CREATE TABLE IF NOT EXISTS observations(seq INTEGER PRIMARY KEY AUTOINCREMENT,
          device TEXT,id TEXT,at REAL,text TEXT,summary_id TEXT DEFAULT '',UNIQUE(device,id));
        CREATE TABLE IF NOT EXISTS observation_summaries(id TEXT PRIMARY KEY,device TEXT,first_at REAL,last_at REAL,text TEXT);
        CREATE TABLE IF NOT EXISTS qa_daily(device TEXT,conversation TEXT,message TEXT,at REAL,role TEXT,text TEXT,settings TEXT,
          PRIMARY KEY(device,conversation,message));
        CREATE TABLE IF NOT EXISTS qa_deleted(device TEXT,conversation TEXT,at REAL,PRIMARY KEY(device,conversation));
        ''')

    def day_start(self):
        return ((int(self.clock()) + 28800) // 86400) * 86400 - 28800

    def prune(self):
        cutoff = self.clock() - 172800
        with self.db:
            stale = [r[0] for r in self.db.execute('SELECT id FROM observation_summaries WHERE first_at<?', (cutoff,))]
            for identity in stale:
                self.db.execute("UPDATE observations SET summary_id='' WHERE summary_id=?", (identity,))
            self.db.execute('DELETE FROM observation_summaries WHERE first_at<?', (cutoff,))
            self.db.execute('DELETE FROM observations WHERE at<?', (cutoff,))
            self.db.execute('DELETE FROM qa_daily WHERE at<?', (self.day_start(),))
            self.db.execute('DELETE FROM qa_deleted WHERE at<?', (cutoff,))

    def _timestamp(self, at):
        if isinstance(at, bool) or not isinstance(at, (int, float)) or not math.isfinite(at) or at > self.clock() + 300:
            raise ValueError('记录时间无效')

    def observation(self, device, identity, at, text):
        self._timestamp(at)
        if not isinstance(text, str) or not 0 < len(text) <= 1000:
            raise ValueError('观察正文须为1至1000字')
        self.prune()
        if at < self.clock() - 172800:
            return False
        with self.db:
            cursor = self.db.execute('INSERT OR IGNORE INTO observations(device,id,at,text) VALUES(?,?,?,?)',
                                     (device, identity, at, text))
        return cursor.rowcount == 1

    def summary_batch(self, device):
        self.prune()
        rows, length = [], 0
        for row in self.db.execute("SELECT * FROM observations WHERE device=? AND summary_id='' ORDER BY seq", (device,)):
            rows.append(dict(row))
            length += len(row['text'])
            if length >= 4000:
                break
        return rows if length > 1000 else []

    def finish_summary(self, device, batch, text):
        self.prune()
        if not batch or not text.strip() or min(r['at'] for r in batch) < self.clock() - 172800:
            return
        identity = uuid.uuid4().hex
        with self.db:
            self.db.execute('INSERT INTO observation_summaries VALUES(?,?,?,?,?)',
                            (identity, device, min(r['at'] for r in batch), max(r['at'] for r in batch), text[:500]))
            self.db.executemany('UPDATE observations SET summary_id=? WHERE device=? AND seq=?',
                                [(identity, device, r['seq']) for r in batch])

    def qa_message(self, device, at, row):
        self._timestamp(at)
        self.prune()
        for key in ('conversation', 'message'):
            if not isinstance(row.get(key), str) or not 0 < len(row[key]) <= 128:
                raise ValueError('答疑标识无效')
        if row.get('role') not in ('user', 'assistant') or not isinstance(row.get('text'), str) or len(row['text']) > 120000:
            raise ValueError('答疑正文无效')
        settings = json.dumps(row.get('settings', {}), ensure_ascii=False)
        if len(settings) > 4000:
            raise ValueError('答疑元数据过长')
        if at < self.day_start() or self.db.execute('SELECT 1 FROM qa_deleted WHERE device=? AND conversation=?',
                                                   (device, row['conversation'])).fetchone():
            return
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO qa_daily VALUES(?,?,?,?,?,?,?)',
                            (device, row['conversation'], row['message'], at, row['role'], row['text'], settings))

    def qa_delete(self, device, conversation):
        if not isinstance(conversation, str) or not 0 < len(conversation) <= 128:
            raise ValueError('答疑会话标识无效')
        with self.db:
            self.db.execute('DELETE FROM qa_daily WHERE device=? AND conversation=?', (device, conversation))
            self.db.execute('INSERT OR REPLACE INTO qa_deleted VALUES(?,?,?)', (device, conversation, self.clock()))

    def read(self, device):
        self.prune()
        return {
            'timezone': 'Asia/Shanghai', 'qa_day_start': self.day_start(),
            'summaries': [dict(r) for r in self.db.execute('SELECT first_at,last_at,text FROM observation_summaries WHERE device=? ORDER BY first_at', (device,))],
            'observations': [dict(r) for r in self.db.execute("SELECT at,text FROM observations WHERE device=? AND summary_id='' ORDER BY seq", (device,))],
            'qa_today': [{**dict(r), 'settings': json.loads(r['settings'])} for r in self.db.execute(
                'SELECT conversation,message,at,role,text,settings FROM qa_daily WHERE device=? ORDER BY at,rowid', (device,))],
        }
