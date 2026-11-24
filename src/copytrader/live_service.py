"""Desktop integration: explicit session arming, serial queue and reconciliation."""
import asyncio,csv,io,json,hashlib,uuid
from dataclasses import replace,asdict
from decimal import Decimal
from pathlib import Path
from .execution import LiveStore,live_defaults,validate_live,filters,stamp,UNCERTAIN,resolved_leg
from .browser_executor import BrowserExecutor
from .parser import parse_message,Leg
from .paper import instant
from . import position

class LiveService:
 def init_live(self):
  self.live_store=LiveStore(self.root/'live-orders.db');self.executor=BrowserExecutor(self.live_store)
  self.position_tasks=set()
  self.live_config=live_defaults();p=self.root/'live-settings.json'
  if p.exists():self.live_config.update(json.loads(p.read_text()))
  validate_live(self.live_config);self.live_armed=False;self.execution_task=None;self.execution_busy=False
  self.execution_prepared=None;self.account_view=None;self.execution_failures=0;self.last_live_poll=0;self.last_settlement_poll=0
 def live_snapshot(self):
  ledger=self.live_store.snapshot(self.settings['risk']);reserved=self.prior_test_spend()
  ledger['totals']['single_test_reserved']=reserved
  ledger['totals']['spent']=str(Decimal(ledger['totals']['spent'])+Decimal(reserved))
  return dict(armed=self.live_armed,busy=self.execution_busy,config=self.live_config,prepared=self.execution_prepared,
              account=self.account_view,ledger=ledger)
 def prior_test_spend(self):
  p=self.live_journal.latest()
  return '2' if p and p['state'] in {'COMMITTING','AWAITING_ACTIVITY','UNKNOWN','ACTIVITY_CONFIRMED','PARTIAL_ACTIVITY'} else '0'
 def live_ingest(self,record,source,version):
  s=parse_message(record)
  for o in self.live_store.orders():
   if o['signal']['signal_id']==s.signal_id and o['state'] in {'QUEUED','PREFLIGHT','READY'}:self.live_store.change(o['id'],'SKIPPED',reason='消息已编辑，不执行旧队列。')
  if s.type=='RESULT':
   ref=s.reply_url.rstrip('/').split('/')[-1] if s.reply_url else ''
   if ref.isdigit():self.live_store.db.execute('INSERT OR REPLACE INTO channel_results VALUES(?,?,?)',(str(record['channel'])+':'+ref,json.dumps(record,ensure_ascii=False),stamp()))
   return [dict(signal_id=s.signal_id,leg_no=0,reason='RESULT')]
  if not self.live_armed or not self.running or source not in {'telegram','public'}:return []
  if s.type!='PICK':
   if s.type=='UNPARSED':self.live_store.alert('频道消息未解析，未下单：'+s.signal_id)
   return [dict(signal_id=s.signal_id,leg_no=0,reason=s.reason or s.type)]
  ds=[];risk=dict(self.settings['risk'],one_leg_per_event=self.settings['one_leg_per_event']);limit=100
  for leg in s.legs:
   reason=filters(s,leg,risk,stamp())
   if not reason and sum(o['state']=='QUEUED' for o in self.live_store.orders())>=limit:reason='实盘队列已满'
   id=self.live_store.enqueue(record,leg.leg_no,risk,dict(self.live_config)) if not reason else None
   ds.append(dict(signal_id=s.signal_id,leg_no=leg.leg_no,reason=reason or ('LIVE_QUEUED' if id else 'DUPLICATE_TRADE')))
  return ds
 def live_disarm(self,reason,notify=True):
  if self.live_armed:self.running=False
  self.live_armed=False;self.execution_prepared=None
  for o in self.live_store.orders():
   if o['state'] in {'QUEUED','PREFLIGHT','READY'}:self.live_store.change(o['id'],'SKIPPED',reason=reason)
  if notify:self.live_store.alert(reason)
 async def execute_one(self,id):
  o=self.live_store.get(id);s=parse_message(o['record']);leg=resolved_leg(s,Leg(**o['leg']),o['config']);self.execution_busy=True
  try:
   if not self.live_armed:raise ValueError('实盘未启用')
   self.live_store.change(id,'PREFLIGHT',resolved_leg=asdict(leg),reason='读取真实账户与目标盘口。')
   async with self.browser_lock:
    page=await self.browser_page()
    if len([p for p in self.browser.pages if not p.is_closed()])!=1:raise ValueError('专用浏览器须只保留一个页面。')
    baseline=await self.executor.account(page,s,leg)
    await self.executor.form(page,self.live_store.get(id),baseline)
    if not self.live_armed:raise ValueError('提交前已暂停。')
    try:
     can_reconcile=await self.executor.submit(page,id,self.prior_test_spend())
    finally:
     committed = self.live_store.get(id)
     if committed.get('committed_at') and committed.get('position_target'):
      task = asyncio.create_task(self.delayed_position_check(id, committed['committed_at']))
      self.position_tasks.add(task)
      task.add_done_callback(self.position_tasks.discard)
    if can_reconcile:
     for _ in range(4):
      await asyncio.sleep(2)
      await self.executor.reconcile(page,id)
      if self.live_store.get(id)['state'] not in {'UNKNOWN','SUBMITTING'}:break
   state=self.live_store.get(id)['state']
   if state in UNCERTAIN:self.live_disarm('真实提交结果需要核对，已停止后续买入。')
   else:self.execution_failures=0
  except BaseException as exc:
   current=self.live_store.get(id)
   if current['state'] in {'QUEUED','PREFLIGHT','READY'}:
    self.live_store.change(id,'SKIPPED',reason=str(exc) if isinstance(exc,ValueError) else '提交前流程未完成，未下单。');self.execution_failures+=1
   elif current['state'] in {'SUBMITTING','UNKNOWN'}:
    self.live_store.change(id,'UNKNOWN',reason='真实提交或核对中断；禁止重复提交。');self.live_disarm('有结果不明的订单，已停止后续买入。')
   if isinstance(exc,asyncio.CancelledError):raise
   if self.execution_failures>=3:self.live_disarm('连续三次准备或执行失败，已自动暂停。')
  finally:self.execution_busy=False
 async def delayed_position_check(self, id, committed_at):
  elapsed = (instant(stamp()) - instant(committed_at)).total_seconds()
  await asyncio.sleep(max(0, 60 - elapsed))
  await self.check_position(id)

 async def check_position(self, id):
  async with self.browser_lock:
   order = self.live_store.get(id)
   target = order['position_target']
   signal = parse_message(order['record'])
   page = None
   try:
    if not self.browser:raise ValueError('专用浏览器已关闭')
    page = await self.browser.new_page()
    await page.goto(target['url'], wait_until='domcontentloaded', timeout=20000)
    after = await position.observe(page, signal, target)
   except Exception as exc:
    after = dict(error=str(exc) if isinstance(exc, ValueError) else '详情页无法读取', at=stamp())
   finally:
    if page:
     try:await page.close()
     except Exception:pass
   message = position.comparison(order.get('position_before'), after)
   self.live_store.change(id, self.live_store.get(id)['state'], position_after=after, position_summary=message)

 async def execution_loop(self):
  while self.live_armed:
   q=next((o for o in reversed(self.live_store.orders()) if o['state']=='QUEUED'),None)
   loop=asyncio.get_running_loop()
   if loop.time()-self.last_live_poll>=10:
    self.last_live_poll=loop.time()
    for o in self.live_store.orders():
     # Settled positions are revisited hourly; only explicit platform settlement counts.
     due=o['state'] in {'OPEN','PARTIAL','UNKNOWN','CANCEL_UNKNOWN'} or o['state'] in {'FILLED','CANCELLED','EXPIRED'} and loop.time()-self.last_settlement_poll>=3600
     if not due:continue
     self.execution_busy=True
     try:
      async with self.browser_lock:
       page=await self.browser_page();await self.executor.reconcile(page,o['id']);current=self.live_store.get(o['id'])
       if self.live_armed and current['state'] in {'OPEN','PARTIAL'} and current['config']['auto_cancel'] and instant(stamp())>=instant(current['expires_at']):
        await self.executor.cancel(page,o['id'])
      if self.live_store.get(o['id'])['state'] in UNCERTAIN:self.live_disarm('挂单或撤单状态不明，停止后续交易。')
     except asyncio.CancelledError:raise
     except Exception:self.live_disarm('订单跟踪失败；保留原记录，暂停后续交易。')
     finally:self.execution_busy=False
     if not self.live_armed:break
    if loop.time()-self.last_settlement_poll>=3600:self.last_settlement_poll=loop.time()
   if q and self.live_armed:await self.execute_one(q['id'])
   else:await asyncio.sleep(1)
 async def stop_execution(self):
  active=self.live_armed or any(o.get('committed_at') and o['state'] in UNCERTAIN|{'SUBMITTING','OPEN','PARTIAL','CANCEL_PENDING'} for o in self.live_store.orders())
  self.live_disarm('实盘已暂停；已有平台挂单仍需核对，暂停不等同于撤单。',notify=active)
  t=self.execution_task
  if t and t is not asyncio.current_task():
   t.cancel()
   try:await t
   except asyncio.CancelledError:pass
  self.execution_task=None
 async def live_action(self,action,data):
  from .desktop_service import atomic_json
  old=self.live_journal.latest()
  if action not in {'live_stop','live_ack_alerts','live_export'} and (self.live_busy or old and old['state']=='READY'):
   raise ValueError('请先完成或取消单次自动测试，避免两种执行器同时操作账户。')
  if action=='live_save':
   if self.live_armed or self.execution_busy:raise ValueError('请先暂停实盘，再保存参数。')
   c=dict(data.get('config',{}));validate_live(c);atomic_json(self.root/'live-settings.json',c);self.live_config=c;self.execution_prepared=None
  elif action=='live_prepare_session':
   if self.live_armed or self.execution_busy:raise ValueError('实盘正在运行。')
   await self.pause()
   if any(o['state'] in UNCERTAIN|{'SUBMITTING','CANCEL_PENDING'} for o in self.live_store.orders()):raise ValueError('存在待核对订单，不能开始新交易。')
   if self.prior_test_spend()!='0':raise ValueError('之前单次测试已占用本轮 2 USDT 额度，须先核对。')
   if self.live_store.totals(stamp(),self.settings['risk'])['spent']>=Decimal(self.live_config['session_budget']):raise ValueError('本轮实盘验收累计额度已用完。')
   self.execution_busy=True
   try:
    async with self.browser_lock:self.account_view=await self.executor.account(await self.browser_page())
    self.fg_status='已检测到登录态';self.balance='$'+self.account_view['balance']
    if Decimal(self.account_view['balance'])<=0:raise ValueError('真实可用余额不足，未准备实盘会话。')
    self.execution_prepared=dict(id=uuid.uuid4().hex,at=stamp(),digest=hashlib.sha256(json.dumps([self.settings,self.live_config],sort_keys=True).encode()).hexdigest())
   finally:self.execution_busy=False
  elif action=='live_arm':
   p=self.execution_prepared
   if self.running or self.execution_busy or self.live_armed:raise ValueError('已有会话运行，请暂停并重新准备。')
   if self.prior_test_spend()!='0':raise ValueError('单次测试已占用额度，须先核对。')
   if data.get('native_authorized') is not True or not p or data.get('plan_id')!=p['id']:raise ValueError('请在客户端亲自启用已准备的实盘会话。')
   digest=hashlib.sha256(json.dumps([self.settings,self.live_config],sort_keys=True).encode()).hexdigest()
   if digest!=p['digest'] or not 0<=(instant(stamp())-instant(p['at'])).total_seconds()<=180:raise ValueError('准备已过期或参数变化，请重新准备。')
   self.execution_prepared=None;self.live_armed=True;self.execution_failures=0
   self.running=True;self.task=asyncio.create_task(self.listen());self.execution_task=asyncio.create_task(self.execution_loop());self.addlog('用户已启用频道实盘验收会话；累计额度最多 2 USDT。')
  elif action=='live_stop':await self.stop_execution();await self.pause()
  elif action=='live_read_account':
   if self.live_armed or self.execution_busy:raise ValueError('请先暂停实盘再手动刷新账户。')
   self.execution_busy=True
   try:
    async with self.browser_lock:self.account_view=await self.executor.account(await self.browser_page())
    self.fg_status='已检测到登录态';self.balance='$'+self.account_view['balance']
   finally:self.execution_busy=False
  elif action=='live_position':
   id = str(data.get('order_id',''))
   o = self.live_store.get(id)
   if not o or not o.get('committed_at') or not o.get('position_target'):
    raise ValueError('该订单没有可核对的市场详情页。')
   await self.check_position(id)
  elif action=='live_reconcile':
   if self.live_armed or self.execution_busy:raise ValueError('请先暂停实盘再手动核对。')
   id=str(data.get('order_id',''));o=self.live_store.get(id)
   if not o or not o.get('committed_at'):raise ValueError('只能核对已提交订单。')
   self.execution_busy=True
   try:
    async with self.browser_lock:await self.executor.reconcile(await self.browser_page(),id)
   finally:self.execution_busy=False
  elif action=='live_cancel':
   if data.get('native_authorized') is not True:raise ValueError('请在客户端确认撤销这笔真实挂单。')
   if self.live_armed or self.execution_busy:raise ValueError('请先暂停实盘再手动撤单。')
   self.execution_busy=True
   try:
    async with self.browser_lock:await self.executor.cancel(await self.browser_page(),str(data.get('order_id','')))
   finally:self.execution_busy=False
  elif action=='live_ack_alerts':self.live_store.db.execute('UPDATE alerts SET read=1')
  elif action=='live_export':
   out=io.StringIO();w=csv.writer(out);w.writerow(['本机编号','平台订单号','信号','赛事','玩法','选项','方向','状态','预算','已成交份额','成交额','费用','原因'])
   for o in self.live_store.orders():
    values=[o['id'],o.get('order_id',''),o['signal']['signal_id'],o['signal']['home']+' vs '+o['signal']['away'],o['leg']['market_type'],o['leg']['selection'],o['leg']['side'],o['state'],o['budget'],o['filled'],o['cost'],o['fee'],o.get('reason','')]
    w.writerow(["'"+str(v) if str(v).startswith(('=','+','-','@')) else v for v in values])
   return dict(csv=out.getvalue())
  else:raise ValueError('不支持的实盘操作。')
  return self.snapshot()
