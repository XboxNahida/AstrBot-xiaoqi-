"""新版记录与独立图文请求协议，保留旧指令/聊天协议。"""
import asyncio
import importlib.util
import json
import logging
import time
from pathlib import Path


def load_records():
    spec = importlib.util.spec_from_file_location('desktop_records', Path(__file__).with_name('records.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Records


class Extension:
    def __init__(self, server, handler=None, summarizer=None):
        self.server, self.handler, self.summarizer = server, handler, summarizer
        self.records = load_records()(server.db)
        self.busy, self.summarizing = set(), set()
        server.db.execute('CREATE TABLE IF NOT EXISTS rpc_results(device TEXT,id TEXT,status TEXT,result TEXT,at REAL,PRIMARY KEY(device,id))')
        server.db.execute("UPDATE rpc_results SET status='failed',result=? WHERE status='running'",
                          (json.dumps({'error': '服务重启，上次答疑结果未知，请手动重新提问'}),))
        server.db.commit()

    async def report(self, device, ws, data):
        facts = data.get('facts', {})
        kind = facts.get('_record_type') if isinstance(facts, dict) else None
        if kind not in ('observation', 'qa_message', 'qa_delete'):
            return False
        identity = data.get('id')
        if not isinstance(identity, str) or not 0 < len(identity) <= 128:
            raise ValueError('记录标识无效')
        fresh = False
        if kind == 'observation':
            fresh = self.records.observation(device, identity, data.get('observed_at'), facts.get('text'))
        elif kind == 'qa_message':
            self.records.qa_message(device, data.get('observed_at'), facts)
        else:
            self.records.qa_delete(device, facts.get('conversation'))
        await ws.send_json({'type': 'report_ack', 'id': identity})
        if fresh:
            self.schedule_summary(device)
        return True

    def _task(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.server.chat_tasks.add(task)
        task.add_done_callback(self.server.chat_tasks.discard)

    def schedule_summary(self, device):
        if not self.summarizer or device in self.summarizing:
            return
        batch = self.records.summary_batch(device)
        if batch:
            self.summarizing.add(device)
            self._task(self._summarize(device, batch))

    async def _summarize(self, device, batch):
        success = False
        try:
            text = await asyncio.wait_for(self.summarizer(device, batch), 120)
            if isinstance(text, str) and text.strip():
                self.records.finish_summary(device, batch, text)
                success = True
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 -- 模型边界保留原文且不循环重试。
            # 不重试循环；原文保留，新记录到达才再次尝试。
            logging.getLogger(__name__).warning('观察总结失败（%s），原文保留', type(exc).__name__)
        finally:
            self.summarizing.discard(device)
        if success:
            self.schedule_summary(device)

    async def rpc(self, device, ws, data):
        identity, op, payload = data.get('id'), data.get('op'), data.get('payload')
        if not isinstance(identity, str) or not 0 < len(identity) <= 128 or op not in ('personas', 'qa', "qa_info") or not isinstance(payload, dict):
            raise ValueError('新版请求格式无效')
        self.server.db.execute('DELETE FROM rpc_results WHERE at<?', (self.records.day_start(),))
        old = self.server.db.execute('SELECT status,result FROM rpc_results WHERE device=? AND id=?', (device, identity)).fetchone()
        if old:
            if old['status'] != 'running':
                await ws.send_json({'type': 'rpc_result', 'id': identity, **json.loads(old['result'])})
            return
        if device in self.busy:
            await ws.send_json({'type': 'rpc_result', 'id': identity, 'error': '上一请求尚未结束'})
            return
        self.server.db.execute("INSERT INTO rpc_results VALUES(?,?,'running','{}',?)", (device, identity, time.time()))
        self.server.db.commit()
        self.busy.add(device)
        self._task(self._run(device, ws, identity, op, payload))

    async def _run(self, device, ws, identity, op, payload):
        result = {}
        try:
            if self.handler is None:
                raise ValueError('未配置新版云端接口')
            result = {'result': await asyncio.wait_for(self.handler(device, op, payload), 120)}
            if len(json.dumps(result, ensure_ascii=False).encode('utf-8')) > 2 * 1024 * 1024:
                raise ValueError('云端输出过长')
        except asyncio.CancelledError:
            result = {'error': '云端请求已取消'}
        except Exception as exc:  # noqa: BLE001 -- 服务端不向配对客户端暴露提供商内部错误。
            logging.getLogger(__name__).warning('图文请求失败（%s）', type(exc).__name__)
            result = {'error': str(exc) if getattr(exc, "desktop_safe_error", False) else
                      '云端答疑或人格读取失败，请检查插件日志、视觉模型和人格配置'}
        finally:
            self.busy.discard(device)
            self.server.db.execute('UPDATE rpc_results SET status=?,result=? WHERE device=? AND id=?',
                                   ('failed' if 'error' in result else 'succeeded', json.dumps(result, ensure_ascii=False), device, identity))
            self.server.db.commit()
        if not ws.closed:
            try:
                await ws.send_json({'type': 'rpc_result', 'id': identity, **result})
            except (ConnectionError, RuntimeError):
                pass

    async def maintain(self):
        while True:
            self.records.prune()
            with self.server.db:
                self.server.db.execute('DELETE FROM rpc_results WHERE at<?', (self.records.day_start(),))
            await asyncio.sleep(60)
