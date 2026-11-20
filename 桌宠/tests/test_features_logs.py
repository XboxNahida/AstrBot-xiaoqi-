"""只使用临时合成日志，绝不扫描用户会话。"""
import json

import pytest

from pet_features.agent_logs import AgentLogReader
from pet_features.usage import UsageTracker


def append(path, row, newline=True):
    with path.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(row) + ('\n' if newline else ''))


def test_baseline_partial_and_restart(tmp_path):
    log = tmp_path / 'a.jsonl'
    append(log, {'state': 'error', 'text': 'secret'})
    state = tmp_path / 'state.sqlite'
    reader = AgentLogReader(tmp_path, state)
    assert reader.poll() == []
    append(log, {'state': 'working'}, False)
    assert reader.poll() == []
    with log.open('a') as stream:
        stream.write('\n')
    rows = reader.poll()
    assert [r['action'] for r in rows] == ['working']
    assert 'secret' not in str(rows)
    assert AgentLogReader(tmp_path, state).poll() == []


def test_new_file_states_and_truncate(tmp_path):
    reader = AgentLogReader(tmp_path)
    assert reader.poll() == []
    log = tmp_path / 'a.jsonl'
    states = ['start', 'working', 'tool_pwsh', 'tool_edit', 'tool_read',
              'tool_web', 'approval', 'error', 'done', 'idle', 'sing']
    for state in states:
        append(log, {'state': state})
    assert [r['action'] for r in reader.poll()] == states
    log.write_text('{bad}\n' + json.dumps({'state': 'done'}) + '\n', encoding='utf-8')
    assert [r['action'] for r in reader.poll()] == ['done']


def token(total, last=3):
    return {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {
        'total_token_usage': {'input_tokens': total, 'output_tokens': total // 2},
        'last_token_usage': {'input_tokens': last}}}}


def test_usage_deltas_dedupe_and_persist(tmp_path):
    reader = AgentLogReader(tmp_path)
    reader.poll()
    log = tmp_path / 'a.jsonl'
    for total in [10, 10, 20]:
        append(log, token(total))
    reader.poll()
    rows = reader.poll_usage()
    tracker = UsageTracker(tmp_path / 'usage.sqlite')
    assert tracker.ingest(rows) == 2
    assert tracker.ingest(rows) == 0
    assert tracker.report()['totals']['input_tokens'] == 20
    tracker.close()
    tracker = UsageTracker(tmp_path / 'usage.sqlite')
    assert tracker.report()['totals']['output_tokens'] == 10
    tracker.close()


def test_boundary_and_bad_compressed(tmp_path):
    reader = AgentLogReader(tmp_path)
    reader.poll()
    (tmp_path / 'bad.jsonl.zstd').write_bytes(b'broken')
    (tmp_path / 'ignored.txt').write_text('{"state":"error"}\n')
    child = tmp_path / 'child'
    child.mkdir()
    append(child / 'hidden.txt', {'state': 'error'})
    assert reader.poll() == []


def test_explicit_usage_invalid_numbers(tmp_path):
    tracker = UsageTracker(tmp_path / 'usage.sqlite')
    assert tracker.record({'input_tokens': 4, 'output_tokens': 2}, 'request')
    assert not tracker.record({'input_tokens': 4}, 'request')
    assert not tracker.record({'input_tokens': -1}, 'bad')
    assert tracker.report()['requests'] == 1
    tracker.close()


def test_historical_total_is_not_new_usage(tmp_path):
    log = tmp_path / 'a.jsonl'
    append(log, token(100))
    reader = AgentLogReader(tmp_path)
    reader.poll()
    assert reader.poll_usage() == []
    append(log, token(110))
    reader.poll()
    assert reader.poll_usage()[0]['input_tokens'] == 10


def test_rotation_and_same_size_rewrite(tmp_path):
    reader = AgentLogReader(tmp_path)
    reader.poll()
    log = tmp_path / 'a.jsonl'
    append(log, {'state': 'start'})
    reader.poll()
    log.write_text('{"state": "error"}\n', encoding='utf-8')
    assert [r['action'] for r in reader.poll()] == ['error']


def test_real_dsh_schema(tmp_path):
    reader = AgentLogReader(tmp_path)
    reader.poll()
    log = tmp_path / 'session.v4.jsonl'
    for row in [{'type': 'turn/start'}, {'type': 'tool/call', 'data': {'name': 'pwsh'}},
                {'type': 'approval/asked'}, {'type': 'tool/result', 'data': {'message': {'isError': True}}},
                {'type': 'turn/end'}]:
        append(log, row)
    assert [r['action'] for r in reader.poll()] == ['start', 'tool_pwsh', 'approval', 'error', 'done']


def test_compressed_frames_partial_and_resume(tmp_path):
    zstd = pytest.importorskip('zstandard')
    reader = AgentLogReader(tmp_path)
    reader.poll()
    log = tmp_path / 'session.v4.jsonl.zstd'
    frame = zstd.ZstdCompressor().compress(b'{"type":"turn/start"}\n')
    log.write_bytes(frame[:8])
    assert reader.poll() == []
    with log.open('ab') as stream:
        stream.write(frame[8:])
    assert [r['action'] for r in reader.poll()] == ['start']
    assert reader.poll() == []


def test_missing_historical_total_uses_only_explicit_last(tmp_path):
    log = tmp_path / 'a.jsonl'
    append(log, {'state': 'working'})
    reader = AgentLogReader(tmp_path)
    reader.poll()
    append(log, token(1000, last=3))
    reader.poll()
    assert reader.poll_usage()[0]['input_tokens'] == 3


def test_nested_sessions_and_source_separation(tmp_path):
    reader = AgentLogReader(tmp_path)
    reader.poll()
    nested = tmp_path / 'sessions' / '2026' / '11'
    nested.mkdir(parents=True)
    append(nested / 'a.jsonl', {'state': 'start'})
    assert reader.poll()[0]['action'] == 'start'
    tracker = UsageTracker(tmp_path / 'usage.sqlite')
    tracker.record({'input_tokens': 3}, 'a', 'agent')
    tracker.record({'input_tokens': 9}, 'b', 'pet')
    report = tracker.report()
    assert report['by_source']['agent']['input_tokens'] == 3
    assert report['by_source']['pet']['input_tokens'] == 9
    assert report['current_is_last_record'] is True
    tracker.close()


def test_newest_files_win_over_old_sessions(tmp_path):
    import os

    for index in range(128):
        path = tmp_path / f'old-{index:03}.jsonl'
        append(path, {'state': 'idle'})
        os.utime(path, (100, 100))
    reader = AgentLogReader(tmp_path)
    reader.poll()
    latest = tmp_path / 'zz-new.jsonl'
    append(latest, {'state': 'start'})
    assert reader.poll()[0]['action'] == 'start'
    assert '上限' in reader.status


def test_unknown_size_compressed_frame(tmp_path):
    import zstandard as zstd

    reader = AgentLogReader(tmp_path)
    reader.poll()
    log = tmp_path / 'unknown.jsonl.zstd'
    frame = zstd.ZstdCompressor(write_content_size=False).compress(b'{"state":"done"}\n')
    log.write_bytes(frame[:-1])
    assert reader.poll() == []
    with log.open('ab') as stream:
        stream.write(frame[-1:])
    assert [row['action'] for row in reader.poll()] == ['done']
    assert reader.poll() == []


def test_unknown_size_output_limit_and_multiple_frames(tmp_path):
    import zstandard as zstd

    reader = AgentLogReader(tmp_path)
    reader.MAX_BYTES = 256
    reader.poll()
    compressor = zstd.ZstdCompressor(write_content_size=False)
    oversized = tmp_path / 'oversized.jsonl.zstd'
    oversized.write_bytes(compressor.compress(b'x' * 10000))
    assert reader.poll() == []
    log = tmp_path / 'normal.jsonl.zstd'
    log.write_bytes(compressor.compress(b'{"state":"start"}\n') +
                    compressor.compress(b'{"state":"done"}\n'))
    assert [row['action'] for row in reader.poll()] == ['start', 'done']
