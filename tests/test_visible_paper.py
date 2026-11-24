import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock
from copytrader.desktop_service import Service,now
from copytrader.parser import parse_message
from copytrader.paper import PaperEngine,Config
from copytrader.visible_paper import quotes_from_page


def raw_page():
    return json.loads((Path(__file__).resolve().parent/'fixtures/market_observation_public.json').read_text())


def record(date=None):
    return dict(channel='AITradeCopy',message_id=1266,date=(date or now()).isoformat(),
                text='🎮 CS2: magic vs Heroic (BO3)\n🟢 Moneyline: HERO\n💵 Est. Profit: 69.49%\nhttps://www.foregate.com/en/event/480194146926072329',links=[])


class VisibleQuoteTests(unittest.TestCase):
    def test_paper_identity_and_local_timestamp_are_explicit(self):
        t=now();r=record(t);q=quotes_from_page(raw_page(),parse_message(r),t,'0.02')[0]
        self.assertTrue(q.market_id.startswith('paper:visible:'))
        self.assertEqual(q.source,'VISIBLE_BOOK_PAPER')
        self.assertEqual(q.observed_at,t.isoformat())
        self.assertEqual(q.ask,'0.59')

    def test_unmatched_selected_book_and_no_direction_rejected(self):
        r=record();s=parse_message(r);s.legs[0].side='NO'
        with self.assertRaises(ValueError):quotes_from_page(raw_page(),s,now(),'0.02')
        raw=raw_page();raw['outcomes'][0]['selected']=True;raw['outcomes'][1]['selected']=False
        with self.assertRaises(ValueError):quotes_from_page(raw,parse_message(r),now(),'0.02')

    def test_repeated_reads_do_not_replenish_and_survive_restart(self):
        with tempfile.TemporaryDirectory() as root:
            t=now();r=record(t);s=parse_message(r);raw=raw_page()
            raw['book']['asks']=[dict(key='ask-0.59',text='59¢ 2.00 $1.18')]
            engine=PaperEngine(Path(root)/'paper.db')
            q=quotes_from_page(raw,s,t,'0.02')
            engine.process(r,t,q);engine.close()
            engine=PaperEngine(Path(root)/'paper.db')
            try:
                later=t+timedelta(seconds=1)
                engine.advance(later,quotes_from_page(raw,s,later,'0.02'))
                self.assertEqual(Decimal(engine.snapshot(later)['orders'][0]['filled']),2)
                raw['book']['asks'][0]['text']='59¢ 3.00 $1.77'
                later+=timedelta(seconds=1)
                engine.advance(later,quotes_from_page(raw,s,later,'0.02'))
                self.assertEqual(Decimal(engine.snapshot(later)['orders'][0]['filled']),3)
            finally:engine.close()

    def test_slippage_anchored_before_selection(self):
        t=now();r=record(t);raw=raw_page();raw['reference_outcome']=dict(group='Moneyline',text='HERO 50¢')
        q=quotes_from_page(raw,parse_message(r),t,'0.02')
        engine=PaperEngine(':memory:')
        try:
            result=engine.process(r,t,q)
            self.assertEqual(result['decisions'][0]['reason'],'PAPER_OPEN')
            self.assertEqual(engine.snapshot(t)['orders'][0]['filled'],'0')
        finally:engine.close()

    def test_one_event_rule_applies_between_legs_in_same_transaction(self):
        t=now();r=record(t);r['text']=r['text'].replace('💵 Est. Profit: 69.49%', '')
        r['text']=r['text'].replace('🟢 Moneyline: HERO','🟢 Moneyline: HERO (+69.49%)\n🟢 Moneyline: MGC (+100%)')
        s=parse_message(r);q=quotes_from_page(raw_page(),s,t,'0.02')[0]
        other=replace(q,quote_id='other',market_id='other',leg_identity=s.legs[1].identity())
        engine=PaperEngine(':memory:',Config(one_leg_per_event=True))
        try:
            result=engine.process(r,t,[q,other])
            self.assertEqual([d['reason'] for d in result['decisions']],['PAPER_FILLED','EVENT_ALREADY_FOLLOWED'])
        finally:engine.close()


class PaperPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.service=Service(self.temp.name)
        self.service.running=True
        async def read(s):return raw_page(),now()
        self.service.read_market=AsyncMock(side_effect=read)

    async def asyncTearDown(self):
        await self.service.close();self.temp.cleanup()

    def job(self):return self.service.db.execute('SELECT id FROM paper_jobs ORDER BY id DESC LIMIT 1').fetchone()[0]

    async def test_fresh_signal_to_real_book_paper_order_idempotent(self):
        r=record();self.service.ingest(r,'public');job=self.job()
        await self.service.process_paper_job(job)
        self.assertFalse(self.service.ingest(r,'public'))
        await self.service.process_paper_job(job)
        snap=self.service.snapshot()
        self.assertEqual(len(snap['ledger']['orders']),1)
        self.assertEqual(snap['ledger']['orders'][0]['status'],'FILLED')
        self.assertEqual(snap['signals'][0]['decisions'][0]['reason'],'PAPER_FILLED')
        self.service.read_market.assert_awaited_once()

    async def test_expired_signals_never_queue(self):
        self.service.ingest(record(now()-timedelta(hours=1)),'public')
        self.assertEqual(self.service.snapshot()['paper_queue'],0)
        self.service.read_market.assert_not_awaited()

    async def test_sample_never_enters_formal_ledger(self):
        self.service.ingest(record(),'sample',True)
        self.assertEqual(self.service.snapshot()['paper_queue'],0)
        self.assertEqual(self.service.snapshot()['ledger']['orders'],[])

    async def test_pause_during_read_prevents_commit(self):
        started=asyncio.Event();resume=asyncio.Event()
        async def read(s):started.set();await resume.wait();return raw_page(),now()
        self.service.read_market=read;self.service.ingest(record(),'public')
        task=asyncio.create_task(self.service.process_paper_job(self.job()))
        await started.wait();await self.service.pause();resume.set();await task
        self.assertEqual(self.service.snapshot()['ledger']['orders'],[])
        self.assertEqual(self.service.snapshot()['signals'][0]['decisions'][0]['reason'],'PAUSED_QUEUE')

    async def test_edit_during_read_invalidates_old_version(self):
        started=asyncio.Event();resume=asyncio.Event()
        async def read(s):started.set();await resume.wait();return raw_page(),now()
        self.service.read_market=read;r=record();self.service.ingest(r,'public')
        task=asyncio.create_task(self.service.process_paper_job(self.job()))
        await started.wait();edited=dict(r,text='✅ 已结束');self.service.ingest(edited,'public')
        resume.set();await task
        self.assertEqual(self.service.snapshot()['ledger']['orders'],[])

    async def test_rules_changed_during_read_rechecked(self):
        async def read(s):
            self.service.settings['risk']['stake_usd']='21'
            return raw_page(),now()
        self.service.read_market=read;self.service.ingest(record(),'public')
        await self.service.process_paper_job(self.job())
        self.assertEqual(self.service.snapshot()['ledger']['orders'],[])
        self.assertEqual(self.service.snapshot()['signals'][0]['decisions'][0]['reason'],'EVENT_LIMIT')

    async def test_restart_does_not_replay_pending_jobs(self):
        r=record();self.service.ingest(r,'public')
        restarted=Service(self.temp.name)
        try:
            self.assertEqual(restarted.snapshot()['paper_queue'],0)
            self.assertFalse(restarted.running)
            self.assertEqual(restarted.snapshot()['signals'][0]['decisions'][0]['reason'],'INTERRUPTED_QUEUE')
            self.assertFalse(restarted.ingest(r,'public'))
        finally:await restarted.close()

    async def test_rehearsal_uses_old_signal_current_quote_without_formal_mutation(self):
        self.service.ingest(record(now()-timedelta(days=1)),'sample',True)
        result=await self.service.dispatch('paper_rehearsal',dict(signal_id='AITradeCopy:1266',source='sample'))
        self.assertEqual(result['ledger']['orders'],[])
        self.assertEqual(result['rehearsal']['ledger']['orders'][0]['status'],'FILLED')
        self.assertEqual(result['rehearsal']['quotes'][0]['fee_basis'],'用户配置的模拟费率')

    async def test_market_failure_skips_without_fake_quote(self):
        self.service.read_market=AsyncMock(side_effect=ValueError('bad book'))
        self.service.ingest(record(),'public');await self.service.process_paper_job(self.job())
        self.assertEqual(self.service.snapshot()['ledger']['orders'],[])
        self.assertEqual(self.service.snapshot()['signals'][0]['decisions'][0]['reason'],'MARKET_UNAVAILABLE')

    async def test_fee_validation_and_cancel(self):
        for value in ['NaN','-0.1','1']:
            cfg=deepcopy(self.service.settings);cfg['paper_fee_rate']=value
            with self.assertRaises(ValueError):await self.service.dispatch('save_settings',cfg)
        raw=raw_page();raw['book']['asks']=[dict(key='ask-0.59',text='59¢ 2.00 $1.18')]
        async def read(s):return raw,now()
        self.service.read_market=read;self.service.ingest(record(),'public');await self.service.process_paper_job(self.job())
        result=await self.service.dispatch('cancel_paper',dict(order_id=1))
        self.assertEqual(result['ledger']['orders'][0]['status'],'CANCELLED')
        self.assertEqual(Decimal(result['ledger']['balances']['reserved']),0)

    async def test_multiple_legs_read_separately_one_failure_does_not_invent_quote(self):
        r=record();r['text']=r['text'].replace('🟢 Moneyline: HERO\n💵 Est. Profit: 69.49%',
            '🟢 Moneyline: HERO (+69.49%)\n🟢 Total Maps: OVER 2.5 (+100%)')
        async def read(s):
            self.assertEqual(len(s.legs),1)
            if s.legs[0].market_type=='total_maps':raise ValueError('empty book')
            return raw_page(),now()
        self.service.read_market=AsyncMock(side_effect=read)
        self.service.ingest(r,'public');await self.service.process_paper_job(self.job())
        result=self.service.snapshot()
        self.assertEqual(len(result['ledger']['orders']),1)
        self.assertEqual([d['reason'] for d in result['signals'][0]['decisions']],['PAPER_FILLED','MARKET_UNAVAILABLE'])
        self.assertEqual(self.service.read_market.await_count,2)

    async def test_btts_no_is_a_named_option_and_independent_no_is_not(self):
        r=record();r['text']=r['text'].replace('🟢 Moneyline: HERO\n💵 Est. Profit: 69.49%',
            '🟢 Both Teams to Score: NO (+100%)\n🟢 Moneyline: HERO - NO (+100%)')
        raw=raw_page();raw['outcomes']=[dict(group='Both Teams to Score?',text='NO 59¢',selected=True,index=0)]
        async def read(s):return raw,now()
        self.service.read_market=AsyncMock(side_effect=read)
        self.service.ingest(r,'public');await self.service.process_paper_job(self.job())
        result=self.service.snapshot()
        self.assertEqual(len(result['ledger']['orders']),1)
        self.assertEqual([d['reason'] for d in result['signals'][0]['decisions']],['PAPER_FILLED','MAPPING_NOT_VERIFIED'])
        self.service.read_market.assert_awaited_once()

    async def test_quote_that_ages_while_other_legs_read_is_rejected(self):
        async def read(s):return raw_page(),now()-timedelta(seconds=35)
        self.service.read_market=read;self.service.ingest(record(),'public')
        await self.service.process_paper_job(self.job())
        result=self.service.snapshot()
        self.assertEqual(result['ledger']['orders'],[])
        self.assertEqual(result['signals'][0]['decisions'][0]['reason'],'INVALID_OR_STALE_QUOTE')

    async def test_rehearsal_btts_no_from_visible_depth(self):
        r=record(now()-timedelta(days=1));r['text']=r['text'].replace('Moneyline: HERO','Both Teams to Score: NO')
        self.service.ingest(r,'sample',True)
        raw=raw_page();raw['outcomes']=[dict(group='Both Teams to Score?',text='NO 59¢',selected=True,index=0)]
        async def read(s):return raw,now()
        self.service.read_market=read
        result=await self.service.dispatch('paper_rehearsal',dict(signal_id='AITradeCopy:1266',source='sample'))
        self.assertEqual(result['ledger']['orders'],[])
        leg=json.loads(result['rehearsal']['ledger']['orders'][0]['leg_identity'])
        self.assertEqual(leg,['both_teams_to_score','both_teams_to_score','NO','',0])

if __name__=='__main__':unittest.main()
