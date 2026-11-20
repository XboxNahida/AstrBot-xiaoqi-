"""后台答疑请求：能力协商、预算、模型身份与用量，独立于Qt窗口。"""
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from cloud_plugin.astrbot_plugin_desktop_link.context_policy import ContextPlanner
from cloud_plugin.astrbot_plugin_desktop_link.model_limits import model_identity
from companion_link import DeviceLink
from qa_budget import MODES, mode_for, output_policy, prepare_budget
from qa_capabilities import CapabilityStore
from qa_models import QAClient


class QARequests:
    def __init__(self, controller, root):
        self.controller = controller
        self.limits = CapabilityStore(root / '模型上下文配置.json')
        self.context = ContextPlanner()

    def profile(self, job):
        if job['model'] == 'cloud':
            scope = DeviceLink.scope_for(job['cloud'])
            info = self.controller.bridge.link.rpc('qa_info', {}, expected_scope=scope)
            profile = {**info['profile'], 'scope': job['cloud'].get('device_id', '') + ':' + info['signature'],
                       'base_url': job['cloud'].get('url', ''), 'provider': 'cloud'}
            self.limits.remember(profile, info.get('metadata', {}))
            return profile, info
        profile = job['profile']
        if job['model'] == 'original':
            from smart_models import SmartModelClient
            cfg = SimpleNamespace(get=lambda key, default=None: job['original'].get(key, default))
            client = SmartModelClient(SimpleNamespace(config_mgr=cfg, engine=self.controller.engine), {}, {})
            profile = client._original_profile()
        return profile, None

    def run(self, job, on_chunk, on_binding=None):
        profile, cloud_info = self.profile(job)
        binding = cloud_info['signature'] if cloud_info else model_identity(profile)
        previous = job['settings'].get('resolved_identity')
        if previous and binding != previous:
            raise ValueError('实际模型或服务商已变更，请开启新对话，旧会话继续保留')
        if on_binding:
            on_binding(binding)
        policy = self.limits.resolve(profile) if cloud_info else self.limits.discover(profile)
        entry = self.limits.entry(profile)
        mode = mode_for(job['settings'])
        policy = output_policy(profile, policy, entry, mode)
        if mode == 'retain':
            policy = dict(policy, max_messages=max(len(job['messages']), policy['max_messages']))
        if cloud_info:
            cap = cloud_info.get('max_messages', 200)
            if type(cap) is not int or not 1 <= cap <= 5000:
                cap = 200
            policy = dict(policy, max_messages=min(policy['max_messages'], cap), server_message_limit=cap)
        prepared = self.prepare(job, policy)
        if cloud_info:
            result = self.controller.bridge.link.rpc('qa', {'messages': prepared['messages'], 'system': job['system'],
                'persona_snapshot': True, 'expected_model': binding,
                'context_options': {k: policy[k] for k in ('context_tokens', 'output_tokens', 'safety_percent', 'image_edge', 'jpeg_quality')}},
                expected_scope=DeviceLink.scope_for(job['cloud']))
            telemetry = {**prepared['stats'], **result.get('telemetry', {})}
            # 客户端已裁过历史，云端只知道传入的轮数；保留完整本地裁剪统计。
            telemetry['dropped_rounds'] = prepared['stats']['dropped_rounds'] + result.get('telemetry', {}).get('dropped_rounds', 0)
            telemetry['budget_start'] = telemetry['dropped_rounds']
            text = result['text']
        else:
            client = QAClient({**profile, '_qa_limits': policy}, job['system'])
            text = client.chat(prepared['messages'], on_chunk)
            telemetry = {**client.telemetry, **prepared['stats']}
        return text, {**telemetry, 'resolved_identity': binding, 'model_name': profile.get('model', '')}

    def prepare(self, job, policy):
        database = job.get('image_database')
        if not database:
            return prepare_budget(self.context, job['messages'], job['system'], policy, job['settings'])
        db = sqlite3.connect(Path(database).as_uri() + '?mode=ro', uri=True)
        try:
            db.execute('BEGIN')
            def read(identity):
                row = db.execute('SELECT data FROM images WHERE id=?', (identity,)).fetchone()
                if row is None:
                    raise ValueError('截图或对话已删除，本次未调用模型')
                return bytes(row[0])
            return prepare_budget(self.context, job['messages'], job['system'], policy, job['settings'], image_reader=read)
        finally:
            # 只在预处理时持有快照；任何元数据/生成网络请求期间均不持有数据库读锁。
            db.close()


def describe_usage(data):
    if not data:
        return ''
    cached = data.get('cached_tokens')
    cache = '未返回缓存用量' if cached is None else f'缓存命中{cached} Token'
    parts = [f"上下文{data.get('context_tokens', '?')}，输入估算{data.get('input_tokens_estimate', '?')}，输出预留{data.get('output_tokens', '?')}",
             f"本轮保留{data.get('kept_rounds', '?')}轮、移出{data.get('dropped_rounds', 0)}轮（本地历史保留）",
             f"上传{data.get('images', 0)}图，{data.get('wire_bytes', 0)/1048576:.2f}MiB；{cache}"]
    if data.get('context_mode') in MODES:
        parts.insert(0, MODES[data['context_mode']] + f"；成本输入估算{data.get('cost_input_estimate', '?')} Token")
        if data.get('budget_compacted'):
            parts.append('本次已批量移出旧轮次，完整本地历史可切换保留模式重新纳入')
        if data.get('budget_protected'):
            parts.append('最近4轮或当前问题优先保留，允许超过日常软目标')
        if data.get('server_message_limit') == 200:
            parts.append('旧云端另限200条消息；升级插件后可按模型窗口保留更长历史')
    if data.get('truncated'):
        parts.append('回答达到输出上限，可追问“继续”')
    if '未知模型' in data.get('source', ''):
        parts.append('模型窗口未知，请在上下文配置核对')
    return '；'.join(parts)
