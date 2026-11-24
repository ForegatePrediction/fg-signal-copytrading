"""Read-only visible-market observations. Never creates executable Quote objects."""
from datetime import datetime,timezone
from decimal import Decimal,InvalidOperation
import re
from urllib.parse import urlsplit
from .parser import keytext


def price_label(text):
    match=re.fullmatch(r'(.+?)\s+(\d+(?:\.\d+)?)¢',text.strip())
    if not match:return dict(selection=text,price='',price_cents='',valid=False)
    cents=Decimal(match[2]);valid=0<cents<100
    return dict(selection=match[1],price=str(cents/100),price_cents=str(cents),valid=valid)


def validate_context(raw,signal):
    p=urlsplit(raw.get('url',''))
    if p.scheme!='https' or p.hostname!='www.foregate.com' or p.port not in (None,443) or p.username or p.password or p.path!='/en/event/'+signal.event_id:
        raise ValueError('赛事页面地址与信号不一致，已停止核对。')
    # Names and order must match, even when an event identifier was supplied.
    expected=keytext(signal.home+' vs '+signal.away)
    if not signal.home or not signal.away or not re.search(r'(?<!\w)'+re.escape(expected)+r'(?!\w)',keytext(raw.get('title',''))):
        raise ValueError('赛事队名与信号不一致，已停止核对。')


def supported_leg(leg):
    return leg.status=='PARSED' and (leg.side=='YES' or leg.market_type=='both_teams_to_score') and leg.market_type in {'moneyline','spreads','totals','map_handicap','total_maps','both_teams_to_score','game_winner'}


def group_type(group):
    name=keytext(group).rstrip('?').strip()
    if re.fullmatch(r'(?:map|game) [1-9]\d* winner',name):return 'game_winner'
    return {'moneyline':'moneyline','spreads':'spreads','totals':'totals','map handicap':'map_handicap','game handicap':'map_handicap','total maps':'total_maps','total games':'total_maps','both teams to score':'both_teams_to_score'}.get(name,'')


def display_identity(outcome):
    """Exact visible semantics only; no team aliases or complementary NO inference."""
    market=group_type(outcome.get('group',''));selection=price_label(outcome.get('text',''))['selection']
    line='';game=0;side='YES'
    if not market:return None
    explicit=re.fullmatch(r'(.+?)\s+-\s+(YES|NO)',selection,re.I)
    if explicit and market!='both_teams_to_score':selection,side=explicit[1],explicit[2].upper()
    if market in {'spreads','map_handicap'}:
        m=re.fullmatch(r'(.+?)\s+([+-]\d+(?:\.\d+)?)',selection)
        if not m:return None
        selection=m[1];line=str(Decimal(m[2]).normalize())
    elif market in {'totals','total_maps'}:
        m=re.fullmatch(r'(OVER|UNDER)\s+(\d+(?:\.\d+)?)',selection,re.I)
        if not m:return None
        selection=m[1];line=str(Decimal(m[2]).normalize())
    elif market=='both_teams_to_score':
        if selection.upper() not in {'YES','NO'}:return None
        side=selection.upper();selection='BOTH_TEAMS_TO_SCORE'
    elif market=='game_winner':game=int(re.fullmatch(r'(?:map|game) (\d+) winner',keytext(outcome['group']))[1])
    return [market,keytext(selection),side,line,game]


def candidates(raw,signal):
    validate_context(raw,signal)
    result=[]
    for leg in signal.legs:
        if leg.status!='PARSED':continue
        supported=supported_leg(leg)
        matches=[o for o in raw['outcomes'] if display_identity(o)==leg.identity()] if supported else []
        valid=len(matches)==1 and price_label(matches[0]['text'])['valid']
        result.append(dict(leg_no=leg.leg_no,selection=leg.selection,side=leg.side,
          reason='DISPLAY_MATCH' if valid else 'NO_VALID_UNIQUE_MATCH' if supported else 'MAPPING_NOT_VERIFIED',
          outcome=matches[0] if valid else None))
    return result


def book_levels(rows,side):
    levels=[];seen=set()
    for r in rows:
        m=re.fullmatch(r'(\d+(?:\.\d+)?)¢\s+([\d,]+(?:\.\d+)?)\s+\$([\d,]+(?:\.\d+)?)',r.get('text',''))
        if not m:raise ValueError('订单簿格式发生变化，不能读取深度。')
        price=Decimal(m[1])/100;shares=Decimal(m[2].replace(',',''))
        try:keyprice=Decimal(r.get('key','').removeprefix(side+'-'))
        except InvalidOperation:raise ValueError('订单簿价格标记无效。') from None
        if not r.get('key','').startswith(side+'-') or keyprice!=price or not 0<price<1 or shares<=0 or price in seen:
            raise ValueError('订单簿价格或数量无效。')
        seen.add(price);levels.append(dict(price=str(price),price_cents=m[1],shares=str(shares)))
    return sorted(levels,key=lambda x:Decimal(x['price']),reverse=side=='bid')


def observation(raw,signal):
    matches=candidates(raw,signal)
    outcomes=[dict(group=o.get('group','') or '未识别分组',selected=bool(o.get('selected')),**price_label(o['text'])) for o in raw['outcomes']]
    selected=[o for o in outcomes if o['selected']]
    book=dict(asks=[],bids=[],reason='尚未核对唯一选中盘口')
    if len(selected)==1 and selected[0]['valid'] and raw.get('book',{}).get('count')==1:
        try:
            asks=book_levels(raw['book']['asks'],'ask');bids=book_levels(raw['book']['bids'],'bid')
            if asks and bids and Decimal(asks[0]['price'])<=Decimal(bids[0]['price']):raise ValueError('买卖盘交叉，需重新读取。')
            if asks and Decimal(asks[0]['price'])!=Decimal(selected[0]['price']):raise ValueError('盘口价格与选项显示不一致，需重新读取。')
            book=dict(asks=asks[:5],bids=bids[:5],reason='页面可见订单簿；可用于保守模拟，服务端时效与平台 ID 尚未验证')
        except ValueError as exc:book['reason']=str(exc)
    return dict(event_id=signal.event_id,signal_id=signal.signal_id,home=signal.home,away=signal.away,
      observed_at=datetime.now(timezone.utc).isoformat(),outcomes=outcomes,matches=matches,book=book,
      selected=selected[0] if len(selected)==1 else None,executable=False,
      blockers=['子市场唯一 ID 尚未核验','实际交易费与服务端报价更新时间尚未核验','唯一订单号与提交超时对账尚未验证'])
