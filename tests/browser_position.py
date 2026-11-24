"""Offline Chromium check of the detail-page reader using the reported SAS layout."""
import unittest
from playwright.async_api import async_playwright
from copytrader.position import observe, position_target
from test_position import SIGNAL, RAW, URL

HTML = """<title>Clippers vs Spurs | ForeGate</title><button>Available $19.029</button>
<section><p>Moneyline</p><button role="tab" aria-selected="false"
 onclick="this.setAttribute('aria-selected','true');document.querySelector('article').hidden=false">My Position 1</button>
<article hidden><button aria-expanded="false" onclick="this.setAttribute('aria-expanded','true');document.querySelector('main').hidden=false">Moneyline · 1</button>
<main hidden><div><span>Outcome</span><span>Avg Price</span><span>Shares</span><span>Pnl</span><span>To Win</span></div>
<div><span>Match Winner SPURS</span><span>90¢</span><span id="shares">2.15</span><span>0.00%</span><span>$2.155</span></div></main></article></section>
<section><p>Spreads</p><button>My Position 1</button><div>Match Winner SPURS 999</div></section>
<aside>Moneyline : LAC <button>Trade</button></aside>"""


class DetailReader(unittest.IsolatedAsyncioTestCase):
    async def test_local_detail(self):
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(channel='chrome', headless=True)
            try:
                context = await browser.new_context(service_workers='block')
                async def local(route):
                    if route.request.url == URL:
                        await route.fulfill(content_type='text/html; charset=utf-8', body=HTML)
                    else:
                        await route.abort()
                await context.route('**/*', local)
                page = await context.new_page()
                await page.goto(URL)
                target = position_target(URL, RAW, SIGNAL, SIGNAL.legs[0], ['Match Winner'])
                self.assertEqual((await observe(page, SIGNAL, target))['shares'], '2.15')
                await page.locator('#shares').evaluate("(n) => n.textContent='3.25'")
                self.assertEqual((await observe(page, SIGNAL, target))['shares'], '3.25')
                await page.get_by_text('Match Winner SPURS', exact=True).evaluate("(n) => n.textContent='Match Winner LAC'")
                self.assertIn('error', await observe(page, SIGNAL, target))
                await page.locator('main').evaluate("(n) => n.textContent='No positions yet'")
                self.assertEqual((await observe(page, SIGNAL, target))['shares'], '0')
            finally:
                await browser.close()


if __name__ == '__main__':
    unittest.main()
