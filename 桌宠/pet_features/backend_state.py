"""应用日累计和提醒游标的最小本地状态，不记录窗口标题。"""
import datetime as dt
import json
import sqlite3


class BackendState:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS backend_state (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")

    def restore(self, apps, reminders):
        row = self.db.execute("SELECT value FROM backend_state WHERE id=1").fetchone()
        if not row:
            return
        value = json.loads(row[0])
        apps.daily = value.get("daily", {})
        # 不恢复应用采样游标，避免将桌宠关闭时间计入前台使用。
        reminders.previous = self._time(value.get("previous"))
        reminders.intervals = {k: self._time(v) for k, v in value.get("intervals", {}).items()}
        reminders.seen = {k: self._time(v).isoformat() for k, v in value.get("seen", {}).items()}

    @staticmethod
    def _time(value):
        return dt.datetime.fromtimestamp(value) if value is not None else None

    def save(self, apps, reminders):
        value = {"daily": apps.stats(),
                     "previous": reminders.previous.timestamp() if reminders.previous else None,
                     "intervals": {k: v.timestamp() for k, v in reminders.intervals.items()},
                     "seen": {k: dt.datetime.fromisoformat(v).timestamp() for k, v in reminders.seen.items()}}
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO backend_state VALUES (1, ?)",
                            (json.dumps(value, ensure_ascii=False),))

    def close(self):
        self.db.close()
