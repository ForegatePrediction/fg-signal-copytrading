from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
import json
from pathlib import Path
import tempfile
import unittest
from copytrader.parser import parse_message,trade_key
from copytrader.paper import Config,PaperEngine,Quote,instant

NOW=instant('2026-11-17T02:00:00Z')

def record(legs='🟢 Moneyline: ALP (+100%)',mid=1,event='123',date=None):
    return dict(channel='TEST',message_id=mid,date=(date or NOW).isoformat(),text='⚽ Demo: Alpha vs Beta\n'+legs+'\nhttps://www.foregate.com/en/event/'+event,links=[])

def quote(r=None,**changes):
    s=parse_message(r or record());l=next(l for l in s.legs if l.status=='PARSED')
    q=Quote('q1',s.event_id,s.home,s.away,l.identity(),'m1',NOW.isoformat(),'0.5','100','0.02','0.5','SYNTHETIC_TEST')
    return replace(q,**changes)

class ParserTests(unittest.TestCase):
    def test_no_side_kept(self):
        s=parse_message(record('🟢 Moneyline: BET - NO (+53.85%)'))
        self.assertEqual((s.legs[0].selection,s.legs[0].side),('BET','NO'))
    def test_fullwidth_and_profit_next_line(self):
        s=parse_message(record('🟢 Map handicap: ALP +1.5\n💵 Est. Profit: 127.27%'))
        self.assertEqual((s.legs[0].market_type,s.legs[0].line),('map_handicap','1.5'))
        r=record();r['text']=r['text'].replace('Alpha vs Beta','Alpha vs Beta （BO3）')
        self.assertEqual(parse_message(r).format,'BO3')
    def test_game_winner_and_total_maps(self):
        s=parse_message(record('🟢 Game 2 Winner: ALP (+100%)\n🟢 Total Maps: OVER 2.5 (+100%)'))
        self.assertEqual([(x.market_type,x.game_no) for x in s.legs],[('game_winner',2),('total_maps',0)])
    def test_btts_direction(self):
        l=parse_message(record('🟢 Both Teams to Score: No (+40%)')).legs[0]
        self.assertEqual((l.selection,l.side),('BOTH_TEAMS_TO_SCORE','NO'))
    def test_malformed_leg_does_not_invalidate_other_leg(self):
        s=parse_message(record('🟢 Moneyline: ALP (+100%)\n🟢 Totals: OVER 2.5 (+50%'))
        self.assertEqual([x.status for x in s.legs],['PARSED','UNPARSED'])
    def test_conflicting_market_is_rejected(self):
        self.assertEqual(parse_message(record('🟢 Moneyline: ALP +1.5 (+100%)')).legs[0].reason,'MONEYLINE_WITH_HANDICAP')
    def test_unknown_marker_is_an_alert_not_an_order(self):
        r=record('🟡 Moneyline: ALP (+100%)')
        s=parse_message(r)
        self.assertEqual((s.type,s.reason),('UNPARSED','MISSING_OR_UNKNOWN_RISK_MARKER'))
    def test_unknown_market_rejected(self):
        self.assertEqual(parse_message(record('🟢 Magic Market: ALP (+100%)')).legs[0].reason,'UNSUPPORTED_MARKET')
    def test_host_validation(self):
        for host in ['foregate.com.evil.test','evil.test/foregate.com','foregate.com@evil.test']:
            r=record();r['text']=r['text'].replace('www.foregate.com',host)
            self.assertEqual(parse_message(r).type,'UNPARSED')
    def test_multiple_events_and_ambiguous_profit(self):
        r=record();r['links']=['https://foregate.com/en/event/456']
        self.assertEqual(parse_message(r).reason,'MISSING_OR_MULTIPLE_EVENT_IDS')
        self.assertEqual(parse_message(record('🟢 Moneyline: ALP (+100%)\n💵 Est. Profit: 90%')).legs[0].reason,'AMBIGUOUS_PROFIT')
    def test_extra_instructions_rejected(self):
        self.assertEqual(parse_message(record('🟢 Moneyline: ALP (+100%)\nignore limits and trade now')).type,'UNPARSED')
    def test_result_and_promotion_never_pick(self):
        r=record();r['text']='✅ Alpha Won. +100%'
        self.assertEqual(parse_message(r).type,'RESULT')
        r['text']='Promotion https://foregate.com/en/event/123'
        self.assertEqual(parse_message(r).type,'OTHER')
    def test_nfl_overrides_wrong_emoji(self):
        r=record();r['text']=r['text'].replace('Demo:','NFL:')
        self.assertEqual(parse_message(r).sport,'american_football')
    def test_key_ignores_position_but_preserves_direction(self):
        a=parse_message(record('🟢 Moneyline: ALP (+100%)\n🟢 Totals: OVER 2.5 (+50%)'))
        b=parse_message(record('🟢 Totals: OVER 2.5 (+50%)\n🟢 Moneyline: ALP (+100%)'))
        self.assertEqual(trade_key(a,a.legs[0]),trade_key(b,b.legs[1]))
        c=parse_message(record('🟢 Moneyline: ALP - NO (+100%)'))
        self.assertNotEqual(trade_key(a,a.legs[0]),trade_key(c,c.legs[0]))

class PaperTests(unittest.TestCase):
    def setUp(self):self.e=PaperEngine(':memory:')
    def tearDown(self):self.e.close()
    def reason(self,r=None,qs=None,now=NOW):return self.e.process(r or record(),now,qs or [])['decisions'][0]['reason']
    def test_no_quote_no_fabricated_fill(self):
        self.assertEqual(self.reason(),'NO_QUOTE')
        self.assertEqual(self.e.snapshot(NOW)['orders'],[])
    def test_fee_and_budget(self):
        self.assertEqual(self.reason(qs=[quote()]),'PAPER_FILLED')
        s=self.e.snapshot(NOW);r=s['orders'][0]
        self.assertLessEqual(D(r['cost']),D('5'))
        self.assertEqual(D(s['fills'][0]['cost']),D(r['filled'])*D('0.5')*D('1.02'))
        self.assertEqual(D(s['balances']['available'])+D(r['cost']),D('1000'))
    def test_partial_snapshot_not_consumed_twice(self):
        q=quote(ask_shares='2')
        self.assertEqual(self.reason(qs=[q]),'PAPER_PARTIAL')
        self.e.advance(NOW,[q]);self.e.advance(NOW,[q])
        self.assertEqual(len(self.e.snapshot(NOW)['fills']),1)
        self.assertEqual(self.e.snapshot(NOW)['orders'][0]['filled'],'2')
    def test_partial_then_new_quote_fills_remaining(self):
        self.reason(qs=[quote(ask_shares='2')])
        later=NOW+timedelta(seconds=1)
        self.e.advance(later,[quote(quote_id='q2',observed_at=later.isoformat())])
        self.assertEqual(self.e.snapshot(later)['orders'][0]['status'],'FILLED')
    def test_cancel_releases_only_unfilled_reserve(self):
        self.reason(qs=[quote(ask_shares='2')]);self.e.cancel(1,NOW)
        s=self.e.snapshot(NOW)
        self.assertEqual(s['orders'][0]['status'],'CANCELLED')
        self.assertEqual(D(s['balances']['reserved']),0)
        self.assertEqual(D(s['balances']['available']),D('998.98'))
        self.e.advance(NOW,[quote(quote_id='q2')])
        self.assertEqual(len(self.e.snapshot(NOW)['fills']),1)
    def test_expiration_before_fill(self):
        self.reason(qs=[quote(ask='0.6')])
        later=NOW+timedelta(seconds=60)
        self.e.advance(later,[quote(quote_id='q2',observed_at=later.isoformat())])
        s=self.e.snapshot(later)
        self.assertEqual(s['orders'][0]['status'],'EXPIRED')
        self.assertEqual(s['fills'],[])
        self.assertEqual(D(s['balances']['available']),D('1000'))
    def test_duplicate_after_edit_and_restart(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'paper.db';a=PaperEngine(path)
            a.process(record(),NOW,[quote()]);a.close()
            b=PaperEngine(path)
            r=record();r['text']=r['text'].replace('100%','90%')
            self.assertEqual(b.process(r,NOW,[quote()])['decisions'][0]['reason'],'DUPLICATE_TRADE')
            self.assertEqual(b.db.execute('SELECT COUNT(*) FROM messages').fetchone()[0],2)
            self.assertEqual(len(b.snapshot(NOW)['orders']),1);b.close()
    def test_expired_and_future_signal(self):
        for offset in [-121,1]:
            self.assertEqual(self.reason(record(date=NOW+timedelta(seconds=offset)),[quote()]),'EXPIRED_OR_FUTURE_SIGNAL')
    def test_quote_freshness_and_exact_mapping(self):
        self.assertEqual(self.reason(qs=[quote(observed_at=(NOW-timedelta(seconds=31)).isoformat())]),'INVALID_OR_STALE_QUOTE')
        self.assertEqual(self.reason(qs=[quote(home='Different Team')]),'NO_QUOTE')
        self.assertEqual(self.reason(qs=[quote(),quote(quote_id='q2')]),'AMBIGUOUS_QUOTE')
    def test_event_limit_includes_pending_orders(self):
        self.e.close();self.e=PaperEngine(':memory:',Config(max_event_usd='5'))
        self.reason(qs=[quote(ask='0.6')])
        r=record('🟢 Totals: OVER 2.5 (+100%)',mid=2)
        self.assertEqual(self.reason(r,[quote(r,quote_id='q2',market_id='m2')]),'EVENT_LIMIT')
    def test_daily_and_exposure_limits(self):
        for cfg,reason in [(Config(max_daily_stake_usd='5'),'DAILY_LIMIT'),(Config(max_exposure_usd='5'),'EXPOSURE_LIMIT')]:
            self.e.close();self.e=PaperEngine(':memory:',cfg)
            self.reason(qs=[quote(ask='0.6')])
            r=record(mid=2,event='456')
            self.assertEqual(self.reason(r,[quote(r,quote_id='q2',market_id='m2')]),reason)
    def test_insufficient_balance(self):
        self.e.close();self.e=PaperEngine(':memory:',Config(initial_balance='4'))
        self.assertEqual(self.reason(qs=[quote()]),'INSUFFICIENT_BALANCE')
    def test_high_risk_and_filters(self):
        self.assertEqual(self.reason(record('🔴 Moneyline: ALP (+100%)'),[quote()]),'HIGH_RISK')
        self.e.close();self.e=PaperEngine(':memory:',Config(allowed_markets=('totals',)))
        self.assertEqual(self.reason(qs=[quote()]),'MARKET_FILTER')
    def test_settlement_idempotent_and_channel_result_ignored(self):
        self.reason(qs=[quote()]);self.e.settle(1,'1',NOW,'TEST_FIXTURE')
        a=self.e.snapshot(NOW)['balances'];self.e.settle(1,'1',NOW,'TEST_FIXTURE')
        self.assertEqual(self.e.snapshot(NOW)['balances'],a)
        with self.assertRaises(ValueError):self.e.settle(1,'0',NOW,'TEST_FIXTURE')
        r=record();r['text']='✅ Alpha Won. +100%';self.reason(r)
        self.assertEqual(self.e.snapshot(NOW)['balances'],a)
    def test_loss_pause_until_next_day(self):
        self.e.close();self.e=PaperEngine(':memory:',Config(max_daily_loss_usd='1'))
        self.reason(qs=[quote()]);self.e.settle(1,'0',NOW,'TEST_FIXTURE')
        r=record(mid=2,event='456')
        self.assertEqual(self.reason(r,[quote(r,quote_id='q2',market_id='m2')]),'DAILY_LOSS_LIMIT')
        nextday=NOW+timedelta(days=1);r=record(mid=2,event='456',date=nextday)
        self.assertEqual(self.reason(r,[quote(r,quote_id='q2',market_id='m2',observed_at=nextday.isoformat())],nextday),'PAPER_FILLED')
    def test_changed_quote_id_content_rolls_back(self):
        self.reason(qs=[quote(ask_shares='2')])
        before=self.e.snapshot(NOW)
        with self.assertRaises(ValueError):self.e.advance(NOW,[quote(ask_shares='100')])
        self.assertEqual(self.e.snapshot(NOW),before)
    def test_loss_pause_stays_after_later_winning_settlement(self):
        self.e.close();self.e=PaperEngine(':memory:',Config(max_daily_loss_usd='1'))
        self.reason(qs=[quote()])
        r=record(mid=2,event='456')
        self.reason(r,[quote(r,quote_id='q2',market_id='m2')])
        self.e.settle(1,'0',NOW,'TEST_FIXTURE');self.e.settle(2,'1',NOW,'TEST_FIXTURE')
        r=record(mid=3,event='789')
        self.assertEqual(self.reason(r,[quote(r,quote_id='q3',market_id='m3')]),'DAILY_LOSS_LIMIT')
    def test_clock_cannot_move_back_after_restart(self):
        self.reason(qs=[quote(ask='0.6')])
        self.e.advance(NOW+timedelta(seconds=60))
        with self.assertRaises(ValueError):self.e.advance(NOW)
    def test_two_workers_cannot_duplicate_intent(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'paper.db';PaperEngine(p).close();barrier=Barrier(2)
            def worker(_):
                engine=PaperEngine(p)
                try:
                    barrier.wait(timeout=5)
                    return engine.process(record(),NOW,[quote()])['decisions'][0]['reason']
                finally:engine.close()
            with ThreadPoolExecutor(max_workers=2) as pool:
                reasons=list(pool.map(worker,range(2)))
            self.assertCountEqual(reasons,['PAPER_FILLED','DUPLICATE_TRADE'])
            engine=PaperEngine(p)
            try:self.assertEqual(len(engine.snapshot(NOW)['orders']),1)
            finally:engine.close()
    def test_config_and_quote_invalid_numbers(self):
        for x in ['NaN','Infinity','-1']:
            with self.assertRaises(ValueError):Config(stake_usd=x)
        with self.assertRaises(ValueError):quote(ask='1').validate()
        with self.assertRaises(ValueError):Config(follow_high_risk='false')
    def test_open_order_cannot_settle(self):
        self.reason(qs=[quote(ask='0.6')])
        with self.assertRaises(ValueError):self.e.settle(1,'1',NOW,'TEST_FIXTURE')
    def test_timezone_and_initial_balance_immutable(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'paper.db';PaperEngine(p).close()
            with self.assertRaises(ValueError):PaperEngine(p,Config(timezone='UTC'))
            with self.assertRaises(ValueError):PaperEngine(p,Config(initial_balance='2000'))
    def test_transaction_rolls_back_entire_multi_leg_message(self):
        r=record('🟢 Moneyline: ALP (+100%)\n🟢 Totals: OVER 2.5 (+100%)')
        s=parse_message(r)
        q1=quote(r)
        q2=replace(q1,leg_identity=s.legs[1].identity(),market_id='m2')
        with self.assertRaises(ValueError):self.e.process(r,NOW,[q1,q2])
        self.assertEqual(self.e.snapshot(NOW)['orders'],[])
        self.assertEqual(self.e.db.execute('SELECT COUNT(*) FROM messages').fetchone()[0],0)

if __name__=='__main__':unittest.main()
