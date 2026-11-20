"""答疑会话与图像持久化；与原锐评/记忆完全独立。"""
import json
import sqlite3
import time
import uuid
from pathlib import Path


class ConversationStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "conversations.sqlite")
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY,title TEXT NOT NULL,settings TEXT NOT NULL,updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,conversation TEXT NOT NULL REFERENCES conversations(id)
                ON DELETE CASCADE,role TEXT NOT NULL,text TEXT NOT NULL,images TEXT NOT NULL,created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS images (id TEXT PRIMARY KEY,data BLOB NOT NULL);
        """)

    def create(self, settings):
        identity = uuid.uuid4().hex
        with self.db:
            self.db.execute("INSERT INTO conversations VALUES(?,?,?,?)",
                            (identity, "新对话", json.dumps(settings, ensure_ascii=False), time.time()))
        return identity

    def conversations(self):
        return [dict(row) for row in self.db.execute("SELECT * FROM conversations ORDER BY updated DESC")]

    def settings(self, identity):
        row = self.db.execute("SELECT settings FROM conversations WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise ValueError("对话已删除")
        return json.loads(row[0])

    def messages(self, identity):
        return [{**dict(row), "images": json.loads(row["images"])} for row in self.db.execute(
            "SELECT * FROM messages WHERE conversation=? ORDER BY id", (identity,))]

    def image(self, identity):
        row = self.db.execute("SELECT data FROM images WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise ValueError("截图已删除")
        return bytes(row[0])

    def append(self, identity, role, text, images=()):
        if role not in ("user", "assistant"):
            raise ValueError("消息角色无效")
        self.settings(identity)
        ids = []
        with self.db:
            for data in images:
                image_id = uuid.uuid4().hex
                self.db.execute("INSERT INTO images VALUES(?,?)", (image_id, bytes(data)))
                ids.append(image_id)
            self.db.execute("INSERT INTO messages(conversation,role,text,images,created) VALUES(?,?,?,?,?)",
                            (identity, role, text, json.dumps(ids), time.time()))
            self.db.execute("UPDATE conversations SET updated=?,title=CASE WHEN title='新对话' AND ?='user' "
                            "THEN ? ELSE title END WHERE id=?", (time.time(), role, text[:24] or "截图答疑", identity))
        return self.messages(identity)[-1]

    def update_settings(self, identity, settings):
        with self.db:
            self.db.execute('UPDATE conversations SET settings=? WHERE id=?',
                            (json.dumps(settings, ensure_ascii=False), identity))

    def delete(self, identity):
        ids = [image for message in self.messages(identity) for image in message["images"]]
        with self.db:
            self.db.execute("DELETE FROM conversations WHERE id=?", (identity,))
            self.db.executemany("DELETE FROM images WHERE id=?", [(image,) for image in ids])

    def close(self):
        self.db.close()
