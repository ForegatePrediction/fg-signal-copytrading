"""One explicitly enabled browser BUY test. No retries after the commit boundary."""
import asyncio
from collections import Counter
from datetime import datetime,timezone
from decimal import Decimal,InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from urllib.parse import urlsplit,parse_qs
import uuid

HEADERS=['Market','Market Type','Type','Shares','Date','Price','Value/Fees','Tx']
UNCERTAIN={'COMMITTING','AWAITING_ACTIVITY','UNKNOWN'}
RESERVED={'COMMITTING','AWAITING_ACTIVITY','UNKNOWN','ACTIVITY_CONFIRMED','PARTIAL_ACTIVITY'}

def utc():return datetime.now(timezone.utc).isoformat()
def dec(value):
    try:d=Decimal(str(value))
    except (InvalidOperation,ValueError):raise ValueError('无效数字。') from None
    if not d.is_finite():raise ValueError('无效数字。')
    return d

def target(event,selection,amount,max_price):
    if not re.fullmatch(r'\d{1,30}',str(event)) or not re.fullmatch(r'[A-Za-z0-9_-]{1,24}',str(selection)):
        raise ValueError('事件编号或 Moneyline 选项格式不正确。')
    a=dec(amount);price=dec(max_price)
    if a!=Decimal('2'):raise ValueError('首轮自动测试仅支持 2 USDT，最多一笔。')
    if not 0<price<100:raise ValueError('提交前最高平均报价须在 0–100 美分之间。')
    return dict(event_id=str(event),selection=str(selection).upper(),amount='2.00',max_price_cents=str(price))

def activity_rows(raw,t):
    if raw.get('url')!='https://www.foregate.com/en/portfolio' or raw.get('table_count')!=1 or raw.get('headers')!=HEADERS:
        raise ValueError('未取得唯一且格式正确的账户活动表格。')
    result=[]
    for row in raw['rows']:
        c=row.get('cells',[])
        if len(c)!=8 or c[1]!='orderbook' or c[2]!='Buy':continue
        if not re.search(r'\bMatch Winner\s+'+re.escape(t['selection'])+r'$',c[0],re.I):continue
        links=[]
        for l in row.get('links',[]):
            p=urlsplit(l.get('href',''))
            if p.scheme and (p.scheme!='https' or p.hostname!='www.foregate.com'):continue
            if p.path=='/en/event/'+t['event_id'] and parse_qs(p.query).get('market')==['moneyline']:links.append(p)
        if len(links)!=1:continue
        money=re.fullmatch(r'\$([\d,]+(?:\.\d+)?)\s+\$([\d,]+(?:\.\d+)?)',c[6])
        price=re.fullmatch(r'(\d+(?:\.\d+)?)¢',c[5])
        if not money or not price:raise ValueError('对应买入活动的金额或价格格式不正确。')
        shares=dec(c[3].replace(',',''));cost=dec(money[1].replace(',',''));fee=dec(money[2].replace(',',''));px=dec(price[1])
        if shares<=0 or cost<=0 or fee<0 or not 0<px<100:raise ValueError('对应买入活动数据无效。')
        item=dict(market=c[0],shares=str(shares),price_cents=str(px),notional=str(cost),fee=str(fee),total=str(cost+fee),displayed_date=c[4],window_id=parse_qs(links[0].query).get('windowId',[''])[0])
        item['fingerprint']=hashlib.sha256(json.dumps(item,sort_keys=True).encode()).hexdigest();result.append(item)
    return result

def new_receipt(before,after,t):
    counts=Counter(x['fingerprint'] for x in before);fresh=[]
    for x in after:
        if counts[x['fingerprint']]:counts[x['fingerprint']]-=1
        else:fresh.append(x)
    if not fresh:return None
    if len(fresh)!=1:raise ValueError('出现多个新增买入记录，无法唯一归属于本次提交。')
    row=fresh[0];total=dec(row['total']);amount=dec(t['amount'])
    if total>amount+Decimal('0.0005'):raise ValueError('活动记录的总支出超过本次测试上限。')
    return dict(status='ACTIVITY_CONFIRMED' if abs(total-amount)<=Decimal('0.0005') else 'PARTIAL_ACTIVITY',activity=row,
                order_id=None,order_terminal_verified=False)

class TestJournal:
    def __init__(self,path):
        self.path=Path(path);self.db=sqlite3.connect(str(path));self.db.row_factory=sqlite3.Row
        if str(path)!=':memory:':os.chmod(path,0o600)
        self.db.execute('CREATE TABLE IF NOT EXISTS tests(id TEXT PRIMARY KEY,state TEXT,body TEXT,updated_at TEXT)')
        for r in self.db.execute("SELECT id,state,body FROM tests WHERE state IN ('PREPARING','READY','COMMITTING','AWAITING_ACTIVITY')").fetchall():
            self.update(r['id'],'UNKNOWN' if r['state'] in UNCERTAIN else 'ABORTED',reason='上次运行中断；不恢复提交、不自动重试。')
        self.db.commit()
    def get(self,id):
        r=self.db.execute('SELECT * FROM tests WHERE id=?',(id,)).fetchone()
        return dict(json.loads(r['body']),id=r['id'],state=r['state'],updated_at=r['updated_at']) if r else None
    def latest(self):
        r=self.db.execute('SELECT id FROM tests ORDER BY updated_at DESC,rowid DESC LIMIT 1').fetchone();return self.get(r[0]) if r else None
    def prepare(self,t):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            if self.db.execute("SELECT 1 FROM tests WHERE state IN ('PREPARING','READY','COMMITTING','AWAITING_ACTIVITY','UNKNOWN','ACTIVITY_CONFIRMED','PARTIAL_ACTIVITY') LIMIT 1").fetchone():
                raise ValueError('已有测试待完成，或已使用本轮 2 USDT 自动测试额度；不能重复买入。')
            id=uuid.uuid4().hex;self.db.execute('INSERT INTO tests VALUES(?,?,?,?)',(id,'PREPARING',json.dumps(dict(target=t)),utc()));self.db.commit();return id
        except BaseException:self.db.rollback();raise
    def update(self,id,state,**values):
        old=self.get(id)
        if not old:raise ValueError('测试计划不存在。')
        body={k:v for k,v in old.items() if k not in {'id','state','updated_at'}};body.update(values)
        self.db.execute('UPDATE tests SET state=?,body=?,updated_at=? WHERE id=?',(state,json.dumps(body,ensure_ascii=False),utc(),id));self.db.commit()
    def refresh_baseline(self,id,baseline):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            row=self.db.execute('SELECT state,body FROM tests WHERE id=?',(id,)).fetchone()
            if not row or row[0]!='READY':raise ValueError('提交状态已变化，不能重新准备或重复提交。')
            body=json.loads(row[1]);body['baseline']=baseline
            self.db.execute('UPDATE tests SET body=?,updated_at=? WHERE id=?',(json.dumps(body,ensure_ascii=False),utc(),id));self.db.commit()
        except BaseException:self.db.rollback();raise
    def commit(self,id):
        # The durable reservation is written before the sole irreversible click.
        self.db.execute('BEGIN IMMEDIATE')
        try:
            r=self.db.execute('SELECT state FROM tests WHERE id=?',(id,)).fetchone()
            if not r or r[0]!='READY':raise ValueError('计划已失效、已提交或尚未准备完成。')
            self.db.execute("UPDATE tests SET state='COMMITTING',updated_at=? WHERE id=?",(utc(),id));self.db.commit()
        except BaseException:self.db.rollback();raise
    def close(self):self.db.close()

async def submission_evidence(page, p):
    # Preserve the trade page before account navigation destroys a toast
    # or additional confirmation. Reading a dialog never accepts it.
    try:
        await page.wait_for_function('''() => [...document.querySelectorAll('[role="dialog"],[role="alertdialog"],[aria-label="Trade feedback notifications"]')].some(n=>n.getClientRects().length&&n.innerText.trim())''',timeout=30000)
    except Exception:pass
    raw=await page.evaluate(Path(__file__).with_name('web_test_dom.js').read_text())
    try:await page.screenshot(path=str(p),full_page=False);os.chmod(p,0o600)
    except Exception:p=None
    evidence = dict(post_submit_feedback=raw.get('feedback', []),
                    post_submit_dialogs=raw.get('dialogs', []),
                    post_submit_screenshot=str(p) if p else None)
    return evidence, feedback_issue(raw)


def feedback_issue(raw):
    if raw.get('dialogs'):return '提交后出现额外确认窗口，已保留当前页面；未代为确认，禁止重复提交。'
    feedback=' '.join(raw.get('feedback',[]))
    # Only a visible, affirmative completion/acceptance message permits
    # leaving the form. A blank/processing/error response stays on screen.
    if re.search(r'\b(?:fail(?:ed|ure)?|reject(?:ed)?|error|unsuccessful|not\s+(?:successful|submitted|placed|filled))\b|失败|拒绝|错误|未成功',feedback,re.I):return '提交后页面提示异常，已保留原页和证据；结果仍待核对，禁止重复提交。'
    if not re.search(r'\b(?:success(?:ful(?:ly)?)?|(?:order|trade)\s+(?:placed|submitted|completed|executed|filled))\b|(?:交易|订单|下单)(?:提交|执行|成交)?成功',feedback,re.I):return '提交后未取得明确完成反馈，已保留交易详情页；结果仍待核对，禁止重复提交。'
    return None


class BrowserBuyTest:
    def __init__(self,journal):self.journal=journal
    async def post_submit_evidence(self,page,id):
        evidence, issue = await submission_evidence(page, self.journal.path.parent / ('web-live-'+id+'-submitted.png'))
        self.journal.update(id, 'AWAITING_ACTIVITY', **evidence)
        return issue
    async def activity(self,page,t):
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError

        timeouts = (PlaywrightTimeoutError, TimeoutError)
        for attempt in range(2):
            try:
                await page.goto('https://www.foregate.com/en/portfolio', wait_until='domcontentloaded', timeout=30000)
                await page.get_by_role('button', name=re.compile(r'^Available')).wait_for(state='visible', timeout=15000)
                break
            except timeouts as exc:
                if attempt:
                    raise ValueError('Portfolio 加载超时，未读到可用余额。请在专用浏览器确认页面已加载并显示可用余额。') from exc

        script=Path(__file__).with_name('live_test_dom.js').read_text()
        account=Path(__file__).with_name('account_dom.js').read_text()
        for attempt in range(2):
            tab=page.get_by_role('tab',name='Activity',exact=True)
            if await tab.count()!=1:tab=page.get_by_role('button',name='Activity',exact=True)
            try:
                await tab.wait_for(state='visible',timeout=15000)
                await tab.click(timeout=10000)
                # A read-only tab click may be lost while the page hydrates.
                # Retry navigation only; never retry the financial submit.
                await page.wait_for_function('() => {const r=('+script+')();const a=('+account+')();return r.table_count===1&&!a.loading}',timeout=15000)
            except timeouts as exc:
                if attempt:raise ValueError('Portfolio 的 Activity 尚未加载，暂时无法读取流水。') from exc
                continue
            return activity_rows(await page.evaluate(script),t)
    async def form(self,page,t,expected_title=None):
        from .market_observer import price_label
        await page.goto('https://www.foregate.com/en/event/'+t['event_id'],wait_until='domcontentloaded',timeout=30000)
        await page.locator('[data-outcome-button]').first.wait_for(state='visible',timeout=20000)
        raw=await page.evaluate(Path(__file__).with_name('market_dom.js').read_text())
        if raw['url']!='https://www.foregate.com/en/event/'+t['event_id']:raise ValueError('事件页面发生跳转。')
        if expected_title and raw['title']!=expected_title:raise ValueError('赛事名称已变化。')
        found=[o for o in raw['outcomes'] if o['group']=='Moneyline' and price_label(o['text'])['selection'].upper()==t['selection']]
        if len(found)!=1 or not price_label(found[0]['text'])['valid']:raise ValueError('未找到唯一有效的 Moneyline 选项。')
        if dec(price_label(found[0]['text'])['price_cents'])>dec(t['max_price_cents']):raise ValueError('页面报价超过提交前报价上限。')
        await page.locator('[data-outcome-button]').nth(found[0]['index']).click()
        # The trade panel mounts asynchronously after selecting an outcome.
        await page.get_by_role('textbox',name='0.00',exact=True).wait_for(state='visible',timeout=10000)
        buy=page.get_by_role('tab',name='Buy',exact=True)
        buy_count=await buy.count();buy_selected=await buy.get_attribute('aria-selected') if buy_count==1 else None
        market_count=await page.get_by_role('combobox',name='Market',exact=True).count()
        if buy_count!=1 or buy_selected!='true' or market_count!=1:
            raise ValueError(f'交易面板不是唯一 Buy / Market 状态（Buy={buy_count}，选中={buy_selected}，Market={market_count}）。')
        amount=page.get_by_role('textbox',name='0.00',exact=True)
        await amount.fill(t['amount'])
        await page.wait_for_function('''() => {const b=Array.from(document.querySelectorAll('button')).filter(n=>n.getClientRects().length&&n.innerText.trim()==='Trade');return b.length===1&&!b[0].disabled}''',timeout=15000)
        # Re-read actual visible control state after the asynchronous quote has completed.
        await self.validate_form(page,t)
        return raw['title']
    async def validate_form(self,page,t):
        from .market_observer import price_label
        raw=await page.evaluate(Path(__file__).with_name('market_dom.js').read_text())
        selected=[o for o in raw['outcomes'] if o.get('selected')]
        if raw['url']!='https://www.foregate.com/en/event/'+t['event_id'] or len(selected)!=1 or selected[0]['group']!='Moneyline' or price_label(selected[0]['text'])['selection'].upper()!=t['selection']:
            raise ValueError('提交前盘口发生变化。')
        w=await page.evaluate(Path(__file__).with_name('web_test_dom.js').read_text())
        if len(w['panel_text'])!=1 or len(w['fields'])!=1 or dec(w['fields'][0]['value'])!=dec(t['amount']):raise ValueError('提交前金额或交易面板不唯一。')
        text=w['panel_text'][0]
        balance=re.search(r'Balance:\s*([\d,.]+)\s+USDT',text);avg=re.search(r'Avg Price\s+(\d+(?:\.\d+)?)¢',text)
        if not balance or dec(balance[1].replace(',',''))<dec(t['amount']) or not avg or not 0<dec(avg[1])<=dec(t['max_price_cents']):
            raise ValueError('余额不足或当前平均报价不符合测试条件。')
        if await page.get_by_role('tab',name='Buy',exact=True).get_attribute('aria-selected')!='true' or await page.get_by_role('combobox',name='Market',exact=True).count()!=1:
            raise ValueError('提交前交易方向或订单类型已变化。')
        button=page.get_by_role('button',name='Trade',exact=True)
        if await button.count()!=1 or not await button.is_enabled():raise ValueError('真实提交控件不可用或不唯一。')
        return button
    async def prepare(self,page,t):
        id=self.journal.prepare(t)
        try:
            baseline=await self.activity(page,t)
            title=await self.form(page,t)
            preview=str(self.journal.path.parent/'web-live-test-preview.png')
            await page.screenshot(path=preview,full_page=False);os.chmod(preview,0o600)
            self.journal.update(id,'READY',baseline=baseline,title=title,preview_path=preview,prepared_at=utc(),reason='准备完成，未提交。由用户启用后客户端自动买入一次。')
            await page.bring_to_front();return id
        except BaseException as exc:
            stage='准备失败，未进入提交边界。'
            if isinstance(exc,ValueError):stage=str(exc)
            self.journal.update(id,'ABORTED',reason=stage)
            try:
                data=await page.evaluate(Path(__file__).with_name('account_dom.js').read_text())
                data['error_type']=type(exc.__cause__ or exc).__name__
                p=self.journal.path.parent/'web-live-test-diagnostic.json';p.write_text(json.dumps(data,ensure_ascii=False,indent=2));os.chmod(p,0o600)
                p=self.journal.path.parent/'web-live-test-diagnostic.png';await page.screenshot(path=str(p),full_page=False);os.chmod(p,0o600)
            except Exception:pass
            raise
    async def execute(self,page,id):
        plan=self.journal.get(id)
        if not plan or plan['state']!='READY':raise ValueError('计划不在待启用状态。')
        age=(datetime.now(timezone.utc)-datetime.fromisoformat(plan['prepared_at'])).total_seconds()
        if not 0<=age<=180:
            self.journal.update(id,'ABORTED',reason='计划超过 3 分钟，请重新准备。');return
        t=plan['target']
        try:
            baseline=await self.activity(page,t) # Immediately before the sole submit, never reuse old activity.
            await self.form(page,t,plan['title'])
            button=await self.validate_form(page,t)
        except BaseException:
            self.journal.update(id,'ABORTED',reason='提交前复核未通过，没有点击 Trade。');raise
        self.journal.refresh_baseline(id,baseline)
        self.journal.commit(id)
        try:
            await button.click(timeout=10000) # Exactly once. No automatic confirmation or second click.
            self.journal.update(id,'AWAITING_ACTIVITY',reason='已发出一次提交；正在核对新增成交活动。')
            issue=await self.post_submit_evidence(page,id)
            if issue:
                self.journal.update(id,'UNKNOWN',reason=issue if isinstance(issue,str) else '提交后出现额外确认窗口，已保留当前页面；未代为确认，禁止重复提交。');return
            # Market order processing may outlive a toast. Only matching new activity counts.
            for attempt in range(4):
                await asyncio.sleep(3)
                receipt=new_receipt(baseline,await self.activity(page,t),t)
                if receipt:
                    self.journal.update(id,receipt['status'],receipt=receipt,reason='发现唯一新增买入活动；唯一订单号和订单终态尚未验证。');return
            self.journal.update(id,'UNKNOWN',reason='没有取得唯一新增成交记录，已锁住额度；禁止自动重试。')
        except BaseException:
            self.journal.update(id,'UNKNOWN',reason='提交边界后中断或核对失败；可能已买入，禁止自动重试。');raise
