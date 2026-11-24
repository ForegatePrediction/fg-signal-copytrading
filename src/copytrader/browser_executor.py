"""Semantic visible-page adapter. Unknown layouts never become successful orders."""
import asyncio
from collections import Counter
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal,ROUND_DOWN
import hashlib,json,re,os
from pathlib import Path
from urllib.parse import urlsplit,parse_qs
from .parser import parse_message,Leg,keytext
from .market_observer import display_identity,validate_context,price_label,group_type
from .execution import stamp,price_cap,filters
from .paper import instant
from .live_test import submission_evidence
from . import position

D=Decimal
MARKETS={'moneyline':['Match Winner','Moneyline'],'spreads':['Spreads','Handicap'],'totals':['Totals','Total Goals','Total Points'],'map_handicap':['Game Handicap','Map Handicap'],'total_maps':['Total Games','Total Maps'],'both_teams_to_score':['Both Teams to Score','Both Teams to Score?'],'game_winner':[]}
ALIASES={'id':['Order ID','Order Id','Order No.','Order Number'],'status':['Status','Order Status'],'filled':['Filled','Filled Shares','Filled Quantity'],'size':['Shares','Size','Quantity'],'cost':['Cost','Value','Amount','Notional'],'fee':['Fee','Fees'],'market':['Market','Event'],'side':['Side','Type'],'price':['Price','Limit Price'],'payout':['Payout','Received'],'pnl':['PnL','Realized PnL','Profit / Loss'],'settled_at':['Settled At','Settlement Time']}
STATES={'open':'OPEN','pending':'OPEN','accepted':'OPEN','partially filled':'PARTIAL','partial':'PARTIAL','filled':'FILLED','cancelled':'CANCELLED','canceled':'CANCELLED','expired':'EXPIRED','rejected':'REJECTED','settled':'SETTLED'}

def readjs(name):return Path(__file__).with_name(name).read_text()
def number(text,allow_zero=True):
 s=re.sub(r'[$¢,\s]','',text)
 if not re.fullmatch(r'[+-]?(?:\d+(?:\.\d+)?|\.\d+)',s):raise ValueError('无法识别平台数字。')
 n=D(s)
 if not n.is_finite() or n<0 or not allow_zero and n==0:raise ValueError('平台数字无效。')
 return n

def market_matches(row,signal,leg):
 links=[]
 for a in row.get('links',[]):
  p=urlsplit(a.get('href',''))
  if (p.scheme or p.netloc) and (p.scheme!='https' or p.netloc!='www.foregate.com'):continue
  if p.path=='/en/event/'+signal.event_id:links.append(p)
 if len(links)!=1:return False
 text=' '.join(row['cells']);labels=MARKETS[leg.market_type] if leg.market_type!='game_winner' else [f'Game {leg.game_no} Winner',f'Map {leg.game_no} Winner']
 sel=leg.side if leg.market_type=='both_teams_to_score' else leg.selection
 if leg.line:sel+=' '+('+' if leg.market_type in {'spreads','map_handicap'} and D(leg.line)>0 else '')+leg.line
 if leg.side=='NO' and leg.market_type!='both_teams_to_score':sel+=' - NO'
 # A trailing boundary prevents TS matching TS8 or OVER 2.5 matching OVER 2.50.
 patterns=[r'(?<!\w)'+re.escape(keytext(label+' '+sel))+r'(?![\w.]|\s*-\s*no\b)' for label in labels]
 return any(re.search(p,keytext(text)) for p in patterns)

def normalize_tables(raw,signal,leg):
 if raw.get('url')!='https://www.foregate.com/en/portfolio':raise ValueError('账户页面地址不正确。')
 rows=[]
 for t in raw['tables']:
  headers=t['headers']
  if len(headers)!=len(set(headers)):raise ValueError('账户表格字段重复。')
  for row in t['rows']:
   if len(row['cells'])!=len(headers) or not market_matches(row,signal,leg):continue
   data=dict(zip(headers,row['cells']));fields={}
   for k,aliases in ALIASES.items():
    hits=[data[x] for x in aliases if x in data]
    if len(hits)>1:raise ValueError('账户字段存在歧义。')
    fields[k]=hits[0] if hits else ''
   if fields['side'].casefold() not in {'buy','买入'}:continue
   oid=fields['id'] or row.get('order_id','')
   if fields['id'] and row.get('order_id') and fields['id']!=row['order_id']:raise ValueError('订单编号字段冲突。')
   # Only an explicit Order ID field/attribute is an order identifier. Tx is not.
   if oid and not re.fullmatch(r'[A-Za-z0-9_-]{1,160}',oid):raise ValueError('订单编号格式无效。')
   item=dict(order_id=oid,state=STATES.get(fields['status'].casefold(),''),fields=fields,raw=row,table_index=t['index'],row_index=row['row_index'])
   item['fingerprint']=hashlib.sha256(json.dumps(dict(data=data,links=row['links']),sort_keys=True).encode()).hexdigest();rows.append(item)
 return rows

def reconcile(before,after,order):
 ids={r['order_id'] for r in before if r['order_id']};oid=order.get('order_id');matches=[r for r in after if r['order_id']==oid] if oid else [r for r in after if r['order_id'] and r['order_id'] not in ids]
 # Multiple fills may share an ID; require one authoritative status row.
 authoritative=[r for r in matches if r['state']]
 if len(authoritative)>1:
  unique={json.dumps(r['fields'],sort_keys=True) for r in authoritative}
  if len(unique)!=1:raise ValueError('同一订单的状态记录冲突。')
 if len({r['order_id'] for r in authoritative})>1:raise ValueError('出现多个新增订单号，不能唯一归属本次提交。')
 if authoritative:
  row=authoritative[0];f=row['fields'];out=dict(order_id=row['order_id'],state=row['state'],evidence=row)
  if not f['filled'] and out['state'] in {'FILLED','PARTIAL','CANCELLED','EXPIRED','SETTLED'}:raise ValueError('缺少已成交数量，不能确认订单终态。')
  filled=number(f['filled']) if f['filled'] else D(0)
  if filled<D(order.get('filled','0')):raise ValueError('已成交数量回退，需人工核对。')
  if out['state']=='OPEN' and filled:raise ValueError('挂单状态与非零成交份额冲突。')
  if out['state']=='PARTIAL' and (not f['size'] or not 0<filled<number(f['size'],False)):raise ValueError('部分成交份额与订单数量不一致。')
  if out['state']=='REJECTED' and filled:raise ValueError('拒绝状态与成交数量冲突。')
  if out['state']=='FILLED' and (not f['size'] or filled!=number(f['size'],False)):raise ValueError('已成交状态与委托数量不一致。')
  out['filled']=str(filled)
  if filled>0:
   if not f['cost'] or not f['fee']:raise ValueError('缺少实际成交金额或费用。')
   out.update(cost=str(number(f['cost'])),fee=str(number(f['fee'])))
   if D(out['cost'])+D(out['fee'])>D(order['budget'])+D('0.0005'):raise ValueError('实际支出超过订单预算。')
  if out['state']=='SETTLED':
   if not all(f[x] for x in ['payout','pnl','settled_at']):raise ValueError('缺少平台结算字段。')
   # Signed PnL is intentionally separate from nonnegative monetary parsing.
   pnl=D(f['pnl'].replace('$','').replace(',','').strip());instant(f['settled_at']);out['settlement']=dict(payout=str(number(f['payout'])),pnl=str(pnl),order_id=row['order_id'],platform_settled_at=f['settled_at'])
   out['state']='FILLED'
  return out
 if oid:return None
 counts=Counter(r['fingerprint'] for r in before);fresh=[]
 for row in after:
  if counts[row['fingerprint']]:counts[row['fingerprint']]-=1
  else:fresh.append(row)
 activity=[r for r in fresh if 'Value/Fees' in r['fields'] or len(r['raw']['cells'])==8 and r['fields']['side']=='Buy']
 if len(activity)==1:
  row=activity[0];data=row['raw']['cells'];money=re.fullmatch(r'\$([\d,.]+)\s+\$([\d,.]+)',data[6]) if len(data)==8 else None
  if money:
   cost=number(money[1]);fee=number(money[2]);filled=number(data[3],False)
   if cost+fee>D(order['budget'])+D('.0005'):raise ValueError('活动记录超过预算。')
   return dict(state='ACTIVITY_CONFIRMED',cost=str(cost),fee=str(fee),filled=str(filled),evidence=row,reason='已确认新增买入活动，但未取得唯一订单编号和终态；停止后续买入。')
 return None

class BrowserExecutor:
 def __init__(self,store):self.store=store
 async def wait_account(self,page):
  try:await page.wait_for_function('() => {const r=('+readjs('account_dom.js')+')();return !r.loading&&r.tables.length>0}',timeout=20000)
  except Exception:
   # Save visible, read-only evidence to diagnose a changed account layout.
   # Never inspect cookies, storage, network credentials or login inputs.
   try:
    root=Path(self.store.path).parent/'live-screenshots';root.mkdir(exist_ok=True,mode=0o700)
    raw=await page.evaluate(readjs('account_dom.js'))
    path=root/'account-read-diagnostic.json';path.write_text(json.dumps(raw,ensure_ascii=False,indent=2));os.chmod(path,0o600)
    path=root/'account-read-diagnostic.png';await page.screenshot(path=str(path),full_page=False);os.chmod(path,0o600)
   except Exception:pass
   raise ValueError('账户记录仍在加载或页面格式变化，未把占位内容当作余额或订单。') from None
 async def capture(self,page,id,step):
  root=Path(self.store.path).parent/'live-screenshots';root.mkdir(exist_ok=True,mode=0o700)
  path=root/(id+'-'+step+'.png');await page.screenshot(path=str(path),full_page=False);os.chmod(path,0o600)
  self.store.change(id,self.store.get(id)['state'],**{step+'_screenshot':str(path)})
 async def account(self,page,signal=None,leg=None):
  await page.goto('https://www.foregate.com/en/portfolio',wait_until='domcontentloaded',timeout=30000)
  await page.get_by_role('button',name=re.compile(r'^Available')).wait_for(state='visible',timeout=15000)
  raw=await page.evaluate(readjs('account_dom.js'));allrows=[];views=[]
  for label in ['Open Orders','Order History','Activity','Positions','My Positions','Closed Positions']:
   ctl=page.get_by_role('tab',name=label,exact=True)
   if await ctl.count()!=1:ctl=page.get_by_role('button',name=label,exact=True)
   if await ctl.count()!=1:continue
   await ctl.click()
   # A mounted table can still consist entirely of loading skeleton cells.
   await self.wait_account(page)
   view=await page.evaluate(readjs('account_dom.js'));views.append(dict(name=label,tables=view['tables']))
   if signal and leg:allrows.extend(normalize_tables(view,signal,leg))
  if not views:raise ValueError('未识别到账户记录页面。')
  raw=await page.evaluate(readjs('account_dom.js'))
  if raw.get('loading'):raise ValueError('账户页面仍在加载，不能读取余额或订单。')
  balance=raw['balance']
  if len(balance)!=1:raise ValueError('登录已失效或余额不可唯一读取。')
  m=re.fullmatch(r'Available\s*\$?([\d,.]+)(?:\s*USDT)?',balance[0])
  if not m:raise ValueError('真实余额格式发生变化。')
  return dict(balance=str(number(m[1])),rows=allrows,views=views,observed_at=stamp())
 async def select(self,page,signal,leg):
  await page.goto('https://www.foregate.com/en/event/'+signal.event_id,wait_until='domcontentloaded',timeout=30000)
  await page.locator('[data-outcome-button]').first.wait_for(state='visible',timeout=20000);seen=set()
  for _ in range(25):
   raw=await page.evaluate(readjs('market_dom.js'));validate_context(raw,signal)
   found=[o for o in raw['outcomes'] if display_identity(o)==leg.identity()]
   if len(found)==1:
    if not price_label(found[0]['text'])['valid']:raise ValueError('盘口报价无效或市场已结束。')
    await page.locator('[data-outcome-button]').nth(found[0]['index']).click();break
   if len(found)>1:raise ValueError('盘口存在多个匹配。')
   fp=json.dumps(raw['outcomes'],sort_keys=True)
   if fp in seen:raise ValueError('盘口档位没有变化。')
   seen.add(fp);groups=[g for g in raw['groups'] if group_type(g['group'])==leg.market_type]
   if len(groups)!=1 or not groups[0]['can_next']:raise ValueError('未找到完整匹配的玩法、档位、方向；不会推断 NO 或队名别称。')
   await page.locator('[aria-label="next line"]').nth(groups[0]['next_index']).click();await page.wait_for_timeout(150)
  else:raise ValueError('超过盘口档位扫描上限。')
  await page.get_by_role('tab',name='Buy',exact=True).wait_for(state='visible',timeout=10000)
  return price_label(found[0]['text'])
 async def inputs(self,page):return (await page.evaluate(readjs('account_dom.js')))['fields']
 async def fill_field(self,page,kind,value):
  fields=await self.inputs(page)
  pattern={'price':r'^(?:Limit\s+)?Price\b','amount':r'^Amount\b','shares':r'^(?:Shares|Quantity)\b'}[kind]
  matches=[f for f in fields if re.search(pattern,f['label'] or f['context'],re.I)]
  if len(matches)!=1:raise ValueError('无法唯一识别交易输入：'+kind)
  f=matches[0]
  if kind=='price' and not re.search(r'¢|cents|美分',f['context']+' '+f['label']):raise ValueError('未确认限价输入的价格单位。')
  await page.locator('input').nth(f['index']).fill(str(value));return f['index']
 async def order_type_control(self,page):
  matches=[]
  for control in await page.get_by_role('combobox').all():
   label=(await control.inner_text()).strip()
   if label in {'Market','Limit'}:matches.append((control,label))
  if len(matches)!=1:raise ValueError('订单类型控件不唯一。')
  return matches[0]
 async def form(self,page,order,baseline):
  s=parse_message(order['record']);l=Leg(**order.get('resolved_leg',order['leg']));c=order['config'];risk=order['risk']
  reason=filters(s,l,risk,stamp())
  if reason:raise ValueError(reason)
  price=await self.select(page,s,l);cap=price_cap(l,risk,c)
  balance=D(baseline['balance']);amount=min(D(c['amount']) if c['amount_mode']=='fixed' else balance*D(c['stake_pct'])/100,D(c['max_order_usd']))
  amount=amount.quantize(D('.01'),rounding=ROUND_DOWN)
  if amount<=0 or amount>balance:raise ValueError('余额或买入金额不足。')
  buy=page.get_by_role('tab',name='Buy',exact=True)
  if await buy.get_attribute('aria-selected')!='true':raise ValueError('当前不是 Buy，停止准备。')
  if c['order_type']=='MARKET' and D(price['price'])>cap:raise ValueError('当前价格高于提交前上限。')
  position_fields = {}
  try:
   detail = await page.evaluate(readjs('market_dom.js'))
   target = position.position_target(page.url, detail, s, l, MARKETS[l.market_type])
   position_fields['position_target'] = target
   position_fields['position_before'] = await position.observe(page, s, target)
  except ValueError as exc:
   position_fields['position_before'] = dict(error=str(exc), at=stamp())
  control,selected=await self.order_type_control(page)
  wanted='Limit' if c['order_type']=='LIMIT' else 'Market'
  if selected!=wanted:
   await control.click();option=page.get_by_role('option',name=wanted,exact=True)
   if await option.count()!=1:raise ValueError('未识别订单类型选项。')
   await option.click()
  expected={}
  if c['order_type']=='LIMIT':
   size=(amount/(cap*(1+D(c['fee_buffer'])))/D(risk['share_step'])).to_integral_value(rounding=ROUND_DOWN)*D(risk['share_step'])
   if size<D(risk['min_shares']):raise ValueError('委托数量小于最小份额。')
   expected[await self.fill_field(page,'price',cap*100)]=str(cap*100)
   expected[await self.fill_field(page,'shares',size)]=str(size)
  else:expected[await self.fill_field(page,'amount',amount)]=str(amount)
  await page.wait_for_function("() => {const b=[...document.querySelectorAll('button')].filter(n=>n.getClientRects().length&&n.innerText.trim()==='Trade');return b.length===1&&!b[0].disabled}",timeout=10000)
  prepared=dict(budget=str(amount),price_cap=str(cap),baseline=baseline['rows'],expected_fields={str(k):v for k,v in expected.items()},prepared_at=stamp(),expires_at=(instant(stamp())+timedelta(seconds=risk['limit_ttl_sec'])).isoformat(),reason='准备完成，尚未提交。')
  if self.store.get(order['id'])['state']!='PREFLIGHT':raise ValueError('准备期间订单已变更。')
  self.store.change(order['id'],'READY',**prepared,**position_fields)
  await self.verify(page,self.store.get(order['id']));await self.capture(page,order['id'],'prepared');return prepared
 async def verify(self,page,o):
  s=parse_message(o['record']);l=Leg(**o.get('resolved_leg',o['leg']));r=await page.evaluate(readjs('market_dom.js'));validate_context(r,s)
  chosen=[x for x in r['outcomes'] if x.get('selected')]
  if len(chosen)!=1 or display_identity(chosen[0])!=l.identity():raise ValueError('提交前盘口发生变化。')
  fields=await self.inputs(page);values={str(f['index']):f['value'] for f in fields}
  if any(k not in values or D(values[k])!=D(v) for k,v in o['expected_fields'].items()):raise ValueError('提交前输入金额、价格或数量变化。')
  buy=page.get_by_role('tab',name='Buy',exact=True)
  if await buy.get_attribute('aria-selected')!='true':raise ValueError('提交前方向发生变化。')
  wanted='Limit' if o['config']['order_type']=='LIMIT' else 'Market'
  _,selected=await self.order_type_control(page)
  if selected!=wanted:raise ValueError('提交前订单类型发生变化。')
  w=await page.evaluate(readjs('web_test_dom.js'))
  if len(w['panel_text'])!=1:raise ValueError('交易面板不唯一。')
  text=w['panel_text'][0]
  bal=re.search(r'Balance:\s*([\d,.]+)\s+USDT',text)
  if not bal or number(bal[1])<D(o['budget']):raise ValueError('提交前余额不足或未识别。')
  if wanted=='Market':
   avg=re.search(r'Avg Price\s+(\d+(?:\.\d+)?)¢',text)
   if not avg or not 0<D(avg[1])/100<=D(o['price_cap']):raise ValueError('平均报价超过上限或尚未就绪。')
  else:
   total=re.search(r'(?:Total Cost|Total Amount|Total)\s*\$([\d,.]+)',text)
   fee=re.search(r'(?:Trading Fee|Fee|Fees)\s*\$([\d,.]+)',text)
   if not total or not fee or not 0<number(total[1])<=D(o['budget']):raise ValueError('限价单缺少明确总支出和费用，或总支出超过预算。')
   size=next(D(v) for k,v in o['expected_fields'].items() if not any(str(f['index'])==k and re.search(r'Price',f['label'] or f['context'],re.I) for f in fields))
   if number(total[1])+D('.0005')<size*D(o['price_cap'])+number(fee[1]):raise ValueError('平台总支出未包含完整费用。')
  ctl=page.get_by_role('button',name='Trade',exact=True)
  if await ctl.count()!=1 or not await ctl.is_enabled():raise ValueError('Trade 不可用或不唯一。')
  return ctl
 async def submit(self,page,id,external_spent='0'):
  o=self.store.get(id);button=await self.verify(page,o)
  if (instant(stamp())-instant(o['prepared_at'])).total_seconds()>o['risk']['max_quote_age_sec']:raise ValueError('准备报价已过期。')
  w=await page.evaluate(readjs('web_test_dom.js'));m=re.search(r'Balance:\s*([\d,.]+)\s+USDT','\n'.join(w['panel_text']))
  if not m:raise ValueError('无法读取真实余额。')
  self.store.reserve(id,str(number(m[1])),external_spent)
  try:await button.click(timeout=10000)
  except BaseException:self.store.change(id,'UNKNOWN',reason='提交边界后中断；禁止重试。');raise
  evidence, issue = await submission_evidence(page, Path(self.store.path).parent / (id+'-submitted.png'))
  self.store.change(id,'UNKNOWN',reason=issue or '已提交一次，等待账户订单记录。',**evidence)
  return issue is None
 async def reconcile(self,page,id):
  o=self.store.get(id);s=parse_message(o['record']);l=Leg(**o.get('resolved_leg',o['leg']));a=await self.account(page,s,l);result=reconcile(o.get('baseline',[]),a['rows'],o)
  if result:
   state=result.pop('state');settlement=result.pop('settlement',None)
   if o['state']=='SETTLED' and not settlement:return result
   if o['state'] in {'CANCEL_PENDING','CANCEL_UNKNOWN'} and state in {'OPEN','PARTIAL'}:
    state='CANCEL_UNKNOWN'
    result['reason']='已发出撤单请求，仍在等待平台终态。'
   if o['state'] in {'FILLED','CANCELLED','EXPIRED','REJECTED'} and not settlement and state!=o['state']:
    raise ValueError('平台记录与已保存终态冲突。')
   if settlement:self.store.settlement(id,settlement['payout'],settlement['pnl'],settlement,result)
   else:self.store.change(id,state,**result)
   try:await self.capture(page,id,'account')
   except Exception:self.store.alert('订单证据截图未保存，保留结构化核对记录。')
  return result
 async def cancel(self,page,id):
  o=self.store.get(id)
  if o['state'] not in {'OPEN','PARTIAL'} or not o.get('order_id'):raise ValueError('只有具备唯一订单号的挂单可以撤销。')
  s=parse_message(o['record']);l=Leg(**o.get('resolved_leg',o['leg']));await self.account(page,s,l)
  # Refresh Open Orders last so the recorded row and its cancel control are current.
  ctl=page.get_by_role('tab',name='Open Orders',exact=True)
  if await ctl.count()!=1:ctl=page.get_by_role('button',name='Open Orders',exact=True)
  if await ctl.count()!=1:raise ValueError('未识别挂单列表。')
  await ctl.click();await self.wait_account(page);raw=await page.evaluate(readjs('account_dom.js'));rows=normalize_tables(raw,s,l)
  matched=[r for r in rows if r['order_id']==o['order_id'] and r['state'] in {'OPEN','PARTIAL'}]
  if len(matched)!=1:raise ValueError('未找到唯一可撤销订单；不会按列表位置猜测。')
  row=matched[0];buttons=[b for b in row['raw']['buttons'] if b['text'] in {'Cancel','Cancel Order'} and not b['disabled']]
  if len(buttons)!=1:raise ValueError('订单撤销控件不唯一。')
  table=page.locator('table').nth(row['table_index'])
  if row['raw'].get('order_id'):
   tr=table.locator('tbody tr[data-order-id="'+o['order_id']+'"]')
  else:
   headers=next(t['headers'] for t in raw['tables'] if t['index']==row['table_index'])
   column=next(i for i,h in enumerate(headers,1) if h in ALIASES['id'])
   id_cell=page.locator(f'td:nth-child({column})').filter(has_text=re.compile('^'+re.escape(o['order_id'])+'$'))
   tr=table.locator('tbody tr').filter(has=id_cell)
  if await tr.count()!=1:raise ValueError('平台订单编号未唯一定位到可撤销行。')
  button=tr.get_by_role('button',name=buttons[0]['text'],exact=True)
  if await button.count()!=1:raise ValueError('订单撤销控件已变化。')
  if self.store.get(id)['state'] not in {'OPEN','PARTIAL'}:raise ValueError('订单已变更，请重新核对。')
  self.store.change(id,'CANCEL_PENDING',reason='已记录一次撤单意图。')
  try:await button.click(timeout=10000)
  except BaseException:self.store.change(id,'CANCEL_UNKNOWN',reason='撤单结果不明，禁止重复撤单或重挂。');raise
  self.store.change(id,'CANCEL_UNKNOWN',reason='已点击一次撤单，待平台明确确认。');await self.reconcile(page,id)
