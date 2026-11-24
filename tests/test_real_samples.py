import json
from pathlib import Path
import unittest
from copytrader.parser import parse_message
FIXTURES=Path(__file__).resolve().parent/'fixtures'
SAMPLES=FIXTURES.parents[1]/'src/fixtures/messages.jsonl'

class RealRegression(unittest.TestCase):
    def test_hand_reviewed_real_examples(self):
        records={r['message_id']:r for r in map(json.loads,SAMPLES.read_text(encoding='utf-8').splitlines())}
        cases=json.loads((FIXTURES/'real_cases.json').read_text(encoding='utf-8'))
        for case in cases:
            with self.subTest(message_id=case['id']):
                parsed=parse_message(records[case['id']]).as_dict()
                for key,value in case.items():
                    if key=='id':continue
                    if key=='legs':
                        self.assertEqual(len(parsed[key]),len(value))
                        for actual,expected in zip(parsed[key],value):
                            for field,v in expected.items():self.assertEqual(actual[field],v,field)
                    else:self.assertEqual(parsed[key],value,key)
    def test_full_snapshot_classification_and_rejection_list(self):
        from collections import Counter
        parsed=[parse_message(json.loads(x)) for x in SAMPLES.read_text(encoding='utf-8').splitlines()]
        self.assertEqual(len(parsed),200)
        self.assertEqual(Counter(x.type for x in parsed),{'RESULT':88,'SUMMARY':6,'PICK':104,'OTHER':2})
        rejected=[(s.signal_id,l.leg_no,l.reason) for s in parsed for l in s.legs if l.status!='PARSED']
        self.assertEqual(rejected,[('FixtureSignals:1168',1,'MONEYLINE_WITH_HANDICAP'),('FixtureSignals:1224',2,'MISSING_OR_MALFORMED_PROFIT')])

if __name__=='__main__':unittest.main()
