import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from unittest.mock import AsyncMock
from types import SimpleNamespace
import csv
import io
from copytrader.desktop_service import Service,defaults

class DesktopTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.s=Service(self.root)
    async def asyncTearDown(self):
        await self.s.close();self.temp.cleanup()
    async def test_settings_persist_and_restart(self):
        cfg=defaults();cfg['risk']['stake_usd']='3';cfg['risk']['initial_balance']='600'
        result=await self.s.dispatch('save_settings',cfg)
        self.assertEqual(result['settings']['risk']['stake_usd'],'3')
        second=Service(self.root)
        try:self.assertEqual(second.snapshot()['ledger']['balances']['available'],'600')
        finally:await second.close()
    async def test_old_poll_default_migrates_and_later_choice_persists(self):
        self.assertEqual(self.s.settings['poll_interval_sec'],10)
        cfg=dict(defaults(),poll_interval_sec=60,channel='AnotherChannel')
        await self.s.dispatch('save_settings',cfg)
        self.s.engine.db.execute("DELETE FROM meta WHERE key='poll_default_10s'")
        await self.s.close()
        self.s=Service(self.root)
        self.assertEqual(self.s.settings['channel'],'AnotherChannel')
        self.assertEqual(self.s.settings['poll_interval_sec'],10)
        self.assertEqual(json.loads((self.root/'settings.json').read_text())['poll_interval_sec'],10)
        await self.s.dispatch('save_settings',cfg)
        await self.s.close()
        self.s=Service(self.root)
        self.assertEqual(self.s.settings['poll_interval_sec'],60)

    async def test_invalid_settings_not_committed(self):
        cfg=defaults();cfg['risk']['stake_usd']='-5'
        with self.assertRaises(ValueError):await self.s.dispatch('save_settings',cfg)
        self.assertEqual(self.s.settings['risk']['stake_usd'],'5')
    async def test_hash_never_persisted_or_exposed(self):
        await self.s.dispatch('credentials',{'api_hash':'a'*32})
        self.assertTrue(self.s.snapshot()['settings']['api_hash_set'])
        self.assertNotIn('a'*32,json.dumps(self.s.snapshot()))
        cfg=defaults();cfg['api_hash']='a'*32
        with self.assertRaises(ValueError):await self.s.dispatch('save_settings',cfg)
        self.assertFalse((self.root/'settings.json').exists())
    async def test_live_is_not_silently_paper(self):
        cfg=defaults();cfg['mode']='live'
        with self.assertRaisesRegex(ValueError,'实盘'):await self.s.dispatch('save_settings',cfg)
        self.assertFalse(self.s.running)
    async def test_sample_and_demo_do_not_modify_main_ledger(self):
        s=await self.s.dispatch('import_samples',{})
        self.assertEqual(len(s['signals']),200)
        await self.s.dispatch('import_samples',{})
        self.assertEqual(len(self.s.snapshot()['signals']),200)
        await self.s.dispatch('demo',{})
        self.assertTrue(self.s.demo_orders)
        self.assertEqual(self.s.snapshot()['ledger']['orders'],[])
        self.assertEqual(self.s.snapshot()['ledger']['balances']['available'],'1000')
    async def test_missing_auth_stays_actionable(self):
        with self.assertRaisesRegex(ValueError,'手机号'):await self.s.dispatch('tg_send_code',{})
        with self.assertRaisesRegex(ValueError,'先打开'):await self.s.dispatch('browser_check',{})
    async def test_channel_and_mode_validation(self):
        for bad in ['../evil','https://example.org','AITradeCopy?x=y']:
            cfg=defaults();cfg['channel']=bad
            with self.assertRaises(ValueError):await self.s.dispatch('save_settings',cfg)
    async def test_no_network_for_status_or_import(self):
        with patch('urllib.request.urlopen',side_effect=AssertionError('Unexpected network')):
            await self.s.dispatch('status',{})
            await self.s.dispatch('import_samples',{})
    async def test_csv_formula_injection_escaped(self):
        r=dict(channel='TEST',message_id=1,date='2026-11-17T02:00:00Z',text='⚽ Demo: =malicious vs Beta\n🟢 Moneyline: ALP (+100%)\nhttps://foregate.com/en/event/123',links=[])
        self.s.ingest(r,'sample',True)
        csv=(await self.s.dispatch('export',{}))['csv']
        self.assertIn("'=malicious",csv)
    async def test_saved_settings_are_ledger_atomic(self):
        cfg=defaults();cfg['risk']['initial_balance']='750'
        with patch('copytrader.desktop_service.atomic_json',side_effect=OSError('snapshot denied')):
            await self.s.dispatch('save_settings',cfg)
        other=Service(self.root)
        try:self.assertEqual(other.snapshot()['ledger']['balances']['available'],'750')
        finally:await other.close()
    async def test_export_includes_records_beyond_ui_limit(self):
        for i in range(260):
            self.s.ingest(dict(channel='TEST',message_id=i,date='2026-11-17T02:00:00Z',text='普通公告',links=[]),'sample',True)
        self.assertEqual(len(self.s.snapshot()['signals']),250)
        self.assertEqual(self.s.snapshot()['signal_counts']['sample'],260)
        result=await self.s.dispatch('export',{})
        self.assertEqual(len(list(csv.reader(io.StringIO(result['csv'])))),261)
    async def test_different_session_account_is_rejected(self):
        self.s.settings['phone']='+8613800000000'
        client=SimpleNamespace(get_me=AsyncMock(return_value=SimpleNamespace(phone='8613900000000')))
        with self.assertRaisesRegex(ValueError,'不一致'):await self.s.verify_account(client)
        self.assertFalse(self.s.account_ready)
    async def test_market_review_persists_without_creating_orders(self):
        record=dict(channel='AITradeCopy',message_id=1266,date='2026-11-17T07:14:00Z',text='🎮 CS2: magic vs Heroic (BO3)\n🟢 Moneyline: HERO\n💵 Est. Profit: 69.49%\nhttps://www.foregate.com/en/event/480194146926072329',links=[])
        self.s.ingest(record,'sample',True)
        raw=json.loads((Path(__file__).resolve().parent/'fixtures/market_observation_public.json').read_text())
        button=SimpleNamespace(count=AsyncMock(return_value=1),click=AsyncMock())
        locator=SimpleNamespace(first=SimpleNamespace(wait_for=AsyncMock()),wait_for=AsyncMock())
        page=SimpleNamespace(goto=AsyncMock(),wait_for_function=AsyncMock(),evaluate=AsyncMock(return_value=raw),locator=lambda _:locator,get_by_role=lambda *a,**kw:button)
        self.s.browser=SimpleNamespace(pages=[page],close=AsyncMock())
        self.s.open_browser=AsyncMock();self.s.check_browser=AsyncMock()
        result=await self.s.dispatch('inspect_market',dict(signal_id='AITradeCopy:1266',source='sample'))
        self.assertEqual(result['market_observation']['event_id'],'480194146926072329')
        self.assertFalse(result['market_observation']['executable'])
        self.assertEqual(result['ledger']['orders'],[])
        button.click.assert_not_awaited()
        restarted=Service(self.root)
        try:self.assertEqual(restarted.snapshot()['market_observation']['selected']['selection'],'HERO')
        finally:await restarted.close()

if __name__=='__main__':unittest.main()
