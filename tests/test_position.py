import datetime
import unittest
from unittest.mock import AsyncMock, patch

from copytrader import position
from copytrader.execution import stamp
from copytrader.live_service import LiveService
from copytrader.parser import parse_message
from test_execution import record

EVENT = '475921566123695598'
URL = f'https://www.foregate.com/en/event/{EVENT}?market=moneyline&windowId=42'
MESSAGE = record(leg='Moneyline: SAS', event=EVENT)
MESSAGE['text'] = MESSAGE['text'].replace('Alpha vs Beta', 'Clippers vs Spurs')
SIGNAL = parse_message(MESSAGE)
RAW = dict(title='Clippers vs Spurs | ForeGate', outcomes=[dict(group='Moneyline', text='SAS 90¢')])


class PositionTests(unittest.IsolatedAsyncioTestCase):
    def test_event_scoped_alias_and_direction(self):
        leg = SIGNAL.legs[0]
        target = position.position_target(URL, RAW, SIGNAL, leg, ['Match Winner'])
        self.assertEqual(target['outcomes'], ['match winner sas', 'match winner spurs'])
        self.assertEqual(target['url'], URL)
        with self.assertRaises(ValueError):
            position.position_target(URL.replace(EVENT,'123'), RAW, SIGNAL, leg, ['Match Winner'])
        self.assertNotIn('match winner lac', target['outcomes'])

    def test_existing_position_is_not_a_new_purchase(self):
        before = dict(shares='2.15')
        self.assertIn('+0.00', position.comparison(before, dict(shares='2.15')))
        self.assertIn('+1.10', position.comparison(before, dict(shares='3.25')))
        self.assertIn('缺少提交前', position.comparison(None, before))
        self.assertIn('待核对', position.comparison(before, dict(error='页面未加载')))

    async def test_one_read_only_check_due_from_commit(self):
        service = LiveService()
        service.check_position = AsyncMock()
        committed = datetime.datetime.fromisoformat(stamp()) - datetime.timedelta(seconds=5)
        with patch('copytrader.live_service.asyncio.sleep', new_callable=AsyncMock) as sleep:
            await service.delayed_position_check('order', committed.isoformat())
            self.assertAlmostEqual(sleep.await_args.args[0], 55, delta=1)
        service.check_position.assert_awaited_once_with('order')
