"""Supplementary read-only position evidence from the event detail card."""
import re
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from .execution import stamp
from .market_observer import display_identity, validate_context
from .parser import keytext

DOM = Path(__file__).with_name('position_dom.js').read_text()
# Verified on this event's order panel and its Match Winner position row.
POSITION_NAMES = {('475921566123695598', 'moneyline', 'sas'): 'SPURS'}


def position_target(url, raw, signal, leg, labels):
    validate_context(dict(raw, url=url), signal)
    matching = [o for o in raw['outcomes'] if display_identity(o) == leg.identity()]
    if len(matching) != 1:
        raise ValueError('无法唯一定位目标盘口')
    group = matching[0]['group']
    query = parse_qs(urlsplit(url).query)
    keys = {}
    for key in ('market', 'windowId'):
        if key in query:
            if len(query[key]) != 1 or not re.fullmatch(r'[a-zA-Z0-9_-]+', query[key][0]):
                raise ValueError('盘口链接含有歧义')
            keys[key] = query[key][0]
    path = 'https://www.foregate.com/en/event/' + signal.event_id
    names = [leg.side if leg.market_type == 'both_teams_to_score' else leg.selection]
    alias = POSITION_NAMES.get((signal.event_id, leg.market_type, keytext(leg.selection)))
    if alias:
        names.append(alias)
    line = leg.line
    if line and leg.market_type in {'spreads', 'map_handicap'} and Decimal(line) > 0:
        line = '+' + line
    suffix = ' - NO' if leg.side == 'NO' and leg.market_type != 'both_teams_to_score' else ''
    outcomes = [keytext(' '.join(filter(None, [label, name, line])) + suffix)
                for label in labels or [group] for name in names]
    return dict(url=path + ('?' + urlencode(keys) if keys else ''), group=group, outcomes=outcomes)


async def read_position(page, signal, target):
    validate_context(dict(url=page.url, title=await page.title()), signal)
    expected = parse_qs(urlsplit(target['url']).query)
    def check_window():
        actual = parse_qs(urlsplit(page.url).query)
        if any(actual.get(k) != v for k, v in expected.items()):
            raise ValueError('盘口链接与提交前不同')
    check_window()
    await page.get_by_role('button', name=re.compile(r'^Available')).wait_for(state='visible', timeout=6000)
    args = dict(group=target['group'])
    for mode in ('tab', 'expand'):
        if mode == 'tab':
            handle = await page.wait_for_function(DOM, arg=dict(args, mode=mode), timeout=4000)
        else:
            handle = await page.evaluate_handle(DOM, dict(args, mode=mode))
        try:
            element = handle.as_element()
            if element:
                await element.click(timeout=3000)
            elif mode == 'tab':
                raise ValueError('未找到唯一的 My Position 入口')
        finally:
            await handle.dispose()
    handle = await page.wait_for_function(DOM, arg=dict(args, mode='read'), timeout=6000)
    try:
        snapshot = await handle.json_value()
    finally:
        await handle.dispose()
    validate_context(dict(url=page.url, title=await page.title()), signal)
    check_window()
    rows = [row for row in snapshot['rows'] if keytext(row['outcome']) in target['outcomes']]
    if snapshot.get('empty'):
        return dict(shares='0', outcome='', at=stamp())
    if len(rows) != 1:
        raise ValueError('未找到唯一匹配的持仓行')
    return dict(**rows[0], at=stamp())


def comparison(before, after):
    if 'shares' not in after:
        return '持仓待核对 · ' + after['error']
    current = after['shares']
    if not before or 'shares' not in before:
        return f'当前持仓 {current} 份，缺少提交前份额'
    delta = Decimal(current) - Decimal(before['shares'])
    return f"提交前 {before['shares']} 份 → 核对时 {current} 份（变化 {delta:+f} 份）"


async def observe(page, signal, target):
    try:
        return await read_position(page, signal, target)
    except Exception as exc:
        return dict(error=str(exc) if isinstance(exc, ValueError) else '持仓区未加载或布局无法识别', at=stamp())
