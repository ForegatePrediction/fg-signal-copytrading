"""Offline Chrome integration check for the desktop live execution path.

Run with the bundled Python, vendor dependencies, and Google Chrome installed.
All ForeGate pages are fulfilled locally in a fresh nonpersistent context.
"""
import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock

from playwright.async_api import async_playwright
from copytrader.desktop_service import Service
from test_execution import record

EVENT = """<title>Alpha vs Beta</title>
<section><p>Moneyline</p><button data-outcome-button
 onclick="this.setAttribute('aria-pressed','true')">ALP 50¢</button></section>
<section><button role="tab" aria-selected="true">Buy</button>
<button role="combobox">Limit</button>
<label>Price ¢<input type="number"></label>
<label>Shares<input type="number"></label>
<p>Balance: 10 USDT</p><p>Total Cost $1.99</p><p>Fee $0.02</p>
<button onclick="sessionStorage.state='Open'; sessionStorage.clicks=1+Number(sessionStorage.clicks||0);
 document.querySelector('aside').innerText='Order submitted'">Trade</button>
<aside aria-label="Trade feedback notifications"></aside></section>"""

PORTFOLIO = """<title>Portfolio</title><button>Available $10</button>
<button role="tab" aria-selected="true">Open Orders</button>
<table><thead><tr></tr></thead><tbody></tbody></table><script>
const headers=['Market','Type','Order ID','Status','Filled','Shares','Cost','Fee','Payout','PnL','Settled At','Action'];
document.querySelector('thead tr').innerHTML=headers.map(h=>'<th>'+h+'</th>').join('');
const state=sessionStorage.state;
if(state){
 const filled=state==='Open'?'0':'1';
 const cells=['<a href="/en/event/123">Match Winner ALP</a>','Buy','ORD-123',state,
 filled,'2.30',filled==='0'?'0':'.85',filled==='0'?'0':'.01','1','.14','2026-11-19T09:00:00Z',
 '<button>Cancel</button>'];
 document.querySelector('tbody').innerHTML='<tr>'+cells.map(c=>'<td>'+c+'</td>').join('')+'</tr>';
 document.querySelector('tbody button').onclick=()=>sessionStorage.state='Cancelled';
}</script>"""


class BrowserLifecycle(unittest.IsolatedAsyncioTestCase):
    async def test_channel_limit_order_through_settlement(self):
        await self.check_lifecycle('LIMIT')

    async def test_channel_market_order_through_settlement(self):
        await self.check_lifecycle('MARKET')

    async def check_lifecycle(self, order_type):
        market = order_type == 'MARKET'
        event = EVENT
        if market:
            event = event.replace('>Limit<','>Market<').replace('Price ¢','Amount').replace('<label>Shares<input type="number"></label>', '<p>Avg Price 50¢</p>')
            event = event.replace("state='Open'", "state='Filled'")
        portfolio = PORTFOLIO
        if market:
            portfolio = portfolio.replace("filled,'2.30'", "filled,'1'")
        with tempfile.TemporaryDirectory() as directory:
            service = Service(directory)
            await service.dispatch('live_save', dict(config=dict(service.live_config, order_type=order_type)))
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(channel='chrome', headless=True)
                try:
                    context = await browser.new_context(service_workers='block')
                    async def serve(route):
                        bodies = {'https://www.foregate.com/en/event/123': event,
                                  'https://www.foregate.com/en/portfolio': portfolio}
                        body = bodies.get(route.request.url)
                        if body is None:
                            await route.abort()
                        else:
                            await route.fulfill(content_type='text/html; charset=utf-8', body=body)
                    await context.route('**/*', serve)
                    page = await context.new_page()
                    service.browser = context
                    service.listen = AsyncMock()  # Feed one local channel message below.
                    prepared = await service.dispatch('live_prepare_session', {})
                    await service.dispatch('live_arm', dict(native_authorized=True,
                        plan_id=prepared['execution']['prepared']['id']))
                    service.ingest(record(), 'public')
                    async def wait_for_open():
                        while service.live_store.orders()[0]['state'] not in {'OPEN','FILLED','UNKNOWN','SKIPPED'} or service.execution_busy:
                            await asyncio.sleep(.05)
                    await asyncio.wait_for(wait_for_open(), timeout=45)
                    order = service.live_store.orders()[0]
                    self.assertEqual(order['state'], 'FILLED' if market else 'OPEN', order['reason'])
                    self.assertEqual(order['order_id'], 'ORD-123')
                    self.assertTrue(Path(order['post_submit_screenshot']).is_file())
                    await service.dispatch('live_stop', {})
                    args = dict(order_id=order['id'])
                    if not market:
                        await page.evaluate("sessionStorage.state='Partially Filled'")
                        await service.dispatch('live_reconcile', args)
                        self.assertEqual(service.live_store.get(order['id'])['state'], 'PARTIAL')
                        await service.dispatch('live_cancel', dict(args,native_authorized=True))
                        self.assertEqual(service.live_store.get(order['id'])['state'], 'CANCELLED')
                    await page.evaluate("sessionStorage.state='Settled'")
                    state = await service.dispatch('live_reconcile', args)
                    self.assertEqual(state['execution']['ledger']['orders'][0]['state'], 'SETTLED')
                    self.assertEqual(state['execution']['ledger']['statistics'][0]['pnl'], '0.14')
                    self.assertEqual(await page.evaluate('sessionStorage.clicks'), '1')
                finally:
                    await service.close()
                    await browser.close()
            restarted = Service(directory)
            try:
                self.assertEqual(restarted.live_store.orders()[0]['state'], 'SETTLED')
                self.assertFalse(restarted.live_armed)
            finally:
                await restarted.close()


if __name__ == '__main__':
    unittest.main()
