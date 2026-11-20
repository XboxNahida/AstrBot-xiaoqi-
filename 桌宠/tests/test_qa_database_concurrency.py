import sqlite3
import time
from pathlib import Path

from qa_store import ConversationStore


def test_read_snapshot_does_not_block_gui_write_or_delete(tmp_path):
    store = ConversationStore(tmp_path)
    identity = store.create({'model': 'one', 'persona': 'local'})
    store.append(identity, 'user', '题目', [b'original'])
    reader = sqlite3.connect(Path(tmp_path / 'conversations.sqlite').as_uri() + '?mode=ro', uri=True)
    try:
        reader.execute('BEGIN')
        reader.execute('SELECT * FROM images').fetchone()
        start = time.monotonic()
        store.update_settings(identity, {'resolved_identity': 'fixed'})
        store.create({'model': 'two'})
        store.delete(identity)
        assert time.monotonic() - start < 1
        assert reader.execute('SELECT data FROM images').fetchone()[0] == b'original'
    finally:
        reader.close()
        store.close()
