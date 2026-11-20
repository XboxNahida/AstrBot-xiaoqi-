"""将内部用量和账本转换为面向用户的中文结果。"""
import datetime as dt


def _tokens(values):
    return (f"输入 {values.get('input_tokens', 0):,}，输出 {values.get('output_tokens', 0):,}"
            f"\n  其中缓存命中 {values.get('cached_input_tokens', 0):,}，"
            f"思考 {values.get('reasoning_output_tokens', 0):,}")


def usage_text(report):
    if not report['requests']:
        return '模型用量（Token）\n暂无已记录的用量。模型未返回的用量不会估算，也不代表服务商账单为零。'
    lines = ['模型用量（Token）', f"已记录用量：{report['requests']:,} 次请求",
             '累计：' + _tokens(report['totals']), '今日：' + _tokens(report['today']),
             '最近一次已记录请求：' + _tokens(report['current']), '', '按来源累计：']
    names = {'pet': '桌宠', 'agent': '开发工具', 'codex': 'Codex', 'dsh': '开发工具', 'claude': 'Claude'}
    for source, values in report['by_source'].items():
        lines.append(names.get(source, '其他来源') + '：' + _tokens(values))
    lines += ['', '估算费用：']
    for source, bucket in report.get('estimated_costs', {}).items():
        parts = [f'{amount:.6f} {currency}' for currency, amount in bucket['by_currency'].items()]
        unknown = bucket.get('unknown_requests', 0)
        detail = ('已知部分 ' if unknown else '') + '，'.join(parts) if parts else '暂无可估算记录'
        if unknown:
            detail += f'；另有 {unknown} 次费用未知（价格、模型或用量不完整）'
        lines.append(names.get(source, '其他来源') + '：' + detail)
    lines += ['', '缓存命中属于输入，思考属于输出，不重复相加。',
              '仅统计本机已记录数据；未知用量不估算，历史旧汇总未补入，不等于服务商账单。']
    return '\n'.join(lines)


def ledger_text(rows):
    if not rows:
        return '余额账本\n暂无余额记录。查询到有效金额或手动校准后会记录。'
    kinds = {'baseline': '首次记录', 'unchanged': '余额未变化', 'recharge': '充值增加',
             'balance_increase': '余额增加（来源未知）', 'unknown_decrease': '余额减少（原因未知）',
             'grant_decrease': '赠额减少', 'grant_expiry': '赠额到期', 'manual': '手动校准'}
    names = {'manual': '手动记账', 'auto': '自动识别', 'deepseek': 'DeepSeek', 'openrouter': 'OpenRouter'}
    lines = ['余额账本（最近10条）', '余额变化不等于桌宠消费。']
    for row in rows[:10]:
        try:
            stamp = dt.datetime.fromisoformat(row['at']).astimezone(dt.timezone(dt.timedelta(hours=8)))
            stamp = stamp.strftime('%m-%d %H:%M')
        except (ValueError, TypeError):
            stamp = '时间未知'
        identity = row['provider'].split('/')
        provider = identity[0]
        label = names.get(provider, provider)
        if len(identity) == 4:
            scope = {'account': '账户余额', 'token': '令牌额度', 'reported': '服务商余额／额度'}.get(identity[3], '范围未确认')
            label += f' · 配置 {identity[1]} · 地址编号 {identity[2]} · {scope}'
        kind = kinds.get(row['kind'], '余额记录')
        lines.append(f"{stamp} · {label} · {kind}\n"
                     f"  余额 {row['balance']:g} {row['currency']}，变化 {row['amount']:+g}")
    return '\n'.join(lines)
