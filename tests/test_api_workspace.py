"""Account binding and execution regressions using the documented ForeGate contracts."""
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from copytrader.api_workspace import ApiWorkspace, match_signal, solana_address, stamp
from copytrader.desktop_service import Service
from copytrader.foregate_api import ApiError, ForeGateAPI
from copytrader.parser import parse_message


MARKET = '476967971554597254'
ACCOUNT_ID = '1234567'
IDS = {'marketId': MARKET, 'outcomeId': '2002', 'optionId': '3003'}
POSITION = {**IDS, 'positionId': '4004', 'marketStatus': 'open', 'marketTitle': 'Sevilla FC vs Real Betis',
            'shares': 2.82, 'availableShare': 1.5, 'averagePrice': 0.6942, 'costValue': 1.96, 'win': False}
DETAIL = {'marketId': MARKET, 'title': 'Sevilla FC vs Real Betis', 'status': 'open', 'isOnline': True,
          'modelType': 'orderbook', 'buyFee': 0.02, 'buyMinAmount': 1, 'outcomes': [
              {'outcomeId': '2002', 'name': 'Moneyline', 'title': 'Match Winner',
               'options': [{'optionId': '3002', 'title': 'sev Win YES'},
                           {'optionId': '3003', 'title': 'sev Win NO'},
                           {'optionId': '3004', 'title': 'bet'}]}]}
CYCLE_IDS = {'marketId': '88880004', 'outcomeId': '2124834596953444354', 'optionId': '0'}
CYCLE_ORDER = '512345678912345678'


def new_signal(date=None):
    return {'channel': 'AITradeCopy', 'message_id': '1334', 'date': date or stamp(), 'links': [],
            'text': '⚽ La Liga: Sevilla FC vs Real Betis\n🟢 Moneyline: SEV - NO\n'
                    '💵 Est. Profit: 47.06%\nhttps://www.foregate.com/en/event/' + MARKET}


class ApiFixture:
    account_id = ACCOUNT_ID
    known_ids = {ACCOUNT_ID}

    def __init__(self):
        self.requests = []
        self.error = None
        self.pos = []
        self.status = 'ACCEPTED'
        self.cancel_success = False
        self.profile_id = ACCOUNT_ID
        self.cycle_active = True
        self.cycle_locator = {k: CYCLE_IDS[k] for k in ('marketId', 'outcomeId')}

    def call(self, path, query=None, body=None, **options):
        self.requests.append((path, query, body, options))
        if path.endswith('/detail'):
            if CYCLE_IDS['marketId'] in path:
                raise ApiError('普通市场不存在', status=404)
            return DETAIL
        if path == '/cycle/windows':
            return {'records': [{'initialMarketId': CYCLE_IDS['marketId'], 'windowId': CYCLE_IDS['outcomeId'],
                                 'isCurrent': True}], 'total': 1}
        if path == '/cycle/windows/' + CYCLE_IDS['outcomeId']:
            now = datetime.now(timezone.utc)
            return {'windowId': CYCLE_IDS['outcomeId'], 'initialMarketId': CYCLE_IDS['marketId'],
                    'coinSymbol': 'BTC', 'cycleType': '5m', 'publicStatus': 'ACTIVE',
                    'tradingEnabled': True, 'serverTime': now.isoformat(),
                    'tradeLockTime': (now + timedelta(seconds=20 if self.cycle_active else -20)).isoformat(),
                    'orderSubmitLocator': self.cycle_locator, 'options': [{'optionId': '0', 'bestAsk': '0.52'}, {'optionId': '1'}]}
        if path == '/cycle/orderbook':
            return {**body, 'asks': [{'price': '0.52', 'size': '10'}], 'bids': []}
        if path == '/cycle/positions':
            return {'data': {'records': self.pos, 'pages': 1}}
        if path == '/cycle/orders/open':
            rows = [{**CYCLE_IDS, 'ordersOpenDetailVOList': [{'id': CYCLE_ORDER}]}]
            return {'data': {'records': rows if body['page'] == 1 else []}}
        if path == '/cycle/activity/user':
            return {'data': {'records': [{**CYCLE_IDS, 'type': 'BUY', 'shares': '2.345'}], 'pages': 1}}
        if path == '/cycle/orders':
            if self.error:
                raise self.error
            return {'orderId': CYCLE_ORDER, 'orderStatus': 'ACCEPTED'}
        if path == '/cycle/orders/cancel':
            return [{'orderId': body['orderOpenIds'][0], 'success': True}]
        if path == '/user/profile':
            return {'userId': self.profile_id}
        if path == '/account/assets':
            return {'userId': ACCOUNT_ID, 'totalAvailableAmount': '20.093'}
        if path == '/account/positions':
            return self.pos
        if path == '/orderbook/book':
            return {'outcomeId': IDS['outcomeId'], 'optionId': IDS['optionId'],
                    'asks': [{'price': '0.69', 'size': '20'}], 'bids': [{'price': '0.68', 'size': '9'}]}
        if path == '/orderbook/tick-size':
            return {'marketId': MARKET, 'optionId': IDS['optionId'], 'tickSize': 0.001}
        if path == '/ordering/create-order':
            if self.error:
                raise self.error
            return {'orderId': '512345678912345678', 'orderStatus': 'ACCEPTED'}
        if path == '/ordering/query-orders':
            return [{**IDS, 'orderId': body['orderIds'][0], 'direction': 'BUY',
                     'orderType': 'LIMIT', 'orderStatus': self.status, 'share': '2.82', 'currentShare': '2.82'}]
        if path == '/ordering/open-orders':
            return {'data': [{**IDS, 'ordersOpenDetailVOList': [{'id': '512345678912345678'}]}], 'pages': 1}
        if path == '/ordering/cancel-order':
            return [{'orderId': body['orderOpenIds'][0], 'success': self.cancel_success, 'message': None}]
        if path == '/withdraw/limit':
            if query != {'userId': ACCOUNT_ID}:
                raise AssertionError('Withdrawal limits require the bound API account ID')
            return {'singleWithdrawalMin': 10, 'singleWithdrawalMax': 500}
        if path == '/withdraw/request':
            return None
        raise AssertionError(path)


class ApiWorkspaceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.service = Service(Path(self.folder.name), api_mode=True, api_workspace_factory=ApiWorkspace)
        self.workspace = self.service.api_workspace
        self.api = ApiFixture()
        self.workspace.api = self.api
        tracker = patch.object(self.workspace, 'start_tracker')
        tracker.start()
        self.addCleanup(tracker.stop)

    async def asyncTearDown(self):
        await self.service.close()
        self.folder.cleanup()

    async def test_totp_enrollment_uses_json_object_and_preserves_leading_zero(self):
        replies = [{'totpEnabled': False}, {'manualEntryKey': 'TESTKEY', 'issuer': 'ForeGate'},
                   {'recoveryCodes': ['test-recovery']}]
        with patch.object(self.workspace, 'get', side_effect=replies) as get:
            setup = await self.workspace.mfa_action('mfa_setup', {'native_authorized': True})
            self.assertEqual(setup['key'], 'TESTKEY')
            get.assert_awaited_with('/user/mfa/setup/totp/init', body={}, mutation=True)
            complete = await self.workspace.mfa_action('mfa_setup_complete', {'code': '012345'})
            self.assertEqual(complete['recoveryCodes'], ['test-recovery'])
            get.assert_awaited_with('/user/mfa/setup/totp/complete', body={'code': '012345'}, mutation=True)

    async def test_email_verification_uses_platform_session_and_requires_positive_receipts(self):
        session = {'availableMethods': ['EMAIL'], 'recommendedMethod': 'EMAIL',
                   'sessionToken': 'test-session', 'expiresAt': (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()}
        with patch.object(self.workspace, 'get', side_effect=[session, False, True, False, True]) as get:
            result = await self.workspace.mfa_action('mfa_begin', {})
            self.assertEqual(result, {'methods': ['EMAIL'], 'recommended': 'EMAIL'})
            get.assert_awaited_with('/user/mfa/verification/create', {'verificationType': 'WITHDRAW'}, post=True)
            with self.assertRaisesRegex(ValueError, '未确认邮箱'):
                await self.workspace.mfa_action('mfa_email', {})
            self.assertTrue((await self.workspace.mfa_action('mfa_email', {}))['sent'])
            get.assert_awaited_with('/user/mfa/verification/send-email-otp', body={'sessionToken': 'test-session'}, mutation=True)
            code = {'method': 'EMAIL', 'code': '012345'}
            with self.assertRaisesRegex(ValueError, '未确认验证'):
                await self.workspace.mfa_action('mfa_verify', code)
            self.assertFalse(self.workspace.active_mfa()['verified'])
            self.assertTrue((await self.workspace.mfa_action('mfa_verify', code))['verified'])
            get.assert_awaited_with('/user/mfa/verification/verify',
                                    body={'sessionToken': 'test-session', 'mfaMethod': 'EMAIL', 'code': '012345'}, mutation=True)

    async def test_market_tree_and_exact_category_pagination(self):
        tree = [{'id': '5', 'nameDisplay': '體育', 'children': [
            {'id': '20124', 'name': 'Basketball', 'children': [{'id': '30163', 'nameDisplay': 'NBA'}]}]}]
        with patch.object(self.workspace, 'get', return_value=tree) as get:
            categories = await self.workspace.reads('market_categories', {})
        get.assert_awaited_once_with('/markets/category-tree', {'language': 'zh-CN'}, public=True)
        self.assertEqual(categories[0]['children'][0]['children'], [{'id': '30163', 'label': 'NBA', 'children': []}])

        row = {**DETAIL, 'categoryIds': [5, 20124, 30163]}
        response = {'pageNum': 2, 'pageSize': 30, 'total': 65, 'list': [row]}
        with patch('copytrader.api_workspace.public_market_catalog', return_value=response) as catalog:
            results = await self.workspace.reads('markets', {'categoryId': '30163', 'page': 2})
            catalog.assert_called_once_with('30163', 2, 30)
            self.assertEqual(results['rows'][0]['marketId'], MARKET)
            self.assertTrue(results['more'])
            with self.assertRaisesRegex(ApiError, '未按所选分类'):
                await self.workspace.reads('markets', {'categoryId': '30164', 'page': 2})

    async def test_cycle_categories_keep_exact_current_window(self):
        catalog = {'pageNum': 1, 'pageSize': 30, 'total': 0, 'list': []}

        def cycles(category, page, size):
            return {'pageNum': page, 'pageSize': size, 'total': 1, 'list': [
                {'categoryL2': int(category), 'initialMarketId': category + '123', 'title': 'BTC Up or Down',
                 'currentWindow': {'windowId': category + '124'} if category == '20542' else None}]}

        with patch('copytrader.api_workspace.public_market_catalog', return_value=catalog), \
             patch('copytrader.api_workspace.public_market_cycles', side_effect=cycles):
            selected = await self.workspace.reads('markets', {'categoryId': '20542'})
            root = await self.workspace.reads('markets', {'categoryId': '3'})
        self.assertEqual([row['marketId'] for row in selected['rows']], ['20542123'])
        self.assertEqual([row['marketId'] for row in root['rows']], ['20541123', '20542123'])
        self.assertTrue(all(row['cycle'] for row in root['rows']))
        self.assertEqual([row['windowId'] for row in root['rows']], [None, '20542124'])
        self.assertFalse(root['more'])

    async def test_cycle_routes_preserve_option_zero_and_do_not_convert_regular_urls(self):
        detail = await self.workspace.reads('detail', {'marketId': MARKET, 'windowId': '2002'})
        self.assertEqual(detail, DETAIL)
        detail = await self.workspace.reads('detail', {'marketId': CYCLE_IDS['marketId']})
        self.assertTrue(detail['tradingEnabled'])
        self.assertEqual(detail['windowId'], CYCLE_IDS['outcomeId'])
        self.assertEqual([o['optionId'] for o in detail['outcomes'][0]['options']], ['0', '1'])
        ids = {**CYCLE_IDS, 'cycle': True}
        book = await self.workspace.reads('book', ids)
        self.assertEqual(book['asks'][0]['price'], '0.52')
        self.assertEqual(self.api.requests[-1], ('/cycle/orderbook', None, CYCLE_IDS, {'public': True}))
        orders = await self.workspace.reads('open_orders', ids)
        self.assertEqual(orders['rows'][0]['id'], CYCLE_ORDER)
        self.assertTrue(orders['more'])
        activity = await self.workspace.reads('cycle_activity', ids)
        self.assertEqual(activity['rows'][0]['shares'], '2.345')
        with self.assertRaisesRegex(ApiError, '不一致'):
            await self.workspace.reads('detail', {'marketId': MARKET, 'windowId': detail['windowId'], 'cycle': True})

    async def test_cycle_market_buy_limit_sell_and_cancel_use_cycle_contract(self):
        ids = {**CYCLE_IDS, 'cycle': True}
        buy = {**ids, 'kind': 'order', 'direction': 'BUY', 'orderType': 'MARKET', 'price': '2.00'}
        plan = await self.workspace.prepare(buy)
        self.assertIn(CYCLE_IDS['outcomeId'], plan['summary'])
        self.assertFalse(any(r[3].get('mutation') for r in self.api.requests))
        result = await self.workspace.execute({**plan, 'native_authorized': True})
        self.assertEqual(result['state'], 'ACCEPTED')
        self.assertEqual(result['optionId'], '0')
        self.assertEqual(self.api.requests[-1], ('/cycle/orders', None,
                         {**CYCLE_IDS, 'direction': 'BUY', 'orderType': 'MARKET', 'price': '2'}, {'mutation': True}))
        await self.workspace.reconcile()
        self.assertFalse(any(r[0] == '/ordering/query-orders' for r in self.api.requests))
        self.api.pos = [{**CYCLE_IDS, 'positionId': '4004', 'availableShare': '3', 'shares': '4'}]
        sell = {**ids, 'kind': 'order', 'direction': 'SELL', 'orderType': 'LIMIT',
                'share': '2.345', 'limitPrice': '0.543', 'positionId': '4004'}
        with self.assertRaisesRegex(ValueError, '整数倍'):
            await self.workspace.prepare({**sell, 'limitPrice': '0.5431'})
        with self.assertRaisesRegex(ValueError, '份额不足'):
            await self.workspace.prepare({**sell, 'share': '3.1'})
        plan = await self.workspace.prepare(sell)
        await self.workspace.execute({**plan, 'native_authorized': True})
        self.assertEqual(self.api.requests[-1], ('/cycle/orders', None,
                         {**CYCLE_IDS, 'direction': 'SELL', 'orderType': 'LIMIT', 'timeInForce': 'GTC',
                          'share': '2.345', 'limitPrice': '0.543', 'positionId': '4004'}, {'mutation': True}))
        plan = await self.workspace.prepare({**ids, 'kind': 'cancel', 'orderId': CYCLE_ORDER})
        self.api.cycle_active = False
        result = await self.workspace.execute({**plan, 'native_authorized': True})
        self.assertEqual(result['state'], 'CANCELED')
        self.assertEqual(self.api.requests[-1], ('/cycle/orders/cancel', None,
                         {**CYCLE_IDS, 'orderOpenIds': [CYCLE_ORDER]}, {'mutation': True}))

    async def test_cycle_cutoff_is_rechecked_and_unknown_amount_cannot_be_resubmitted(self):
        payload = {**CYCLE_IDS, 'cycle': True, 'kind': 'order', 'direction': 'BUY', 'orderType': 'MARKET', 'price': '2'}
        plan = await self.workspace.prepare(payload)
        self.api.cycle_active = False
        with self.assertRaisesRegex(ValueError, '当前不可交易'):
            await self.workspace.execute({**plan, 'native_authorized': True})
        self.assertFalse(any(r[3].get('mutation') for r in self.api.requests))
        self.api.cycle_active = True
        self.api.cycle_locator = {**self.api.cycle_locator, 'outcomeId': '777'}
        with self.assertRaisesRegex(ValueError, '当前不可交易'):
            await self.workspace.prepare(payload)
        self.api.cycle_locator['outcomeId'] = CYCLE_IDS['outcomeId']
        self.api.error = ApiError('响应超时', uncertain=True)
        plan = await self.workspace.prepare(payload)
        self.assertEqual((await self.workspace.execute({**plan, 'native_authorized': True}))['state'], 'UNKNOWN')
        plan = await self.workspace.prepare({**payload, 'price': '2.000'})
        with self.assertRaisesRegex(ValueError, '待核对'):
            await self.workspace.execute({**plan, 'native_authorized': True})
        self.assertEqual(sum(r[0] == '/cycle/orders' for r in self.api.requests), 1)

    async def test_fixture_direction_no_maps_only_to_no(self):
        signal = parse_message(new_signal())
        self.assertEqual(match_signal(DETAIL, signal, signal.legs[0])[1]['optionId'], '3003')
        group = DETAIL['outcomes'][0]
        without_no = {**DETAIL, 'outcomes': [{**group, 'options': group['options'][::2]}]}
        with self.assertRaisesRegex(ValueError, '无法唯一'):
            match_signal(without_no, signal, signal.legs[0])

    async def test_team_binary_moneyline_matches_exact_fixture_and_side(self):
        signal = parse_message(new_signal())
        yes_no = [{'optionId': '3002', 'title': 'Yes'}, {'optionId': '3003', 'title': 'No'}]
        sevilla = {'outcomeId': '2002', 'name': 'sev', 'title': 'Sevilla FC', 'options': yes_no,
                   'description': 'If Sevilla FC wins, this market will resolve to "Yes". The result is determined within the first 90 minutes of regular play plus stoppage time.'}
        other = {**sevilla, 'outcomeId': '2003', 'description': 'If Sevilla FC scores first, this market will resolve to "Yes".'}
        group, option = match_signal({**DETAIL, 'outcomes': [other, sevilla]}, signal, signal.legs[0])
        self.assertEqual((group['outcomeId'], option['optionId']), ('2002', '3003'))
        with self.assertRaisesRegex(ValueError, '无法唯一'):
            match_signal({**DETAIL, 'outcomes': [other]}, signal, signal.legs[0])

    async def test_market_buy_prepares_when_platform_maximum_exceeds_local_input_maximum(self):
        payload = {**IDS, 'kind': 'order', 'direction': 'BUY', 'orderType': 'MARKET', 'price': '2'}
        with patch.dict(DETAIL, {'buyMaxAmount': 100000002.0}):
            plan = await self.workspace.prepare(payload)
        self.assertEqual(self.workspace.pending[plan['token']]['body']['price'], 2.0)
        self.assertFalse(any(request[0] == '/ordering/create-order' for request in self.api.requests))

    async def test_quote_fallback_requires_exact_outcome_and_option(self):
        asset = '1234567890' * 7
        snapshot = {'asks': [{'price': '0.69', 'size': '20'}], 'bids': [], 'tick_size': '0.01'}
        mapping = {'outcomeId': IDS['outcomeId'], 'outcomeOptionHolderVoList': [{**IDS, 'assetId': asset}]}
        with patch.object(self.workspace, 'get', side_effect=[ApiError('reject', 400), mapping,
                ApiError('reject', 500), mapping, ApiError('reject', 400), {**mapping, 'outcomeId': '9999'}]):
            with patch('copytrader.api_workspace.public_book', return_value=snapshot) as market_stream:
                self.assertEqual((await self.workspace.book(IDS))['asks'], snapshot['asks'])
                self.assertEqual(await self.workspace.get_tick(IDS), Decimal('0.01'))
                with self.assertRaisesRegex(ApiError, '所选盘口的资产编号'):
                    await self.workspace.book(IDS)
                self.assertEqual([call.args for call in market_stream.call_args_list], [(asset,), (asset,)])

    async def test_position_contract_and_limit_sell_native_confirmation(self):
        self.api.pos = [POSITION]
        rows = await self.workspace.reads('positions', {})
        self.assertEqual((rows['rows'][0]['quantity'],rows['rows'][0]['avgPrice']), (2.82,0.6942))
        payload = {**IDS, 'kind':'order', 'positionId': POSITION['positionId'], 'direction':'SELL',
                   'orderType':'LIMIT', 'limitPrice':'0.750', 'share':'1.20'}
        with self.assertRaisesRegex(ValueError, '份额不足'):
            await self.workspace.prepare({**payload,'share':'1.51'})
        plan = await self.workspace.prepare(payload)
        with self.assertRaisesRegex(ValueError, '确认'):
            await self.workspace.execute(plan)
        done = await self.workspace.execute({**plan,'native_authorized':True})
        self.assertEqual(done['state'],'ACCEPTED')
        self.assertEqual(self.api.requests[-1][2]['positionId'],POSITION['positionId'])
        self.assertNotIn('price',self.api.requests[-1][2])
        with self.assertRaisesRegex(ValueError, '过期'):
            await self.workspace.execute({**plan,'native_authorized':True})

    async def test_api_follow_watermark_dedup_confirmed_order_then_delayed_position(self):
        self.service.running = True
        self.workspace.armed_at = datetime.now(timezone.utc) - timedelta(seconds=3)
        old = new_signal((datetime.now(timezone.utc)-timedelta(seconds=4)).isoformat())
        self.service.ingest(old)
        self.assertEqual(self.workspace.status()['signals'],[])
        record = new_signal()
        self.service.ingest(record)
        self.service.ingest(record,'public')
        row = self.workspace.db.execute("SELECT * FROM follow WHERE state='QUEUED'").fetchone()
        await self.workspace.process_follow(row)
        self.assertEqual(len(self.workspace.status()['signals']),1)
        op = self.workspace.operations()[0]
        self.assertEqual(op['state'],'ACCEPTED')
        self.assertFalse(op['position_check'])
        self.api.status = 'FILLED'
        self.api.pos = [POSITION]
        past = (datetime.now(timezone.utc)-timedelta(seconds=63)).isoformat()
        self.workspace.db.execute('UPDATE operations SET created=? WHERE id=?',(past,op['id']))
        await self.workspace.reconcile()
        op = self.workspace.operations()[0]
        self.assertEqual(op['state'],'FILLED')
        self.assertIn('0 → 2.82 份',op['position_check'])
        creates = [r for r in self.api.requests if r[0] == '/ordering/create-order']
        self.assertEqual(len(creates),1)
        self.assertEqual(creates[0][2]['optionId'],'3003')
        self.assertEqual(creates[0][2]['orderType'], 'LIMIT')
        self.assertGreater(self.workspace.available_budget(MARKET),-1)
        await self.workspace.reconcile()
        self.assertEqual(len([r for r in self.api.requests if r[0] == '/ordering/create-order']),1)

    async def test_market_follow_uses_budget_and_does_not_apply_limit_price_or_cancel_timer(self):
        cfg = self.workspace.set_config({**self.workspace.config, 'max_price': '5', 'ttl': 45})['config']
        self.workspace.set_config({'order_type': 'MARKET', 'amount': '2', 'daily': '10', 'event': '2'})
        self.assertEqual((self.workspace.config['max_price'], self.workspace.config['ttl']), (cfg['max_price'], cfg['ttl']))
        self.service.running = True
        self.workspace.armed_at = datetime.now(timezone.utc) - timedelta(seconds=3)
        self.service.ingest(new_signal())
        row = self.workspace.db.execute("SELECT * FROM follow WHERE state='QUEUED'").fetchone()
        await self.workspace.process_follow(row)
        self.assertFalse(any(r[0].startswith('/orderbook/') for r in self.api.requests))
        request = next(r[2] for r in self.api.requests if r[0] == '/ordering/create-order')
        self.assertEqual(request, {**IDS, 'direction': 'BUY', 'orderType': 'MARKET', 'chooseOutcome': 'sev Win NO', 'price': 2.0})
        saved = self.workspace.db.execute('SELECT meta FROM operations').fetchone()
        self.assertEqual(json.loads(saved['meta'])['ttl'], 0)
        self.assertEqual(self.workspace.available_budget(MARKET), Decimal(0))

    async def test_unknown_blocks_repeat_until_review(self):
        self.api.error = ApiError('响应超时', uncertain=True)
        payload = {**IDS,'kind':'order','direction':'BUY','orderType':'MARKET','price':'2'}
        plan = await self.workspace.prepare(payload)
        result = await self.workspace.execute({**plan,'native_authorized':True})
        self.assertEqual(result['state'],'UNKNOWN')
        again = await self.workspace.prepare(payload)
        with self.assertRaisesRegex(ValueError,'待核对'):
            await self.workspace.execute({**again,'native_authorized':True})
        self.assertEqual(sum(r[0] == '/ordering/create-order' for r in self.api.requests),1)
        self.assertEqual(self.workspace.operations()[0]['state'],'UNKNOWN')

    async def test_follow_cancels_expired_order_while_position_check_is_unavailable(self):
        self.service.running = True
        self.workspace.armed_at = datetime.now(timezone.utc) - timedelta(seconds=3)
        self.service.ingest(new_signal())
        row = self.workspace.db.execute("SELECT * FROM follow WHERE state='QUEUED'").fetchone()
        await self.workspace.process_follow(row)
        operation = self.workspace.operations()[0]
        past = (datetime.now(timezone.utc) - timedelta(seconds=63)).isoformat()
        self.workspace.db.execute('UPDATE operations SET created=? WHERE id=?', (past, operation['id']))
        withdraw_position_check = AsyncMock(side_effect=ApiError('持仓查询暂不可用'))
        self.api.cancel_success = True
        with patch.object(self.workspace, 'matching_positions', withdraw_position_check):
            for _ in range(2):
                with self.assertRaisesRegex(ApiError, '持仓查询暂不可用'):
                    await self.workspace.reconcile()
        cancels = [r for r in self.api.requests if r[0] == '/ordering/cancel-order']
        self.assertEqual([r[2]['orderOpenIds'] for r in cancels], [['512345678912345678']])

    async def test_cancel_uses_live_open_id_and_checks_per_item_result(self):
        with self.assertRaisesRegex(ValueError,'已不在'):
            await self.workspace.prepare({**IDS,'kind':'cancel','orderId':'100'})
        plan = await self.workspace.prepare({**IDS,'kind':'cancel','orderId':'512345678912345678'})
        result = await self.workspace.execute({**plan,'native_authorized':True})
        self.assertEqual(result['state'],'REJECTED')
        self.assertIn('平台拒绝撤单',result['message'])

    async def test_withdraw_mfa_is_bound_to_one_request(self):
        self.assertEqual((await self.workspace.reads('withdraw_limit', {}))['singleWithdrawalMin'], 10)
        address = '11111111111111111111111111111111'
        self.assertEqual(solana_address(address),address)
        payload = {'kind':'withdraw','amount':'12','toAddress':address}
        with self.assertRaisesRegex(ValueError,'开启本次'):
            await self.workspace.prepare(payload)
        self.workspace.mfa = {'expires':datetime.now(timezone.utc).timestamp()+300,'verified':True,'token':'fixture'}
        plan = await self.workspace.prepare(payload)
        result = await self.workspace.execute({**plan,'native_authorized':True})
        self.assertEqual(result['state'],'SUBMITTED')
        self.assertEqual(self.api.requests[-1][2]['mfaSessionToken'],'fixture')
        self.assertNotIn('fixture', self.workspace.db.execute('SELECT request FROM operations').fetchone()[0])
        self.assertIsNone(self.workspace.mfa)

    async def test_account_mismatch_and_pause_during_enable(self):
        self.api.profile_id = '999'
        with self.assertRaisesRegex(ValueError,'身份'):
            await self.workspace.connect()
        self.api.profile_id = ACCOUNT_ID
        async def connecting():
            self.workspace.pause()
        with patch.object(self.workspace, 'connect', side_effect=connecting):
            with self.assertRaisesRegex(ValueError,'重新确认'):
                await self.workspace.start_follow('follow_arm', {'native_authorized':True,
                    'channel':self.service.settings['channel'],'config':self.workspace.config})
        self.assertIsNone(self.workspace.armed_at)

    async def test_restart_reconciles_interrupted_local_state_without_replay(self):
        self.workspace.db.execute("INSERT INTO operations VALUES ('op','order','label','SUBMITTING','{}',NULL,?,'{}')",(stamp(),))
        self.workspace.db.execute("INSERT INTO follow VALUES ('key','signal',1,?,'public','{}',?,'SUBMITTING','','op')",(MARKET,stamp()))
        await self.workspace.close()
        self.workspace = self.service.api_workspace = ApiWorkspace(self.service)
        status = self.workspace.status()
        self.assertFalse(status['armed'])
        self.assertEqual(status['operations'][0]['state'],'UNKNOWN')
        self.assertEqual(status['signals'][0]['state'],'UNKNOWN')


class ForeGateTransportTests(unittest.TestCase):
    def test_profile_mismatch_during_renewal_never_sets_cookie(self):
        account = {'profile_user_id': ACCOUNT_ID, 'registration': {'marchantUserId':'same'}}
        client = ForeGateAPI({'appKey':'demo','appSecret':'demo','apiKey':'demo'},account)
        with patch.object(client,'_send',side_effect=[({'data':{'userId':ACCOUNT_ID}},'Authorization=temporary'),
                               ({'data':{'userId':'90000'}},'')]):
            with self.assertRaisesRegex(ValueError,'归属'):
                client.ensure_session()
        self.assertFalse(client.cookie)
        self.assertFalse(client.verified)

    def test_query_signs_account_cookie_nonce_and_exact_bytes(self):
        account = {'profile_user_id': ACCOUNT_ID, 'registration': {'marchantUserId':'same'}}
        client = ForeGateAPI({'appKey':'demo','appSecret':'demo','apiKey':'demo'},account)
        first, second = [client.signed_request('/ordering/query-orders','POST',None,{'orderIds':['512345678912345678']},'Authorization=temporary')
                         for _ in range(2)]
        self.assertEqual(first.get_header('X-ca-signature-headers'),'cookie,x-api-key,x-nonce')
        self.assertNotEqual(first.get_header('X-nonce'),second.get_header('X-nonce'))
        self.assertEqual(first.data,b'{"orderIds":["512345678912345678"]}')


class AccountSelectionTests(unittest.IsolatedAsyncioTestCase):
    keys = {'appKey': 'gateway', 'appSecret': 'test-secret', 'apiKey': 'test-key'}

    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.service = Service(self.folder.name, api_mode=True)
        self.accounts = self.service.api_workspace
        self.transport = patch.object(ForeGateAPI, '_send', autospec=True, side_effect=self.reply)
        self.transport.start()
        self.addCleanup(self.transport.stop)
        self.bad_assets = False
        self.registrations = {}

    async def asyncTearDown(self):
        await self.service.close()

    def reply(self, client, path, method='GET', query=None, body=None, cookie='', mutation=False):
        reference = client.registration['marchantUserId']
        uid = self.registrations.setdefault(reference, str(6000 + len(self.registrations)))
        if path == '/user/register':
            return {'data': {'userId': 'platform-' + uid}}, 'Authorization=local-session'
        if path == '/user/profile':
            return {'data': {'userId': uid, 'isActive': True}}, ''
        if path == '/account/assets':
            return {'data': {'userId': 'wrong' if self.bad_assets else 'platform-' + uid,
                             'totalAvailableAmount': '2'}}, ''
        raise AssertionError(path)

    async def enroll(self, digit):
        draft = self.accounts.catalog()['draft_reference']
        return await self.accounts.dispatch('account_enroll', {'mode': 'new', 'reference': draft,
            'slot': digit * 32, 'credentials': self.keys})

    async def test_switch_preserves_per_account_limits_journal_and_rejects_old_context(self):
        first = await self.enroll('1')
        current = self.accounts.active
        current.set_config({'order_type':'LIMIT', 'amount':'3', 'daily':'10', 'event':'5', 'max_price':'90', 'ttl':'60'})
        current.db.execute("INSERT INTO operations VALUES ('prior','order','previous','UNKNOWN','{}',NULL,?,'{}')", (stamp(),))
        current.armed_at = datetime.now(timezone.utc)
        current.pending['old-plan'] = {'expires': 999999999}
        second = await self.enroll('2')
        status = await self.accounts.dispatch('status', {})
        self.assertFalse(status['armed'])
        self.assertEqual(status['operations'], [])
        self.assertEqual(status['config']['amount'], '2')
        with self.assertRaisesRegex(ApiError, '账户已切换'):
            await self.accounts.dispatch('execute', {'account_context': first['account_context'],
                'token': 'old-plan', 'native_authorized': True})
        with self.assertRaisesRegex(ValueError, '已过期'):
            await self.accounts.dispatch('execute', {'account_context': second['account_context'],
                'token': 'old-plan', 'native_authorized': True})
        back = await self.accounts.dispatch('account_select', {'slot': '1' * 32, 'credentials': self.keys})
        self.assertEqual(back['account_id'], first['account_id'])
        self.assertNotEqual(back['account_context'], first['account_context'])
        self.assertEqual(self.accounts.status()['config']['amount'], '3')
        self.assertEqual(self.accounts.status()['operations'][0]['id'], 'prior')
        await self.service.close()
        self.service = Service(self.folder.name, api_mode=True)
        catalog = self.service.api_workspace.catalog()
        self.assertIsNone(catalog['account_id'])
        self.assertEqual(catalog['last_uid'], first['account_id'])
        self.assertEqual(len(catalog['profiles']), 2)
        self.assertNotIn(self.keys['appSecret'], (Path(self.folder.name) / 'foregate-accounts/profiles.db')
                         .read_bytes().decode(errors='ignore'))

    async def test_failed_enrollment_keeps_draft_and_mismatched_uid_is_never_saved(self):
        draft = self.accounts.catalog()['draft_reference']
        self.bad_assets = True
        with self.assertRaisesRegex(ApiError, '资产账户'):
            await self.enroll('1')
        self.assertEqual(self.accounts.catalog()['draft_reference'], draft)
        self.assertEqual(self.accounts.catalog()['profiles'], [])
        self.bad_assets = False
        first = await self.enroll('1')
        self.assertEqual(first['account_id'], self.registrations[draft])
        self.assertNotEqual(self.accounts.catalog()['draft_reference'], draft)
        with self.assertRaisesRegex(ApiError, '会话归属'):
            await self.accounts.dispatch('account_enroll', {'mode':'existing', 'reference':draft,
                'uid':'8888', 'slot':'2' * 32, 'credentials':self.keys})
        self.assertEqual(self.accounts.catalog()['account_id'], first['account_id'])
        self.assertEqual(len(self.accounts.catalog()['profiles']), 1)

    async def test_verified_original_account_copies_legacy_journal(self):
        self.registrations['original'] = '9400'
        legacy = ApiWorkspace(self.service)
        legacy.db.execute("INSERT INTO operations VALUES ('legacy','order','previous','UNKNOWN','{}',NULL,?,'{}')", (stamp(),))
        await legacy.close()
        binding = Path(self.folder.name) / 'legacy-binding.json'
        binding.write_text(json.dumps({'profile_user_id': '9400', 'registration': {'marchantUserId': 'original'}}))
        with patch.dict(os.environ, {'COPYTRADER_FOREGATE_ACCOUNT':str(binding)}):
            await self.accounts.dispatch('account_enroll', {'mode':'existing', 'reference':'original',
                'uid':'9400', 'slot':'3' * 32, 'credentials':self.keys})
        self.assertEqual(self.accounts.status()['operations'][0]['id'], 'legacy')
        self.assertTrue((Path(self.folder.name) / 'foregate-api.db').is_file())


if __name__ == '__main__':
    unittest.main()
