"""Private stdio RPC service for the desktop clients. No listening TCP ports."""
import argparse
import asyncio
from collections import Counter
from dataclasses import asdict,replace
from datetime import datetime,timezone
from decimal import Decimal
import csv
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import urllib.request
from .parser import parse_message
from .paper import Config,PaperEngine,Quote,instant
from .public_preview import extract
from .market_observer import candidates,observation,supported_leg,group_type,display_identity,price_label
from .visible_paper import quotes_from_page,describe
import hashlib
from .live_test import TestJournal,BrowserBuyTest,target as live_target
from .live_service import LiveService
from .api_accounts import ApiAccounts

REASONS={'NO_QUOTE':'等待可靠报价与盘口映射','HIGH_RISK':'已过滤高风险','DISABLED':'已暂停',
 'EXPIRED_OR_FUTURE_SIGNAL':'信号已过期或时间异常','MONEYLINE_WITH_HANDICAP':'玩法与让分冲突',
 'MISSING_OR_MALFORMED_PROFIT':'收益率格式不完整','DUPLICATE_TRADE':'已处理，防止重复下单',
 'RESULT':'结算消息已归档','SUMMARY':'汇总已归档','OTHER':'其他消息已归档',
 'DAILY_LOSS_LIMIT':'达到当日亏损上限','EVENT_LIMIT':'达到单场累计上限','DAILY_LIMIT':'达到每日累计上限',
 'EXPOSURE_LIMIT':'达到未结算投入上限','INSUFFICIENT_BALANCE':'模拟余额不足',
 'PAPER_FILLED':'模拟成交','PAPER_PARTIAL':'模拟部分成交','PAPER_OPEN':'模拟挂单中',
 'PROFIT_FILTER':'不在收益率区间','SPORT_FILTER':'运动类型已过滤','MARKET_FILTER':'玩法已过滤','LEAGUE_FILTER':'联赛已过滤',
 'WAITING_MARKET':'正在等待读取模拟行情','MARKET_UNAVAILABLE':'未取得有效盘口，已跳过',
 'PAUSED_QUEUE':'已暂停，未补下单','INTERRUPTED_QUEUE':'上次运行中断，未补下单',
 'SUPERSEDED':'消息已编辑，旧版本已停止','EVENT_ALREADY_FOLLOWED':'该赛事已有模拟订单',
 'INVALID_OR_STALE_QUOTE':'行情读取过期或无效','QUEUE_FULL':'行情队列已满，已跳过'}
REASONS['MAPPING_NOT_VERIFIED']='此玩法映射待验证，已跳过'

def now():return datetime.now(timezone.utc)

def atomic_json(path,value):
    tmp=path.with_suffix('.tmp')
    fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w',encoding='utf-8') as f:json.dump(value,f,ensure_ascii=False,indent=2)
    os.replace(tmp,path);os.chmod(path,0o600)


def defaults():
    return dict(risk=asdict(Config()),mode='paper',channel='AITradeCopy',api_id='',phone='',
      amount_mode='fixed',stake_pct='1',max_order_usd='10',one_leg_per_event=False,
      pending_policy='cancel',notify=False,paper_market_enabled=True,paper_fee_rate='0.02',poll_interval_sec=10)


class Service(LiveService):
    def __init__(self,data_dir,api_mode=False,api_workspace_factory=ApiAccounts):
        self.root=Path(data_dir);self.root.mkdir(parents=True,exist_ok=True);os.chmod(self.root,0o700)
        self.settings=defaults();self.credentials={};self.running=False;self.task=None;self.paper_task=None;self.client=None
        self.api_workspace=None
        self.live_journal=TestJournal(self.root/'web-live-test.db');self.live_runner=BrowserBuyTest(self.live_journal);self.live_busy=False
        self.web_test=None;self.rehearsal=None;self.paper_status='已暂停';self.pending_refresh={};self.closed=False
        self.tg_status='未连接';self.fg_status='未连接';self.balance='—';self.error='';self.code_hash=None
        self.browser=None;self.pw=None;self.browser_lock=asyncio.Lock();self.demo_orders=[];self.demo_fills=[]
        self.log=[];self.seen=set();self.entity=None;self.account_ready=False;self.tg_api_identity=None
        p=self.root/'settings.json'
        if p.exists():self.settings.update(json.loads(p.read_text(encoding='utf-8')))
        ledger_path=self.root/'paper.db'
        if ledger_path.exists():
            prior=sqlite3.connect(str(ledger_path))
            try:
                saved=prior.execute("SELECT value FROM meta WHERE key='desktop_settings'").fetchone()
                if saved:self.settings.update(json.loads(saved[0]))
            except sqlite3.OperationalError:pass
            finally:prior.close()
        self.validate_settings(self.settings)
        self.settings['mode']='paper' # Every application launch starts safely in paper mode.
        self.init_live()
        self.engine=PaperEngine(self.root/'paper.db',Config(**self.settings['risk']))
        self.migrate_poll_interval()
        self.db=sqlite3.connect(str(self.root/'signals.db'))
        self.db.execute('CREATE TABLE IF NOT EXISTS signals(id TEXT,version TEXT,source TEXT,record TEXT,parsed TEXT,decisions TEXT,seen_at TEXT,PRIMARY KEY(id,version,source))')
        self.db.execute('CREATE TABLE IF NOT EXISTS market_observations(event_id TEXT PRIMARY KEY,body TEXT NOT NULL,seen_at TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS paper_jobs(id INTEGER PRIMARY KEY,signal_id TEXT,version TEXT,source TEXT,status TEXT,reason TEXT,created_at TEXT,UNIQUE(signal_id,version,source))')
        self.db.execute('CREATE TABLE IF NOT EXISTS run_log(id INTEGER PRIMARY KEY, time TEXT, level TEXT, message TEXT)')
        self.log=[dict(time=t,level=l,message=m) for t,l,m in self.db.execute('SELECT time,level,message FROM (SELECT * FROM run_log ORDER BY id DESC LIMIT 150) ORDER BY id')]
        interrupted=self.db.execute("SELECT * FROM paper_jobs WHERE status IN ('QUEUED','READING')").fetchall()
        for job in interrupted:self._finish_job(job[0],'INTERRUPTED_QUEUE')
        self.db.commit()
        if api_mode:
            self.settings['poll_interval_sec']=10
            self.api_workspace=api_workspace_factory(self)
        self.addlog('API 工作台已启动，自动跟单已暂停。' if api_mode else '客户端已启动；模拟模式。')
        previous=self.db.execute('SELECT body FROM market_observations ORDER BY seen_at DESC LIMIT 1').fetchone()
        self.market_observation=json.loads(previous[0]) if previous else None

    def migrate_poll_interval(self):
        # Migrate the former default once. Later user choices, including 60s, persist.
        db=self.engine.db
        db.execute('BEGIN IMMEDIATE')
        try:
            marked=db.execute("SELECT 1 FROM meta WHERE key='poll_default_10s'").fetchone()
            migrate=not marked and self.settings['poll_interval_sec']==60
            if migrate:
                self.settings['poll_interval_sec']=defaults()['poll_interval_sec']
                db.execute("INSERT OR REPLACE INTO meta VALUES ('desktop_settings',?)",
                           (json.dumps(self.settings,ensure_ascii=False),))
            db.execute("INSERT OR IGNORE INTO meta VALUES ('poll_default_10s','1')")
            db.execute('COMMIT')
        except Exception:
            db.execute('ROLLBACK')
            raise
        if migrate:
            try:atomic_json(self.root/'settings.json',self.settings)
            except OSError:pass  # The ledger is authoritative on restart.

    def addlog(self,message,level='info'):
        # Log only fixed operational messages, never raw errors/credentials.
        at=now().isoformat()
        with self.db:
            self.db.execute('INSERT INTO run_log(time,level,message) VALUES(?,?,?)',(at,level,message))
            self.db.execute('DELETE FROM run_log WHERE id <= (SELECT max(id)-10000 FROM run_log)')
        self.log.append(dict(time=at,message=message,level=level))
        self.log=self.log[-150:]

    @staticmethod
    def validate_settings(s):
        cfg=Config(**s['risk'])
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{3,63}',s['channel']):raise ValueError('频道名格式不正确，请填写不含 @ 的频道名。')
        if s['mode'] not in {'paper','live'}:raise ValueError('无效模式。')
        if s['amount_mode'] not in {'fixed','percent'}:raise ValueError('无效金额策略。')
        pct=Decimal(str(s['stake_pct']));cap=Decimal(str(s['max_order_usd']))
        if not pct.is_finite() or not 0<pct<=100 or not cap.is_finite() or cap<=0:raise ValueError('余额比例须在 0–100% 之间，单笔上限须大于零。')
        if Decimal(cfg.stake_usd)>cap and s['amount_mode']=='fixed':raise ValueError('每笔金额不能高于单笔上限。')
        if s.get('pending_policy')!='cancel':raise ValueError('当前支持到期撤销剩余量；自动重挂需完成实盘对账验证。')
        fee=Decimal(str(s['paper_fee_rate']))
        if not fee.is_finite() or not 0<=fee<1:raise ValueError('模拟手续费率须在 0（含）至 1（不含）之间，例如 0.02 表示 2%。')
        for key in ['one_leg_per_event','notify','paper_market_enabled']:
            if type(s.get(key)) is not bool:raise ValueError('无效开关。')
        if type(s.get('poll_interval_sec')) is not int or not 10<=s['poll_interval_sec']<=3600:raise ValueError('频道采集间隔须为 10–3600 秒。')
        if s.get('api_id') and not str(s['api_id']).isdigit():raise ValueError('Telegram API ID 必须是数字。')
        if s.get('phone') and not re.fullmatch(r'\+[0-9]{6,18}',s['phone'].replace(' ','')):raise ValueError('手机号须包含国家代码，例如 +86…')

    def snapshot(self):
        plan=self.live_journal.latest()
        if plan and plan['state']=='READY' and not self.live_busy and (now()-datetime.fromisoformat(plan['prepared_at'])).total_seconds()>180:
            self.live_journal.update(plan['id'],'ABORTED',reason='准备计划已过期，未提交；可以重新准备。')
        self.engine.advance(now()) # Expiry continues even while signal following is paused.
        s=json.loads(json.dumps(self.settings));s['api_hash_set']=bool(self.credentials.get('api_hash'))
        rows=[]
        for r in self.db.execute('SELECT id,source,parsed,decisions,seen_at FROM signals ORDER BY seen_at DESC,rowid DESC LIMIT 250'):
            sig=json.loads(r[2]);ds=json.loads(r[3])
            rows.append(dict(id=r[0],source=r[1],signal=sig,decisions=[dict(d,label=REASONS.get(d['reason'],d['reason'])) for d in ds],seen_at=r[4]))
        ledger=self.engine.snapshot(now())
        counts=dict(self.db.execute('SELECT source,COUNT(DISTINCT id) FROM signals GROUP BY source').fetchall())
        return dict(settings=s,running=self.running,tg_status=self.tg_status,fg_status=self.fg_status,signal_counts=counts,
          fg_balance=self.balance,account_ready=self.account_ready,live_ready=False,
          live_blocker='频道实盘执行模块已开发，网页兼容性与真实交易待验收；仅由用户在实盘工作台启用。',
          signals=rows,ledger=ledger,demo_orders=self.demo_orders,logs=self.log,error=self.error,
          data_dir=str(self.root),version='0.6.1',market_observation=self.market_observation,execution=self.live_snapshot(),
          auto_test=({k:v for k,v in (self.live_journal.latest() or {}).items() if k!='baseline'} or None),auto_test_busy=self.live_busy,
          web_test=self.web_test,rehearsal=self.rehearsal,paper_status=self.paper_status,
          paper_queue=self.db.execute("SELECT COUNT(*) FROM paper_jobs WHERE status IN ('QUEUED','READING')").fetchone()[0])

    def paper_config(self):
        cfg=replace(Config(**self.settings['risk']),one_leg_per_event=self.settings['one_leg_per_event'])
        if self.settings['amount_mode']=='percent':
            avail=self.engine._totals(now())['available']
            amount=min(avail*Decimal(self.settings['stake_pct'])/100,Decimal(self.settings['max_order_usd']))
            if amount<=0:raise ValueError('模拟可用余额不足。')
            cfg=replace(cfg,stake_usd=str(amount))
        return cfg

    def ingest(self,record,source='telegram',review=False):
        version=hashlib.sha256(json.dumps(record,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        sig=parse_message(record);ds=[]
        if self.db.execute('SELECT 1 FROM signals WHERE id=? AND version=? AND source=?',(sig.signal_id,version,source)).fetchone():return False
        # An edit invalidates any older queued work, including work currently awaiting the browser.
        for job in self.db.execute("SELECT id FROM paper_jobs WHERE signal_id=? AND status IN ('QUEUED','READING')",(sig.signal_id,)).fetchall():self._finish_job(job[0],'SUPERSEDED')
        if review:ds=[dict(signal_id=sig.signal_id,leg_no=l.leg_no,reason=l.reason or 'SAMPLE_REVIEW') for l in sig.legs]
        elif self.api_workspace:
            ds=self.api_workspace.ingest(record,source,sig)
        elif self.live_armed and source in {'telegram','public'}:
            ds=self.live_ingest(record,source,version)
        else:
            try:self.engine.config=self.paper_config()
            except ValueError:ds=[dict(signal_id=sig.signal_id,leg_no=l.leg_no,reason='INSUFFICIENT_BALANCE') for l in sig.legs]
            if not ds:ds=self.engine.process(record,now(),[])['decisions']
            supported={l.leg_no for l in sig.legs if supported_leg(l)}
            ds=[dict(d,reason='MAPPING_NOT_VERIFIED') if d['reason']=='NO_QUOTE' and d['leg_no'] not in supported else d for d in ds]
            if self.running and source in {'telegram','public'} and self.settings['paper_market_enabled'] and any(d['reason']=='NO_QUOTE' for d in ds):
                reason='WAITING_MARKET' if self.db.execute("SELECT COUNT(*) FROM paper_jobs WHERE status IN ('QUEUED','READING')").fetchone()[0]<100 else 'QUEUE_FULL'
                if reason=='WAITING_MARKET':self.db.execute('INSERT INTO paper_jobs(signal_id,version,source,status,reason,created_at) VALUES(?,?,?,?,?,?)',(sig.signal_id,version,source,'QUEUED','',now().isoformat()))
                ds=[dict(d,reason=reason) if d['reason']=='NO_QUOTE' else d for d in ds]
        self.db.execute('INSERT OR IGNORE INTO signals VALUES(?,?,?,?,?,?,?)',(sig.signal_id,version,source,json.dumps(record,ensure_ascii=False),json.dumps(sig.as_dict(),ensure_ascii=False),json.dumps(ds,ensure_ascii=False),now().isoformat()))
        self.db.commit()
        return True

    def _finish_job(self,job_id,reason,decisions=None):
        job=self.db.execute('SELECT signal_id,version,source FROM paper_jobs WHERE id=?',(job_id,)).fetchone()
        if not job:return
        if decisions is None:
            row=self.db.execute('SELECT decisions FROM signals WHERE id=? AND version=? AND source=?',tuple(job)).fetchone()
            decisions=[dict(d,reason=reason) if d['reason'] in {'NO_QUOTE','WAITING_MARKET'} else d for d in json.loads(row[0])] if row else []
        self.db.execute('UPDATE signals SET decisions=? WHERE id=? AND version=? AND source=?',(json.dumps(decisions,ensure_ascii=False),*job))
        self.db.execute('UPDATE paper_jobs SET status=?,reason=? WHERE id=?',('DONE',reason,job_id));self.db.commit()

    async def process_paper_job(self,job_id):
        job=self.db.execute('SELECT signal_id,version,source,status FROM paper_jobs WHERE id=?',(job_id,)).fetchone()
        if not job or job[3]!='QUEUED' or not self.running:return
        record=json.loads(self.db.execute('SELECT record FROM signals WHERE id=? AND version=? AND source=?',job[:3]).fetchone()[0])
        signal=parse_message(record)
        try:self.engine.config=self.paper_config()
        except ValueError:self._finish_job(job_id,'INSUFFICIENT_BALANCE');return
        if not any(not self.engine._check(signal,l,now()) for l in signal.legs):
            result=self.engine.process(record,now(),[]);self._finish_job(job_id,'RULES_REJECTED',result['decisions']);return
        self.db.execute("UPDATE paper_jobs SET status='READING' WHERE id=?",(job_id,));self.db.commit()
        self.paper_status='读取行情 · '+signal.home+' vs '+signal.away
        try:
            quotes,raw,failures=await asyncio.wait_for(self.read_signal_quotes(signal),timeout=75)
            current=self.db.execute('SELECT status FROM paper_jobs WHERE id=?',(job_id,)).fetchone()
            if not self.running or not self.settings['paper_market_enabled'] or not current or current[0]!='READING':return
            self.engine.config=self.paper_config() # Rules and balances are rechecked at commit time.
            if raw:self.save_observation(raw,signal,job[2])
            result=self.engine.process(record,now(),quotes)
            result['decisions']=[dict(d,reason=failures.get(d['leg_no'],'MARKET_UNAVAILABLE')) if d['reason']=='NO_QUOTE' else d for d in result['decisions']]
            self._finish_job(job_id,'PROCESSED',result['decisions'])
            self.addlog('真实页面行情模拟处理完成：'+', '.join(REASONS.get(d['reason'],d['reason']) for d in result['decisions'])+'。')
        except asyncio.CancelledError:raise
        except Exception:
            current=self.db.execute('SELECT status FROM paper_jobs WHERE id=?',(job_id,)).fetchone()
            if current and current[0]=='READING':self._finish_job(job_id,'MARKET_UNAVAILABLE')
            self.addlog('本条信号没有取得有效且唯一对应的盘口，已跳过模拟。','warning')
        finally:self.paper_status='等待新信号' if self.running else '已暂停'

    async def paper_loop(self):
        while self.running:
            self.engine.advance(now())
            if self.settings['paper_market_enabled']:
                job=self.db.execute("SELECT id FROM paper_jobs WHERE status='QUEUED' ORDER BY id LIMIT 1").fetchone()
                if job:
                    try:await self.process_paper_job(job[0])
                    except asyncio.CancelledError:raise
                    except Exception:
                        self._finish_job(job[0],'MARKET_UNAVAILABLE');self.addlog('模拟处理异常，本条已停止。','warning')
                    continue
                # Only unexpired pending orders are watched; a restart never creates a new order.
                pending=self.engine.db.execute("SELECT signal_id FROM orders WHERE status IN ('OPEN','PARTIAL') GROUP BY signal_id").fetchall()
                for order in pending:
                    if not self.running:break
                    loop=asyncio.get_running_loop()
                    if loop.time()-self.pending_refresh.get(order[0],0)<10:continue
                    self.pending_refresh[order[0]]=loop.time()
                    row=self.db.execute("SELECT record FROM signals WHERE id=? AND source IN ('public','telegram') ORDER BY rowid DESC LIMIT 1",(order[0],)).fetchone()
                    if not row:continue
                    signal=parse_message(json.loads(row[0]))
                    try:
                        quotes,_,_=await asyncio.wait_for(self.read_signal_quotes(signal),timeout=75)
                        if self.running and self.settings['paper_market_enabled']:
                            self.engine.advance(now(),quotes)
                    except asyncio.CancelledError:raise
                    except Exception:pass # Keep the original expiry; never reprice or resubmit.
            await asyncio.sleep(1)

    async def dispatch(self,action,data):
        if self.api_workspace:
            if action.startswith('api_'):
                return await self.api_workspace.dispatch(action[4:],data)
            if action not in {'credentials','save_settings','tg_send_code','tg_sign_in','shutdown'}:
                raise ValueError('当前工作台使用 ForeGate API。')
            if action in {'save_settings','tg_send_code','tg_sign_in'} and self.running:
                raise ValueError('请先暂停频道监听，再更改 Telegram 连接。')
            if action=='save_settings':
                if set(data)-{'channel','api_id','phone','risk_enabled'} or type(data.get('risk_enabled')) is not bool:
                    raise ValueError('Telegram 配置无效。')
                data={**self.settings, **{key:data.get(key,self.settings[key]) for key in ('channel','api_id','phone')},
                      'risk':{**self.settings['risk'],'enabled':data['risk_enabled']},'poll_interval_sec':10}
        if action=='status':return self.snapshot()
        if action.startswith('live_'):return await self.live_action(action,data)
        if (self.live_armed or self.execution_busy) and action in {'auto_test_prepare','auto_test_execute','auto_test_reconcile','browser_open','market_open','inspect_market','paper_rehearsal','web_test_open','web_test_prepare','web_test_read','browser_check','save_settings','start'}:
            raise ValueError('频道实盘运行中，请先暂停实盘再操作浏览器或参数。')
        if action in {'auto_test_prepare','auto_test_execute','auto_test_cancel','auto_test_reconcile'}:
            return await self.auto_test_action(action,data)
        active=self.live_journal.latest()
        if (self.live_busy or active and active['state']=='READY') and action in {'start','browser_open','market_open','inspect_market','paper_rehearsal','web_test_open','web_test_prepare'}:
            raise ValueError('自动实盘测试正在准备、待启用或运行，请先完成或取消准备。')
        if action=='credentials':
            self.credentials={k:v for k,v in data.items() if k in {'api_hash','bot_token'}};return dict(ok=True)
        if action=='save_settings':
            if set(data)-set(defaults()):raise ValueError('配置中存在未知字段。')
            new=defaults();new.update(data)
            if new['mode']=='live':raise ValueError('实盘尚未通过订单对账验证，当前不能启用。')
            self.validate_settings(new)
            if self.account_ready and new['phone']!=self.settings['phone']:
                raise ValueError('当前已有 Telegram 会话；更换账户前需另建独立会话，当前版本请保留原手机号。')
            changed_ledger=new['risk']['initial_balance']!=self.settings['risk']['initial_balance'] or new['risk']['timezone']!=self.settings['risk']['timezone']
            if changed_ledger and self.engine.db.execute('SELECT COUNT(*) FROM orders').fetchone()[0]:
                raise ValueError('已有模拟订单，不能修改初始资金或账本时区。')
            connection_changed=any(new[k]!=self.settings[k] for k in ('channel','api_id','phone'))
            if (connection_changed or not new['paper_market_enabled'] or not new['risk']['enabled']) and self.running:await self.pause()
            self.engine.db.execute('BEGIN IMMEDIATE')
            try:
                if changed_ledger:
                    self.engine.db.execute("UPDATE meta SET value=? WHERE key='initial_balance'",(new['risk']['initial_balance'],))
                    self.engine.db.execute("UPDATE meta SET value=? WHERE key='timezone'",(new['risk']['timezone'],))
                self.engine.db.execute("INSERT OR REPLACE INTO meta VALUES ('desktop_settings',?)",(json.dumps(new,ensure_ascii=False),))
                self.engine.db.execute('COMMIT')
            except Exception:self.engine.db.execute('ROLLBACK');raise
            self.settings=new;self.engine.config=Config(**new['risk'])
            try:atomic_json(self.root/'settings.json',new)
            except OSError:self.addlog('参数已保存到账本；配置快照文件未能写入。','warning')
            self.addlog('参数已保存。新参数应用于后续信号，已有委托保留原参数。')
            return self.api_workspace.settings_view() if self.api_workspace else self.snapshot()
        if action=='start':
            if self.web_test and self.web_test.get('status')=='AWAITING_USER':raise ValueError('网页测试面板已准备，请完成核对后重启客户端再启动监听，避免覆盖测试页面。')
            if self.settings['mode']!='paper':raise ValueError('实盘未就绪。')
            if self.running:return self.snapshot()
            if not self.settings['risk']['enabled']:raise ValueError('请先在跟单参数中启用总开关。')
            self.error='';self.running=True;self.task=asyncio.create_task(self.listen());self.paper_task=asyncio.create_task(self.paper_loop());self.paper_status='等待新信号';self.addlog('开始监听频道，按已保存规则处理新信号。');return self.snapshot()
        if action=='pause':
            if self.live_armed or self.execution_task:await self.stop_execution()
            await self.pause();return self.snapshot()
        if action=='import_samples':
            path=Path(__file__).resolve().parents[1]/'fixtures/messages.jsonl'
            for line in path.read_text(encoding='utf-8').splitlines():self.ingest(json.loads(line),'sample',True)
            self.addlog('已载入 200 条历史样本，仅作解析查看，不补下单。');return self.snapshot()
        if action=='demo':return self.demo()
        if action=='tg_send_code':return await self.send_code()
        if action=='tg_sign_in':return await self.sign_in(data)
        if action=='browser_open':await self.open_browser();return self.snapshot()
        if action=='browser_check':await self.check_browser();return self.snapshot()
        if action=='web_test_open':await self.open_web_test(data);return self.snapshot()
        if action=='web_test_read':await self.read_web_test();return self.snapshot()
        if action=='web_test_prepare':await self.prepare_web_test(data);return self.snapshot()
        if action=='market_open':
            if not re.fullmatch(r'\d{1,30}',str(data.get('event_id',''))):raise ValueError('无效事件编号。')
            await self.open_browser(str(data['event_id']));return self.snapshot()
        if action=='inspect_market':await self.inspect_market(data);return self.snapshot()
        if action=='paper_rehearsal':await self.rehearse(data);return self.snapshot()
        if action=='cancel_paper':
            order_id=data.get('order_id')
            if type(order_id) is not int or order_id<=0:raise ValueError('无效模拟订单编号。')
            self.engine.cancel(order_id,now());self.addlog('已撤销模拟委托的未成交部分。');return self.snapshot()
        if action=='export':
            out=io.StringIO();w=csv.writer(out);w.writerow(['信号编号','来源','类型','赛事','玩法','选项','方向','让分','收益率','状态/原因'])
            for signal_id,source,parsed in self.db.execute('SELECT id,source,parsed FROM signals ORDER BY seen_at DESC,rowid DESC'):
                s=json.loads(parsed)
                for l in s['legs'] or [{}]:
                    vals=[signal_id,source,s['type'],s['home']+' vs '+s['away'],l.get('market_type',''),l.get('selection',''),l.get('side',''),l.get('line',''),l.get('profit_pct',''),l.get('reason','') or s['reason']]
                    # Neutralize spreadsheet formula injection in external channel text.
                    w.writerow(["'"+str(v) if str(v).startswith(('=','+','-','@')) else v for v in vals])
            return dict(csv=out.getvalue())
        if action=='shutdown':await self.close();return dict(ok=True)
        raise ValueError('不支持的操作。')

    async def pause(self):
        self.running=False
        for task in [self.task,self.paper_task]:
            if task:
                task.cancel()
                try:await task
                except asyncio.CancelledError:pass
        self.task=None;self.paper_task=None;self.paper_status='已暂停'
        for job in self.db.execute("SELECT id FROM paper_jobs WHERE status IN ('QUEUED','READING')").fetchall():self._finish_job(job[0],'PAUSED_QUEUE')
        self.addlog('已暂停跟单；频道记录和现有数据保留。')

    async def get_client(self):
        api_id=str(self.settings['api_id']);secret=self.credentials.get('api_hash','')
        if not api_id or not secret:raise ValueError('请先填写 Telegram API ID、API Hash 和手机号并保存。')
        identity=(api_id,secret)
        if self.client and self.tg_api_identity!=identity:
            await self.client.disconnect();self.client=None;self.account_ready=False
        if not self.client:
            from telethon import TelegramClient,events
            self.client=TelegramClient(str(self.root/'telegram'),int(api_id),secret,catch_up=True)
            self.tg_api_identity=identity
            async def receive(event):
                from telethon.utils import get_peer_id
                if not self.running or not self.entity or event.chat_id!=get_peer_id(self.entity):return
                try:self.ingest(self.to_record(event.message,self.settings['channel']))
                except Exception:self.error='有一条频道更新未能处理，已保留连接，请查看网络和消息格式。';self.addlog(self.error,'error')
            self.client.add_event_handler(receive,events.NewMessage())
            self.client.add_event_handler(receive,events.MessageEdited())
        if not self.client.is_connected():await self.client.connect()
        return self.client

    async def send_code(self):
        if not self.settings['phone']:raise ValueError('请先填写手机号并保存。')
        c=await self.get_client()
        if await c.is_user_authorized():
            await self.verify_account(c)
            self.account_ready=True;self.tg_status='Telegram 已授权'
            return self.api_workspace.settings_view() if self.api_workspace else self.snapshot()
        try:
            sent=await c.send_code_request(self.settings['phone']);self.code_hash=sent.phone_code_hash
        except Exception as exc:
            from telethon.errors import FloodWaitError
            if isinstance(exc,FloodWaitError):raise ValueError('Telegram 限流，请等待 '+str(exc.seconds)+' 秒后再试。') from None
            raise ValueError('验证码发送失败，请检查 API 参数、手机号及网络。') from None
        self.tg_status='等待验证码'
        return dict(next='code',state=self.api_workspace.settings_view() if self.api_workspace else self.snapshot())

    async def sign_in(self,data):
        c=await self.get_client()
        from telethon.errors import SessionPasswordNeededError
        try:
            if data.get('password'):await c.sign_in(password=data['password'])
            else:await c.sign_in(phone=self.settings['phone'],code=data.get('code',''),phone_code_hash=self.code_hash)
        except SessionPasswordNeededError:
            self.tg_status='等待两步验证密码'
            return dict(next='password',state=self.api_workspace.settings_view() if self.api_workspace else self.snapshot())
        except Exception:raise ValueError('登录未完成，请检查验证码或两步验证密码，过期时重新获取验证码。') from None
        await self.verify_account(c)
        self.account_ready=True;self.tg_status='Telegram 已授权';self.addlog('Telegram 授权完成。验证码和密码不保存。')
        return self.api_workspace.settings_view() if self.api_workspace else self.snapshot()

    async def verify_account(self,c):
        me=await c.get_me()
        configured=self.settings['phone'].replace(' ','').lstrip('+')
        if configured and configured!=str(getattr(me,'phone','')):
            self.account_ready=False
            raise ValueError('保存的手机号与本机 Telegram 会话不一致，请恢复该会话对应的手机号后重试。')

    @staticmethod
    def to_record(m,channel):
        links=[]
        for e in m.entities or []:
            if getattr(e,'url',None):links.append(e.url)
        for row in getattr(m,'buttons',None) or []:
            for b in row:
                if getattr(b,'url',None):links.append(b.url)
        return dict(channel=channel,message_id=m.id,date=m.date.isoformat(),edit_date=m.edit_date.isoformat() if m.edit_date else None,text=m.message or '',links=links,reply_url=('https://t.me/'+channel+'/'+str(m.reply_to_msg_id)) if m.reply_to_msg_id else None,channel_id=m.chat_id)

    async def listen(self):
        try:
            if self.settings['api_id'] and self.credentials.get('api_hash'):
                c=await self.get_client()
                if not await c.is_user_authorized():self.running=False;self.tg_status='需要 Telegram 授权';self.error='请到账户连接中完成 Telegram 登录。';return
                await self.verify_account(c)
                self.account_ready=True;self.entity=await c.get_entity(self.settings['channel']);self.tg_status='Telegram 实时连接'
                # Catch up safely: old entries are subject to original-date expiry, edits included.
                async for m in c.iter_messages(self.entity,limit=200):
                    self.ingest(self.to_record(m,self.settings['channel']))
                await c.catch_up()
                reconciled=asyncio.get_running_loop().time()
                while self.running:
                    if not c.is_connected():self.tg_status='Telegram 正在重连'
                    else:self.tg_status='Telegram 实时连接'
                    # Public channels may not push updates unless the account is a member.
                    # Reconcile recent history as a fallback without joining on the user's behalf.
                    if c.is_connected() and asyncio.get_running_loop().time()-reconciled>=self.settings['poll_interval_sec']:
                        reconciled=asyncio.get_running_loop().time()
                        async for m in c.iter_messages(self.entity,limit=30):self.ingest(self.to_record(m,self.settings['channel']))
                    if not self.api_workspace:self.engine.advance(now())
                    await asyncio.sleep(5)
            else:
                self.addlog('未授权 Telegram 实时连接，使用公开预览采集；延迟可能超过采集间隔。')
                while self.running:
                    started=asyncio.get_running_loop().time()
                    self.tg_status=f"公开预览 · 每 {self.settings['poll_interval_sec']} 秒检查"
                    def fetch():
                        url='https://t.me/s/'+self.settings['channel']
                        with urllib.request.urlopen(url,timeout=20) as r:return extract(r.read().decode('utf-8'))[0]
                    records=await asyncio.to_thread(fetch)
                    for r in records:
                        key=(r['message_id'],r['text'])
                        if key not in self.seen:self.ingest(r,'public');self.seen.add(key)
                    if not self.api_workspace:self.engine.advance(now())
                    elapsed=asyncio.get_running_loop().time()-started
                    await asyncio.sleep(max(0,self.settings['poll_interval_sec']-elapsed))
        except asyncio.CancelledError:raise
        except Exception:
            self.running=False;self.tg_status='连接异常';self.error='采集连接中断，请检查网络后重新启动。';self.addlog(self.error,'error')
            if self.live_armed:self.live_disarm('频道连接异常，暂停实盘。')
        finally:
            if not self.running:
                if self.api_workspace and self.api_workspace.armed_at:
                    self.api_workspace.pause('频道监听已停止，已暂停自动跟单。')
                if self.live_armed:self.live_disarm('频道监听已停止，已暂停实盘。')
                if self.paper_task:self.paper_task.cancel()
                for job in self.db.execute("SELECT id FROM paper_jobs WHERE status IN ('QUEUED','READING')").fetchall():self._finish_job(job[0],'INTERRUPTED_QUEUE')

    async def open_browser(self,event_id=None):
        async with self.browser_lock:
            page=await self.browser_page()
            await page.goto('https://www.foregate.com/en'+('/event/'+event_id if event_id else ''),wait_until='domcontentloaded',timeout=45000)
            await page.bring_to_front();self.fg_status='专用浏览器已打开，请登录';self.addlog('已打开 ForeGate 专用浏览器，登录态仅保存在本机。')

    async def browser_page(self):
        # Caller holds browser_lock for navigation, selection and the complete read.
        if not self.browser:
            from playwright.async_api import async_playwright
            if self.pw:await self.pw.stop()
            self.pw=await async_playwright().start()
            self.browser=await self.pw.chromium.launch_persistent_context(str(self.root/'foregate-browser'),channel='chrome',headless=False,no_viewport=True,accept_downloads=False)
            self.browser.on('close',self.browser_closed)
        return self.browser.pages[0] if self.browser.pages else await self.browser.new_page()

    def browser_closed(self,*args):self.browser=None;self.fg_status='专用浏览器已关闭';self.balance='—'

    async def check_browser(self):
        async with self.browser_lock:
            if not self.browser:raise ValueError('请先打开 ForeGate 登录窗口。')
            page=self.browser.pages[0]
            available=page.get_by_role('button',name=re.compile(r'^Available'))
            if await available.count()==1:
                text=await available.inner_text();self.balance=' '.join(text.split()).replace('Available','').strip();self.fg_status='已检测到登录态'
            else:self.fg_status='尚未检测到登录，请在专用窗口完成登录';self.balance='—'

    async def auto_test_action(self,action,data):
        if self.live_busy:raise ValueError('自动测试正在运行，请勿重复操作。')
        if action in {'auto_test_prepare','auto_test_execute'} and self.live_store.totals(now(),self.settings['risk'])['spent']>0:
            raise ValueError('频道实盘已占用本轮验收额度，不能再提交单次测试。')
        if action=='auto_test_reconcile':
            plan=self.live_journal.latest()
            if not plan:raise ValueError('尚无自动测试记录。')
            if plan['state'] in {'ACTIVITY_CONFIRMED','PARTIAL_ACTIVITY'}:return self.snapshot()
            if plan['state']!='UNKNOWN':raise ValueError('只核对结果不明的已提交测试；不会发起新买入。')
            self.live_busy=True
            try:
                await self.pause()
                async with self.browser_lock:
                    page=await self.browser_page()
                    from .live_test import new_receipt
                    receipt=new_receipt(plan['baseline'],await self.live_runner.activity(page,plan['target']),plan['target'])
                    if receipt:self.live_journal.update(plan['id'],receipt['status'],receipt=receipt,reason='只读核对发现对应新增活动；没有再次提交。')
                return self.snapshot()
            finally:self.live_busy=False
        if action=='auto_test_cancel':
            plan=self.live_journal.latest()
            if not plan or plan['state']!='READY':raise ValueError('只有尚未提交的准备计划可以取消。')
            self.live_journal.update(plan['id'],'ABORTED',reason='用户取消准备，未下单。');return self.snapshot()
        if action=='auto_test_execute' and data.get('native_authorized') is not True:
            raise ValueError('请在客户端亲自启用自动交易测试。')
        self.live_busy=True
        try:
            await self.pause()
            async with self.browser_lock:
                page=await self.browser_page()
                if len([p for p in self.browser.pages if not p.is_closed()])!=1:raise ValueError('专用浏览器必须只保留一个页面。')
                if action=='auto_test_prepare':
                    t=live_target(data.get('event_id'),data.get('selection'),data.get('amount'),data.get('max_price_cents'))
                    await self.live_runner.prepare(page,t)
                    self.addlog('单次自动实盘测试准备完成；尚未启用，未提交。')
                else:
                    await self.live_runner.execute(page,str(data.get('plan_id','')))
                    self.addlog('单次自动实盘测试已停止；请查看自动测试的成交核对状态。')
            return self.snapshot()
        finally:self.live_busy=False

    async def open_web_test(self,data):
        event=str(data.get('event_id',''));selection=str(data.get('selection','')).strip()
        if not re.fullmatch(r'\d{1,30}',event) or not re.fullmatch(r'[A-Za-z0-9_-]{1,24}',selection):
            raise ValueError('请输入事件编号和网页 Moneyline 选项缩写。')
        await self.pause()
        async with self.browser_lock:
            page=await self.browser_page()
            await page.goto('https://www.foregate.com/en/event/'+event,wait_until='domcontentloaded',timeout=30000)
            await page.locator('[data-outcome-button]').first.wait_for(state='visible',timeout=20000)
            raw=await page.evaluate((Path(__file__).parent/'market_dom.js').read_text())
            matches=[o for o in raw['outcomes'] if o['group']=='Moneyline' and price_label(o['text'])['selection'].casefold()==selection.casefold()]
            if len(matches)!=1:raise ValueError('未找到唯一 Moneyline 选项，请在浏览器亲自核对。')
            await page.locator('[data-outcome-button]').nth(matches[0]['index']).click()
            await page.get_by_role('textbox',name='0.00',exact=True).wait_for(state='visible',timeout=10000)
            await page.bring_to_front()
        await self.read_web_test()

    async def prepare_web_test(self,data):
        # Stage a user-selected BUY/Market form only; no submit or financial click.
        from urllib.parse import urlsplit
        try:amount=Decimal(str(data.get('amount','')))
        except Exception:raise ValueError('请填写有效的测试金额。') from None
        if not amount.is_finite() or amount<=0 or amount>2 or amount.as_tuple().exponent < -2:
            raise ValueError('测试买入金额须大于零、不超过 2 USDT，最多两位小数；含费总支出仍须在网页确认。')
        await self.pause()
        async with self.browser_lock:
            if not self.browser:raise ValueError('请先打开专用浏览器，亲自选择测试赛事和方向。')
            pages=[p for p in self.browser.pages if not p.is_closed()]
            if len(pages)!=1:raise ValueError('请在专用浏览器仅保留一个测试页面。')
            page=pages[0];url=urlsplit(page.url)
            if url.scheme!='https' or url.hostname!='www.foregate.com' or not re.fullmatch(r'/en/event/\d+',url.path):
                raise ValueError('测试准备仅支持 ForeGate 英文赛事页面。')
            buy=page.get_by_role('tab',name='Buy',exact=True)
            market=page.get_by_role('combobox',name='Market',exact=True)
            amount_input=page.get_by_role('textbox',name='0.00',exact=True)
            if await buy.count()!=1 or await buy.get_attribute('aria-selected')!='true' or await market.count()!=1 or await amount_input.count()!=1:
                raise ValueError('首轮测试只准备已选定的 Buy / Market 面板，不会自动切换买卖方向。')
            await amount_input.fill(format(amount,'f'))
            await page.bring_to_front()
        await self.read_web_test()
        self.web_test['status']='AWAITING_USER';self.web_test['prepared_amount']=format(amount,'f')
        self.web_test['notice']='已填写买入金额，未提交订单，监听已暂停。请亲自核对赛事、盘口、方向及含费总支出不超过 2 USDT，再决定是否点击网页 Trade。完成后读取网页与订单记录；结果不明确时不要再次提交。'
        self.addlog('网页测试金额已准备，等待用户在网页完成最终操作。')

    async def read_web_test(self):
        # Read the user's current page without navigation or any trade interaction.
        # A visible toast or balance change is evidence only, never a confirmed order.
        from urllib.parse import urlsplit
        async with self.browser_lock:
            if not self.browser:raise ValueError('请先打开 ForeGate 专用浏览器并选择测试赛事。')
            pages=[p for p in self.browser.pages if not p.is_closed()]
            if len(pages)!=1:raise ValueError('请在专用浏览器仅保留一个测试页面，以免读取错误的订单。')
            page=pages[0];url=urlsplit(page.url)
            if url.scheme!='https' or url.hostname!='www.foregate.com':raise ValueError('只能读取 ForeGate 官方交易页面。')
            raw=await page.evaluate((Path(__file__).parent/'web_test_dom.js').read_text())
            market=await page.evaluate((Path(__file__).parent/'market_dom.js').read_text())
            raw['selected_options']=[o for o in market['outcomes'] if o.get('selected')]
            if re.fullmatch(r'/en/event/\d+', url.path) and any(o['group']=='Moneyline' for o in market['outcomes']):
                from .position import DOM
                raw['visible_moneyline_position'] = await page.evaluate(DOM, dict(group='Moneyline', mode='read'))
            raw['observed_at']=now().isoformat();raw['spend_cap']='2.00'
            raw['status']='AWAITING_USER' if self.web_test and self.web_test.get('status')=='AWAITING_USER' else 'READ_ONLY_EVIDENCE';raw['verified_order']=False
            raw['notice']='网页测试总支出上限 2 USDT。此操作只读取页面；最终下单由你操作。提示、余额变化或持仓变化均不单独证明本次下单成功，需核对订单号、盘口、金额与成交状态。'
            try:
                preview=self.root/'web-read-only-preview.png'
                await page.screenshot(path=str(preview),full_page=False);os.chmod(preview,0o600)
                raw['preview_path']=str(preview)
            except Exception:pass
            self.web_test=raw
        self.addlog('已读取网页测试面板；未发送订单。')

    def signal_record(self,data):
        row=self.db.execute('SELECT record FROM signals WHERE id=? AND source=? ORDER BY seen_at DESC,rowid DESC LIMIT 1',(str(data.get('signal_id','')),str(data.get('source','')))).fetchone()
        if not row:raise ValueError('请从信号记录选择一条已有消息进行核对。')
        signal=parse_message(json.loads(row[0]))
        if signal.type!='PICK' or not signal.event_id:raise ValueError('这条消息不是可核对的下单信号。')
        return json.loads(row[0]),signal

    async def read_market(self,signal):
        if len(signal.legs)!=1:raise ValueError('请逐盘口读取订单簿。')
        leg=signal.legs[0]
        if not supported_leg(leg):raise ValueError('这个方向或玩法尚未验证。')
        async with self.browser_lock:
            page=await self.browser_page()
            # A fresh navigation prevents a suspended tab's old DOM being treated as a fresh read.
            await page.goto('https://www.foregate.com/en/event/'+signal.event_id,wait_until='domcontentloaded',timeout=20000)
            try:await page.locator('[data-outcome-button]').first.wait_for(state='visible',timeout=20000)
            except Exception:raise ValueError('未读取到可见盘口，请确认赛事仍可访问，稍后重试。') from None
            script=(Path(__file__).parent/'market_dom.js').read_text()
            raw=await page.evaluate(script)
            visited=set()
            # Scope to one exact market group; bounded arrow navigation also handles
            # duplicate numeric labels used by opposite handicap lines.
            for _ in range(24):
                candidates(raw,signal) # Also checks URL and event team names on every step.
                if any(display_identity(o)==leg.identity() for o in raw['outcomes']):break
                fingerprint=json.dumps([(o.get('group'),price_label(o.get('text',''))['selection']) for o in raw['outcomes']])
                if fingerprint in visited:raise ValueError('档位切换没有进展，已停止。')
                visited.add(fingerprint)
                groups=[g for g in raw.get('groups',[]) if group_type(g['group'])==leg.market_type]
                if len(groups)!=1 or not groups[0]['can_next'] or groups[0]['next_index']<0:break
                next_button=page.locator('[aria-label="next line"]').nth(groups[0]['next_index'])
                if not await next_button.is_enabled():break
                before=json.dumps([[o.get('group'),price_label(o.get('text',''))['selection']] for o in raw['outcomes']],ensure_ascii=False,separators=(',',':'))
                await next_button.click()
                predicate='JSON.stringify(('+script+')().outcomes.map(o=>[o.group,o.text.replace(/\\s+\\d+(?:\\.\\d+)?¢$/, "")])) !== '+json.dumps(before)
                try:await page.wait_for_function(predicate,timeout=2500)
                except Exception:raise ValueError('盘口档位没有更新，已停止。') from None
                raw=await page.evaluate(script)
            possible=[m for m in candidates(raw,signal) if m['reason']=='DISPLAY_MATCH']
            if len(possible)>1:raise ValueError('同一消息有多个可选盘口，尚未验证逐盘口读取，已跳过。')
            # Select the exact card and inspect its visible book. Never enter amounts or trade.
            if len(possible)==1:
                text=possible[0]['outcome']['text']
                if not possible[0]['outcome'].get('selected') or raw.get('book',{}).get('count')!=1:
                    # Index identifies the already verified group, avoiding identical OVER labels
                    # in match totals and each team's separate totals market.
                    fresh=await page.evaluate(script)
                    found=[o for o in fresh['outcomes'] if o['index']==possible[0]['outcome']['index']]
                    if len(found)!=1 or found[0]['group']!=possible[0]['outcome']['group'] or found[0]['text']!=text:
                        raise ValueError('报价或页面布局已变化，请重新读取。')
                    await page.locator('[data-outcome-button]').nth(found[0]['index']).click()
                try:
                    await page.locator('[aria-label="Orderbook"]').wait_for(state='visible',timeout=10000)
                    # An empty/suspended or previously selected book must never be labelled fresh.
                    await page.wait_for_function('(() => {const r=('+script+')(); const s=r.outcomes.filter(o=>o.selected); if(s.length!==1 || r.book.count!==1 || !r.book.asks.length)return false; const p=s[0].text.match(/\\s+(\\d+(?:\\.\\d+)?)¢$/); return p && r.book.asks.some(a=>a.key==="ask-"+String(Number(p[1])/100));})()',timeout=5000)
                except Exception:raise ValueError('选中盘口的订单簿尚未就绪，请稍后重试。') from None
                raw=await page.evaluate(script)
                raw['reference_outcome']=possible[0]['outcome']
            return raw,now()

    async def read_signal_quotes(self,signal):
        quotes=[];last=None;failures={}
        if len(signal.legs)>8:raise ValueError('单条消息超过 8 个盘口，已停止处理。')
        for leg in signal.legs:
            if not supported_leg(leg):failures[leg.leg_no]='MAPPING_NOT_VERIFIED';continue
            try:
                single=replace(signal,legs=[leg])
                raw,read_at=await asyncio.wait_for(self.read_market(single),timeout=40)
                last=raw
                quotes.extend(quotes_from_page(raw,single,read_at,self.settings['paper_fee_rate']))
            except asyncio.CancelledError:raise
            except Exception:failures[leg.leg_no]='MARKET_UNAVAILABLE'
        return quotes,last,failures

    def save_observation(self,raw,signal,source,leg_no=None):
        result=observation(raw,signal);result['source']=source
        if leg_no is not None:result['leg_no']=leg_no
        self.market_observation=result
        self.db.execute('INSERT OR REPLACE INTO market_observations VALUES(?,?,?)',(signal.event_id,json.dumps(result,ensure_ascii=False),result['observed_at']))
        self.db.commit()

    async def inspect_market(self,data):
        _,signal=self.signal_record(data)
        legs=[l for l in signal.legs if supported_leg(l)]
        if not legs:raise ValueError('此消息的玩法或 NO 方向尚未验证。')
        leg=next((l for l in legs if l.leg_no==data.get('leg_no')),None) if data.get('leg_no') is not None else legs[0]
        if not leg:raise ValueError('所选盘口不可读取，请重新选择。')
        raw,_=await self.read_market(replace(signal,legs=[leg]))
        self.save_observation(raw,signal,str(data['source']),leg.leg_no)
        self.addlog('已读取可见盘口；可用于独立行情试算，不能用于实盘。')
        await self.check_browser()

    async def rehearse(self,data):
        record,signal=self.signal_record(data)
        quotes,raw,failures=await asyncio.wait_for(self.read_signal_quotes(signal),timeout=75)
        if raw:self.save_observation(raw,signal,str(data['source']))
        if not quotes:raise ValueError('没有读取到可供试算的精确盘口及有效深度。')
        read_at=now()
        # An explicit rehearsal bypasses historical age only in an isolated in-memory ledger.
        record=dict(record,date=read_at.isoformat(),channel='PAPER_REHEARSAL')
        engine=PaperEngine(':memory:',self.paper_config())
        try:
            result=engine.process(record,now(),quotes)
            self.rehearsal=dict(original_signal_id=signal.signal_id,original_date=signal.date,
                observed_at=read_at.isoformat(),quotes=describe(quotes),
                decisions=[dict(d,reason=failures.get(d['leg_no'],d['reason']) if d['reason']=='NO_QUOTE' else d['reason'],label=REASONS.get(failures.get(d['leg_no'],d['reason']) if d['reason']=='NO_QUOTE' else d['reason'],d['reason'])) for d in result['decisions']],
                ledger=engine.snapshot(now()),notice='独立试算：使用当前盘口和保存参数，忽略原消息过期；不计入正式模拟资金或额度。')
        finally:engine.close()
        self.addlog('已完成当前行情独立试算，正式模拟账本未改动。')

    def demo(self):
        # Demo ledger is separate and never alters actual paper funds or histories.
        cfg=Config(**self.settings['risk']);engine=PaperEngine(':memory:',cfg)
        try:
            t=now();r=dict(channel='SYNTHETIC_DEMO',message_id=1,date=t.isoformat(),text='⚽ 演示联赛: Alpha vs Beta\n🟢 Moneyline: ALP (+100%)\nhttps://www.foregate.com/en/event/999999999999999999',links=[])
            s=parse_message(r)
            q=Quote('demo-1',s.event_id,s.home,s.away,s.legs[0].identity(),'SYNTHETIC_MARKET',t.isoformat(),'0.50','2','0.02','0.50','SYNTHETIC_DEMO')
            result=engine.process(r,t,[q]);self.demo_orders=[dict(o,source='合成演示') for o in engine.snapshot(t)['orders']]
            self.addlog('已运行独立合成案例：'+', '.join(REASONS.get(d['reason'],d['reason']) for d in result['decisions'])+'；不计入正式模拟账户。')
        finally:engine.close()
        return self.snapshot()

    async def close(self):
        if self.closed:return
        await self.stop_execution()
        await self.pause()
        if self.api_workspace:await self.api_workspace.close()
        for task in list(self.position_tasks):task.cancel()
        if self.position_tasks:await asyncio.gather(*self.position_tasks,return_exceptions=True)
        if self.client:await self.client.disconnect()
        if self.browser:await self.browser.close()
        if self.pw:await self.pw.stop()
        self.engine.close();self.db.close();self.live_journal.close();self.live_store.close()
        self.closed=True


async def main():
    ap=argparse.ArgumentParser();ap.add_argument('--data-dir',required=True);ap.add_argument('--api-client',action='store_true');args=ap.parse_args()
    service=Service(args.data_dir,api_mode=args.api_client);inflight=set()
    async def respond(request):
        try:
            result=await service.dispatch(request['action'],request.get('data',{}))
            response=dict(id=request.get('id'),ok=True,data=result)
        except ValueError as e:response=dict(id=request.get('id'),ok=False,error=str(e))
        except Exception:
            error='操作未完成，请查看订单与成交页的本机提交记录。' if args.api_client else '操作未完成。若已启用自动实盘测试，请查看测试状态；提交结果不明时不要重复启用。'
            response=dict(id=request.get('id'),ok=False,error=error)
        print(json.dumps(response,ensure_ascii=False),flush=True)
    try:
        while True:
            line=await asyncio.to_thread(sys.stdin.readline)
            if not line:break
            try:request=json.loads(line)
            except ValueError:
                print(json.dumps(dict(id=None,ok=False,error='无效请求。'),ensure_ascii=False),flush=True);continue
            if request.get('action')=='shutdown':
                for task in inflight:task.cancel()
                await asyncio.gather(*inflight,return_exceptions=True)
                await respond(request);return
            # A slow page read must not block status, cancellation or pause requests.
            task=asyncio.create_task(respond(request));inflight.add(task);task.add_done_callback(inflight.discard)
    finally:
        for task in inflight:task.cancel()
        await asyncio.gather(*inflight,return_exceptions=True)
        if not service.closed:await service.close()

if __name__=='__main__':asyncio.run(main())
