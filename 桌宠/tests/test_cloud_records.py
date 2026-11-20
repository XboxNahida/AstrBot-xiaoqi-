import importlib.util
from datetime import UTC
from pathlib import Path


def records(db, clock):
    path = Path(__file__).parents[1] / 'cloud_plugin/astrbot_plugin_desktop_link/records.py'
    spec = importlib.util.spec_from_file_location('test_records_module', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Records(db, clock=clock)


def test_ttl_threshold_snapshot_and_daily_deletion():
    import sqlite3
    now = [1800000000.0]
    store = records(sqlite3.connect(':memory:'), lambda: now[0])
    for identity, count in [('a', 500), ('b', 500)]:
        store.observation('pc', identity, now[0], '字' * count)
    assert store.summary_batch('pc') == []
    store.observation('pc', 'c', now[0], '字')
    batch = store.summary_batch('pc')
    assert len(batch) == 3
    store.observation('pc', 'd', now[0], '新记录')
    store.finish_summary('pc', batch, '摘要')
    assert store.read('pc')['observations'][-1]['text'] == '新记录'
    row = {'conversation': 'c1', 'message': 'm1', 'role': 'user', 'text': '题目', 'settings': {}}
    store.qa_message('pc', now[0], row)
    store.qa_message('pc', now[0], row)
    assert len(store.read('pc')['qa_today']) == 1
    assert store.read('other')['qa_today'] == []
    store.qa_delete('pc', 'c1')
    store.qa_message('pc', now[0], row)
    assert store.read('pc')['qa_today'] == []
    now[0] += 172801
    assert store.read('pc')['summaries'] == []
    assert store.read('pc')['observations'] == []


def test_daily_retention_is_beijing_calendar_day():
    import sqlite3
    from datetime import datetime
    now = [datetime(2026, 11, 17, 15, 59, tzinfo=UTC).timestamp()]
    store = records(sqlite3.connect(':memory:'), lambda: now[0])
    store.qa_message('pc', now[0], {'conversation': 'c', 'message': 'm', 'role': 'assistant', 'text': '解答', 'settings': {}})
    now[0] += 120
    assert store.read('pc')['qa_today'] == []


def test_many_short_observations_use_total_not_first_page():
    import sqlite3
    store = records(sqlite3.connect(':memory:'), lambda: 1800000000.0)
    for i in range(101):
        store.observation('pc', str(i), 1800000000.0, '字' * 10)
    assert sum(len(r['text']) for r in store.summary_batch('pc')) > 1000
