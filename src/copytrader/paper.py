"""SQLite-backed offline paper ledger. All quotes must be explicitly supplied.

No API, browser, live executor, implicit prices or channel-based settlement exists.
"""
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
import hashlib
import json
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo
from .parser import keytext, parse_message, trade_key

D = Decimal
ZERO = D('0')


def instant(s):
    dt = datetime.fromisoformat(s.replace('Z','+00:00')) if isinstance(s,str) else s
    if dt.tzinfo is None:
        raise ValueError('Timezone required')
    return dt.astimezone(timezone.utc)


def positive(v, allow_zero=False):
    n = D(str(v))
    if not n.is_finite() or (n < 0 if allow_zero else n <= 0):
        raise ValueError('Invalid finite positive decimal')
    return n


@dataclass(frozen=True)
class Config:
    initial_balance: str = '1000'
    stake_usd: str = '5'
    max_event_usd: str = '20'
    max_daily_stake_usd: str = '100'
    max_daily_loss_usd: str = '50'
    max_exposure_usd: str = '100'
    max_signal_age_sec: int = 120
    max_quote_age_sec: int = 30
    max_slippage: str = '0.03'
    min_profit_pct: str = '10'
    max_profit_pct: str = '300'
    follow_high_risk: bool = False
    limit_ttl_sec: int = 60
    share_step: str = '0.01'
    price_tick: str = '0.001'
    min_shares: str = '0.01'
    timezone: str = 'Asia/Shanghai'
    allowed_sports: tuple = ()
    allowed_markets: tuple = ()
    allowed_leagues: tuple = ()
    blocked_leagues: tuple = ()
    enabled: bool = True
    one_leg_per_event: bool = False

    def __post_init__(self):
        for name in ('initial_balance','stake_usd','max_event_usd','max_daily_stake_usd','max_daily_loss_usd','max_exposure_usd','max_profit_pct','share_step','price_tick','min_shares'):
            positive(getattr(self,name))
        positive(self.max_slippage,True);positive(self.min_profit_pct,True)
        if D(self.max_profit_pct)<D(self.min_profit_pct) or D(self.price_tick)>=1:
            raise ValueError('Invalid config range')
        for name in ('max_signal_age_sec','max_quote_age_sec','limit_ttl_sec'):
            if type(getattr(self,name)) is not int or getattr(self,name)<=0:
                raise ValueError('Invalid duration')
        for name in ('enabled','follow_high_risk','one_leg_per_event'):
            if type(getattr(self,name)) is not bool: raise ValueError('Invalid boolean')
        for name in ('allowed_sports','allowed_markets','allowed_leagues','blocked_leagues'):
            if not isinstance(getattr(self,name),(tuple,list)) or not all(isinstance(x,str) for x in getattr(self,name)):
                raise ValueError('Invalid filter list')
        ZoneInfo(self.timezone)


@dataclass(frozen=True)
class Quote:
    quote_id: str
    event_id: str
    home: str
    away: str
    leg_identity: list
    market_id: str
    observed_at: str
    ask: str
    ask_shares: str
    fee_rate: str
    reference_price: str
    source: str

    def validate(self):
        ask=positive(self.ask); reference=positive(self.reference_price)
        fee=positive(self.fee_rate,True);positive(self.ask_shares,True)
        if ask>=1 or reference>=1 or fee>=1 or not self.quote_id or not self.market_id or not self.source:
            raise ValueError('Invalid quote')
        instant(self.observed_at)


class PaperEngine:
    def __init__(self, db_path, config=None):
        self.config=config or Config()
        if str(db_path)!=':memory:': Path(db_path).parent.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(str(db_path),timeout=10,isolation_level=None)
        self.db.row_factory=sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS messages(signal_id TEXT,version TEXT,body TEXT NOT NULL,parsed TEXT NOT NULL,PRIMARY KEY(signal_id,version));
        CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY,trade_key TEXT UNIQUE NOT NULL,signal_id TEXT NOT NULL,event_id TEXT NOT NULL,market_id TEXT NOT NULL,leg_identity TEXT NOT NULL,home TEXT NOT NULL,away TEXT NOT NULL,status TEXT NOT NULL,budget TEXT NOT NULL,limit_price TEXT NOT NULL,size TEXT NOT NULL,filled TEXT NOT NULL DEFAULT '0',cost TEXT NOT NULL DEFAULT '0',fee_rate TEXT NOT NULL,created_at TEXT NOT NULL,expires_at TEXT NOT NULL,trade_day TEXT NOT NULL,config_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS decisions(id INTEGER PRIMARY KEY,signal_id TEXT,leg_no INTEGER,reason TEXT NOT NULL,created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS quotes(id TEXT PRIMARY KEY,payload TEXT NOT NULL,remaining TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS fills(id INTEGER PRIMARY KEY,order_id INTEGER NOT NULL REFERENCES orders(id),quote_id TEXT NOT NULL,shares TEXT NOT NULL,price TEXT NOT NULL,cost TEXT NOT NULL,UNIQUE(order_id,quote_id));
        CREATE TABLE IF NOT EXISTS settlements(order_id INTEGER PRIMARY KEY REFERENCES orders(id),payout TEXT NOT NULL,pnl TEXT NOT NULL,settled_at TEXT NOT NULL,settle_day TEXT NOT NULL,source TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS visible_depth(market_id TEXT,price TEXT,consumed TEXT NOT NULL,PRIMARY KEY(market_id,price));
        ''')
        try:
            self.db.execute('BEGIN IMMEDIATE')
            row=self.db.execute("SELECT value FROM meta WHERE key='initial_balance'").fetchone()
            if row and D(row['value'])!=D(self.config.initial_balance):
                raise ValueError('Existing ledger initial_balance cannot change; use a new database')
            self.db.execute("INSERT OR IGNORE INTO meta VALUES ('initial_balance',?)",(self.config.initial_balance,))
            tz=self.db.execute("SELECT value FROM meta WHERE key='timezone'").fetchone()
            if tz and tz['value']!=self.config.timezone:raise ValueError('Existing ledger timezone cannot change')
            self.db.execute("INSERT OR IGNORE INTO meta VALUES ('timezone',?)",(self.config.timezone,))
            self.db.execute('COMMIT')
        except Exception:
            self.db.execute('ROLLBACK');self.db.close();raise

    def close(self): self.db.close()

    def day(self,now): return instant(now).astimezone(ZoneInfo(self.config.timezone)).date().isoformat()

    def _totals(self,now,event_id=''):
        orders=self.db.execute('SELECT * FROM orders').fetchall()
        settlements={r['order_id']:r for r in self.db.execute('SELECT * FROM settlements')}
        spent=ZERO;reserved=ZERO;exposure=ZERO;event=ZERO;daily=ZERO
        for r in orders:
            cost=D(r['cost']);res=D(r['budget'])-cost if r['status'] in {'OPEN','PARTIAL'} else ZERO
            allocation=cost+res
            spent+=cost;reserved+=res
            if r['id'] not in settlements:exposure+=allocation
            if r['event_id']==event_id:event+=D(r['budget'])
            if r['trade_day']==self.day(now):daily+=D(r['budget'])
        payout=sum((D(r['payout']) for r in settlements.values()),ZERO)
        pnl=sum((D(r['pnl']) for r in settlements.values() if r['settle_day']==self.day(now)),ZERO)
        return dict(available=D(self.config.initial_balance)+payout-spent-reserved,reserved=reserved,spent=spent,exposure=exposure,event=event,daily=daily,daily_loss=max(ZERO,-pnl))

    def _decision(self,signal,leg_no,reason,now):
        self.db.execute('INSERT INTO decisions(signal_id,leg_no,reason,created_at) VALUES(?,?,?,?)',(signal.signal_id,leg_no,reason,instant(now).isoformat()))
        return dict(signal_id=signal.signal_id,leg_no=leg_no,reason=reason)

    def _quote_age(self,quote,now):
        quote.validate();age=(instant(now)-instant(quote.observed_at)).total_seconds()
        if not 0<=age<=self.config.max_quote_age_sec: raise ValueError('STALE_OR_FUTURE_QUOTE')

    def _register_quote(self,quote):
        payload=json.dumps(asdict(quote),sort_keys=True,ensure_ascii=False)
        old=self.db.execute('SELECT payload FROM quotes WHERE id=?',(quote.quote_id,)).fetchone()
        if old and old['payload']!=payload:raise ValueError('QUOTE_ID_REUSED_WITH_DIFFERENT_CONTENT')
        self.db.execute('INSERT OR IGNORE INTO quotes VALUES(?,?,?)',(quote.quote_id,payload,quote.ask_shares))

    def _expire(self,now):
        current=instant(now)
        old=self.db.execute("SELECT value FROM meta WHERE key='last_clock'").fetchone()
        if old and current<instant(old['value']):raise ValueError('Ledger clock cannot move backward; use a fresh replay database')
        self.db.execute("INSERT OR REPLACE INTO meta VALUES ('last_clock',?)",(current.isoformat(),))
        for r in self.db.execute("SELECT id,expires_at FROM orders WHERE status IN ('OPEN','PARTIAL')").fetchall():
            if instant(r['expires_at'])<=instant(now):self.db.execute("UPDATE orders SET status='EXPIRED' WHERE id=?",(r['id'],))

    def _fill(self,order_id,quote):
        r=self.db.execute('SELECT * FROM orders WHERE id=?',(order_id,)).fetchone()
        if r['status'] not in {'OPEN','PARTIAL'} or self.db.execute('SELECT 1 FROM fills WHERE order_id=? AND quote_id=?',(order_id,quote.quote_id)).fetchone():return
        # Existing orders preserve their original fee and increment settings.
        cfg=json.loads(r['config_json']);step=D(cfg['share_step'])
        if D(quote.ask)>D(r['limit_price']) or D(quote.fee_rate)!=D(r['fee_rate']):return
        depth=D(self.db.execute('SELECT remaining FROM quotes WHERE id=?',(quote.quote_id,)).fetchone()['remaining'])
        visible = quote.source == 'VISIBLE_BOOK_PAPER'
        consumed = ZERO
        if visible:
            previous=self.db.execute('SELECT consumed FROM visible_depth WHERE market_id=? AND price=?',(quote.market_id,str(D(quote.ask)))).fetchone()
            consumed=D(previous['consumed']) if previous else ZERO
            # Without exchange sequence IDs there is no proof of replenishment.
            # Re-reading the same displayed quantity must not create liquidity.
            depth=min(depth,max(ZERO,D(quote.ask_shares)-consumed))
        size=min(D(r['size'])-D(r['filled']),depth)
        size=(size/step).to_integral_value(rounding=ROUND_DOWN)*step
        if size<=0:return
        cost=size*D(quote.ask)*(1+D(r['fee_rate']))
        if D(r['cost'])+cost>D(r['budget']):raise ValueError('Budget invariant violated')
        filled=D(r['filled'])+size
        self.db.execute('INSERT INTO fills(order_id,quote_id,shares,price,cost) VALUES(?,?,?,?,?)',(order_id,quote.quote_id,str(size),quote.ask,str(cost)))
        self.db.execute('UPDATE quotes SET remaining=? WHERE id=?',(str(depth-size),quote.quote_id))
        if visible:self.db.execute('INSERT OR REPLACE INTO visible_depth VALUES(?,?,?)',(quote.market_id,str(D(quote.ask)),str(consumed+size)))
        self.db.execute('UPDATE orders SET status=?,filled=?,cost=? WHERE id=?',('FILLED' if filled==D(r['size']) else 'PARTIAL',str(filled),str(D(r['cost'])+cost),order_id))

    def process(self,record,now,quotes=()):
        signal=parse_message(record);out=[]
        # Only exact matches; never choose the first of ambiguous quotes.
        quotes=list(quotes)
        self.db.execute('BEGIN IMMEDIATE')
        try:
            self._expire(now)
            version=hashlib.sha256(json.dumps(record,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
            self.db.execute('INSERT OR IGNORE INTO messages VALUES(?,?,?,?)',(signal.signal_id,version,json.dumps(record,ensure_ascii=False),json.dumps(signal.as_dict(),ensure_ascii=False)))
            if signal.type!='PICK':
                out.append(self._decision(signal,0,signal.reason or signal.type,now))
            for leg in signal.legs if signal.type=='PICK' else []:
                reason=self._check(signal,leg,now)
                matches=[q for q in quotes if q.event_id==signal.event_id and q.leg_identity==leg.identity() and keytext(q.home)==keytext(signal.home) and keytext(q.away)==keytext(signal.away)]
                if not reason and len(matches)!=1:reason='NO_QUOTE' if not matches else 'AMBIGUOUS_QUOTE'
                q=matches[0] if len(matches)==1 else None
                if not reason:
                    try:self._quote_age(q,now)
                    except (ValueError,ArithmeticError):reason='INVALID_OR_STALE_QUOTE'
                if not reason:
                    cap=min(D('1')-D(self.config.price_tick),D(q.reference_price)+D(self.config.max_slippage))
                    cap=(cap/D(self.config.price_tick)).to_integral_value(rounding=ROUND_DOWN)*D(self.config.price_tick)
                    size=(D(self.config.stake_usd)/(cap*(1+D(q.fee_rate)))/D(self.config.share_step)).to_integral_value(rounding=ROUND_DOWN)*D(self.config.share_step) if cap>0 else ZERO
                    if size<D(self.config.min_shares):reason='BELOW_MIN_SIZE'
                    else:
                        self._register_quote(q)
                        cfg=json.dumps(asdict(self.config),sort_keys=True)
                        self.db.execute('''INSERT INTO orders(trade_key,signal_id,event_id,market_id,leg_identity,home,away,status,budget,limit_price,size,fee_rate,created_at,expires_at,trade_day,config_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                            (trade_key(signal,leg),signal.signal_id,signal.event_id,q.market_id,json.dumps(leg.identity()),signal.home,signal.away,'OPEN',self.config.stake_usd,str(cap),str(size),q.fee_rate,instant(now).isoformat(),(instant(now)+timedelta(seconds=self.config.limit_ttl_sec)).isoformat(),self.day(now),cfg))
                        order_id=self.db.execute('SELECT last_insert_rowid()').fetchone()[0]
                        self._fill(order_id,q)
                        reason='PAPER_'+self.db.execute('SELECT status FROM orders WHERE id=?',(order_id,)).fetchone()['status']
                out.append(self._decision(signal,leg.leg_no,reason,now))
            self.db.execute('COMMIT')
        except Exception:
            self.db.execute('ROLLBACK');raise
        return dict(signal=signal.as_dict(),decisions=out)

    def _check(self,s,l,now):
        c=self.config
        if l.status!='PARSED':return l.reason
        if not c.enabled:return 'DISABLED'
        if self.db.execute('SELECT 1 FROM orders WHERE trade_key=?',(trade_key(s,l),)).fetchone():return 'DUPLICATE_TRADE'
        if c.one_leg_per_event and self.db.execute('SELECT 1 FROM orders WHERE event_id=?',(s.event_id,)).fetchone():return 'EVENT_ALREADY_FOLLOWED'
        try:age=(instant(now)-instant(s.date)).total_seconds()
        except (ValueError,TypeError):return 'INVALID_SIGNAL_DATE'
        if age<0 or age>c.max_signal_age_sec:return 'EXPIRED_OR_FUTURE_SIGNAL'
        if s.sport=='unknown':return 'UNKNOWN_SPORT'
        if c.allowed_sports and s.sport not in c.allowed_sports:return 'SPORT_FILTER'
        if c.allowed_markets and l.market_type not in c.allowed_markets:return 'MARKET_FILTER'
        if c.allowed_leagues and keytext(s.league) not in {keytext(x) for x in c.allowed_leagues}:return 'LEAGUE_FILTER'
        if keytext(s.league) in {keytext(x) for x in c.blocked_leagues}:return 'LEAGUE_FILTER'
        if l.risk_flag=='high' and not c.follow_high_risk:return 'HIGH_RISK'
        if not D(c.min_profit_pct)<=D(l.profit_pct)<=D(c.max_profit_pct):return 'PROFIT_FILTER'
        t=self._totals(now,s.event_id);budget=D(c.stake_usd)
        pause=self.db.execute("SELECT value FROM meta WHERE key='loss_pause_day'").fetchone()
        if (pause and pause['value']==self.day(now)) or t['daily_loss']>=D(c.max_daily_loss_usd):
            self.db.execute("INSERT OR REPLACE INTO meta VALUES ('loss_pause_day',?)",(self.day(now),))
            return 'DAILY_LOSS_LIMIT'
        for name,limit in [('event',c.max_event_usd),('daily',c.max_daily_stake_usd),('exposure',c.max_exposure_usd)]:
            if t[name]+budget>D(limit):return name.upper()+'_LIMIT'
        if budget>t['available']:return 'INSUFFICIENT_BALANCE'
        return ''

    def advance(self,now,quotes=()):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            self._expire(now)
            for q in quotes:
                self._quote_age(q,now);self._register_quote(q)
                for r in self.db.execute("SELECT * FROM orders WHERE status IN ('OPEN','PARTIAL') AND event_id=? AND market_id=? ORDER BY id",(q.event_id,q.market_id)).fetchall():
                    if json.loads(r['leg_identity'])==q.leg_identity and keytext(r['home'])==keytext(q.home) and keytext(r['away'])==keytext(q.away) and instant(q.observed_at)>=instant(r['created_at']):self._fill(r['id'],q)
            self.db.execute('COMMIT')
        except Exception:self.db.execute('ROLLBACK');raise

    def cancel(self,order_id,now):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            self._expire(now)
            self.db.execute("UPDATE orders SET status='CANCELLED' WHERE id=? AND status IN ('OPEN','PARTIAL')",(order_id,))
            self.db.execute('COMMIT')
        except Exception:self.db.execute('ROLLBACK');raise

    def settle(self,order_id,payout_per_share,now,source):
        # Explicit offline fixture only. Never derive cash settlement from TG claims.
        payout_per_share=positive(payout_per_share,True)
        if payout_per_share not in {ZERO,D('1')} or not source:raise ValueError('Explicit binary outcome and source required')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            self._expire(now)
            r=self.db.execute('SELECT * FROM orders WHERE id=?',(order_id,)).fetchone()
            if not r or r['status'] in {'OPEN','PARTIAL'} or D(r['filled'])<=0:raise ValueError('Order has no closed filled position')
            old=self.db.execute('SELECT * FROM settlements WHERE order_id=?',(order_id,)).fetchone()
            payout=D(r['filled'])*payout_per_share;pnl=payout-D(r['cost'])
            if old:
                if D(old['payout'])!=payout:raise ValueError('Conflicting settlement')
            else:self.db.execute('INSERT INTO settlements VALUES(?,?,?,?,?,?)',(order_id,str(payout),str(pnl),instant(now).isoformat(),self.day(now),source))
            if self._totals(now)['daily_loss']>=D(self.config.max_daily_loss_usd):
                self.db.execute("INSERT OR REPLACE INTO meta VALUES ('loss_pause_day',?)",(self.day(now),))
            self.db.execute('COMMIT')
        except Exception:self.db.execute('ROLLBACK');raise

    def snapshot(self,now):
        return dict(mode='paper',balances={k:str(v) for k,v in self._totals(now).items() if k!='event'},orders=[dict(r) for r in self.db.execute('SELECT * FROM orders ORDER BY id')],fills=[dict(r) for r in self.db.execute('SELECT * FROM fills ORDER BY id')],settlements=[dict(r) for r in self.db.execute('SELECT * FROM settlements ORDER BY order_id')])
