"""指定目录的有限增量读取器；不保留、转发会话正文。"""
import hashlib
import json
import sqlite3
import time
from pathlib import Path

from .usage import FIELDS, normalize

STATES = {'start': '开发任务开始', 'working': '正在思考', 'tool_pwsh': '正在执行命令',
          'tool_edit': '正在编辑文件', 'tool_read': '正在读取文件', 'tool_web': '正在搜索',
          'approval': '等待你的批准', 'error': '开发任务遇到错误', 'done': '开发任务完成',
          'idle': '开发任务空闲', 'sing': '正在播放音乐'}


class AgentLogReader:
    MAX_BYTES = 2 * 1024 * 1024
    MAX_FILES = 128
    MAX_ROWS = 1000

    def __init__(self, directory, state_path=None, compressed=True):
        self.directory = Path(directory).resolve()
        self.compressed = compressed
        self.states = {}
        self.usage = []
        self.status = '就绪'
        self.db = sqlite3.connect(str(state_path) if state_path else ':memory:')
        self.db.execute('CREATE TABLE IF NOT EXISTS offsets (id TEXT PRIMARY KEY, data TEXT)')
        for key, value in self.db.execute('SELECT id,data FROM offsets'):
            self.states[key] = json.loads(value)
        self.started = False

    def _paths(self):
        if not self.directory.is_dir():
            return []
        paths, pending, inspected = [], [(self.directory.stat().st_mtime_ns, self.directory, 0)], 0
        limited = False
        while pending and inspected < 4096:
            pending.sort(key=lambda row: row[0])
            _, folder, depth = pending.pop()
            children = []
            for path in folder.iterdir():
                inspected += 1
                if inspected > 4096:
                    limited = True
                    break
                if path.is_symlink() or path.is_junction() or not path.resolve().is_relative_to(self.directory):
                    continue
                children.append((path.stat().st_mtime_ns, path))
            for modified, path in sorted(children, key=lambda row: row[0], reverse=True):
                if path.is_dir() and depth < 8:
                    pending.append((modified, path, depth + 1))
                elif path.is_dir():
                    limited = True
                elif path.is_file() and (path.name.endswith('.jsonl') or
                        self.compressed and path.name.endswith('.jsonl.zstd')):
                    paths.append((modified, path))
        if pending or limited or len(paths) > self.MAX_FILES:
            self.status = '日志发现达到上限，仅监视最新128个文件；可缩小所选目录'
        else:
            self.status = '就绪'
        return [path for _, path in sorted(paths, key=lambda row: row[0], reverse=True)[:self.MAX_FILES]]

    def _read(self, path, offset):
        if path.name.endswith('.zstd'):
            try:
                import zstandard
            except ImportError:
                self.status = '压缩日志需要可选依赖 zstandard'
                return None
            try:
                with path.open('rb') as stream:
                    stream.seek(offset)
                    chunk = stream.read(self.MAX_BYTES)
                data, consumed = bytearray(), 0
                while chunk and len(data) < self.MAX_BYTES:
                    # DSH每次追加独立帧；预先拒绝声明过大的帧。
                    size = zstandard.frame_content_size(chunk)
                    if size > self.MAX_BYTES:
                        self.status = '压缩帧超出限量或未声明长度'
                        break
                    # 即使帧未声明长度，也先用输出上限验证完整首帧。
                    decoded = zstandard.ZstdDecompressor().decompress(
                        chunk, max_output_size=self.MAX_BYTES)
                    decoder = zstandard.ZstdDecompressor().decompressobj()
                    decoder.decompress(chunk)  # 首帧已经证明完整且输出有界，仅取帧边界。
                    if not decoder.eof:
                        break
                    if len(data) + len(decoded) > self.MAX_BYTES:
                        break
                    data.extend(decoded)
                    used = len(chunk) - len(decoder.unused_data)
                    consumed += used
                    chunk = decoder.unused_data
                self._compressed_end = offset + consumed
                return bytes(data)
            except (OSError, zstandard.ZstdError):
                self.status = '压缩日志损坏，已跳过'
                return None
        with path.open('rb') as stream:
            stream.seek(offset)
            return stream.read(self.MAX_BYTES)

    @staticmethod
    def _anchor(path, offset):
        with path.open('rb') as stream:
            stream.seek(max(0, offset - 128))
            return hashlib.sha256(stream.read(min(offset, 128))).hexdigest()

    def _baseline(self, path, state):
        state['total_unknown'] = True
        if path.name.endswith('.zstd'):
            # 已有压缩内容直接建立物理EOF基线，不重播、不全量解压。
            state['offset'] = path.stat().st_size
            data = b''
        else:
            state['offset'] = path.stat().st_size
            with path.open('rb') as stream:
                stream.seek(max(0, state['offset'] - self.MAX_BYTES))
                data = stream.read(self.MAX_BYTES)
        # 只提取末尾累计数字建立用量基线，历史状态不播报。
        for line in reversed(data.splitlines()):
            try:
                row = json.loads(line)
                payload = row.get('payload', row)
                info = payload.get('info', {})
                total = normalize(info.get('total_token_usage'))
                if total is not None:
                    state['total'] = total
                    state['total_unknown'] = False
                    break
            except (ValueError, AttributeError, TypeError):
                continue

    def _row(self, row, state, key, position):
        if not isinstance(row, dict):
            return None
        payload = row.get('payload', row)
        if not isinstance(payload, dict):
            return None
        kind = payload.get('state', payload.get('status', payload.get('type')))
        aliases = {'task_started': 'start', 'task_complete': 'done', 'turn_started': 'start',
                   'turn_completed': 'done', 'agent_reasoning': 'working',
                   'turn/start': 'start', 'turn/end': 'done', 'approval/asked': 'approval',
                   'approval/decided': 'working'}
        kind = aliases.get(kind, kind)
        detail = payload.get('data', {})
        if kind == 'tool/call' and isinstance(detail, dict):
            name = str(detail.get('name', '')).lower()
            kind = {'pwsh': 'tool_pwsh', 'bash': 'tool_pwsh', 'shell': 'tool_pwsh',
                    'edit': 'tool_edit', 'apply_patch': 'tool_edit', 'write': 'tool_edit',
                    'read': 'tool_read', 'web': 'tool_web', 'search': 'tool_web'}.get(name)
        if kind == 'tool/result' and isinstance(detail, dict):
            message = detail.get('message')
            if isinstance(message, dict) and message.get('isError'):
                kind = 'error'
        info = payload.get('info')
        if kind == 'token_count' and isinstance(info, dict):
            total = normalize(info.get('total_token_usage'))
            last = normalize(info.get('last_token_usage'))
            previous = state.get('total', dict.fromkeys(FIELDS, 0))
            if total is not None:
                values = {k: max(0, total[k] - previous.get(k, 0)) for k in FIELDS}
                if any(total[k] < previous.get(k, 0) for k in FIELDS):
                    values = total
                state['total'] = total
                if state.pop('total_unknown', False):
                    values = last
            else:
                values = last
            if values and any(values.values()):
                identity = str(payload.get('request_id') or payload.get('turn_id') or position)
                self.usage.append(dict(values, request_id=f'{key}:{state["generation"]}:{identity}',
                                       source='agent', created=time.time()))
        if kind not in STATES:
            return None
        return {'id': f'agent:{key}:{state["generation"]}:{position}', 'source': 'agent',
                'text': STATES[kind], 'action': kind,
                'priority': 80 if kind in ('error', 'approval') else 55,
                'ttl': 15, 'manual': False, 'created': time.time()}

    def poll(self):
        events = []
        try:
            paths = self._paths()
        except OSError:
            self.status = '日志目录不可读取'
            return events
        for path in paths:
            try:
                stat = path.stat()
                key = hashlib.sha256(str(path).encode()).hexdigest()
                identity = [stat.st_dev, stat.st_ino]
                state = self.states.get(key)
                if state is None:
                    state = {'offset': 0, 'identity': identity, 'size': stat.st_size,
                             'generation': time.time_ns()}
                    self.states[key] = state
                    if not self.started:
                        self._baseline(path, state)
                        state['anchor'] = self._anchor(path, min(state['offset'], stat.st_size))
                        self._save(key, state)
                        continue
                elif (state['identity'] != identity or stat.st_size < state['size'] or
                      (not path.name.endswith('.zstd') and state.get('anchor') !=
                       self._anchor(path, state['offset']))):
                    state.update(offset=0, identity=identity, generation=time.time_ns())
                    state.pop('total', None)
                state['size'] = stat.st_size
                data = self._read(path, state['offset'])
                if data is None:
                    continue
                position = state['offset']
                lines = data.splitlines(keepends=True)
                if path.name.endswith('.zstd') and len(lines) > self.MAX_ROWS:
                    self.status = '压缩帧事件超出限量，已跳过超量部分'
                for line in lines[:self.MAX_ROWS]:
                    if not line.endswith(b'\n'):
                        break
                    position += len(line)
                    try:
                        event = self._row(json.loads(line), state, key, position)
                        if event:
                            events.append(event)
                    except (ValueError, UnicodeError, TypeError):
                        pass
                # 超长坏行不无限重读；正常半行保持原偏移。
                state['offset'] = position if position > state['offset'] or len(data) < self.MAX_BYTES else position + len(data)
                if path.name.endswith('.zstd'):
                    state['offset'] = self._compressed_end
                state['anchor'] = self._anchor(path, min(state['offset'], stat.st_size))
                self._save(key, state)
            except OSError:
                self.status = '部分日志暂时不可读取'
        self.started = True
        return events

    def _save(self, key, state):
        self.db.execute('INSERT OR REPLACE INTO offsets VALUES (?,?)', (key, json.dumps(state)))
        self.db.commit()

    def poll_usage(self):
        rows, self.usage = self.usage, []
        return rows

    def close(self):
        self.db.close()
