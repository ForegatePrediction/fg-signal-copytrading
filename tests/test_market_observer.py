from copy import deepcopy
import json
from pathlib import Path
import unittest
from copytrader.parser import parse_message
from copytrader.market_observer import price_label,candidates,observation,display_identity


class MarketObserverTests(unittest.TestCase):
    def setUp(self):
        self.raw=json.loads((Path(__file__).resolve().parent/'fixtures/market_observation_public.json').read_text())
        self.signal=parse_message(dict(channel='AITradeCopy',message_id=1266,date='2026-11-17T07:14:00Z',
          text='🎮 CS2: magic vs Heroic (BO3)\n🟢 Moneyline: HERO\n💵 Est. Profit: 69.49%\nhttps://www.foregate.com/en/event/480194146926072329',links=[]))

    def test_real_public_dom_fixture_maps_only_unique_moneyline(self):
        self.assertEqual(self.signal.type,'PICK')
        result=observation(self.raw,self.signal)
        self.assertEqual(result['matches'][0]['reason'],'DISPLAY_MATCH')
        self.assertFalse(result['executable'])
        self.assertEqual(result['selected']['selection'],'HERO')
        self.assertEqual(result['book']['asks'][0]['price'],'0.59')
        self.assertEqual(result['book']['bids'][0]['price'],'0.58')
        self.assertEqual(sum(o['valid'] for o in result['outcomes']),2)

    def test_no_direction_never_treated_as_yes(self):
        self.signal.legs[0].side='NO'
        self.assertEqual(candidates(self.raw,self.signal)[0]['reason'],'MAPPING_NOT_VERIFIED')

    def test_wrong_event_or_team_cannot_match(self):
        for field,value in [('url','https://www.foregate.com/en/event/999'),('url','https://evil.test/en/event/480194146926072329'),('title','Counter-Strike: magic vs Heroic2 (BO3)')]:
            raw=deepcopy(self.raw);raw[field]=value
            with self.assertRaises(ValueError):candidates(raw,self.signal)

    def test_ambiguous_or_zero_quote_rejected(self):
        raw=deepcopy(self.raw);raw['outcomes'].append(deepcopy(raw['outcomes'][1]))
        self.assertEqual(candidates(raw,self.signal)[0]['reason'],'NO_VALID_UNIQUE_MATCH')
        raw=deepcopy(self.raw);raw['outcomes'][1]['text']='HERO 0¢'
        self.assertEqual(candidates(raw,self.signal)[0]['reason'],'NO_VALID_UNIQUE_MATCH')
        self.assertFalse(price_label('HERO NaN¢')['valid'])
        self.assertFalse(price_label('HERO 100¢')['valid'])

    def test_wrong_book_side_and_crossed_book_not_published(self):
        raw=deepcopy(self.raw);raw['book']['asks'][0]['key']='bid-0.99'
        self.assertEqual(observation(raw,self.signal)['book']['asks'],[])
        raw=deepcopy(self.raw);raw['book']['bids']=[dict(key='bid-0.60',text='60¢ 20.00 $12.00')]
        self.assertEqual(observation(raw,self.signal)['book']['asks'],[])

    def test_old_selection_book_not_attached_to_new_selection(self):
        raw=deepcopy(self.raw);raw['outcomes'][1]['text']='HERO 60¢'
        self.assertEqual(observation(raw,self.signal)['book']['asks'],[])

    def test_multiple_selected_outcomes_have_no_book(self):
        raw=deepcopy(self.raw);raw['outcomes'][0]['selected']=True
        result=observation(raw,self.signal)
        self.assertIsNone(result['selected'])
        self.assertEqual(result['book']['asks'],[])

    def test_all_seven_market_types_match_exact_visible_semantics(self):
        cases=[('Moneyline: HERO','Moneyline','HERO'),('Spreads: HERO -1.5','Spreads','HERO -1.5'),
               ('Totals: OVER 2.5','Totals','Over 2.5'),('Map Handicap: HERO +1.5','Map Handicap','HERO +1.5'),
               ('Total Maps: UNDER 2.5','Total Maps','UNDER 2.5'),
               ('Both Teams to Score: NO','Both Teams to Score?','NO'),
               ('Game 2 Winner: HERO','Map 2 Winner','HERO')]
        for label,group,selection in cases:
            with self.subTest(label=label):
                sig=parse_message(dict(channel='TEST',message_id=1,date=self.signal.date,
                    text='🎮 CS2: magic vs Heroic (BO3)\n🟢 '+label+' (+100%)\nhttps://www.foregate.com/en/event/'+self.signal.event_id,links=[]))
                raw=deepcopy(self.raw);raw['outcomes']=[dict(group=group,text=selection+' 59¢',selected=True,index=0)]
                self.assertEqual(candidates(raw,sig)[0]['reason'],'DISPLAY_MATCH')
                self.assertEqual(display_identity(raw['outcomes'][0]),sig.legs[0].identity())

    def test_wrong_handicap_sign_or_game_number_never_matches(self):
        for label,group,selection in [('Spreads: HERO +1.5','Spreads','HERO -1.5'),
                                      ('Game 2 Winner: HERO','Map 1 Winner','HERO'),
                                      ('Totals: OVER 2.5','Totals','OVER 1.5')]:
            with self.subTest(label=label):
                sig=parse_message(dict(channel='TEST',message_id=1,date=self.signal.date,
                    text='🎮 CS2: magic vs Heroic (BO3)\n🟢 '+label+' (+100%)\nhttps://www.foregate.com/en/event/'+self.signal.event_id,links=[]))
                raw=deepcopy(self.raw);raw['outcomes']=[dict(group=group,text=selection+' 59¢')]
                self.assertEqual(candidates(raw,sig)[0]['reason'],'NO_VALID_UNIQUE_MATCH')

    def test_team_totals_cannot_impersonate_match_totals(self):
        self.assertIsNone(display_identity(dict(group='China PR Totals',text='OVER 0.5 79¢')))
        self.assertIsNone(display_identity(dict(group='First Team to Score',text='HERO 59¢')))

    def test_zero_price_is_still_rejected_for_new_market_types(self):
        sig=parse_message(dict(channel='TEST',message_id=1,date=self.signal.date,
            text='🎮 CS2: magic vs Heroic (BO3)\n🟢 Total Maps: UNDER 2.5 (+100%)\nhttps://www.foregate.com/en/event/'+self.signal.event_id,links=[]))
        raw=deepcopy(self.raw)
        self.assertEqual(candidates(raw,sig)[0]['reason'],'NO_VALID_UNIQUE_MATCH')

if __name__=='__main__':unittest.main()
