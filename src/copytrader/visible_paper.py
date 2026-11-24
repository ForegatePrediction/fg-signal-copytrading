"""Visible-book paper quotes, deliberately unsuitable for a live executor.

The read timestamp is local, the market key is semantic, and fees are a user
assumption. None of these is represented as a verified platform identifier.
"""
from dataclasses import asdict
from decimal import Decimal
import hashlib
import json
from .market_observer import observation,price_label
from .paper import Quote, instant
from .parser import keytext

SOURCE = 'VISIBLE_BOOK_PAPER'


def quotes_from_page(raw, signal, read_at, fee_rate):
    fee = Decimal(str(fee_rate))
    if not fee.is_finite() or not 0 <= fee < 1:
        raise ValueError('模拟手续费率无效。')
    observed = observation(raw, signal)
    selected = observed['selected']
    asks = observed['book']['asks']
    if not selected or not asks:
        raise ValueError('未得到唯一选中选项及有效订单簿，跳过模拟。')
    matches = [m for m in observed['matches'] if m['reason'] == 'DISPLAY_MATCH'
               and keytext(m['outcome']['group']) == keytext(selected['group'])
               and keytext(price_label(m['outcome']['text'])['selection']) == keytext(selected['selection'])]
    if len(matches) != 1:
        raise ValueError('选中盘口与信号不唯一对应，跳过模拟。')
    leg = next(l for l in signal.legs if l.leg_no == matches[0]['leg_no'])
    identity = [signal.event_id, keytext(signal.home), keytext(signal.away), leg.identity()]
    market_id = 'paper:visible:' + hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()
    # One best ask only. Deeper levels are never aggregated or invented.
    best = asks[0]
    stamp = instant(read_at).isoformat()
    anchor=raw.get('reference_outcome')
    reference=price_label(anchor['text'])['price'] if anchor and keytext(anchor['group'])==keytext(selected['group']) and keytext(price_label(anchor['text'])['selection'])==keytext(selected['selection']) else best['price']
    payload = [identity, stamp, best, str(fee), reference]
    quote_id = 'visible:' + hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    quote = Quote(quote_id, signal.event_id, signal.home, signal.away, leg.identity(),
                  market_id, stamp, best['price'], best['shares'], str(fee), reference, SOURCE)
    quote.validate()
    return [quote]


def describe(quotes):
    return [dict(asdict(q), fee_basis='用户配置的模拟费率',
                 time_basis='本机读取时间，非服务端更新时间',
                 identity_basis='本地语义标识，非平台子市场 ID') for q in quotes]
