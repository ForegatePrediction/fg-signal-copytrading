import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from copytrader.desktop_service import Service,now

class WebTestReadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.service=Service(self.tmp.name)
    async def asyncTearDown(self):
        self.service.browser=None
        await self.service.close();self.tmp.cleanup()
    def page(self,url='https://www.foregate.com/en/event/123'):
        return SimpleNamespace(url=url,is_closed=lambda:False,evaluate=AsyncMock(return_value={
            'url':url,'title':'Test','outcomes':[],'fields':[],'controls':[],'feedback':['Order successful'],'tables':[]}))
    async def test_requires_existing_browser(self):
        with self.assertRaises(ValueError):await self.service.read_web_test()
    async def test_rejects_ambiguous_tabs_and_other_hosts(self):
        for pages in ([self.page(),self.page()],[self.page('https://www.foregate.com.evil.test/en')],[self.page('http://www.foregate.com/en')]):
            self.service.browser=SimpleNamespace(pages=pages)
            with self.assertRaises(ValueError):await self.service.read_web_test()
    async def test_success_toast_is_not_confirmed_order(self):
        p=self.page();self.service.browser=SimpleNamespace(pages=[p])
        before=self.service.engine.snapshot(now())
        await self.service.read_web_test()
        self.assertFalse(self.service.web_test['verified_order'])
        self.assertEqual(self.service.web_test['spend_cap'],'2.00')
        self.assertEqual(self.service.web_test['status'],'READ_ONLY_EVIDENCE')
        self.assertEqual(before['orders'],self.service.snapshot()['ledger']['orders'])
        self.assertEqual(p.evaluate.await_count,2)
    async def test_invalid_amounts_never_touch_browser(self):
        for amount in ['NaN','Infinity','-1','0','2.01','1.001','invalid']:
            with self.assertRaises(ValueError):await self.service.prepare_web_test({'amount':amount})
        self.assertIsNone(self.service.web_test)
    async def test_prepare_only_fills_and_blocks_listener(self):
        from unittest.mock import Mock
        p=self.page();buy=SimpleNamespace(count=AsyncMock(return_value=1),get_attribute=AsyncMock(return_value='true'))
        market=SimpleNamespace(count=AsyncMock(return_value=1))
        amount=SimpleNamespace(count=AsyncMock(return_value=1),fill=AsyncMock())
        def role(kind,**args):return {'tab':buy,'combobox':market,'textbox':amount}[kind]
        panel=SimpleNamespace(count=AsyncMock(return_value=1),get_by_role=role)
        p.get_by_role=Mock(side_effect=role);p.bring_to_front=AsyncMock()
        self.service.browser=SimpleNamespace(pages=[p])
        await self.service.prepare_web_test({'amount':'1'})
        amount.fill.assert_awaited_once_with('1')
        self.assertEqual(self.service.web_test['status'],'AWAITING_USER')
        self.assertFalse(self.service.running)
        with self.assertRaises(ValueError):await self.service.dispatch('start',{})
    async def test_open_rejects_non_event_and_injected_selection(self):
        for data in [{'event_id':'https://evil.test','selection':'TS8'},{'event_id':'123','selection':'TS8" onclick='}]:
            with self.assertRaises(ValueError):await self.service.open_web_test(data)
