import asyncio,json,tempfile,unittest,sqlite3
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime,timezone,timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
from copytrader.execution import LiveStore,live_defaults,validate_live,filters,price_cap,stamp,resolved_leg
from copytrader.browser_executor import normalize_tables,reconcile,market_matches,BrowserExecutor
from copytrader.parser import parse_message,Leg
from copytrader.paper import Config
from copytrader.market_observer import display_identity
from copytrader.desktop_service import Service,defaults

RISK=asdict(Config(stake_usd='2'))
def record(id=1,leg='Moneyline: ALP',date=None,event='123'):
 return dict(channel='AITradeCopy',message_id=id,date=date or stamp(),text='⚽ Demo: Alpha vs Beta\n🟢 '+leg+' (+100%)\nhttps://www.foregate.com/en/event/'+event,links=[])
def table(status='Open',id='oid1',filled='0',size='4',cost='0',fee='0',label='Match Winner ALP'):
 headers=['Market','Type','Order ID','Status','Filled','Shares','Cost','Fee']
 row=dict(row_index=0,cells=[label,'Buy',id,status,filled,size,cost,fee],links=[dict(href='/en/event/123?market=moneyline&windowId=9')],buttons=[dict(text='Cancel',disabled=False)],order_id='')
 return dict(url='https://www.foregate.com/en/portfolio',tables=[dict(index=2,headers=headers,rows=[row])])
class StoreTests(unittest.TestCase):
 def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'live.db';self.s=LiveStore(self.path);self.c=live_defaults()
 def tearDown(self):self.s.close();self.tmp.cleanup()
 def ready(self,id=1,event='123'):
  i=self.s.enqueue(record(id,event=event),1,RISK,self.c);self.s.change(i,'READY',budget='2',filled='0',cost='0',fee='0');return i
 def test_edit_does_not_create_second_order(self):
  i=self.ready();r=record();r['text']=r['text'].replace('ALP','BET');self.assertIsNone(self.s.enqueue(r,1,RISK,self.c))
 def test_same_trade_in_second_message_is_deduped(self):
  self.ready();self.assertIsNone(self.s.enqueue(record(2),1,RISK,self.c))
 def test_reserve_twice_never_repeats(self):
  i=self.ready();self.s.reserve(i,'10')
  with self.assertRaises(ValueError):self.s.reserve(i,'10')
 def test_global_acceptance_budget_durable(self):
  i=self.ready();self.s.reserve(i,'10');self.s.change(i,'FILLED');j=self.ready(2,'456')
  with self.assertRaisesRegex(ValueError,'验收'):self.s.reserve(j,'10')
 def test_single_test_and_channel_share_budget(self):
  i=self.ready()
  with self.assertRaisesRegex(ValueError,'验收'):self.s.reserve(i,'10','2')
  self.assertEqual(self.s.get(i)['state'],'READY')
 def test_balance_and_each_risk_cap_reject_before_commit(self):
  for key in ['max_event_usd','max_daily_stake_usd','max_exposure_usd']:
   i=self.ready();risk=dict(RISK);risk[key]='1';self.s.change(i,'READY',risk=risk)
   with self.assertRaises(ValueError):self.s.reserve(i,'10')
   self.assertNotIn('committed_at',self.s.get(i));self.s.db.execute('DELETE FROM orders')
  i=self.ready()
  with self.assertRaisesRegex(ValueError,'余额'):self.s.reserve(i,'1')
 def test_unknown_order_prevents_new_submit(self):
  i=self.ready();self.s.reserve(i,'10');self.s.change(i,'UNKNOWN');j=self.ready(2,'456')
  with self.assertRaisesRegex(ValueError,'不明'):self.s.reserve(j,'10')
 def test_restart_cannot_replay_preflight_or_submit(self):
  i=self.ready();j=self.ready(2,'456');self.s.reserve(i,'10');self.s.close();self.s=LiveStore(self.path)
  self.assertEqual(self.s.get(i)['state'],'UNKNOWN');self.assertEqual(self.s.get(j)['state'],'SKIPPED')
 def test_cancel_unknown_survives_restart(self):
  i=self.ready();self.s.change(i,'CANCEL_PENDING');self.s.close();self.s=LiveStore(self.path);self.assertEqual(self.s.get(i)['state'],'CANCEL_UNKNOWN')
 def test_cancelled_partial_keeps_cost_in_exposure(self):
  i=self.ready();self.s.reserve(i,'10');self.s.change(i,'CANCELLED',cost='.98',fee='.02')
  self.assertEqual(self.s.totals(stamp(),RISK)['exposure'],Decimal('1'))
 def test_settlement_requires_id_and_platform_time(self):
  i=self.ready();self.s.change(i,'FILLED',cost='1.96',fee='.04')
  with self.assertRaises(ValueError):self.s.settlement(i,'4','2',{})
 def test_verified_settlement_statistics_and_duplicate(self):
  i=self.ready();self.s.reserve(i,'10');self.s.change(i,'FILLED',order_id='oid',cost='1.96',fee='.04');e=dict(order_id='oid',platform_settled_at=stamp())
  self.s.settlement(i,'4','2',e);self.s.settlement(i,'4','2',e)
  self.assertEqual(len(self.s.statistics(RISK)),3);self.assertEqual(self.s.statistics(RISK)[0]['count'],1);self.assertEqual(self.s.totals(stamp(),RISK)['exposure'],0)
 def test_conflicting_settlement_and_bad_arithmetic_rejected(self):
  i=self.ready();self.s.change(i,'FILLED',order_id='oid',cost='1.96',fee='.04');e=dict(order_id='oid',platform_settled_at=stamp())
  with self.assertRaises(ValueError):self.s.settlement(i,'4','9',e)
  self.s.settlement(i,'4','2',e)
  with self.assertRaises(ValueError):self.s.settlement(i,'5','3',e)
 def test_live_configuration_limits_and_nonfinite(self):
  for key,value in [('amount','3'),('session_budget','3'),('fee_buffer','NaN'),('max_price_cents','100')]:
   c=live_defaults();c[key]=value
   with self.assertRaises(ValueError):validate_live(c)
 def test_age_highrisk_and_filters(self):
  s=parse_message(record());l=s.legs[0];self.assertEqual(filters(s,l,RISK,stamp()),'')
  s.date=(datetime.now(timezone.utc)-timedelta(hours=1)).isoformat();self.assertTrue(filters(s,l,RISK,stamp()))
  s.date=stamp();l.risk_flag='high';self.assertTrue(filters(s,l,RISK,stamp()))
 def test_price_source_and_tick_rounding(self):
  l=parse_message(record()).legs[0];c=live_defaults();self.assertEqual(price_cap(l,RISK,c),Decimal('.85'));c['price_source']='signal_profit';self.assertEqual(price_cap(l,RISK,c),Decimal('.53'))

class AccountTests(unittest.TestCase):
 def setUp(self):self.signal=parse_message(record());self.leg=self.signal.legs[0];self.order=dict(budget='2',filled='0',order_id=None)
 def rows(self,r):return normalize_tables(r,self.signal,self.leg)
 def test_explicit_platform_order_status(self):
  out=reconcile([],self.rows(table()),self.order);self.assertEqual(out['state'],'OPEN');self.assertEqual(out['order_id'],'oid1')
 def test_existing_order_is_not_new(self):
  rows=self.rows(table());self.assertIsNone(reconcile(rows,rows,self.order))
 def test_two_new_order_ids_are_ambiguous(self):
  with self.assertRaises(ValueError):reconcile([],self.rows(table())+self.rows(table(id='oid2')),self.order)
 def test_filled_requires_consistent_size_cost_and_fee(self):
  r=table(status='Filled',filled='4',cost='1.96',fee='.04');self.assertEqual(reconcile([],self.rows(r),self.order)['state'],'FILLED')
  for key,value in [(4,'3'),(6,'3'),(7,'')]:
   bad=deepcopy(r);bad['tables'][0]['rows'][0]['cells'][key]=value
   with self.assertRaises(ValueError):reconcile([],self.rows(bad),self.order)
 def test_partial_fill_cannot_go_backwards(self):
  o=dict(self.order,order_id='oid1',filled='2')
  with self.assertRaises(ValueError):reconcile([],self.rows(table(status='Partially Filled',filled='1',cost='.49',fee='.01')),o)
 def test_absence_is_not_cancel_confirmation(self):self.assertIsNone(reconcile([],[],dict(self.order,order_id='oid1')))
 def test_wrong_event_selection_host_and_side_do_not_match(self):
  for mutate in [lambda r:r['links'][0].update(href='/en/event/999'),lambda r:r['links'][0].update(href='https://evil.test/en/event/123'),lambda r:r['cells'].__setitem__(0,'Match Winner ALP8'),lambda r:r['cells'].__setitem__(1,'Sell')]:
   raw=table();mutate(raw['tables'][0]['rows'][0]);self.assertEqual(self.rows(raw),[])
 def test_activity_without_order_id_is_not_terminal(self):
  raw=table();raw['tables'][0]['headers']=['Market','Market Type','Type','Shares','Date','Price','Value/Fees','Tx'];raw['tables'][0]['rows'][0]['cells']=['Match Winner ALP','orderbook','Buy','3.9','2026-11-18 12:00','50¢','$1.960 $0.040','']
  out=reconcile([],self.rows(raw),self.order);self.assertEqual(out['state'],'ACTIVITY_CONFIRMED');self.assertNotIn('order_id',out)
 def test_all_seven_markets_match_full_semantics(self):
  cases=[('Moneyline: ALP','Match Winner ALP'),('Spreads: ALP -1.5','Spreads ALP -1.5'),('Totals: OVER 2.5','Totals OVER 2.5'),('Map Handicap: ALP +1.5','Game Handicap ALP +1.5'),('Total Maps: UNDER 2.5','Total Games UNDER 2.5'),('Both Teams to Score: NO','Both Teams to Score NO'),('Game 2 Winner: ALP','Game 2 Winner ALP')]
  for text,label in cases:
   s=parse_message(record(leg=text));row=table(label=label)['tables'][0]['rows'][0];self.assertTrue(market_matches(row,s,s.legs[0]),text)
 def test_explicit_no_never_maps_to_opponent(self):
  s=parse_message(record(leg='Moneyline: ALP - NO'));l=s.legs[0]
  self.assertNotEqual(display_identity(dict(group='Moneyline',text='BET 50¢')),l.identity());self.assertEqual(display_identity(dict(group='Moneyline',text='ALP - NO 50¢')),l.identity())
 def test_explicit_opposite_side_and_network_path_are_not_matches(self):
  self.assertEqual(self.rows(table(label='Match Winner ALP - NO')),[])
  raw=table();raw['tables'][0]['rows'][0]['links'][0]['href']='//other.test/en/event/123'
  self.assertEqual(self.rows(raw),[])
 def test_order_id_is_not_transaction_hash(self):
  raw=table();raw['tables'][0]['headers'][2]='Tx';rows=self.rows(raw);self.assertFalse(rows[0]['order_id'])

class IntegrationTests(unittest.IsolatedAsyncioTestCase):
 async def asyncSetUp(self):self.tmp=tempfile.TemporaryDirectory();self.s=Service(self.tmp.name)
 async def asyncTearDown(self):await self.s.close();self.tmp.cleanup()
 async def test_no_arming_without_native_confirmation_or_plan(self):
  with self.assertRaises(ValueError):await self.s.dispatch('live_arm',{})
  self.assertFalse(self.s.live_armed);self.assertIsNone(self.s.browser)
 async def test_paper_ingest_never_enqueues_live(self):
  self.s.running=True;self.s.ingest(record(),'public');self.assertEqual(self.s.live_store.orders(),[])
 async def test_armed_ingest_enqueues_separate_real_orders(self):
  self.s.running=True;self.s.live_armed=True;self.s.ingest(record(),'public');self.assertEqual(self.s.live_store.orders()[0]['state'],'QUEUED');self.assertEqual(self.s.engine.snapshot(stamp())['orders'],[])
 async def test_stale_signal_and_samples_never_queue(self):
  self.s.running=True;self.s.live_armed=True;r=record(date='2026-01-01T00:00:00Z');self.s.ingest(r,'public');self.s.ingest(record(2),'sample',True);self.assertEqual(self.s.live_store.orders(),[])
 async def test_pause_skips_queued_orders_and_disarms(self):
  self.s.running=True;self.s.live_armed=True;self.s.ingest(record(),'public');await self.s.dispatch('pause',{});self.assertFalse(self.s.live_armed);self.assertEqual(self.s.live_store.orders()[0]['state'],'SKIPPED')
 async def test_browser_and_settings_locked_while_live(self):
  self.s.live_armed=True
  for action in ['browser_open','inspect_market','auto_test_prepare','save_settings']:
   with self.assertRaises(ValueError):await self.s.dispatch(action,{})
 async def test_saved_poll_interval_persists(self):
  c=defaults();c['poll_interval_sec']=60;await self.s.dispatch('save_settings',c);self.assertEqual(self.s.settings['poll_interval_sec'],60)
  c['poll_interval_sec']=0
  with self.assertRaises(ValueError):await self.s.dispatch('save_settings',c)
 async def test_prepare_does_not_submit_or_start_listener(self):
  self.s.browser_page=AsyncMock(return_value=object());self.s.executor.account=AsyncMock(return_value=dict(balance='10',views=[],rows=[],observed_at=stamp()))
  result=await self.s.dispatch('live_prepare_session',{});self.assertIsNotNone(result['execution']['prepared']);self.assertFalse(self.s.running);self.assertFalse(self.s.live_armed);self.assertEqual(self.s.live_store.orders(),[])
 async def test_single_test_and_live_cannot_prepare_at_once(self):
  i=self.s.live_journal.prepare(dict(event_id='123'));self.s.live_journal.update(i,'READY',prepared_at=stamp())
  with self.assertRaises(ValueError):await self.s.dispatch('live_prepare_session',{})
 async def test_edit_invalidates_preparing_live_order(self):
  self.s.running=True;self.s.live_armed=True;r=record();self.s.ingest(r,'public')
  order=self.s.live_store.orders()[0];self.s.live_store.change(order['id'],'PREFLIGHT')
  self.s.ingest(dict(r,text=r['text'].replace('ALP','BET')),'public')
  self.assertEqual(self.s.live_store.get(order['id'])['state'],'SKIPPED')
 async def test_operational_logs_survive_restart(self):
  self.s.addlog('测试记录','warning')
  await self.s.close();self.s=Service(self.tmp.name)
  self.assertTrue(any(x['message']=='测试记录' and x['level']=='warning' for x in self.s.snapshot()['logs']))
 async def test_raw_errors_cannot_trigger_retry(self):
  self.s.running=True;self.s.live_armed=True;self.s.ingest(record(),'public');o=self.s.live_store.orders()[0]
  self.s.browser_page=AsyncMock(side_effect=TimeoutError());await self.s.execute_one(o['id']);self.assertEqual(self.s.live_store.get(o['id'])['state'],'SKIPPED');self.assertNotIn('committed_at',self.s.live_store.get(o['id']))
 async def test_csv_is_separate_and_formula_safe(self):
  r=record();r['text']=r['text'].replace('Demo: Alpha','Demo: =Alpha');self.s.live_store.enqueue(r,1,RISK,live_defaults());result=await self.s.dispatch('live_export',{});self.assertIn("'=Alpha",result['csv'])

class BoundaryTests(unittest.IsolatedAsyncioTestCase):
 async def asyncSetUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.store=LiveStore(Path(self.tmp.name)/'live.db');self.runner=BrowserExecutor(self.store)
  self.id=self.store.enqueue(record(),1,RISK,live_defaults());self.store.change(self.id,'READY',budget='2',prepared_at=stamp())
  self.button=SimpleNamespace(click=AsyncMock());self.runner.verify=AsyncMock(return_value=self.button)
  self.page=SimpleNamespace(evaluate=AsyncMock(return_value=dict(panel_text=['Balance: 10 USDT'])))
 async def asyncTearDown(self):self.store.close();self.tmp.cleanup()
 async def test_submit_click_occurs_after_durable_reservation(self):
  async def click(**kw):self.assertEqual(self.store.get(self.id)['state'],'SUBMITTING')
  self.button.click.side_effect=click;await self.runner.submit(self.page,self.id);self.button.click.assert_awaited_once();self.assertEqual(self.store.get(self.id)['state'],'UNKNOWN')
 async def test_timeout_after_boundary_never_retries(self):
  self.button.click.side_effect=TimeoutError()
  with self.assertRaises(TimeoutError):await self.runner.submit(self.page,self.id)
  self.assertEqual(self.store.get(self.id)['state'],'UNKNOWN');self.button.click.assert_awaited_once()
  with self.assertRaises(ValueError):await self.runner.submit(self.page,self.id)
  self.button.click.assert_awaited_once()
 async def test_dialog_holds_page_and_persists_evidence(self):
  self.page.evaluate.return_value=dict(panel_text=['Balance: 10 USDT'],dialogs=['Confirm order'])
  self.assertFalse(await self.runner.submit(self.page,self.id))
  order=self.store.get(self.id)
  self.assertEqual(order['post_submit_dialogs'],['Confirm order'])
  self.assertEqual(order['state'],'UNKNOWN')
  self.button.click.assert_awaited_once()
 async def test_open_row_after_cancel_does_not_enable_another_cancel(self):
  self.store.change(self.id,'CANCEL_UNKNOWN',order_id='oid1')
  signal=parse_message(record());rows=normalize_tables(table(),signal,signal.legs[0])
  self.runner.account=AsyncMock(return_value=dict(rows=rows));self.runner.capture=AsyncMock()
  await self.runner.reconcile(self.page,self.id)
  self.assertEqual(self.store.get(self.id)['state'],'CANCEL_UNKNOWN')
  with self.assertRaises(ValueError):await self.runner.cancel(self.page,self.id)
 async def test_invalid_settlement_leaves_original_order_intact(self):
  self.store.change(self.id,'PARTIAL',order_id='oid1',filled='1')
  with self.assertRaises(ValueError):
   self.store.settlement(self.id,'2','2',dict(order_id='oid1',platform_settled_at=stamp()),dict(cost='1',fee='0',filled='2'))
  self.assertEqual(self.store.get(self.id)['state'],'PARTIAL')
  self.assertEqual(self.store.get(self.id)['filled'],'1')
  self.assertEqual(self.store.statistics(RISK),[])
 async def test_stale_preparation_does_not_submit(self):
  self.store.change(self.id,'READY',prepared_at='2026-01-01T00:00:00+00:00')
  with self.assertRaises(ValueError):await self.runner.submit(self.page,self.id)
  self.button.click.assert_not_awaited()
 async def test_invalid_form_never_crosses_boundary(self):
  self.runner.verify.side_effect=ValueError('changed amount')
  with self.assertRaises(ValueError):await self.runner.submit(self.page,self.id)
  self.assertEqual(self.store.get(self.id)['state'],'READY');self.button.click.assert_not_awaited()
 async def test_cancel_requires_unique_platform_order_and_live_open_state(self):
  for state,oid in [('UNKNOWN','id'),('OPEN',None),('FILLED','id')]:
   self.store.change(self.id,state,order_id=oid)
   with self.assertRaises(ValueError):await self.runner.cancel(self.page,self.id)
  self.page.evaluate.assert_not_awaited()
 async def test_cancel_rechecks_row_before_click(self):
  self.store.change(self.id,'OPEN',order_id='real1');self.runner.account=AsyncMock()
  ctl=SimpleNamespace(count=AsyncMock(return_value=1),click=AsyncMock());self.page.get_by_role=lambda *a,**k:ctl;self.page.wait_for_timeout=AsyncMock();self.page.wait_for_function=AsyncMock();self.page.evaluate.return_value=table(id='other')
  with self.assertRaises(ValueError):await self.runner.cancel(self.page,self.id)
  self.assertEqual(self.store.get(self.id)['state'],'OPEN')
 async def test_activity_does_not_turn_into_order_terminal(self):
  raw=table();raw['tables'][0]['headers']=['Market','Market Type','Type','Shares','Date','Price','Value/Fees','Tx'];raw['tables'][0]['rows'][0]['cells']=['Match Winner ALP','orderbook','Buy','3.9','2026-11-18 12:00','50¢','$1.960 $0.040','']
  signal=parse_message(record());rows=normalize_tables(raw,signal,signal.legs[0]);self.runner.account=AsyncMock(return_value=dict(rows=rows));self.runner.capture=AsyncMock()
  self.store.change(self.id,'UNKNOWN',baseline=[]);await self.runner.reconcile(self.page,self.id);self.assertEqual(self.store.get(self.id)['state'],'ACTIVITY_CONFIRMED');self.assertIsNone(self.store.get(self.id)['order_id'])
 async def test_alias_is_explicit_event_scoped_and_preserves_side(self):
  signal=parse_message(record(leg='Moneyline: ALP - NO'));c=live_defaults();c['aliases']={'ALP':'ALP8','123:ALP':'ALP9'}
  leg=resolved_leg(signal,signal.legs[0],c);self.assertEqual(leg.selection,'ALP9');self.assertEqual(leg.side,'NO');self.assertEqual(signal.legs[0].selection,'ALP')
 async def test_alias_cannot_encode_direction(self):
  c=live_defaults();c['aliases']={'ALP':'BET - NO'}
  with self.assertRaises(ValueError):validate_live(c)

class LoadingTests(unittest.IsolatedAsyncioTestCase):
 async def test_read_account_updates_real_connection_status(self):
  with tempfile.TemporaryDirectory() as d:
   s=Service(d);s.browser_page=AsyncMock(return_value=object());s.executor.account=AsyncMock(return_value=dict(balance='21.029',rows=[],views=[],observed_at=stamp()))
   try:
    await s.dispatch('live_read_account',{})
    self.assertEqual(s.fg_status,'已检测到登录态');self.assertEqual(s.balance,'$21.029');self.assertFalse(s.live_armed)
   finally:await s.close()
 async def test_closing_unused_workspace_does_not_claim_pending_orders(self):
  with tempfile.TemporaryDirectory() as d:
   s=Service(d)
   await s.close()
   db=sqlite3.connect(Path(d)/'live-orders.db')
   try:self.assertEqual(db.execute('SELECT count(*) FROM alerts').fetchone()[0],0)
   finally:db.close()
 async def test_loading_timeout_becomes_explicit_failure(self):
  store=LiveStore(':memory:');runner=BrowserExecutor(store);page=SimpleNamespace(wait_for_function=AsyncMock(side_effect=TimeoutError()))
  try:
   with self.assertRaisesRegex(ValueError,'占位'):await runner.wait_account(page)
  finally:store.close()
 async def test_zero_real_balance_does_not_prepare_session(self):
  with tempfile.TemporaryDirectory() as d:
   s=Service(d);s.browser_page=AsyncMock(return_value=object());s.executor.account=AsyncMock(return_value=dict(balance='0',rows=[],views=[],observed_at=stamp()))
   try:
    with self.assertRaisesRegex(ValueError,'余额'):await s.dispatch('live_prepare_session',{})
    self.assertIsNone(s.execution_prepared);self.assertFalse(s.live_armed)
   finally:await s.close()
