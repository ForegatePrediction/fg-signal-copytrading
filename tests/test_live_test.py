import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,patch
from copytrader.live_test import TestJournal,BrowserBuyTest,target,activity_rows,new_receipt,HEADERS,utc
from copytrader.desktop_service import Service

T=target('480143723791914715','TS8','2','85')
def raw(cost='1.960',fee='0.040',shares='2.47',selection='TS8',event=T['event_id'],stamp='2026-11-17 23:12'):
 return dict(url='https://www.foregate.com/en/portfolio',table_count=1,headers=HEADERS,rows=[dict(cells=[f'Dota 2: Team Spirit vs Aurora (BO3) - 1win Essence Playoffs Match Winner {selection}','orderbook','Buy',shares,stamp,'79.21¢',f'${cost} ${fee}',''],links=[dict(href=f'/en/event/{event}?market=moneyline&windowId=480143723791914722')])])

class ReceiptTests(unittest.TestCase):
 def test_exact_observed_activity_and_included_fee(self):
  row=activity_rows(raw(),T)[0]
  self.assertEqual(row['total'],'2.000');self.assertEqual(row['shares'],'2.47')
  self.assertEqual(new_receipt([], [row],T)['status'],'ACTIVITY_CONFIRMED')
 def test_old_activity_is_not_new(self):
  rows=activity_rows(raw(),T);self.assertIsNone(new_receipt(rows,rows,T))
 def test_identical_new_row_count_is_detected(self):
  rows=activity_rows(raw(),T);self.assertIsNotNone(new_receipt(rows,rows+rows,T))
 def test_wrong_event_or_selection_cannot_confirm(self):
  self.assertEqual(activity_rows(raw(event='999'),T),[])
  self.assertEqual(activity_rows(raw(selection='TS'),T),[])
 def test_missing_or_ambiguous_table_fails_closed(self):
  for change in [dict(table_count=0),dict(table_count=2),dict(headers=HEADERS[:-1]),dict(url='https://evil.test/en/portfolio')]:
   r=raw();r.update(change)
   with self.assertRaises(ValueError):activity_rows(r,T)
 def test_wrong_type_and_external_link_are_ignored(self):
  r=raw();r['rows'][0]['cells'][2]='Claim';self.assertEqual(activity_rows(r,T),[])
  r=raw();r['rows'][0]['links'][0]['href']='https://evil.test/en/event/'+T['event_id']+'?market=moneyline';self.assertEqual(activity_rows(r,T),[])
 def test_excess_spend_cannot_be_success(self):
  with self.assertRaises(ValueError):new_receipt([],activity_rows(raw(cost='2.000'),T),T)
 def test_partial_spend_is_not_complete(self):
  self.assertEqual(new_receipt([],activity_rows(raw(cost='0.980',fee='0.020'),T),T)['status'],'PARTIAL_ACTIVITY')
 def test_multiple_new_rows_are_ambiguous(self):
  with self.assertRaises(ValueError):new_receipt([],activity_rows(raw(),T)*2,T)
 def test_malformed_fee_or_nonpositive_shares_fail(self):
  for r in [raw(fee='NaN'),raw(shares='0'),raw(shares='-1')]:
   with self.assertRaises(ValueError):activity_rows(r,T)
 def test_target_hard_cap_and_nonfinite_values(self):
  for amount,price in [('1','85'),('2.01','85'),('NaN','85'),('2','NaN'),('2','100'),('2','0')]:
   with self.assertRaises(ValueError):target(T['event_id'],'TS8',amount,price)

class JournalTests(unittest.TestCase):
 def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'tests.db';self.j=TestJournal(self.path)
 def tearDown(self):self.j.close();self.tmp.cleanup()
 def ready(self):
  id=self.j.prepare(T);self.j.update(id,'READY',baseline=[],title='test',prepared_at=utc());return id
 def test_budget_reserved_before_click_and_never_reusable(self):
  id=self.ready();self.j.commit(id)
  with self.assertRaises(ValueError):self.j.commit(id)
  self.j.update(id,'ACTIVITY_CONFIRMED')
  with self.assertRaises(ValueError):self.j.prepare(T)
 def test_aborted_precommit_can_prepare_again(self):
  id=self.ready();self.j.update(id,'ABORTED');self.assertNotEqual(self.j.prepare(T),id)
 def test_restart_makes_prepared_plan_aborted(self):
  id=self.ready();self.j.close();self.j=TestJournal(self.path);self.assertEqual(self.j.get(id)['state'],'ABORTED')
 def test_restart_after_commit_is_unknown_and_blocked(self):
  id=self.ready();self.j.commit(id);self.j.close();self.j=TestJournal(self.path)
  self.assertEqual(self.j.get(id)['state'],'UNKNOWN')
  with self.assertRaises(ValueError):self.j.prepare(T)

class RunnerTests(unittest.IsolatedAsyncioTestCase):
 async def asyncSetUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.j=TestJournal(Path(self.tmp.name)/'tests.db');self.runner=BrowserBuyTest(self.j)
  self.id=self.j.prepare(T);self.j.update(self.id,'READY',baseline=[],title='test',prepared_at=utc())
  self.page=SimpleNamespace();self.button=SimpleNamespace(click=AsyncMock())
  self.runner.form=AsyncMock(return_value='test');self.runner.validate_form=AsyncMock(return_value=self.button)
  self.runner.post_submit_evidence=AsyncMock(return_value=False)
 async def asyncTearDown(self):self.j.close();self.tmp.cleanup()
 async def test_one_click_and_new_activity(self):
  self.runner.activity=AsyncMock(side_effect=[[],activity_rows(raw(),T)])
  with patch('copytrader.live_test.asyncio.sleep',new=AsyncMock()):await self.runner.execute(self.page,self.id)
  self.button.click.assert_awaited_once();self.assertEqual(self.j.get(self.id)['state'],'ACTIVITY_CONFIRMED')
  with self.assertRaises(ValueError):await self.runner.execute(self.page,self.id)
 async def test_click_timeout_locks_unknown_without_retry(self):
  self.runner.activity=AsyncMock(return_value=[]);self.button.click.side_effect=TimeoutError()
  with self.assertRaises(TimeoutError):await self.runner.execute(self.page,self.id)
  self.button.click.assert_awaited_once();self.assertEqual(self.j.get(self.id)['state'],'UNKNOWN')
 async def test_no_new_activity_is_unknown_not_resubmitted(self):
  self.runner.activity=AsyncMock(return_value=[])
  with patch('copytrader.live_test.asyncio.sleep',new=AsyncMock()):await self.runner.execute(self.page,self.id)
  self.button.click.assert_awaited_once();self.assertEqual(self.j.get(self.id)['state'],'UNKNOWN')
 async def test_preflight_failure_never_clicks(self):
  self.runner.activity=AsyncMock(return_value=[]);self.runner.form.side_effect=ValueError('test')
  with self.assertRaises(ValueError):await self.runner.execute(self.page,self.id)
  self.button.click.assert_not_awaited();self.assertEqual(self.j.get(self.id)['state'],'ABORTED')
 async def test_cancel_after_boundary_is_unknown(self):
  self.runner.activity=AsyncMock(return_value=[]);self.button.click.side_effect=asyncio.CancelledError()
  with self.assertRaises(asyncio.CancelledError):await self.runner.execute(self.page,self.id)
  self.assertEqual(self.j.get(self.id)['state'],'UNKNOWN')
 async def test_extra_confirmation_preserves_page_and_never_retries(self):
  self.runner.activity=AsyncMock(return_value=[]);self.runner.post_submit_evidence.return_value=True
  await self.runner.execute(self.page,self.id)
  self.button.click.assert_awaited_once();self.assertEqual(self.runner.activity.await_count,1)
  self.assertEqual(self.j.get(self.id)['state'],'UNKNOWN')
  self.assertIn('额外确认',self.j.get(self.id)['reason'])
 async def test_requires_native_enable(self):
  with tempfile.TemporaryDirectory() as d:
   s=Service(d)
   try:
    with self.assertRaises(ValueError):await s.dispatch('auto_test_execute',{'plan_id':self.id})
    self.assertIsNone(s.browser)
   finally:await s.close()

class ActivityNavigationTests(unittest.IsolatedAsyncioTestCase):
 async def test_portfolio_reloads_when_available_balance_appears_late(self):
  from playwright.async_api import TimeoutError as PlaywrightTimeoutError

  runner=BrowserBuyTest(None)
  balance=SimpleNamespace(wait_for=AsyncMock(side_effect=[PlaywrightTimeoutError('balance was not visible'),None]))
  tab=SimpleNamespace(count=AsyncMock(return_value=1),wait_for=AsyncMock(),click=AsyncMock())
  def role(kind,**kw):return tab if kw.get('name')=='Activity' else balance
  page=SimpleNamespace(goto=AsyncMock(),get_by_role=role,wait_for_function=AsyncMock(),evaluate=AsyncMock(return_value=raw()))

  self.assertEqual(len(await runner.activity(page,T)),1)
  self.assertEqual(page.goto.await_count,2)
  tab.click.assert_awaited_once()

 async def test_portfolio_with_no_available_balance_stops_before_form(self):
  with tempfile.TemporaryDirectory() as d:
   journal=TestJournal(Path(d)/'test.db');runner=BrowserBuyTest(journal)
   try:
    balance=SimpleNamespace(wait_for=AsyncMock(side_effect=TimeoutError()))
    page=SimpleNamespace(goto=AsyncMock(),get_by_role=lambda *a,**kw:balance)
    runner.form=AsyncMock()
    with self.assertRaisesRegex(ValueError,'Portfolio 加载超时'):
     await runner.prepare(page,T)
    runner.form.assert_not_awaited()
    self.assertEqual(page.goto.await_count,2)
    self.assertEqual(journal.latest()['state'],'ABORTED')
    self.assertIn('未读到可用余额',journal.latest()['reason'])
   finally:journal.close()

 async def test_hydration_retry_only_clicks_read_only_activity(self):
  with tempfile.TemporaryDirectory() as d:
   j=TestJournal(Path(d)/'test.db');runner=BrowserBuyTest(j)
   tab=SimpleNamespace(count=AsyncMock(return_value=1),wait_for=AsyncMock(),click=AsyncMock())
   balance=SimpleNamespace(wait_for=AsyncMock())
   def role(kind,**kw):return tab if kw.get('name')=='Activity' else balance
   page=SimpleNamespace(goto=AsyncMock(),get_by_role=role,wait_for_function=AsyncMock(side_effect=[TimeoutError(),None]),evaluate=AsyncMock(return_value=raw()))
   try:
    rows=await runner.activity(page,T)
    self.assertEqual(len(rows),1);self.assertEqual(tab.click.await_count,2)
    self.assertIsNone(j.latest())
   finally:j.close()

class SubmissionFeedbackTests(unittest.IsolatedAsyncioTestCase):
 async def run_feedback(self,feedback,dialogs=(),timeout=False):
  with tempfile.TemporaryDirectory() as d:
   j=TestJournal(Path(d)/'test.db');runner=BrowserBuyTest(j);id=j.prepare(T)
   page=SimpleNamespace(wait_for_function=AsyncMock(side_effect=TimeoutError() if timeout else None),evaluate=AsyncMock(return_value=dict(feedback=feedback,dialogs=list(dialogs))),screenshot=AsyncMock())
   try:
    issue=await runner.post_submit_evidence(page,id)
    self.assertEqual(page.wait_for_function.call_args.kwargs['timeout'],30000)
    self.assertEqual(j.get(id)['post_submit_feedback'],feedback)
    return issue
   finally:j.close()
 async def test_blank_feedback_keeps_details_page(self):
  self.assertIn('保留交易详情页',await self.run_feedback([],timeout=True))
 async def test_processing_feedback_keeps_details_page(self):
  self.assertIn('保留交易详情页',await self.run_feedback(['Processing order']))
 async def test_error_feedback_is_not_completion(self):
  self.assertIn('异常',await self.run_feedback(['Transaction failed']))
 async def test_extra_confirmation_is_never_accepted(self):
  self.assertIn('额外确认',await self.run_feedback([],['Confirm transaction']))
 async def test_explicit_success_permits_read_only_reconciliation(self):
  self.assertIsNone(await self.run_feedback(['Order placed successfully']))
 async def test_unloaded_activity_never_becomes_empty_baseline(self):
  with tempfile.TemporaryDirectory() as d:
   j=TestJournal(Path(d)/'test.db');runner=BrowserBuyTest(j)
   ctl=SimpleNamespace(count=AsyncMock(return_value=1),wait_for=AsyncMock(),click=AsyncMock())
   page=SimpleNamespace(goto=AsyncMock(),get_by_role=lambda *a,**kw:ctl,wait_for_function=AsyncMock(side_effect=TimeoutError()),evaluate=AsyncMock())
   try:
    with self.assertRaisesRegex(ValueError,'Activity 尚未加载'):await runner.activity(page,T)
    page.evaluate.assert_not_awaited();self.assertIsNone(j.latest())
   finally:j.close()

class FormGuardTests(unittest.IsolatedAsyncioTestCase):
 async def asyncSetUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.j=TestJournal(Path(self.tmp.name)/'test.db');self.runner=BrowserBuyTest(self.j)
  self.market=dict(url='https://www.foregate.com/en/event/'+T['event_id'],outcomes=[dict(group='Moneyline',text='TS8 76¢',selected=True)])
  self.form=dict(panel_text=['Moneyline：TS8\nBalance: 18.554 USDT\nAvg Price\n76¢'],fields=[dict(value='2')])
  self.trade=SimpleNamespace(count=AsyncMock(return_value=1),is_enabled=AsyncMock(return_value=True))
  self.buy=SimpleNamespace(get_attribute=AsyncMock(return_value='true'));self.market_control=SimpleNamespace(count=AsyncMock(return_value=1))
  def role(kind,**kw):return {'button':self.trade,'tab':self.buy,'combobox':self.market_control}[kind]
  self.page=SimpleNamespace(get_by_role=role,evaluate=AsyncMock(side_effect=lambda js:self.market if 'allButtons=' in js else self.form))
 async def asyncTearDown(self):self.j.close();self.tmp.cleanup()
 async def test_good_form_returns_single_submit_button(self):self.assertIs(await self.runner.validate_form(self.page,T),self.trade)
 async def test_changed_selection_fails(self):
  self.market['outcomes'][0]['text']='AUR1 25¢'
  with self.assertRaises(ValueError):await self.runner.validate_form(self.page,T)
 async def test_changed_event_fails(self):
  self.market['url']='https://www.foregate.com/en/event/999'
  with self.assertRaises(ValueError):await self.runner.validate_form(self.page,T)
 async def test_wrong_amount_fails(self):
  self.form['fields'][0]['value']='20'
  with self.assertRaises(ValueError):await self.runner.validate_form(self.page,T)
 async def test_unready_quote_or_insufficient_balance_fails(self):
  for text in ['Balance: 18.554 USDT\nAvg Price\n90¢','Balance: 1 USDT\nAvg Price\n76¢','Balance: 18.554 USDT\nAvg Price']:
   self.form['panel_text']=[text]
   with self.assertRaises(ValueError):await self.runner.validate_form(self.page,T)
 async def test_sell_or_disabled_trade_fails(self):
  self.buy.get_attribute.return_value='false'
  with self.assertRaises(ValueError):await self.runner.validate_form(self.page,T)
  self.buy.get_attribute.return_value='true';self.trade.is_enabled.return_value=False
  with self.assertRaises(ValueError):await self.runner.validate_form(self.page,T)
 async def test_concurrent_refresh_cannot_reset_commit_boundary(self):
  id=self.j.prepare(T);self.j.update(id,'READY');self.j.refresh_baseline(id,[]);self.j.commit(id)
  with self.assertRaises(ValueError):self.j.refresh_baseline(id,[])
  self.assertEqual(self.j.get(id)['state'],'COMMITTING')

class ServicePlanTests(unittest.IsolatedAsyncioTestCase):
 async def test_uncertain_single_test_is_visible_in_shared_budget(self):
  with tempfile.TemporaryDirectory() as d:
   s=Service(d)
   try:
    id=s.live_journal.prepare(T);s.live_journal.update(id,'READY',prepared_at=utc());s.live_journal.commit(id);s.live_journal.update(id,'UNKNOWN')
    t=s.live_snapshot()['ledger']['totals']
    self.assertEqual(t['single_test_reserved'],'2');self.assertEqual(t['spent'],'2')
   finally:await s.close()
 async def test_expired_ready_plan_is_aborted_and_can_prepare_again(self):
  with tempfile.TemporaryDirectory() as d:
   s=Service(d)
   try:
    self.assertIsNone(s.snapshot()['auto_test'])
    id=s.live_journal.prepare(T);s.live_journal.update(id,'READY',prepared_at='2000-01-01T00:00:00+00:00')
    self.assertEqual(s.snapshot()['auto_test']['state'],'ABORTED')
    self.assertNotEqual(s.live_journal.prepare(T),id)
   finally:await s.close()
 async def test_ready_plan_blocks_other_browser_navigation(self):
  with tempfile.TemporaryDirectory() as d:
   s=Service(d)
   try:
    id=s.live_journal.prepare(T);s.live_journal.update(id,'READY',prepared_at=utc())
    for action in ['start','browser_open','market_open','inspect_market','paper_rehearsal','web_test_open','web_test_prepare']:
     with self.assertRaises(ValueError):await s.dispatch(action,{})
    self.assertIsNone(s.browser)
   finally:await s.close()
