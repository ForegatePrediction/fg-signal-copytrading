"""Account, trading and Telegram execution for the local Forecast workspace."""
import asyncio
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN
import json
import os
import re
import sqlite3
import time
import uuid
from zoneinfo import ZoneInfo

from .execution import filters
from .foregate_api import ApiError, public_book, public_market_catalog, public_market_cycles
from .paper import instant
from .parser import keytext, parse_message, trade_key


SIDES = ('BUY', 'SELL')
TERMINAL = {'FILLED', 'CANCELED', 'CANCELLED', 'REJECTED', 'FAILED', 'EXPIRED'}
WORKING = {'NEW', 'ACCEPTED', 'OPEN', 'MATCHING', 'PARTIAL', 'PARTIAL_FILLED', 'PARTIALLY_FILLED', 'PENDING'}
PAGE_SIZE = 30
CYCLE_CATEGORIES = ('20541', '20542')
CYCLE_OPTIONS = {'0': '涨', '1': '跌'}
DEFAULTS = {'order_type': 'LIMIT', 'amount': '2', 'daily': '10', 'event': '2', 'max_price': '85', 'ttl': 60}
QUOTE_FALLBACK_STATUSES = {400, 404, 500, 502, 503, 504}
BASE58 = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'


def stamp():
    return datetime.now(timezone.utc).isoformat()


def identifier(value, field='编号'):
    if not isinstance(value, str) or not re.fullmatch(r'[1-9][0-9]{0,18}', value) or int(value) > 9223372036854775807:
        raise ValueError(f'{field}须为有效的平台数字编号。')
    return value


def number(value, label, maximum=1000000, zero=False):
    try:
        d = Decimal(str(value))
        if d.is_finite() and Decimal(0) <= d <= maximum and (zero or d > 0) and d.as_tuple().exponent >= -24:
            return d
    except (ValueError, InvalidOperation):
        pass
    raise ValueError(f'{label}无效。')


def input_number(value, label, maximum=1000000):
    if not re.fullmatch(r'(?:[0-9]{1,7})(?:\.[0-9]{1,8})?', str(value)):
        raise ValueError(f'{label}须为数字，最多 8 位小数。')
    return number(value, label, maximum)


def object_data(value):
    if not isinstance(value, dict):
        raise ApiError('ForeGate 的数据格式与接口约定不符。')
    return value


def rows_data(value):
    if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
        raise ApiError('ForeGate 的列表格式与接口约定不符。')
    return value


def page_data(result, page, empty_message=''):
    data = result.get('data')
    pagination = data if isinstance(data, dict) else result
    rows = rows_data(data.get('records') if isinstance(data, dict) else data)
    pages = pagination.get('pages')
    total = pagination.get('total', pagination.get('count'))
    has_next = (page < pages if type(pages) is int else
                page * PAGE_SIZE < total if type(total) is int else len(rows) == PAGE_SIZE)
    return {'rows': rows, 'page': page, 'more': has_next, 'total': total, 'empty_message': empty_message}


def market_categories(tree, depth=1):
    categories = []
    for node in rows_data(tree):
        category_id = identifier(node.get('id'), '分类编号')
        label = node.get('nameDisplay') or node.get('name') or category_id
        children = market_categories(node.get('children') or [], depth + 1) if depth < 3 else []
        categories.append({'id': category_id, 'label': label, 'children': children})
    return categories


def catalog_page(result, category_id, page, *, cycles=False):
    rows = []
    total = result.get('total')
    if type(total) is not int or total < 0 or result.get('pageNum') != page or result.get('pageSize') != PAGE_SIZE:
        raise ApiError('ForeGate 公开市场目录未返回有效的分页信息。')
    for row in rows_data(result.get('list')):
        category_ids = [row.get('categoryL2')] if cycles else row.get('categoryIds')
        if not isinstance(category_ids, list) or category_id not in {str(value) for value in category_ids}:
            raise ApiError('ForeGate 公开市场目录未按所选分类返回。')
        id_field = 'initialMarketId' if cycles else 'marketId'
        entry = {'marketId': identifier(row.get(id_field), '赛事编号'), 'title': row.get('title'),
                 'status': row.get('status')}
        if cycles:
            window = row.get('currentWindow')
            entry.update(cycle=True, windowId=identifier(window.get('windowId'), '窗口编号') if window else None)
        rows.append(entry)
    return {'rows': rows, 'page': page, 'more': page * PAGE_SIZE < total, 'total': total,
            'empty_message': '该分类暂无市场'}


def option_for(detail, outcome_id, option_id):
    for group in detail.get('outcomes', []):
        if group.get('outcomeId') != outcome_id:
            continue
        for option in group.get('options', []):
            if option.get('optionId') == option_id:
                return group, option
    raise ValueError('市场详情中找不到该选项。')


# Verified TG code, fixture team and OpenAPI label. Never strip digits from
# arbitrary option IDs or infer a team from the order of the options.
TEAM_LABELS = {
    ('lin', 'lask linz'): 'lin2', ('bah', 'bahrain'): 'bah1',
    ('sin', 'sinners'): 'sin2', ('inox', 'ex-inox division'): 'inox1',
    ('aur', 'aurora'): 'aur1', ('tl', 'liquid'): 'tl1',
    ('ts', 'team spirit'): 'ts8', ('ch', 'cyberhero'): 'ch1',
    ('ore', 'oregon ducks'): 'oregon ducks', ('den', 'nuggets'): 'nuggets',
    ('lac', 'clippers'): 'clippers', ('ott', 'senators'): 'senators',
    ('barcelon', 'barcelona'): 'barcelona',
    ('uscb', 'uc santa barbara gauchos'): 'uc santa barbara gauchos',
}


def soccer_team_codes(groups, fixtures):
    teams = {}
    for group in groups:
        title = keytext(group.get('title') or '')
        rules = keytext(group.get('description') or '')
        if (title in fixtures and f'if {title} wins, this market will resolve to "yes".' in rules
                and 'within the first 90 minutes of regular play plus stoppage time' in rules
                and {keytext(o.get('title') or '') for o in group.get('options', [])} == {'yes', 'no'}):
            teams.setdefault(title, set()).add(keytext(group.get('name') or ''))
    return teams


def soccer_spread(group, name, title, fixtures, teams, line):
    # Some soccer spreads use just the unsigned line as the market name.
    header = re.fullmatch(r'(.+) \(-([0-9]+(?:\.[0-9]+)?)\)', title)
    if not header or header[1] not in fixtures or len(fixtures) != 2 or name != header[2]:
        return False
    magnitude = Decimal(header[2])
    if magnitude != abs(line) or magnitude == 0:
        return False
    favorite = header[1]
    opponent, = fixtures - {favorite}
    favorite_codes = teams.get(favorite, set())
    opponent_codes = teams.get(opponent, set())
    if len(favorite_codes) != 1 or len(opponent_codes) != 1 or favorite_codes == opponent_codes:
        return False
    expected = {(next(iter(favorite_codes)), -magnitude), (next(iter(opponent_codes)), magnitude)}
    options = group.get('options', [])
    actual = set()
    for item in options:
        match = re.fullmatch(r'(.+) ([+-]?[0-9]+(?:\.[0-9]+)?)', keytext(item.get('title') or ''))
        if match:
            actual.add((match[1], Decimal(match[2])))
    rules = keytext(group.get('description') or '')
    return (len(options) == 2 and actual == expected and f'if {favorite} win the game by ' in rules
            and f'otherwise, this market will resolve to "{opponent}".' in rules)


def match_signal(detail, signal, leg):
    if detail.get('marketId') != signal.event_id:
        raise ValueError('信号事件编号和 API 市场编号不同。')
    selection = keytext(leg.selection)
    fixtures = {keytext(signal.home), keytext(signal.away)} - {''}
    labels = {selection}
    event_title = keytext(detail.get('title') or '')
    for team in fixtures:
        if (selection, team) in TEAM_LABELS and re.search(r'(?<!\w)' + re.escape(team) + r'(?!\w)', event_title):
            labels.add(TEAM_LABELS[selection, team])
    groups = detail.get('outcomes', [])
    teams = soccer_team_codes(groups, fixtures) if leg.market_type == 'spreads' else {}
    candidates = []
    for group in groups:
        name = keytext(group.get('name') or '')
        title = keytext(group.get('title') or '')
        options = group.get('options', [])
        if leg.market_type in {'moneyline', 'game_winner'}:
            if leg.market_type == 'moneyline':
                eligible = name == 'moneyline' or title == 'match winner'
                option_names = {keytext(item.get('title') or '') for item in options}
                fixture_title = ' vs. '.join(keytext(t) for t in (signal.home, signal.away))
                eligible |= (title == fixture_title and name == fixture_title and len(options) == 2
                             and option_names == fixtures and len(fixtures) == 2)
                # ForeGate can reuse the same team name for several soccer markets.
                rules = keytext(group.get('description') or '')
                team_binary = (name in labels and title in fixtures and
                               f'if {title} wins, this market will resolve to "yes".' in rules and
                               'within the first 90 minutes of regular play plus stoppage time' in rules and
                               {keytext(item.get('title') or '') for item in options} == {'yes', 'no'})
            else:
                eligible = any(s in {name, title} for s in (f'game {leg.game_no} winner', f'map {leg.game_no} winner'))
                team_binary = False
            if not eligible and not team_binary:
                continue
            binary_match = re.fullmatch(r'(.+?) (?:will win|win|to win)', title)
            for item in options:
                label = keytext(item.get('title') or '')
                direct = leg.side == 'YES' and label in labels
                qualified = label in {f'{selection} win {leg.side.lower()}', f'{selection} {leg.side.lower()}'}
                binary = (team_binary or (binary_match and binary_match[1] == selection)) and label == leg.side.lower()
                if direct or qualified or binary:
                    candidates.append((group, item))
        elif leg.market_type == 'both_teams_to_score' and ('both teams to score' in {name, title} or name == 'btts'):
            candidates.extend((group, item) for item in options if keytext(item.get('title') or '') == leg.side.lower())
        elif leg.market_type in {'totals', 'total_maps', 'spreads', 'map_handicap'} and leg.side == 'YES' and leg.line:
            group_patterns = {
                'totals': r'^(?:total goals|total points|totals)(?: [\d.]+)?$|^o/u [\d.]+(?: goals| points)?$',
                'total_maps': r'^(?:total maps|o/u [\d.]+ (?:maps|games))\b',
                'spreads': r'^(?:spreads?|point spread)\b',
                'map_handicap': r'^map handicap\b',
            }
            named_group = any(re.search(group_patterns[leg.market_type], label) for label in (name, title))
            if not named_group and not (leg.market_type == 'spreads' and
                    soccer_spread(group, name, title, fixtures, teams, Decimal(leg.line))):
                continue
            for item in options:
                label = keytext(item.get('title') or '')
                match = re.fullmatch(r'(.+?) ([+-]?\d+(?:\.\d+)?)', label)
                if not match or Decimal(match[2]) != Decimal(leg.line):
                    continue
                identities = set(labels)
                if leg.market_type in {'totals', 'total_maps'} and selection:
                    identities.add(selection[0])
                if match[1] in identities:
                    candidates.append((group, item))
    if not candidates:
        raise ValueError('未找到匹配的 API 盘口选项。')
    if len(candidates) > 1:
        raise ValueError(f'找到 {len(candidates)} 个匹配选项，无法唯一确定盘口。')
    return candidates[0]


def solana_address(value):
    if not isinstance(value, str) or not re.fullmatch(r'[1-9A-HJ-NP-Za-km-z]{32,44}', value):
        raise ValueError('请输入有效的 Solana 收款地址。')
    n = 0
    for c in value:
        n = n * 58 + BASE58.index(c)
    byte_length = (n.bit_length() + 7) // 8 + len(value) - len(value.lstrip('1'))
    if byte_length != 32:
        raise ValueError('请输入有效的 Solana 收款地址。')
    return value


def connection_settings(service):
    return {'settings': {**{key: service.settings[key] for key in ('api_id', 'phone', 'channel')},
                         'api_hash_set': bool(service.credentials.get('api_hash')),
                         'risk_enabled': service.settings['risk']['enabled']},
            'tg_status': service.tg_status}


class ApiWorkspace:
    def __init__(self, service, api=None, path=None):
        self.service = service
        self.api = api
        self.verified = False
        self.armed_at = None
        self.pause_generation = 0
        self.task = None
        self.pending = {}
        self.mfa = None
        self.totp_started = False
        self.operation_lock = asyncio.Lock()
        self.config = dict(DEFAULTS)
        path = path or service.root / 'foregate-api.db'
        self.db = sqlite3.connect(str(path), isolation_level=None)
        os.chmod(path, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS follow (
                key TEXT PRIMARY KEY, signal_id TEXT NOT NULL, leg INTEGER NOT NULL,
                event TEXT NOT NULL, source TEXT NOT NULL, record TEXT NOT NULL,
                seen TEXT NOT NULL, state TEXT NOT NULL, note TEXT NOT NULL, op_id TEXT,
                UNIQUE(signal_id, leg)
            );
            CREATE TABLE IF NOT EXISTS operations (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, label TEXT NOT NULL, state TEXT NOT NULL,
                request TEXT NOT NULL, result TEXT, created TEXT NOT NULL, meta TEXT NOT NULL
            );
        ''')
        old = self.db.execute('SELECT body FROM settings WHERE id=1').fetchone()
        if old:
            self.config.update(json.loads(old['body']))
        self.db.execute("UPDATE follow SET state='SKIPPED', note='客户端重启，旧队列已结束。' WHERE state IN ('QUEUED','PRECHECK')")
        self.db.execute("UPDATE operations SET state='UNKNOWN' WHERE state='SUBMITTING'")
        self.db.execute("UPDATE follow SET state='UNKNOWN',note='上次提交中断，请人工核对。' WHERE state='SUBMITTING'")

    def client(self):
        if self.api is None:
            raise ApiError('请先连接 ForeGate API 账户。')
        return self.api

    @property
    def account_id(self):
        return self.api.account_id if self.api else None

    async def get(self, path, query=None, body=None, **kwargs):
        return await asyncio.to_thread(self.client().call, path, query, body, **kwargs)

    async def category_markets(self, category_id, page):
        cycles = category_id in CYCLE_CATEGORIES
        fetch = public_market_cycles if cycles else public_market_catalog
        result = await asyncio.to_thread(fetch, category_id, page, PAGE_SIZE)
        listing = catalog_page(result, category_id, page, cycles=cycles)
        if category_id == '3' and page == 1:
            # The public catalog serves recurring crypto series from a separate endpoint.
            series = await asyncio.gather(*(self.category_markets(child, 1) for child in CYCLE_CATEGORIES),
                                          return_exceptions=True)
            listing['rows'] = [row for child in series if isinstance(child, dict) for row in child['rows']] + listing['rows']
            listing['series_error'] = any(isinstance(child, Exception) for child in series)
        return listing

    def status(self, include_follow=True):
        data = {'account_id': self.account_id, 'verified': self.verified,
                'armed': self.armed_at is not None, 'listening': self.service.running,
                'poll_seconds': 10, 'tg_status': self.service.tg_status, 'error': self.service.error,
                'config': self.config, 'channel': self.service.settings['channel'],
                'risk': {key: self.service.settings['risk'][key] for key in
                         ('max_signal_age_sec', 'min_profit_pct', 'max_profit_pct', 'follow_high_risk')}}
        if include_follow:
            data['signals'] = [dict(r) for r in self.db.execute(
                'SELECT signal_id,leg,event,source,seen,state,note,op_id FROM follow ORDER BY rowid DESC LIMIT 100')]
            data['recent_signals'] = [
                {'source': row[0], 'signal': json.loads(row[1]), 'seen': row[2]}
                for row in self.service.db.execute('SELECT source,parsed,seen_at FROM signals ORDER BY rowid DESC LIMIT 35')
            ]
            data['operations'] = self.operations()
        return data

    def operations(self):
        return [self.public_op(row) for row in self.db.execute('SELECT * FROM operations ORDER BY rowid DESC LIMIT 100')]

    def public_op(self, row):
        response = json.loads(row['result'] or '{}')
        meta = json.loads(row['meta'])
        locator = self.cycle_ids(json.loads(row['request'])) if meta.get('cycle') else {}
        return {'id': row['id'], 'kind': row['kind'], 'label': row['label'], 'state': row['state'],
                'created': row['created'], 'orderId': response.get('orderId'),
                'currentShare': response.get('currentShare'), 'share': response.get('share'),
                'position_check': meta.get('position_check', ''),
                'message': response.get('message'), **({'cycle': True, **locator} if locator else {})}

    async def connect(self):
        self.verified = False
        profile = object_data(await self.get('/user/profile'))
        if str(profile.get('userId')) != self.account_id:
            raise ApiError('接口返回的账户身份与已绑定的账户不符。')
        assets = await self.assets()
        self.verified = True
        self.start_tracker()
        return {'account_id': self.account_id, 'assets': assets, 'verified': True}

    async def assets(self):
        self.client()
        value = object_data(await self.get('/account/assets'))
        if str(value.get('userId')) not in self.client().known_ids:
            self.verified = False
            self.pause('资产账户与已绑定的账户不一致。')
            raise ApiError('资产账户标识不一致。')
        return value

    async def withdraw_limit(self):
        return object_data(await self.get('/withdraw/limit', {'userId': self.client().account_id}))

    async def detail(self, market):
        market = identifier(market, '市场编号')
        data = object_data(await self.get(f'/markets/{market}/detail', public=True))
        if data.get('marketId') != market:
            raise ApiError('API 返回的市场编号与请求不同。')
        return data

    @staticmethod
    def ids(data):
        return {field: identifier(data.get(field), field) for field in ('marketId', 'outcomeId', 'optionId')}

    @staticmethod
    def cycle_ids(data):
        option = data.get('optionId')
        if not isinstance(option, str) or option not in CYCLE_OPTIONS:
            raise ValueError('请选择周期盘的涨或跌方向。')
        return {'marketId': identifier(data.get('marketId'), '系列编号'),
                'outcomeId': identifier(data.get('outcomeId'), '窗口编号'), 'optionId': option}

    async def cycle_detail(self, market, window):
        market = identifier(market, '系列编号')
        window = identifier(window, '窗口编号')
        data = object_data(await self.get(f'/cycle/windows/{window}', public=True))
        if data.get('windowId') != window or data.get('initialMarketId') != market:
            raise ApiError('周期盘窗口与所选系列不一致。')
        locator = data.get('orderSubmitLocator')
        try:
            lock = instant(data.get('tradeLockTime') or '')
            checked_at = max(instant(stamp()), instant(data.get('serverTime') or ''))
            within_cutoff = lock > checked_at
        except (TypeError, ValueError):
            within_cutoff = False
        trading = (data.get('tradingEnabled') is True and within_cutoff and isinstance(locator, dict) and
                   locator.get('marketId') == market and locator.get('outcomeId') == window)
        options = [{'optionId': item['optionId'], 'title': CYCLE_OPTIONS[item['optionId']],
                    'priceBuy': item.get('bestAsk'), 'price': item.get('lastPrice')}
                   for item in rows_data(data.get('options') or []) if item.get('optionId') in ('0', '1')]
        return {'cycle': True, 'marketId': market, 'windowId': window, 'modelType': 'orderbook',
                'title': data.get('title') or f"{data.get('coinSymbol', '')} {data.get('cycleType', '')} 涨跌",
                'status': 'open' if trading else data.get('publicStatus'), 'tradingEnabled': trading,
                'endTime': data.get('tradeLockTime'), 'windowEndTime': data.get('windowEndTime'),
                'outcomes': [{'outcomeId': window, 'options': options}]}

    async def current_cycle_window(self, market):
        page = 1
        while True:
            data = object_data(await self.get('/cycle/windows', {'pageNum': page, 'pageSize': 100}, public=True))
            rows = rows_data(data.get('records'))
            matches = [row for row in rows if row.get('initialMarketId') == market and row.get('isCurrent') is True]
            if matches:
                if len(matches) != 1:
                    raise ApiError('ForeGate 返回了多个当前窗口，请从市场列表选择。')
                return identifier(matches[0].get('windowId'), '窗口编号')
            total = data.get('total')
            if len(rows) < 100 or type(total) is int and page * 100 >= total:
                return None
            if page == 20:
                raise ApiError('周期盘窗口页数超出核对范围。')
            page += 1

    async def cycle_book(self, ids):
        data = object_data(await self.get('/cycle/orderbook', body=ids, public=True))
        if any(data.get(key) != ids[key] for key in ('outcomeId', 'optionId')):
            raise ApiError('周期盘订单簿与所选窗口或方向不一致。')
        return {'asks': rows_data(data.get('asks')), 'bids': rows_data(data.get('bids'))}

    async def cycle_page(self, path, ids, page, *, query=None):
        # The cycle gateway can omit totals even on a short, nonempty page.
        pagination = {'page': page, 'pageSize': PAGE_SIZE}
        envelope = await (self.get(path, {**pagination, **query}, envelope=True) if query is not None
                          else self.get(path, body={**ids, **pagination}, envelope=True))
        result = page_data(envelope, page)
        if 'pages' not in envelope['data'] and result['total'] is None:
            result['more'] = bool(result['rows'])
        return result

    async def cycle_positions(self, ids, position=None):
        scope = {key: ids[key] for key in ('marketId', 'outcomeId')}
        if position is not None:
            scope['positionId'] = position
        matches = []
        for page in range(1, 21):
            result = await self.cycle_page('/cycle/positions', scope, page)
            matches.extend(row for row in result['rows'] if all(row.get(key) == val for key, val in ids.items())
                           and (position is None or row.get('positionId') == position))
            if not result['more']:
                return matches
        raise ApiError('周期盘持仓页数超出核对范围。')

    async def cycle_activity(self, ids, page=1):
        query = {key: ids[key] for key in ('marketId', 'outcomeId')}
        result = await self.cycle_page('/cycle/activity/user', ids, page, query=query)
        return {**result, 'rows': [row for row in result['rows'] if all(row.get(key) == val for key, val in ids.items())]}

    async def book(self, ids):
        try:
            raw = await self.get('/orderbook/book', ids)
        except ApiError as exc:
            if exc.status not in QUOTE_FALLBACK_STATUSES:
                raise
            snapshot = await self.public_snapshot(ids)
            return {'asks': rows_data(snapshot.get('asks')), 'bids': rows_data(snapshot.get('bids')),
                    'tickSize': snapshot.get('tick_size')}
        data = object_data(raw)
        if any(data.get(k) != ids[k] for k in ('outcomeId', 'optionId')):
            raise ApiError('订单簿标识与所选盘口不同。')
        return {'asks': rows_data(data.get('asks')), 'bids': rows_data(data.get('bids'))}

    async def public_snapshot(self, ids):
        holders = object_data(await self.get(f"/markets/{ids['marketId']}/holders", public=True))
        matches = [row.get('assetId') for row in rows_data(holders.get('outcomeOptionHolderVoList'))
                   if row.get('outcomeId') == ids['outcomeId'] and row.get('optionId') == ids['optionId']]
        if (holders.get('outcomeId') != ids['outcomeId'] or len(matches) != 1 or
                not isinstance(matches[0], str) or not re.fullmatch(r'[1-9][0-9]{0,77}', matches[0])):
            raise ApiError('订单簿接口暂不可用，公开行情也未返回所选盘口的资产编号。')
        return await asyncio.to_thread(public_book, matches[0])

    async def positions(self, market=None, page=1, size=PAGE_SIZE):
        query = {'page': page, 'pageSize': size}
        if market:
            query['marketId'] = market
        rows = rows_data(await self.get('/account/positions', query))
        return [{**row, 'quantity': row.get('quantity', row.get('shares')),
                 'availableShare': row.get('availableShare', row.get('quantity', row.get('shares'))),
                 'avgPrice': row.get('avgPrice', row.get('averagePrice')),
                 'cost': row.get('cost', row.get('costValue'))} for row in rows]

    async def matching_positions(self, ids):
        result = []
        for page in range(1, 21):
            rows = await self.positions(ids['marketId'], page, 100)
            result.extend(row for row in rows if all(row.get(k) == v for k, v in ids.items()))
            if len(rows) < 100:
                return result
        raise ApiError('该市场持仓页数超出核对范围。')

    @staticmethod
    def position_size(rows):
        return sum((number(row.get('quantity'), '持仓份额', zero=True) for row in rows), Decimal(0))

    async def open_orders(self, ids, page=1, *, cycle=False):
        if cycle:
            result = await self.cycle_page('/cycle/orders/open', ids, page)
        else:
            result = page_data(await self.get('/ordering/open-orders',
                body={**ids, 'page': page, 'pageSize': PAGE_SIZE}, envelope=True), page)
        flat = []
        for group in result['rows']:
            if any(group.get(k) not in ((value,) if cycle else (None, value)) for k, value in ids.items()):
                continue
            for row in rows_data(group.get('ordersOpenDetailVOList') if cycle else group.get('ordersOpenDetailVOList', [group])):
                if all(row.get(key) in (None, val) for key, val in ids.items()):
                    flat.append({**ids, **row})
        return {**result, 'rows': flat}

    async def find_open(self, ids, order_id, *, cycle=False):
        for page in range(1, 21):
            result = await self.open_orders(ids, page, cycle=cycle)
            candidates = [row for row in result['rows'] if row.get('id') == order_id or row.get('orderId') == order_id]
            if candidates:
                if len(candidates) != 1:
                    raise ValueError('平台返回了重复的挂单编号。')
                return identifier(candidates[0].get('id') or candidates[0].get('orderId'), '挂单编号')
            if not result['more']:
                return None
        raise ApiError('挂单页数超出核对范围。')

    async def get_tick(self, ids):
        query = {k: ids[k] for k in ('marketId', 'optionId')}
        try:
            raw = await self.get('/orderbook/tick-size', query)
        except ApiError as exc:
            if exc.status not in QUOTE_FALLBACK_STATUSES:
                raise
            snapshot = await self.public_snapshot(ids)
            return number(snapshot.get('tick_size'), '最小价格步长', 1)
        response = object_data(raw)
        if any(response.get(k) != val for k, val in query.items()):
            raise ApiError('价格步长标识与所选盘口不同。')
        return number(response.get('tickSize'), '最小价格步长', 1)

    async def order_request(self, data, detail=None, tick=None):
        cycle = data.get('cycle') is True
        ids = self.cycle_ids(data) if cycle else self.ids(data)
        if cycle:
            detail = await self.cycle_detail(ids['marketId'], ids['outcomeId'])
            if not detail['tradingEnabled']:
                raise ValueError('这一期周期盘当前不可交易。请核对窗口及截止时间。')
        elif detail is None:
            detail = await self.detail(ids['marketId'])
        assert detail is not None
        group, option = option_for(detail, ids['outcomeId'], ids['optionId'])
        if not cycle:
            if detail.get('modelType') != 'orderbook' or detail.get('status') != 'open' or detail.get('isOnline') is not True:
                raise ValueError('该市场当前未开放订单簿交易。')
            if detail.get('endTime') and instant(detail['endTime']) <= instant(stamp()):
                raise ValueError('市场已过交易截止时间。')
        direction, order_type = data.get('direction'), data.get('orderType')
        if direction not in SIDES or order_type not in ('LIMIT', 'MARKET'):
            raise ValueError('请选择买卖方向和委托类型。')
        body = {**ids, 'direction': direction, 'orderType': order_type}
        if not cycle and option.get('title'):
            body['chooseOutcome'] = str(option['title'])
        labels = []
        if order_type == 'LIMIT':
            price = input_number(data.get('limitPrice'), '限价（USDT/份）', Decimal(1))
            if cycle:
                tick = Decimal('0.001')
                body['timeInForce'] = 'GTC'
            elif tick is None:
                tick = await self.get_tick(ids)
            if price % tick:
                raise ValueError(f'限价必须为 {tick} 的整数倍。')
            body['limitPrice'] = format(price.normalize(), 'f') if cycle else float(price)
            labels.append(f'限价 {price} USDT/份')
        if direction == 'BUY' and order_type == 'MARKET':
            value = input_number(data.get('price'), '支出金额')
            body['price'] = format(value.normalize(), 'f') if cycle else float(value)
            labels.append(f'支出 {value} USDT')
        else:
            share = input_number(data.get('share'), '份额')
            body['share'] = format(share.normalize(), 'f') if cycle else float(share)
            labels.append(f'{share} 份')
            if direction == 'BUY':
                value = price * share
                labels.append(f'委托金额 {value:f} USDT（费用以平台为准）')
            else:
                position = identifier(data.get('positionId'), '持仓编号')
                rows = await (self.cycle_positions(ids, position) if cycle else self.matching_positions(ids))
                match = [row for row in rows if row.get('positionId') == position]
                if len(match) != 1 or number(match[0].get('availableShare'), '可卖份额', zero=True) < share:
                    raise ValueError('当前账户该持仓的份额不足，或持仓已变化。')
                body['positionId'] = position
                self.check_bounds(share, detail, 'sellMinShares', 'sellMaxShares', '卖出份额')
        if direction == 'BUY':
            self.check_bounds(value, detail, 'buyMinAmount', 'buyMaxAmount', '买入金额')
        target = ' · '.join(str(s) for s in (detail.get('title'), group.get('title') or group.get('name'), option.get('title')) if s)
        if cycle:
            target += f" · 窗口 {ids['outcomeId']}"
        label = target + '\n' + ('买入' if direction == 'BUY' else '卖出') + (' · 限价' if order_type == 'LIMIT' else ' · 市价') + ' · ' + ' · '.join(labels)
        return body, label, detail

    @staticmethod
    def check_bounds(value, data, minimum, maximum, label):
        low = data.get(minimum)
        high = data.get(maximum)
        if low is not None and value < number(low, label, maximum=10**15, zero=True):
            raise ValueError(f'{label}低于平台最低要求 {low}。')
        if high is not None:
            upper = number(high, label, maximum=10**15, zero=True)
            if upper and value > upper:
                raise ValueError(f'{label}超出平台上限 {high}。')

    async def prepare(self, data):
        kind = data.get('kind')
        cycle = data.get('cycle') is True
        if kind == 'order':
            body, label, _ = await self.order_request(data)
        elif kind == 'cancel':
            ids = self.cycle_ids(data) if cycle else self.ids(data)
            order_id = identifier(data.get('orderId'), '挂单编号')
            order_id = await self.find_open(ids, order_id, cycle=cycle)
            if not order_id:
                raise ValueError('该订单已不在当前账户的挂单列表。')
            body = {**ids, 'orderOpenIds': [order_id]}
            target = f"周期盘窗口 {ids['outcomeId']} · " if cycle else ''
            label = f'{target}撤销挂单 {order_id} 的剩余数量。'
        elif cycle:
            raise ValueError('该周期盘操作不可用。')
        elif kind == 'claim':
            ids = self.ids(data)
            position = identifier(data.get('positionId'), '持仓编号')
            rows = await self.matching_positions(ids)
            held = next((row for row in rows if row.get('positionId') == position), None)
            if not held or self.position_size([held]) <= 0:
                raise ValueError('该持仓不在当前账户中。')
            detail = await self.detail(ids['marketId'])
            group, option = option_for(detail, ids['outcomeId'], ids['optionId'])
            winning = held.get('win') is True or str(option.get('isWin')).lower() in {'1', 'true', 'yes'}
            if detail.get('status') == 'open' or not winning or held.get('orderStatus') in {'CLAIMING', 'CLAIMED'}:
                raise ValueError('市场尚未显示该选项已结算获胜。')
            body = {'marketId': ids['marketId'], 'positionId': position}
            label = f"申领 {detail.get('title')} · {group.get('title')} · {option.get('title')} 的收益。"
        elif kind == 'withdraw':
            amt = input_number(data.get('amount'), '提现金额')
            destination = solana_address(str(data.get('toAddress', '')).strip())
            if not self.active_mfa().get('verified'):
                raise ValueError('请先完成本次提现的 MFA 验证。')
            limits = await self.withdraw_limit()
            if limits.get('singleWithdrawalMin') is None or limits.get('singleWithdrawalMax') is None:
                raise ValueError('未取得平台提现限额。')
            self.check_bounds(amt, limits, 'singleWithdrawalMin', 'singleWithdrawalMax', '提现金额')
            assets = await self.assets()
            if self.balance(assets) < amt:
                raise ValueError('账户可用余额不足。')
            body = {'amount': float(amt), 'toAddress': destination}
            label = f'提现 {amt} USDT（Solana）\n收款地址 {destination}'
        else:
            raise ValueError('未知账户操作。')
        key = uuid.uuid4().hex
        now = time.monotonic()
        self.pending = {k: v for k, v in self.pending.items() if v['expires'] > now}
        self.pending[key] = {'kind': kind, 'label': label, 'body': body, 'expires': now + 90,
                             'cycle': cycle, 'mfa': self.mfa['token'] if kind == 'withdraw' else None}
        return {'token': key, 'kind': kind, 'summary': 'ForeGate API 账户 ' + self.account_id + '\n' + label}

    @staticmethod
    def balance(assets):
        amount = assets.get('totalAvailableAmount')
        return number(assets.get('availableAmount') if amount is None else amount, '可用余额', maximum=10**15, zero=True)

    async def execute(self, data):
        if data.get('native_authorized') is not True:
            raise ValueError('需要在客户端确认这项操作。')
        plan = self.pending.pop(data.get('token', ''), None)
        if not plan or plan['expires'] <= time.monotonic():
            raise ValueError('操作确认已过期，请重新准备。')
        async with self.operation_lock:
            kind = plan['kind']
            body = plan['body']
            if kind == 'withdraw' and (self.active_mfa()['token'] != plan['mfa'] or not self.mfa['verified']):
                raise ValueError('提现身份验证已更新，请重新准备。')
            if plan['cycle']:
                if kind == 'order':
                    body, _, detail = await self.order_request({**body, 'cycle': True})
                    if instant(detail['endTime']) <= instant(stamp()):
                        raise ValueError('这一期周期盘已截止，请选择新的窗口。')
                elif not await self.find_open(self.cycle_ids(body), body['orderOpenIds'][0], cycle=True):
                    raise ValueError('该周期盘订单已不在挂单列表。')
            op = await self.mutate(kind, body, plan['label'], {'cycle': True} if plan['cycle'] else None)
            if kind == 'withdraw':
                self.mfa = None
            self.start_tracker()
            return op

    async def mutate(self, kind, body, label, meta=None, follow_key=None, parent=None):
        meta = dict(meta or {})
        if parent:
            meta['parent'] = parent
        canonical = json.dumps(body, sort_keys=True)
        # Never repeat an unresolved identical financial action after a timeout.
        if self.db.execute("SELECT 1 FROM operations WHERE kind=? AND request=? AND state IN ('UNKNOWN','SUBMITTING') "
                           "AND COALESCE(json_extract(meta, '$.cycle'), 0)=?",
                           (kind, canonical, bool(meta.get('cycle')))).fetchone():
            raise ValueError('同一操作已有待核对结果。请先查看订单或资金流水。')
        paths = {'order': '/ordering/create-order', 'cancel': '/ordering/cancel-order',
                 'claim': '/ordering/claim', 'withdraw': '/withdraw/request'}
        if meta.get('cycle'):
            paths.update(order='/cycle/orders', cancel='/cycle/orders/cancel')
        req = {**body, 'mfaSessionToken': self.active_mfa()['token']} if kind == 'withdraw' else body
        op_id = uuid.uuid4().hex
        self.db.execute('BEGIN IMMEDIATE')
        try:
            self.db.execute('INSERT INTO operations VALUES(?,?,?,?,?,?,?,?)',
                            (op_id, kind, label, 'SUBMITTING', canonical, None, stamp(), json.dumps(meta)))
            if follow_key:
                self.db.execute("UPDATE follow SET state='SUBMITTING',op_id=? WHERE key=?", (op_id, follow_key))
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise
        try:
            reply = await self.get(paths[kind], body=req, mutation=True)
            if kind == 'order':
                if not isinstance(reply, dict) or not isinstance(reply.get('orderId'), str) or not reply['orderId'].isdigit():
                    raise ApiError('平台响应里没有可查询的订单号。', uncertain=True)
                state = reply.get('orderStatus') or 'ACCEPTED'
                result = {k: reply[k] for k in ('orderId', 'fgRequestId', 'createdAt') if k in reply}
            elif kind == 'cancel':
                matches = [item for item in reply if isinstance(item, dict) and item.get('orderId') == body['orderOpenIds'][0]] if isinstance(reply, list) else []
                if len(matches) != 1 or type(matches[0].get('success')) is not bool:
                    raise ApiError('撤单回执未返回对应的订单结果。', uncertain=True)
                state = 'CANCELED' if matches[0]['success'] else 'REJECTED'
                result = {'orderId': matches[0]['orderId']}
                if state == 'REJECTED':
                    result['message'] = '平台拒绝撤单，请查询原订单。'
            else:
                state, result = 'SUBMITTED', {}
        except ApiError as exc:
            state, result = ('UNKNOWN' if exc.uncertain else 'REJECTED'), {'message': str(exc)}
        except BaseException:
            self.finish_op(op_id, 'UNKNOWN', {'message': '请求中断，需从平台记录核对。'})
            self.pause('提交结果待核对，自动跟单已暂停。')
            raise
        self.finish_op(op_id, state, result)
        if meta.get('cycle') and kind == 'cancel' and state == 'CANCELED':
            for order in self.db.execute("SELECT * FROM operations WHERE kind='order' AND state NOT IN "
                                         "('FILLED','CANCELED','CANCELLED','REJECTED','FAILED','EXPIRED') "
                                         "AND json_extract(meta, '$.cycle')=1 AND json_extract(result, '$.orderId')=?",
                                         (result['orderId'],)).fetchall():
                if self.cycle_ids(json.loads(order['request'])) == self.cycle_ids(body):
                    self.finish_op(order['id'], 'CANCELED', json.loads(order['result']))
        if state == 'UNKNOWN':
            self.pause('提交结果待核对，自动跟单已暂停。')
        return self.public_op(self.db.execute('SELECT * FROM operations WHERE id=?', (op_id,)).fetchone())

    def finish_op(self, op_id, state, result):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            self.db.execute('UPDATE operations SET state=?,result=? WHERE id=?', (state, json.dumps(result), op_id))
            self.db.execute('UPDATE follow SET state=?,note=? WHERE op_id=?',
                            (state, result.get('message', ''), op_id))
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def active_mfa(self):
        if not self.mfa or self.mfa['expires'] <= time.time():
            self.mfa = None
            raise ValueError('请开启本次提现的 MFA 验证。')
        return self.mfa

    async def mfa_action(self, action, data):
        async with self.operation_lock:
            if action == 'mfa_begin':
                self.mfa = None
                result = object_data(await self.get('/user/mfa/verification/create', {'verificationType': 'WITHDRAW'}, post=True))
                methods = [m for m in result.get('availableMethods', []) if m in {'TOTP','EMAIL','RECOVERY'}]
                token = result.get('sessionToken')
                if not methods:
                    raise ValueError('平台未提供可用验证方式。请检查此 API 账户的验证器和邮箱验证码状态。')
                if not isinstance(token, str) or not token or not isinstance(result.get('expiresAt'), str):
                    raise ValueError('平台未返回完整的提现验证会话。')
                expiry = instant(result['expiresAt']).timestamp()
                if expiry <= time.time():
                    raise ValueError('本次提现验证已过期，请重新开启。')
                self.mfa = {'token': token, 'methods': methods, 'expires': expiry, 'verified': False}
                return {'methods': methods, 'recommended': result.get('recommendedMethod')}
            if action == 'mfa_setup':
                if data.get('native_authorized') is not True:
                    raise ValueError('请在客户端确认启动验证器绑定。')
                state = object_data(await self.get('/user/mfa/status'))
                if state.get('locked') or state.get('forceReset'):
                    raise ValueError('平台已锁定验证或要求重置，请先联系 ForeGate 处理此 API 账户。')
                if state.get('totpEnabled') is True:
                    raise ValueError('账户已经绑定验证器。')
                if state.get('totpEnabled') is not False:
                    raise ValueError('平台未返回验证器绑定状态，请刷新后重试。')
                self.totp_started = False
                setup = object_data(await self.get('/user/mfa/setup/totp/init', body={}, mutation=True))
                if not isinstance(setup.get('manualEntryKey'), str) or not setup['manualEntryKey'].strip():
                    raise ValueError('平台未返回验证器绑定密钥。')
                self.totp_started = time.monotonic()
                return {'key': setup.get('manualEntryKey'), 'issuer': setup.get('issuer'), 'account': setup.get('account')}
            if action == 'mfa_setup_complete':
                code = str(data.get('code', '')).strip()
                if not self.totp_started or time.monotonic() - self.totp_started > 600 or not re.fullmatch(r'[0-9]{6}', code):
                    raise ValueError('请重新开始绑定或输入正确的 6 位验证码。')
                result = object_data(await self.get('/user/mfa/setup/totp/complete', body={'code': code}, mutation=True))
                self.totp_started = False
                return {'recoveryCodes': result.get('recoveryCodes', [])}
            active = self.active_mfa()
            if action == 'mfa_email':
                if 'EMAIL' not in active['methods']:
                    raise ValueError('此账户的验证会话未提供邮箱验证码。')
                sent = await self.get('/user/mfa/verification/send-email-otp', body={'sessionToken': active['token']}, mutation=True)
                if sent is not True:
                    raise ValueError('平台未确认邮箱验证码已发送。')
                return {'sent': True}
            method = data.get('method')
            code = str(data.get('code', '')).strip()
            if method not in active['methods'] or not re.fullmatch(r'[a-zA-Z0-9-]{6,80}', code):
                raise ValueError('请选择有效验证方式并输入该方式的验证码。')
            result = await self.get('/user/mfa/verification/verify',
                                    body={'sessionToken': active['token'], 'mfaMethod': method, 'code': code}, mutation=True)
            if result is not True:
                raise ValueError('ForeGate 未确认验证通过。')
            active['verified'] = True
            return {'verified': True}

    async def reads(self, action, data):
        try:
            page = int(data.get('page', 1))
        except (TypeError, ValueError):
            raise ValueError('页码无效。') from None
        if not 1 <= page <= 10000:
            raise ValueError('页码无效。')
        query = {'page': page, 'pageSize': PAGE_SIZE}
        if action == 'connect':
            return await self.connect()
        if action == 'assets':
            return await self.assets()
        if action == 'market_categories':
            return market_categories(await self.get('/markets/category-tree', {'language': 'zh-CN'}, public=True))
        if action == 'markets':
            if data.get('categoryId'):
                return await self.category_markets(identifier(data['categoryId'], '分类编号'), page)
            keyword = str(data.get('search', '')).strip()[:100]
            if keyword:
                payload = await self.get('/search', {**query, 'searchBy': 'Market', 'search': keyword}, public=True, envelope=True)
            else:
                payload = await self.get('/markets/list', query, public=True, envelope=True)
            return page_data(payload, page)
        if action == 'detail':
            market, window = data.get('marketId'), data.get('windowId')
            if data.get('cycle') is True:
                return await self.cycle_detail(market, window)
            try:
                return await self.detail(market)
            except ApiError as exc:
                if exc.status != 404:
                    raise
                window = window or await self.current_cycle_window(market)
                if not window:
                    raise
                return await self.cycle_detail(market, window)
        if action in {'book', 'open_orders', 'sell_positions', 'cycle_activity'} and data.get('cycle') is True:
            ids = self.cycle_ids(data)
            if action == 'book':
                return await self.cycle_book(ids)
            if action == 'open_orders':
                return await self.open_orders(ids, page, cycle=True)
            if action == 'sell_positions':
                return await self.cycle_positions(ids)
            if action == 'cycle_activity':
                return await self.cycle_activity(ids, page)
        if action in {'book', 'open_orders', 'sell_positions'}:
            ids = self.ids(data)
            if action == 'sell_positions':
                return await self.matching_positions(ids)
            return await (self.book(ids) if action == 'book' else self.open_orders(ids, page))
        if action == 'positions':
            market = identifier(data['marketId']) if data.get('marketId') else None
            rows = await self.positions(market, page)
            return {'rows': rows, 'more': len(rows) == PAGE_SIZE, 'page': page}
        if action == 'query_orders':
            raw_ids = str(data.get('ids', '')).strip()
            ids = list(dict.fromkeys(identifier(value, '订单编号') for value in re.split(r'[,，\s]+', raw_ids)))
            if len(ids) > 100:
                raise ValueError('每次最多查询 100 个订单。')
            return {'rows': rows_data(await self.get('/ordering/query-orders', body={'orderIds': ids}))}
        if action == 'withdraw_limit':
            return await self.withdraw_limit()
        if action in {'deposit_address', 'mfa_status'}:
            path = {'deposit_address': '/deposit/address', 'mfa_status': '/user/mfa/status'}[action]
            result = object_data(await self.get(path))
            if action == 'deposit_address':
                if str(result.get('userId')) not in self.client().known_ids:
                    raise ApiError('充值地址账户与所绑定的 API 账户不一致。')
                if str(result.get('chain')).lower() in {'sol', 'solana'}:
                    solana_address(result.get('walletAddress'))
                elif not result.get('chain') or not result.get('walletAddress'):
                    raise ApiError('平台未返回完整的充值地址或网络。')
            return result
        paths = {'trades': '/trades', 'account_history': '/account/history', 'deposits': '/deposit/history', 'withdrawals': '/withdraw/history'}
        if action not in paths:
            raise ValueError('未知 ForeGate 查询。')
        return page_data(await self.get(paths[action], query, envelope=True), page)

    def set_config(self, data):
        if self.armed_at:
            raise ValueError('请暂停自动跟单后修改参数。')
        order_type = data.get('order_type', self.config['order_type'])
        if order_type not in ('MARKET', 'LIMIT'):
            raise ValueError('请选择市价或限价跟单。')
        config = {key: str(input_number(data.get(key), label, maximum)) for key, label, maximum in (
            ('amount', '单笔上限', 10000), ('daily', '当日上限', 100000),
            ('event', '单赛事上限', 100000))}
        if Decimal(config['amount']) > min(Decimal(config['daily']), Decimal(config['event'])):
            raise ValueError('单笔上限须小于或等于单赛事与当日上限。')
        config.update(order_type=order_type, max_price=self.config['max_price'], ttl=self.config['ttl'])
        if order_type == 'LIMIT':
            config['max_price'] = str(input_number(data.get('max_price'), '最高价格', Decimal('99.99')))
            ttl = data.get('ttl')
            if not re.fullmatch(r'[0-9]{1,4}', str(ttl)) or not (str(ttl) == '0' or 30 <= int(ttl) <= 3600):
                raise ValueError('撤单等待须为 30–3600 秒，填 0 则保留挂单。')
            config['ttl'] = int(ttl)
        self.db.execute('INSERT OR REPLACE INTO settings VALUES (1,?)', (json.dumps(config),))
        self.config = config
        return self.status()

    async def start_follow(self, action, data):
        if action == 'follow_arm':
            if data.get('native_authorized') is not True:
                raise ValueError('请在客户端确认启用真实跟单。')
            if data.get('config') != self.config or data.get('channel') != self.service.settings['channel']:
                raise ValueError('跟单参数已更新，请重新确认。')
            if self.db.execute("SELECT 1 FROM operations WHERE state IN ('UNKNOWN','SUBMITTING')").fetchone():
                raise ValueError('有提交结果待人工核对，请先在订单与成交页处理。')
            if not self.service.settings['risk']['enabled']:
                raise ValueError('原有跟单总开关已关闭，请在连接设置中启用。')
            generation = self.pause_generation
            await self.connect()
            if generation != self.pause_generation or data.get('config') != self.config or data.get('channel') != self.service.settings['channel']:
                raise ValueError('跟单参数已更新，请重新确认。')
            if self.db.execute("SELECT 1 FROM operations WHERE state IN ('UNKNOWN','SUBMITTING')").fetchone():
                raise ValueError('当前有提交结果待核对。')
            self.armed_at = instant(stamp())
        if not self.service.running:
            self.service.error = ''
            self.service.running = True
            self.service.task = asyncio.create_task(self.service.listen())
        self.start_tracker()
        return self.status()

    def pause(self, reason='已暂停自动跟单。'):
        self.armed_at = None
        self.pause_generation += 1
        self.db.execute("UPDATE follow SET state='SKIPPED',note=? WHERE state IN ('QUEUED','PRECHECK')", (reason,))
        self.service.addlog(reason)

    def ingest(self, record, source, signal):
        if source not in {'telegram', 'public'} or not self.armed_at or not self.service.running:
            return []
        try:
            received = instant(signal.date)
        except (TypeError, ValueError):
            return []
        if received < self.armed_at:
            return []
        if self.db.execute('SELECT 1 FROM follow WHERE signal_id=?', (signal.signal_id,)).fetchone():
            if record.get('edit_date'):
                self.db.execute("UPDATE follow SET state='SKIPPED',note='原消息已编辑，请人工核对。' WHERE signal_id=? AND state IN ('QUEUED','PRECHECK')",
                                (signal.signal_id,))
            return []
        if not signal.legs:
            return []
        decisions = []
        for leg in signal.legs:
            reason = filters(signal, leg, self.service.settings['risk'], stamp())
            if record.get('edit_date'):
                reason = '收到的消息已经编辑，请人工核对。'
            if not reason and self.db.execute("SELECT COUNT(*) FROM follow WHERE state='QUEUED'").fetchone()[0] >= 100:
                reason = '待处理队列已满。'
            key = trade_key(signal, leg)
            state = 'SKIPPED' if reason else 'QUEUED'
            self.db.execute('INSERT OR IGNORE INTO follow VALUES(?,?,?,?,?,?,?,?,?,NULL)',
                            (key, signal.signal_id, leg.leg_no, signal.event_id, source, json.dumps(record), stamp(), state, reason))
            decisions.append({'leg_no': leg.leg_no, 'reason': reason or '已进入 API 跟单队列'})
        return decisions

    def available_budget(self, event_id):
        daily = event = Decimal(0)
        tz = ZoneInfo(self.service.settings['risk']['timezone'])
        today = instant(stamp()).astimezone(tz).date()
        for row in self.db.execute(
            "SELECT o.state,o.created,o.meta,f.event FROM follow f JOIN operations o ON f.op_id=o.id "
            "WHERE o.state NOT IN ('REJECTED','FAILED','CLEARED_NO_ORDER')"
        ):
            reserve = Decimal(json.loads(row['meta']).get('budget', '0'))
            if instant(row['created']).astimezone(tz).date() == today:
                daily += reserve
            if row['event'] == event_id:
                event += reserve
        return min(Decimal(self.config['amount']), Decimal(self.config['daily']) - daily, Decimal(self.config['event']) - event)

    async def process_follow(self, row):
        self.db.execute("UPDATE follow SET state='PRECHECK' WHERE key=?", (row['key'],))
        try:
            record = json.loads(row['record'])
            signal = parse_message(record)
            leg = next(leg for leg in signal.legs if leg.leg_no == row['leg'])
            async with self.operation_lock:
                reason = filters(signal, leg, self.service.settings['risk'], stamp())
                if reason or not self.armed_at or not self.service.running:
                    raise ValueError(reason or '自动跟单已暂停。')
                budget = self.available_budget(signal.event_id)
                if budget <= 0:
                    raise ValueError('当日或单赛事额度已用完。')
                detail = await self.detail(signal.event_id)
                group, option = match_signal(detail, signal, leg)
                ids = self.ids({'marketId': signal.event_id, 'outcomeId': group['outcomeId'], 'optionId': option['optionId']})
                order_type = self.config['order_type']
                request = {**ids, 'direction': 'BUY', 'orderType': order_type}
                tick, ttl = None, 0
                if order_type == 'MARKET':
                    request['price'] = format(budget, 'f')
                else:
                    book = await self.book(ids)
                    tick = (number(book['tickSize'], '最小价格步长', 1)
                            if book.get('tickSize') is not None else await self.get_tick(ids))
                    asks = [number(ask.get('price'), '卖盘价格', 1) for ask in book['asks']
                            if number(ask.get('size'), '卖盘份额', zero=True) > 0]
                    limit = Decimal(self.config['max_price']) / 100
                    if not asks or min(asks) > limit or min(asks) % tick:
                        raise ValueError('卖盘为空，或价格超过跟单上限，或价格步长无效。')
                    price = min(asks)
                    fee = max(number(detail.get(key) or 0, '平台费用率', 1, zero=True)
                              for key in ('buyFee', 'fee', 'makerFee', 'takerFee'))
                    fee = max(fee, Decimal('0.02'))
                    share = (budget / (price * (1 + fee))).quantize(Decimal('0.01'), rounding=ROUND_DOWN)
                    request.update(limitPrice=format(price, 'f'), share=format(share, 'f'))
                    ttl = self.config['ttl']
                body, label, _ = await self.order_request(request, detail=detail, tick=tick)
                baseline = self.position_size(await self.matching_positions(ids))
                balance = self.balance(await self.assets())
                if balance < budget:
                    raise ValueError('API 账户可用余额不足。')
                reason = filters(signal, leg, self.service.settings['risk'], stamp())
                state = self.db.execute('SELECT state FROM follow WHERE key=?', (row['key'],)).fetchone()[0]
                if reason or state != 'PRECHECK' or not self.armed_at or not self.service.running:
                    raise ValueError(reason or '该信号的跟单已经暂停或取消。')
                await self.mutate('order', body, label, {'baseline': str(baseline), 'budget': str(budget),
                                  'ttl': ttl}, follow_key=row['key'])
        except (ValueError, KeyError, StopIteration) as exc:
            self.db.execute("UPDATE follow SET state='SKIPPED',note=? WHERE key=? AND state='PRECHECK'",
                            (str(exc) if isinstance(exc, ValueError) else '平台盘口缺少所需数据。', row['key']))

    def start_tracker(self):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.track())

    async def track(self):
        rounds = 0
        while True:
            queued = self.db.execute("SELECT * FROM follow WHERE state='QUEUED' ORDER BY rowid LIMIT 1").fetchone()
            try:
                if queued and self.armed_at:
                    await self.process_follow(queued)
                if rounds % 4 == 0:
                    await self.reconcile()
            except (ApiError, ValueError, KeyError) as exc:
                self.service.error = str(exc)
            except Exception:
                self.service.error = '跟单后台检查失败，请在订单与成交中核对。'
                if self.armed_at:
                    self.pause(self.service.error)
            rounds += 1
            await asyncio.sleep(2)

    async def reconcile(self):
        async with self.operation_lock:
            rows = self.db.execute("SELECT * FROM operations WHERE kind='order' AND result IS NOT NULL ORDER BY rowid DESC").fetchall()
            now = instant(stamp())
            orders = {}
            errors = []
            for row in rows:
                response = json.loads(row['result'])
                body = json.loads(row['request'])
                meta = json.loads(row['meta'])
                if meta.get('cycle'):
                    continue
                order = response.get('orderId')
                if order and (row['state'] not in TERMINAL or 'baseline' in meta and not meta.get('position_check')):
                    orders[order] = (row, response, body, meta)
            query_ids = [order for order, (row, *_rest) in orders.items() if row['state'] not in TERMINAL]
            for start in range(0, len(query_ids), 100):
                try:
                    found = rows_data(await self.get('/ordering/query-orders', body={'orderIds': query_ids[start:start + 100]}))
                except ApiError as exc:
                    errors.append(exc)
                    continue
                for item in found:
                    order_id = item.get('orderId')
                    if order_id not in orders:
                        continue
                    row, response, body, _ = orders[order_id]
                    if not all(item.get(k) == body.get(k) for k in ('marketId', 'outcomeId', 'optionId', 'direction', 'orderType')):
                        continue
                    state = item.get('orderStatus')
                    if state not in (WORKING | TERMINAL):
                        continue
                    response['currentShare'] = item.get('currentShare')
                    response['share'] = item.get('share')
                    self.finish_op(row['id'], state, response)

            for order_id, (row, response, body, meta) in orders.items():
                if 'baseline' not in meta:
                    continue
                age = (now - instant(row['created'])).total_seconds()
                current = self.db.execute('SELECT state FROM operations WHERE id=?', (row['id'],)).fetchone()[0]
                ids = self.ids(body)
                if age >= 60 and not meta.get('position_check'):
                    try:
                        current_shares = self.position_size(await self.matching_positions(ids))
                    except ValueError as exc:
                        errors.append(exc)
                    else:
                        before = Decimal(meta['baseline'])
                        local_time = now.astimezone(ZoneInfo('Asia/Shanghai')).strftime('%H:%M:%S')
                        meta['position_check'] = f'持仓 {before:f} → {current_shares:f} 份（{local_time}）'
                        self.db.execute('UPDATE operations SET meta=? WHERE id=?', (json.dumps(meta), row['id']))
                if meta.get('ttl') and age >= meta['ttl'] and current in WORKING and not self.service.closed:
                    # Open-order lookup must establish the ID is currently cancellable for this exact option.
                    if any(json.loads(found['meta']).get('parent') == row['id'] for found in self.db.execute(
                        "SELECT meta FROM operations WHERE kind='cancel'")):
                        continue
                    try:
                        cancel_id = await self.find_open(ids, order_id)
                        if cancel_id:
                            await self.mutate('cancel', {**ids, 'orderOpenIds': [cancel_id]}, f'到期撤销 {order_id} 剩余数量', parent=row['id'])
                    except ValueError as exc:
                        errors.append(exc)
            if errors:
                raise errors[0]
            return {'operations': self.operations()}

    def settings_view(self):
        return connection_settings(self.service)

    async def dispatch(self, action, data):
        if action == 'status':
            return self.status()
        if action == 'settings':
            return self.settings_view()
        if action == 'config':
            return self.set_config(data)
        if action in {'follow_listen', 'follow_arm'}:
            return await self.start_follow(action, data)
        if action == 'follow_pause':
            self.pause()
            await self.service.pause()
            return self.status()
        if action == 'prepare':
            return await self.prepare(data)
        if action == 'execute':
            return await self.execute(data)
        if action.startswith('mfa_') and action != 'mfa_status':
            if action not in {'mfa_begin', 'mfa_verify', 'mfa_email', 'mfa_setup', 'mfa_setup_complete'}:
                raise ValueError('未知 MFA 操作。')
            return await self.mfa_action(action, data)
        if action == 'reconcile':
            return await self.reconcile()
        if action == 'review_unknown':
            if data.get('native_authorized') is not True:
                raise ValueError('请在客户端确认核对结果。')
            identifier_ = data.get('id')
            async with self.operation_lock:
                row = self.db.execute("SELECT * FROM operations WHERE id=? AND state='UNKNOWN'", (identifier_,)).fetchone()
                if not row:
                    raise ValueError('该操作已不在待核对状态。')
                outcome = data.get('outcome')
                if outcome not in {'absent','found'}:
                    raise ValueError('请选择核对结果。')
                known_absent = outcome == 'absent'
                resolved = 'CLEARED_NO_ORDER' if known_absent else 'MANUALLY_REVIEWED'
                self.finish_op(row['id'], resolved, {'message': '用户已查阅平台记录并完成核对。'})
                return {'operations': self.operations()}
        return await self.reads(action, data)

    async def close(self):
        self.pause('客户端退出，自动跟单已暂停。')
        async with self.operation_lock:
            if self.task:
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)
            self.db.close()
