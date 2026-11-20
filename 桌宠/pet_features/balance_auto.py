"""按模型请求地址识别只读余额接口；不执行外部查询脚本。"""
import math
import time
from urllib.parse import urlsplit, urlunsplit

import requests


class BalanceError(ValueError):
    """仅由本模块固定文案产生，可安全展示。"""


def number(value):
    if isinstance(value, bool):
        raise TypeError('金额无效')
    result = float(value)
    if not math.isfinite(result):
        raise ValueError('金额无效')
    return result


class AutoBalanceClient:
    def __init__(self, profile, session=None):
        self.profile, self.session = profile, session

    def fetch(self):
        result = {'available': False, 'currency': None, 'total': None, 'paid': None, 'granted': None,
                  'expires_at': None, 'error': '', 'scope': 'unknown', 'source': '', 'unlimited': False}
        session, owned = self.session, self.session is None
        try:
            parts = urlsplit(self.profile.get('base_url', ''))
            if (parts.scheme != 'https' or not parts.hostname or parts.username or parts.password
                    or parts.query or parts.fragment or '%' in parts.path or '..' in parts.path):
                raise BalanceError('请输入有效的HTTPS模型请求地址')
            key = self.profile.get('api_key')
            if not isinstance(key, str) or not key.strip() or '\n' in key or '\r' in key:
                raise BalanceError('当前模型配置没有有效API密钥')
            prefix = parts.path.rstrip('/')
            for suffix in ('/chat/completions', '/responses'):
                prefix = prefix.removesuffix(suffix)
            official = {'api.deepseek.com': 'deepseek', 'openrouter.ai': 'openrouter'}
            if parts.hostname in official:
                from .accounting import BalanceClient
                profile = dict(self.profile, base_url=urlunsplit((parts.scheme, parts.netloc, prefix, '', '')))
                data = BalanceClient(official[parts.hostname], profile, session).fetch()
                return dict(result, **data, scope='account', source=parts.hostname)
            prefix = prefix.removesuffix('/v1')
            base = urlunsplit((parts.scheme, parts.netloc, prefix, '', ''))
            result['source'] = parts.hostname
            if owned:
                session = requests.Session()
                session.trust_env = False
            deadline = time.monotonic() + 15

            def get(path, auth=True):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise BalanceError('余额查询超时，请稍后重试')
                response = session.get(base + path,
                                       headers={'Authorization': 'Bearer ' + key} if auth else {},
                                       timeout=min(6, remaining), allow_redirects=False)
                if response.status_code != 200:
                    return response.status_code, {}
                try:
                    body = response.json()
                except ValueError:
                    return 200, {}
                return 200, body if isinstance(body, dict) else {}

            status, body = get('/v1/usage')
            if body.get('error') or body.get('success') is False or body.get('code') is False:
                raise BalanceError('服务商返回查询失败，请检查当前密钥权限；未记录为余额')
            if status == 200 and any(k in body for k in ('remaining', 'balance', 'quota')):
                if body.get('isValid') is False or body.get('is_active') is False:
                    raise BalanceError('服务商提示当前密钥已停用或失效')
                quota = body.get('quota') if isinstance(body.get('quota'), dict) else {}
                amount = next((v for v in (body.get('remaining'), quota.get('remaining'), body.get('balance'))
                               if v is not None), None)
                unit = str(body.get('unit') or quota.get('unit') or 'UNKNOWN').upper()
                if len(unit) > 16 or not unit.isascii() or not unit.replace('_', '').isalnum():
                    raise BalanceError('服务商返回的余额单位无效')
                return dict(result, available=True, total=number(amount), currency=unit, scope='reported')
            if status not in (200, 404, 405):
                raise BalanceError(f'余额接口HTTP {status}；请检查当前密钥权限或稍后重试')
            status, body = get('/api/usage/token/')
            if status != 200:
                raise BalanceError(f'未识别到可用余额接口（HTTP {status}）；该站可能需要独立查询凭据')
            row = body.get('data', {})
            if (not isinstance(row, dict) or row.get('object') != 'token_usage'
                    or body.get('error') or body.get('code') is False or body.get('success') is False):
                raise BalanceError('该站未提供可识别的余额数据，不能将失败当作0余额')
            account = row.get('user_quota')
            result['scope'] = 'account' if isinstance(account, dict) else 'token'
            if isinstance(account, dict):
                row = account
            elif row.get('unlimited_quota') is True:
                return dict(result, available=True, unlimited=True)
            amount = number(row.get('total_available'))
            try:
                status, info = get('/api/status', auth=False)
            except (requests.RequestException, BalanceError):
                status, info = 0, {}
            valid = status == 200 and not info.get('error') and info.get('success') is not False and info.get('code') is not False
            info = info.get('data', {}) if valid else {}
            divisor, multiplier, currency = 1, 1, 'QUOTA'
            if isinstance(info, dict):
                try:
                    rate = number(info.get('quota_per_unit'))
                    unit = info.get('quota_display_type')
                    if rate > 0 and unit in ('USD', 'CNY'):
                        conversion = number(info.get('usd_exchange_rate')) if unit == 'CNY' else 1
                        if conversion <= 0:
                            raise ValueError('汇率无效')
                        divisor, currency, multiplier = rate, unit, conversion
                except (ValueError, TypeError):
                    pass
            return dict(result, available=True, total=number(amount / divisor * multiplier), currency=currency)
        except BalanceError as exc:
            result['error'] = str(exc)
        except (ValueError, TypeError, KeyError, OverflowError):
            # 配置/协议异常不回显HTTP正文或可能携带凭据的第三方异常。
            result['error'] = '模型地址、密钥或余额响应无效；请检查所选配置和查询权限'
        except requests.RequestException:
            result['error'] = '余额网络请求失败或超时，请稍后重试'
        finally:
            if owned and session is not None:
                session.close()
        return result
