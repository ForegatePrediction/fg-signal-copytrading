"""Deterministic parsing. A parsed leg is not proof of a resolved platform market."""
from dataclasses import asdict, dataclass, field
from decimal import Decimal
import hashlib
import json
import re
import unicodedata
from urllib.parse import urlsplit

VERSION = '0.1.0'
NUMBER = r'\d+(?:\.\d+)?'


def norm(text):
    # Normalize full-width punctuation and invisible presentation characters only.
    return unicodedata.normalize('NFKC', text).translate(dict.fromkeys(map(ord, '\ufe0f\u200d\u200b\u2060\ufeff'), None)).strip()


def keytext(text):
    return ' '.join(norm(text).casefold().split())


@dataclass
class Leg:
    leg_no: int
    raw: str
    status: str = 'UNPARSED'
    reason: str = ''
    market_type: str = ''
    selection: str = ''
    side: str = 'YES'
    line: str = ''
    game_no: int = 0
    profit_pct: str = ''
    risk_flag: str = 'normal'

    def identity(self):
        return [self.market_type, keytext(self.selection), self.side, self.line, self.game_no]


@dataclass
class Signal:
    signal_id: str
    type: str
    raw_text: str
    date: str = ''
    event_id: str = ''
    league: str = ''
    sport: str = 'unknown'
    home: str = ''
    away: str = ''
    format: str = ''
    reason: str = ''
    reply_url: str = ''
    legs: list = field(default_factory=list)
    parse_version: str = VERSION

    def as_dict(self):
        return asdict(self)


def event_ids(urls):
    found = set()
    for url in urls:
        try:
            p = urlsplit(url.rstrip('.,)'))
            if p.scheme != 'https' or p.hostname not in {'foregate.com', 'www.foregate.com'} or p.username or p.password or p.port not in {None,443}:
                continue
            match = re.fullmatch(r'/[A-Za-z-]+/event/(\d+)/?', p.path)
            if match:
                found.add(match[1])
        except ValueError:
            continue
    return found


def parse_leg(number, raw, profit_line=''):
    leg = Leg(number, raw)
    try:
        text = norm(raw)
        m = re.fullmatch(r'([🟢🔴])\s*([^:]+):\s*(.+)', text)
        if not m:
            raise ValueError('INVALID_LEG_FORMAT')
        marker, label, value = m.groups()
        leg.risk_flag = 'high' if marker == '🔴' or re.search(r'\bhigh\s+risk\b', value, re.I) else 'normal'
        value = re.sub(r'\s*-?\s*High\s+risk\s*$', '', value, flags=re.I).strip()
        # Profit must be explicitly and unambiguously attached to this leg.
        pm = re.search(r'\s*\(\+?(' + NUMBER + r')%\)\s*$', value)
        if pm:
            leg.profit_pct = str(Decimal(pm[1]))
            value = value[:pm.start()].strip()
        if profit_line:
            sm = re.fullmatch(r'[💵🔥💰]?\s*(?:Est\.\s*Profit|Expected\s+Profit):\s*\+?(' + NUMBER + r')%', norm(profit_line), re.I)
            if not sm or leg.profit_pct:
                raise ValueError('AMBIGUOUS_PROFIT')
            leg.profit_pct = str(Decimal(sm[1]))
        if not leg.profit_pct:
            raise ValueError('MISSING_OR_MALFORMED_PROFIT')
        if Decimal(leg.profit_pct) <= 0:
            raise ValueError('INVALID_PROFIT')
        if any(c in value for c in '()%'):
            raise ValueError('MALFORMED_SELECTION')
        side = re.search(r'\s+-\s+(YES|NO)$', value, re.I)
        if side:
            leg.side = side[1].upper()
            value = value[:side.start()].strip()
        label = keytext(label)
        mapping = {'moneyline':'moneyline','spreads':'spreads','totals':'totals',
                   'map handicap':'map_handicap','total maps':'total_maps',
                   'both teams to score':'both_teams_to_score'}
        game = re.fullmatch(r'(?:game|map)\s+([1-9]\d*)\s+winner', label)
        if game:
            leg.market_type, leg.game_no = 'game_winner', int(game[1])
        elif label in mapping:
            leg.market_type = mapping[label]
        else:
            raise ValueError('UNSUPPORTED_MARKET')
        if leg.market_type in {'spreads', 'map_handicap'}:
            sm = re.fullmatch(r'(.+?)\s+([+-]' + NUMBER + r')', value)
            if not sm:
                raise ValueError('MISSING_SIGNED_HANDICAP')
            leg.selection, leg.line = sm[1].strip(), str(Decimal(sm[2]).normalize())
        elif leg.market_type in {'totals','total_maps'}:
            sm = re.fullmatch(r'(OVER|UNDER)\s+(' + NUMBER + r')', value, re.I)
            if not sm:
                raise ValueError('INVALID_TOTALS')
            leg.selection, leg.line = sm[1].upper(), str(Decimal(sm[2]).normalize())
        elif leg.market_type == 'both_teams_to_score':
            if value.upper() not in {'YES','NO'} or side:
                raise ValueError('INVALID_BTTS_SELECTION')
            leg.selection, leg.side = 'BOTH_TEAMS_TO_SCORE', value.upper()
        else:
            if re.search(r'\s[+-]\d', value):
                raise ValueError('MONEYLINE_WITH_HANDICAP')
            leg.selection = value
        if not leg.selection or len(leg.selection) > 100 or any(c in leg.selection for c in ':🟢🔴💵🔥💎'):
            raise ValueError('INVALID_SELECTION')
        leg.status = 'PARSED'
    except ValueError as e:
        leg.reason = str(e)
    return leg


def parse_message(record):
    text = norm(record.get('text', ''))
    signal = Signal(str(record['channel']) + ':' + str(record['message_id']), 'OTHER', record.get('text', ''), record.get('date', ''), reply_url=record.get('reply_url') or '')
    if text.startswith(('✅','❌')):
        signal.type = 'RESULT'
        return signal
    if re.search(r'WINNINGS|LOSSES|YESTERDAY', text, re.I):
        signal.type = 'SUMMARY'
        return signal
    lines = [norm(x) for x in text.splitlines() if norm(x)]
    candidates = [i for i,x in enumerate(lines) if x.startswith(('🟢','🔴'))]
    if not candidates:
        if re.search(r'\s+vs\.?\s+', text, re.I) and event_ids(re.findall(r'https?://[^\s<>]+', text) + record.get('links', [])):
            signal.type, signal.reason = 'UNPARSED', 'MISSING_OR_UNKNOWN_RISK_MARKER'
        return signal
    signal.type = 'UNPARSED'
    urls = re.findall(r'https?://[^\s<>]+', text) + record.get('links', [])
    ids = event_ids(urls)
    if len(ids) != 1:
        signal.reason = 'MISSING_OR_MULTIPLE_EVENT_IDS'
        return signal
    signal.event_id = next(iter(ids))
    header = re.fullmatch(r'([⚽🎮🏀🎾🏈🏒⚾])\s*([^:]+):\s*(.+)', lines[0])
    if not header:
        signal.reason = 'INVALID_HEADER'
        return signal
    emoji, league, fixture = header.groups()
    fmt = re.search(r'\s*\((BO[1357])\)\s*$', fixture, re.I)
    if fmt:
        signal.format = fmt[1].upper(); fixture = fixture[:fmt.start()]
    # A format before the header colon is also explicitly supported.
    prefix_fmt = re.search(r'\s*\((BO[1357])\)\s*$', league, re.I)
    if prefix_fmt:
        if signal.format and signal.format != prefix_fmt[1].upper():
            signal.reason = 'CONFLICTING_FORMAT'; return signal
        signal.format = prefix_fmt[1].upper(); league = league[:prefix_fmt.start()]
    teams = re.split(r'\s+vs\.?\s+', fixture, flags=re.I)
    if len(teams) != 2 or not all(x.strip() for x in teams):
        signal.reason = 'INVALID_FIXTURE'; return signal
    signal.league = league.strip(); signal.home, signal.away = (x.strip() for x in teams)
    if '(' in fixture or ')' in fixture:
        signal.reason = 'UNRECOGNIZED_FIXTURE_SUFFIX'; return signal
    # League overrides the known erroneous soccer emoji used on NFL posts.
    signal.sport = ('american_football' if keytext(league) in {'nfl','cfb'} else
                    {'⚽':'soccer','🎮':'esports','🏀':'basketball','🎾':'tennis','🏈':'american_football','🏒':'hockey','⚾':'baseball'}[emoji])
    consumed = {0}
    for i in candidates:
        extra = ''
        if i + 1 < len(lines) and re.search(r'(?:Est\.\s*Profit|Expected\s+Profit):', lines[i+1], re.I):
            extra = lines[i+1]; consumed.add(i+1)
        leg = parse_leg(len(signal.legs)+1, lines[i], extra)
        signal.legs.append(leg); consumed.add(i)
    for i,line in enumerate(lines):
        if i in consumed or line == '💎 First Trade Protection':
            continue
        if re.fullmatch(r'https?://\S+', line) and event_ids([line]):
            continue
        signal.reason = 'UNRECOGNIZED_MESSAGE_LINE'
        return signal
    identities = [tuple(x.identity()) for x in signal.legs if x.status == 'PARSED']
    if len(identities) != len(set(identities)):
        signal.reason = 'DUPLICATE_LEGS'; return signal
    signal.type = 'PICK'
    return signal


def trade_key(signal, leg):
    payload = [signal.event_id] + leg.identity()
    return hashlib.sha256(json.dumps(payload,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
