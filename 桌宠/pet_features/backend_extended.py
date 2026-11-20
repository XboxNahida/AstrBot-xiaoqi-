"""日志、用量和余额的后台装配；数据来源分开。"""
import json
import sqlite3
from urllib.parse import urlsplit

from .accounting import AccountBook, BalanceClient
from .agent_logs import AgentLogReader
from .backend import result
from .report_text import ledger_text, usage_text
from .usage import UsageTracker


class ExtendedBackend:
    def _ensure_usage(self):
        if self.usage is None:
            self.usage = UsageTracker(self.root / "features_usage.sqlite")
        return self.usage

    def _ensure_account(self):
        if self.account is None:
            self.account = AccountBook(self.root / "features_state.sqlite")
        return self.account

    def _poll_logs(self, cfg, now):
        events, statuses = [], {}
        active_dirs = {}
        for name in ("agent", "usage"):
            section = cfg[name]
            if section["enabled"] and section["directory"]:
                active_dirs.setdefault(section["directory"], []).append(name)
        readers = getattr(self, "readers", {})
        for path in list(readers):
            if path not in active_dirs:
                readers.pop(path).close()
        for directory, names in active_dirs.items():
            if getattr(self, "cancelled", lambda: False)():
                return result(events=events, status=statuses)
            if directory not in readers:
                import hashlib
                identity = hashlib.sha256(directory.encode("utf-8")).hexdigest()[:16]
                readers[directory] = AgentLogReader(directory, self.root / ("features_log_" + identity + ".sqlite"))
            reader = readers[directory]
            new = reader.poll()
            if getattr(self, "cancelled", lambda: False)():
                break
            if "agent" in names:
                for row in new:
                    if not cfg["agent"]["animation_enabled"]:
                        row["action"] = ""
                    if not cfg["agent"]["text_enabled"]:
                        row["text"] = ""
                    events.append(row)
            usage = reader.poll_usage()
            if "usage" in names:
                self._ensure_usage().ingest(usage)
            statuses["logs"] = reader.status
        self.readers = readers
        if cfg["usage"]["enabled"] and cfg["usage"]["include_pet"] and not getattr(self, "cancelled", lambda: False)():
            self._import_pet_usage()
        return result(events=events, status=statuses)

    def _import_pet_usage(self):
        path = self.root / "features_model_usage.sqlite"
        if not path.is_file():
            return
        try:
            tracker = self._ensure_usage()
            tracker.db.execute("CREATE TABLE IF NOT EXISTS pet_completions "
                               "(request_id TEXT PRIMARY KEY, created REAL, data TEXT)")
            tracker.db.execute("CREATE TABLE IF NOT EXISTS pet_cursor (id INTEGER PRIMARY KEY, seq INTEGER)")
            row = tracker.db.execute("SELECT seq FROM pet_cursor WHERE id=1").fetchone()
            cursor = row[0] if row else 0
            with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
                rows = db.execute("SELECT seq,request_id,created,data FROM completions WHERE seq>? ORDER BY seq",
                                  (cursor,)).fetchall()
            for seq, identity, created, data in rows:
                request = json.loads(data)
                if request.get("usage_known") and all(type(request.get(k)) is int for k in ("input_tokens", "output_tokens")):
                    tracker.record({"input_tokens": request["input_tokens"], "output_tokens": request["output_tokens"],
                                        "cached_input_tokens": request.get("cached_input_tokens", 0),
                                        "reasoning_output_tokens": request.get("reasoning_tokens", 0), "created": created},
                                   identity, source="pet")
                tracker.db.execute("INSERT OR IGNORE INTO pet_completions VALUES (?,?,?)", (identity, created, data))
                tracker.db.execute("INSERT OR REPLACE INTO pet_cursor VALUES (1,?)", (seq,))
                tracker.db.commit()
        except (ValueError, OSError, sqlite3.Error):
            return

    def _usage_costs(self, cfg):
        tracker = self._ensure_usage()
        costs = {"agent": {"by_currency": {}, "unknown_requests": 0},
                 "pet": {"by_currency": {}, "unknown_requests": 0}}
        # AgentLogReader当前未保留可核验的模型/服务商；不套用桌宠价格。
        costs["agent"]["unknown_requests"] = tracker.db.execute(
            "SELECT COUNT(*) FROM usage WHERE source!='pet'").fetchone()[0]
        exists = tracker.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='pet_completions'").fetchone()
        if exists:
            for created, data in tracker.db.execute("SELECT created,data FROM pet_completions"):
                request = json.loads(data)
                estimate = {"unknown": True}
                if (request.get("usage_known") and request.get("model") and request.get("provider")
                        and all(type(request.get(k)) is int for k in ("input_tokens", "output_tokens"))):
                    estimate = self._ensure_account().estimate(request, cfg["balance"]["prices"],
                                                              request["model"], request["provider"], now=created)
                bucket = costs["pet"]
                if estimate["unknown"]:
                    bucket["unknown_requests"] += 1
                else:
                    currency = estimate["currency"]
                    bucket["by_currency"][currency] = bucket["by_currency"].get(currency, 0) + estimate["cost"]
        for bucket in costs.values():
            bucket["cost_unknown"] = bool(bucket["unknown_requests"])
        return costs

    def _poll_balance(self, cfg, now):
        section = cfg["balance"]
        if section["enabled"] and section["auto_refresh"] and self._due("balance", max(60, section["refresh_minutes"] * 60), now):
            data = self._fetch_balance(cfg)
            return result(status={"balance": data})
        return {}

    def _fetch_balance(self, cfg):
        if getattr(self, "cancelled", lambda: False)():
            return {"available": False, "error": "操作已取消"}
        section = cfg["balance"]
        if section["provider"] == "manual":
            return {"available": True, "total": section["manual_balance"], "currency": section["currency"], "paid": None, "granted": None, "expires_at": None, "error": "", "scope": "manual"}
        from companion_config import SettingsStore
        profiles = SettingsStore(self.root / "companion_settings.json").load()
        identity = section["profile_id"] or profiles["active_profile"]
        profile = next((p for p in profiles["profiles"] if p["id"] == identity), None)
        if section['provider'] == 'auto' and not section['profile_id']:
            if profiles['route'] == 'cloud':
                return {'available': False, 'error': '当前使用云端模型，本机没有其余额凭据；可在“复用模型配置”指定本机配置查询。'}
            if profiles['route'] == 'original':
                from config_manager import ConfigManager
                original = ConfigManager()
                if original.get('api_provider') != 'openai_compatible':
                    return {'available': False, 'error': '当前原版模型通道没有已支持的余额接口，请指定本机模型配置查询。'}
                profile = {'id': 'original', 'name': '沿用原版模型',
                           'base_url': original.get('custom_api_url', ''), 'key': original.get('api_key', '')}
        if profile is None:
            raise ValueError("请选择有效的余额模型配置")
        client = BalanceClient(section["provider"], {**profile, "api_key": profile.get("key", ""), "currency": section["currency"]})
        data = client.fetch()
        data.update(profile_name=profile.get('name', ''), host=urlsplit(profile.get('base_url', '')).hostname or '')
        if data['available'] and data.get('total') is not None and data.get('currency') in ('CNY', 'USD'):
            ledger_provider = section['provider']
            if ledger_provider == 'auto':
                import hashlib
                scope = hashlib.sha256(profile['base_url'].encode('utf-8')).hexdigest()[:12]
                ledger_provider = f"{data['host']}/{profile['id']}/{scope}/{data.get('scope', 'unknown')}"
            self._ensure_account().snapshot(ledger_provider, data)
        return data

    @staticmethod
    def _balance_text(data):
        name = data.get('profile_name', '')
        host = data.get('host', '')
        heading = '余额查询' + (f' · {name}（{host}）' if host else '')
        if not data.get('available'):
            return heading + '\n查询失败：' + data.get('error', '服务商未返回可用数据')
        if data.get('unlimited'):
            return heading + '\n当前API令牌：无限额度。\n账户余额：接口未返回；无限令牌不代表账户有无限余额。'
        label = {'token': '令牌剩余额度', 'manual': '手动余额',
                 'reported': '服务商返回余额／剩余额度'}.get(data.get('scope'), '账户余额')
        if data.get('currency') == 'QUOTA':
            return heading + f"\n{label}：{data['total']:g} 原始额度（未取得有效换算信息，不作为金额记账）。"
        text = heading + f"\n{label}：{data['total']:g} {data.get('currency') or '未知单位'}"
        if data.get('scope') == 'token':
            text += '\n这是当前密钥的额度，不是账户总余额。'
        return text

    def extended_action(self, action, cfg, argument):
        if action == "usage:report":
            if cfg["usage"]["include_pet"]:
                self._import_pet_usage()
            report = self._ensure_usage().report()
            report["estimated_costs"] = self._usage_costs(cfg)
            report["legacy_note"] = "旧smart_state累计未导入为单次请求；仅统计完成账本启用后的记录"
            return result(usage_text(report), status={'usage': report})
        if action == "agent:status":
            return result("日志状态：\n" + str(self.snapshot.get("logs", "尚未启用或尚未采样")))
        if action == "balance:refresh":
            data = self._fetch_balance(cfg)
            return result(self._balance_text(data), status={'balance': data})
        if action == "balance:ledger":
            return result(ledger_text(self._ensure_account().ledger()))
        if action == "balance:adjust":
            data = self._ensure_account().adjust(cfg["balance"]["provider"], cfg["balance"]["manual_balance"], cfg["balance"]["currency"])
            return result(f"手动余额已校准为 {data['balance']:g} {data['currency']}。\n仅修改本地账本，不会修改服务商余额。")
        raise ValueError("未知扩展操作：" + action)

    def close_extended(self):
        for reader in getattr(self, "readers", {}).values():
            reader.close()
