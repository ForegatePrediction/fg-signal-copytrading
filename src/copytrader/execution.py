"""Durable live orders and risk gates. No browser/network access in this module."""
from dataclasses import asdict
from datetime import datetime,timezone,timedelta
from decimal import Decimal,ROUND_DOWN
import hashlib,json,os,sqlite3,uuid
from zoneinfo import ZoneInfo
from .parser import parse_message,trade_key,keytext
from .paper import Config,instant,positive

ACTIVE={'QUEUED','PREFLIGHT','READY','SUBMITTING','UNKNOWN','OPEN','PARTIAL','CANCEL_PENDING','CANCEL_UNKNOWN','ACTIVITY_CONFIRMED'}
UNCERTAIN={'UNKNOWN','CANCEL_UNKNOWN','ACTIVITY_CONFIRMED'}
COMMITTED={'SUBMITTING','UNKNOWN','OPEN','PARTIAL','FILLED','CANCEL_PENDING','CANCEL_UNKNOWN','CANCELLED','EXPIRED','ACTIVITY_CONFIRMED','SETTLED'}
D=Decimal

def stamp():return datetime.now(timezone.utc).isoformat()
def live_defaults():
 return dict(order_type='LIMIT',amount_mode='fixed',amount='2',stake_pct='1',max_order_usd='2',
             price_source='fixed',max_price_cents='85',fee_buffer='0.02',session_budget='2',
             auto_cancel=True,notify=True,aliases={})
def validate_live(c):
 if set(c)!=set(live_defaults()):raise ValueError('实盘配置字段不完整。')
 if c['order_type'] not in {'LIMIT','MARKET'} or c['amount_mode'] not in {'fixed','percent'} or c['price_source'] not in {'fixed','signal_profit'}:raise ValueError('无效实盘策略。')
 for k in ['amount','max_order_usd','session_budget']:
  if not 0<positive(c[k])<=2:raise ValueError('本轮实盘验收单笔及累计上限均为 2 USDT。')
 if D(c['amount'])>D(c['max_order_usd']):raise ValueError('实盘固定金额不能高于单笔上限。')
 if not 0<positive(c['stake_pct'])<=100 or not 0<positive(c['max_price_cents'])<100 or not 0<=positive(c['fee_buffer'],True)<1:raise ValueError('无效比例、价格或费用缓冲。')
 if type(c['auto_cancel']) is not bool or type(c['notify']) is not bool:raise ValueError('无效开关。')
 if not isinstance(c['aliases'],dict) or len(c['aliases'])>200 or any(not isinstance(k,str) or not isinstance(v,str) or not 0<len(k)<=100 or not 0<len(v)<=100 or '\n' in k+v or v.upper().endswith((' - YES',' - NO')) for k,v in c['aliases'].items()):raise ValueError('选项映射格式无效，映射不能改变 YES/NO 方向。')
 return c

def filters(signal,leg,risk,t):
 cfg=Config(**risk)
 if not cfg.enabled:return '总开关已关闭'
 if signal.type!='PICK' or leg.status!='PARSED':return '消息未完整解析'
 try:age=(instant(t)-instant(signal.date)).total_seconds()
 except (ValueError,TypeError):return '信号时间无效'
 if not 0<=age<=cfg.max_signal_age_sec:return '信号已过期或来自未来'
 if leg.risk_flag=='high' and not cfg.follow_high_risk:return '已过滤高风险'
 for name,value in [('allowed_sports',signal.sport),('allowed_markets',leg.market_type),('allowed_leagues',signal.league)]:
  allowed=getattr(cfg,name)
  if allowed and keytext(value) not in [keytext(x) for x in allowed]:return '不在白名单'
 if keytext(signal.league) in [keytext(x) for x in cfg.blocked_leagues]:return '联赛已屏蔽'
 if not D(cfg.min_profit_pct)<=D(leg.profit_pct)<=D(cfg.max_profit_pct):return '收益率不在区间'
 return ''

def price_cap(leg,risk,c):
 if c['price_source']=='fixed':p=D(c['max_price_cents'])/100
 else:p=min(D('0.999'),1/(1+D(leg.profit_pct)/100)+D(risk['max_slippage']))
 tick=D(risk['price_tick']);return (p/tick).to_integral_value(rounding=ROUND_DOWN)*tick

def resolved_leg(signal,leg,config):
 from dataclasses import replace
 aliases=config['aliases'];value=aliases.get(signal.event_id+':'+leg.selection,aliases.get(leg.selection,leg.selection))
 return replace(leg,selection=value) if leg.market_type not in {'totals','total_maps','both_teams_to_score'} else leg

class LiveStore:
 def __init__(self,path):
  self.path=path;self.db=sqlite3.connect(str(path),isolation_level=None);self.db.row_factory=sqlite3.Row
  self.db.execute('PRAGMA journal_mode=WAL');self.db.execute('PRAGMA synchronous=FULL')
  self.db.executescript('''CREATE TABLE IF NOT EXISTS orders(id TEXT PRIMARY KEY,signal_id TEXT,leg_no INTEGER,trade_key TEXT UNIQUE,event_id TEXT,state TEXT,body TEXT,created_at TEXT,updated_at TEXT,UNIQUE(signal_id,leg_no));
  CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY,order_id TEXT,state TEXT,body TEXT,at TEXT);
  CREATE TABLE IF NOT EXISTS settlements(order_id TEXT PRIMARY KEY,pnl TEXT,payout TEXT,at TEXT,body TEXT);
  CREATE TABLE IF NOT EXISTS alerts(id INTEGER PRIMARY KEY,level TEXT,message TEXT,at TEXT,read INTEGER DEFAULT 0);
  CREATE TABLE IF NOT EXISTS channel_results(signal_id TEXT PRIMARY KEY,body TEXT,at TEXT);''')
  if str(path)!=':memory:':os.chmod(path,0o600)
  for o in self.orders():
   if o['state'] in {'QUEUED','PREFLIGHT','READY'}:self.change(o['id'],'SKIPPED',reason='程序重启；不补提交旧队列。')
   elif o['state'] in {'SUBMITTING','CANCEL_PENDING'}:self.change(o['id'],'UNKNOWN' if o['state']=='SUBMITTING' else 'CANCEL_UNKNOWN',reason='上次执行中断，只读对账后处理，不重试。')
 def get(self,id):
  r=self.db.execute('SELECT * FROM orders WHERE id=?',(id,)).fetchone()
  return dict(json.loads(r['body']),id=r['id'],state=r['state'],created_at=r['created_at'],updated_at=r['updated_at']) if r else None
 def orders(self):return [self.get(r[0]) for r in self.db.execute('SELECT id FROM orders ORDER BY rowid DESC')]
 def alert(self,message,level='warning'):self.db.execute('INSERT INTO alerts(level,message,at) VALUES(?,?,?)',(level,message,stamp()))
 def change(self,id,state,**data):
  self.db.execute('BEGIN IMMEDIATE')
  try:
   self._write_change(id,state,data)
   self.db.execute('COMMIT')
  except BaseException:self.db.execute('ROLLBACK');raise
 def _write_change(self,id,state,data):
  old=self.get(id)
  if not old:raise ValueError('订单不存在。')
  b={k:v for k,v in old.items() if k not in {'id','state','created_at','updated_at'}};b.update(data);at=stamp()
  self.db.execute('UPDATE orders SET state=?,body=?,updated_at=? WHERE id=?',(state,json.dumps(b,ensure_ascii=False),at,id))
  self.db.execute('INSERT INTO events(order_id,state,body,at) VALUES(?,?,?,?)',(id,state,json.dumps(data,ensure_ascii=False),at))
  if state in UNCERTAIN or state in {'REJECTED','SKIPPED','CANCELLED','FILLED'}:self.alert('订单 '+id[:8]+' · '+state+' · '+str(data.get('reason','')), 'error' if state in UNCERTAIN else 'info')
 def enqueue(self,record,leg_no,risk,c):
  validate_live(c);s=parse_message(record);l=next((l for l in s.legs if l.leg_no==leg_no),None)
  if not l:raise ValueError('盘口不存在。')
  id=uuid.uuid4().hex;body=dict(record=record,signal=s.as_dict(),leg=asdict(l),config=c,risk=risk,budget='0',cost='0',fee='0',filled='0',order_id=None,reason='等待串行处理')
  try:self.db.execute('INSERT INTO orders VALUES(?,?,?,?,?,?,?,?,?)',(id,s.signal_id,leg_no,trade_key(s,l),s.event_id,'QUEUED',json.dumps(body,ensure_ascii=False),stamp(),stamp()))
  except sqlite3.IntegrityError:return None
  return id
 def totals(self,t,risk,event_id=''):
  tz=ZoneInfo(risk['timezone']);day=instant(t).astimezone(tz).date();daily=event=exposure=spent=D(0)
  settled={r['order_id']:r for r in self.db.execute('SELECT * FROM settlements')}
  for o in self.orders():
   if not o.get('committed_at'):continue
   b=D(o['budget']);spent+=b
   if instant(o['committed_at']).astimezone(tz).date()==day:daily+=b
   if o['signal']['event_id']==event_id:event+=b
   if o['id'] not in settled:exposure+=b if o['state'] not in {'CANCELLED','EXPIRED','REJECTED'} else D(o.get('cost','0'))+D(o.get('fee','0'))
  pnl=sum((D(r['pnl']) for r in settled.values() if instant(r['at']).astimezone(tz).date()==day),D(0))
  return dict(daily=daily,event=event,exposure=exposure,spent=spent,pnl=pnl,loss=max(D(0),-pnl))
 def reserve(self,id,balance,external_spent='0'):
  # Final risk checks and durable submit boundary are one SQLite transaction.
  self.db.execute('BEGIN IMMEDIATE')
  try:
   o=self.get(id)
   if not o or o['state']!='READY':raise ValueError('订单不在可提交状态。')
   if any(x['id']!=id and x['state'] in UNCERTAIN|{'SUBMITTING','CANCEL_PENDING'} for x in self.orders()):raise ValueError('存在结果不明的交易，先核对。')
   s=parse_message(o['record']);l=next(x for x in s.legs if x.leg_no==o['leg']['leg_no']);reason=filters(s,l,o['risk'],stamp())
   if reason:raise ValueError(reason)
   risk=o['risk'];c=o['config'];a=positive(o['budget']);b=positive(balance,True);tot=self.totals(stamp(),risk,s.event_id)
   if a>b:raise ValueError('真实余额不足。')
   for used,limit,label in [(tot['daily'],risk['max_daily_stake_usd'],'当日'),(tot['event'],risk['max_event_usd'],'单场'),(tot['exposure'],risk['max_exposure_usd'],'未结算投入'),(tot['spent']+D(external_spent),c['session_budget'],'本轮验收')]:
    if used+a>D(limit):raise ValueError(label+'额度不足。')
   if tot['loss']>=D(risk['max_daily_loss_usd']):raise ValueError('达到当日最大亏损。')
   if risk.get('one_leg_per_event') and tot['event']>0:raise ValueError('同一赛事仅跟一项。')
   o['committed_at']=stamp();body={k:v for k,v in o.items() if k not in {'id','state','created_at','updated_at'}}
   self.db.execute("UPDATE orders SET state='SUBMITTING',body=?,updated_at=? WHERE id=?",(json.dumps(body,ensure_ascii=False),stamp(),id));self.db.execute('INSERT INTO events(order_id,state,body,at) VALUES(?,?,?,?)',(id,'SUBMITTING','{}',stamp()));self.db.execute('COMMIT')
  except BaseException:self.db.execute('ROLLBACK');raise
 def settlement(self,id,payout,pnl,evidence,receipt=None):
  self.db.execute('BEGIN IMMEDIATE')
  try:
   o=self.get(id)
   if not o or (receipt is None and o['state'] not in {'FILLED','CANCELLED','EXPIRED','SETTLED'}):raise ValueError('缺少可核对的真实订单。')
   receipt=receipt or {}
   if o.get('order_id') and receipt.get('order_id',o['order_id'])!=o['order_id']:raise ValueError('结算订单编号冲突。')
   current=dict(o,**receipt)
   if not current.get('order_id') or evidence.get('order_id')!=current['order_id'] or not evidence.get('platform_settled_at'):raise ValueError('缺少平台结算时间或订单标识。')
   at=instant(evidence['platform_settled_at']).isoformat()
   p=positive(payout,True);n=D(str(pnl));cost=D(current['cost'])+D(current['fee'])
   if not n.is_finite() or abs(p-cost-n)>D('0.005'):raise ValueError('结算金额、成本与盈亏不一致。')
   old=self.db.execute('SELECT payout,pnl FROM settlements WHERE order_id=?',(id,)).fetchone()
   if old and (D(old['payout'])!=p or D(old['pnl'])!=n):raise ValueError('平台结算记录发生冲突。')
   self.db.execute('INSERT OR IGNORE INTO settlements VALUES(?,?,?,?,?)',(id,str(n),str(p),at,json.dumps(evidence,ensure_ascii=False)))
   if not old or o['state']!='SETTLED':self._write_change(id,'SETTLED',dict(receipt,reason='按平台明确结算字段对账。'))
   self.db.execute('COMMIT')
  except BaseException:self.db.execute('ROLLBACK');raise
 def statistics(self,risk):
  groups={}
  for row in self.db.execute('SELECT * FROM settlements'):
   o=self.get(row['order_id']);day=instant(row['at']).astimezone(ZoneInfo(risk['timezone'])).date().isoformat()
   for kind,key in [('按天',day),('按玩法',o['leg']['market_type']),('按联赛',o['signal']['league'])]:
    g=groups.setdefault((kind,key),dict(kind=kind,key=key,count=0,wins=0,pnl=D(0)));g['count']+=1;g['wins']+=D(row['pnl'])>0;g['pnl']+=D(row['pnl'])
  return [dict(g,pnl=str(g['pnl']),win_rate=str(round(100*g['wins']/g['count'],2))) for g in groups.values()]
 def snapshot(self,risk):
  orders=self.orders()
  for o in orders:
   report=self.db.execute('SELECT body FROM channel_results WHERE signal_id=?',(o['signal']['signal_id'],)).fetchone()
   o['channel_report']=json.loads(report[0])['text'] if report else None
  return dict(orders=[{k:v for k,v in o.items() if k not in {'baseline','record','risk'}} for o in orders[:250]],
    totals={k:str(v) for k,v in self.totals(stamp(),risk).items()},statistics=self.statistics(risk),
    alerts=[dict(r) for r in self.db.execute('SELECT * FROM alerts ORDER BY id DESC LIMIT 100')])
 def close(self):self.db.close()
