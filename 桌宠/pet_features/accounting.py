"""显式服务商余额与独立账本；余额变动不代表桌宠消费。"""
import math
import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests


def _number(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError('数值无效')
    return number


class BalanceClient:
    def __init__(self, provider, profile, session=None):
        self.provider, self.profile, self.session = provider, profile, session

    def fetch(self):
        if self.provider == 'auto':
            from .balance_auto import AutoBalanceClient
            return AutoBalanceClient(self.profile, self.session).fetch()
        result = {'available': False, 'currency': None, 'total': None, 'paid': None,
                      'granted': None, 'expires_at': None, 'error': ''}
        try:
            official = {'deepseek': ('api.deepseek.com', '/user/balance'),
                        'openrouter': ('openrouter.ai', '/api/v1/credits')}
            if self.provider not in official:
                raise ValueError('未支持')
            host, path = official[self.provider]
            base = self.profile.get('base_url') or f'https://{host}'
            parts = urlsplit(base)
            if (parts.scheme != 'https' or not parts.hostname or parts.username or
                    parts.password or parts.query or parts.fragment or parts.port not in (None, 443)):
                raise ValueError('地址无效')
            if parts.hostname == host:
                allowed = ('', '/', '/v1', '/v1/', '/user/balance') if self.provider == 'deepseek' else ('', '/', '/api/v1', '/api/v1/', '/api/v1/credits')
                if parts.path not in allowed:
                    raise ValueError('路径无效')
                url = f'https://{host}{path}'
            elif parts.path.endswith(path) and '%' not in parts.path and '..' not in parts.path:
                url = base
            else:
                raise ValueError('自定义地址须明确余额路径')
            key = self.profile.get('api_key')
            if not key or not isinstance(key, str):
                raise ValueError('缺少凭据')
        except (ValueError, TypeError, AttributeError):
            result['error'] = '服务商、余额地址或密钥配置无效'
            return result
        session = self.session or requests.Session()
        owned = self.session is None
        if owned:
            session.trust_env = False
        try:
            response = session.get(url, headers={'Authorization': f'Bearer {key}'},
                                   timeout=15, allow_redirects=False)
            if response.status_code != 200:
                result['error'] = f'HTTP {int(response.status_code)}'
                return result
            data = response.json()
            if self.provider == 'deepseek':
                rows = data['balance_infos']
                wanted = self.profile.get('currency', 'CNY')
                row = next((row for row in rows if row.get('currency') == wanted), None)
                if row is None:
                    raise ValueError('缺少币种')
                result.update(total=_number(row['total_balance']), currency=row['currency'],
                              paid=_number(row['topped_up_balance']) if row.get('topped_up_balance') is not None else None,
                              granted=_number(row['granted_balance']) if row.get('granted_balance') is not None else None,
                              available=bool(data.get('is_available', True)))
            else:
                row = data['data']
                result.update(total=_number(row['total_credits']) - _number(row['total_usage']),
                              currency='USD', available=True)
        except (requests.RequestException, ValueError, TypeError, KeyError, IndexError, AttributeError, OverflowError):
            result.update(total=None, paid=None, granted=None, available=False,
                          error='余额请求失败或响应格式无效')
        finally:
            if owned:
                session.close()
        return result


def _time(now=None):
    if now is None:
        return datetime.now(UTC)
    if isinstance(now, (float, int)):
        return datetime.fromtimestamp(now, UTC)
    if isinstance(now, str):
        now = datetime.fromisoformat(now)
    if now.tzinfo is None:
        raise ValueError('时间必须包含时区')
    return now


def _local_time(instant, name):
    if name == 'UTC':
        return instant.astimezone(UTC)
    try:
        return instant.astimezone(ZoneInfo(name))
    except ZoneInfoNotFoundError:
        # Windows独立Python可能无tzdata；复用现有Qt的系统时区数据库。
        from PyQt6.QtCore import QDateTime, QTimeZone
        zone = QTimeZone(name.encode('utf-8'))
        if not zone.isValid():
            raise ValueError('未知时区') from None
        point = QDateTime.fromSecsSinceEpoch(int(instant.timestamp()))
        return instant.astimezone(timezone(timedelta(seconds=zone.offsetFromUtc(point))))


class AccountBook:
    """每条记录带服务商和币种；只存数字，不持久化HTTP原文和密钥。"""
    def __init__(self, db_path):
        self.db_path = str(db_path)
        with sqlite3.connect(self.db_path) as db:
            db.execute('''CREATE TABLE IF NOT EXISTS balance_ledger (
                id INTEGER PRIMARY KEY, provider TEXT, currency TEXT, at TEXT,
                kind TEXT, amount REAL, balance REAL, paid REAL, granted REAL,
                expires_at TEXT, reason TEXT)''')

    def _last(self, db, provider, currency):
        db.row_factory = sqlite3.Row
        return db.execute('SELECT * FROM balance_ledger WHERE provider=? AND currency=? ORDER BY id DESC LIMIT 1',
                          (provider, currency)).fetchone()

    def snapshot(self, provider, data, now=None):
        if data.get('error') or data.get('total') is None:
            return None
        total, currency = _number(data['total']), str(data['currency'])
        paid = _number(data['paid']) if data.get('paid') is not None else None
        granted = _number(data['granted']) if data.get('granted') is not None else None
        at = _time(now)
        expires = _time(data['expires_at']).isoformat() if data.get('expires_at') else None
        with sqlite3.connect(self.db_path) as db:
            prev = self._last(db, provider, currency)
            delta = total - prev['balance'] if prev else 0
            kind = 'baseline' if not prev else 'unchanged'
            if prev and delta > 0:
                kind = 'recharge' if paid is not None and prev['paid'] is not None and paid > prev['paid'] else 'balance_increase'
            if prev and delta < 0:
                kind = 'unknown_decrease'
                if granted is not None and prev['granted'] is not None and granted < prev['granted']:
                    kind = 'grant_decrease'
                    if prev['expires_at'] and _time(prev['expires_at']) <= at:
                        kind = 'grant_expiry'
            values = (provider, currency, at.isoformat(), kind, delta, total, paid, granted, expires, '')
            db.execute('INSERT INTO balance_ledger (provider,currency,at,kind,amount,balance,paid,granted,expires_at,reason) VALUES (?,?,?,?,?,?,?,?,?,?)', values)
        return {'provider': provider, 'currency': currency, 'kind': kind, 'amount': delta, 'balance': total}

    def ledger(self, limit=100):
        with sqlite3.connect(self.db_path) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute('SELECT * FROM balance_ledger ORDER BY id DESC LIMIT ?',
                                                   (max(0, min(int(limit), 10000)),))]

    def adjust(self, provider, amount, currency, reason='manual'):
        """手动校准为绝对余额，不解释成充值或消费。"""
        total, at = _number(amount), _time().isoformat()
        with sqlite3.connect(self.db_path) as db:
            prev = self._last(db, provider, currency)
            delta = total - prev['balance'] if prev else 0
            db.execute('INSERT INTO balance_ledger (provider,currency,at,kind,amount,balance,reason) VALUES (?,?,?,?,?,?,?)',
                       (provider, currency, at, 'manual', delta, total, str(reason)[:200]))
        return {'provider': provider, 'currency': currency, 'kind': 'manual', 'amount': delta, 'balance': total}

    def estimate(self, usage, prices, model, provider='', now=None):
        unknown = {'cost': None, 'currency': None, 'unknown': True}
        try:
            inp = _number(usage.get('input_tokens', usage.get('prompt_tokens', 0)))
            out = _number(usage.get('output_tokens', usage.get('completion_tokens', 0)))
            cached = _number(usage.get('cached_input_tokens', usage.get('cache_read_input_tokens', 0)))
            if min(inp, out, cached) < 0 or cached > inp:
                return unknown
            matches = []
            for price in prices:
                if not price.get('enabled', True):
                    continue
                if price.get('model') != model or price.get('provider', '') != provider:
                    continue
                instant = _local_time(_time(now), price.get('timezone', 'UTC'))
                start, end = price.get('start', '00:00'), price.get('end', '00:00')
                start = datetime.strptime(start, '%H:%M').time()
                end = datetime.strptime(end, '%H:%M').time()
                clock = instant.time().replace(tzinfo=None)
                active = start == end or (start <= clock < end if start < end else clock >= start or clock < end)
                if active:
                    matches.append(price)
            if len(matches) != 1:
                return unknown
            price = matches[0]
            rates = [_number(price[name]) for name in ('input', 'cached_input', 'output')]
            multiplier = _number(price.get('multiplier', 1))
            if min(*rates, multiplier) < 0 or not price.get('currency'):
                return unknown
            cost = ((inp - cached) * rates[0] + cached * rates[1] + out * rates[2]) * multiplier / 1_000_000
            if not math.isfinite(cost):
                return unknown
            return {'cost': round(cost, 12), 'currency': price['currency'], 'unknown': False}
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
            return unknown
