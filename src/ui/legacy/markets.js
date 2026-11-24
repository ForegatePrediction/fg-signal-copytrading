// Read-only market review. Shares the native bridge and escaped rendering helpers.
titles.markets='市场核对';
let marketRequest=null;
let marketBusy=false;
const baseRender=render;
render=function(s){baseRender(s);renderMarket();renderRehearsal()};

async function inspectMarket(signal_id,source,leg_no){
  marketRequest={signal_id,source,...(leg_no?{leg_no}:{})};marketBusy=true;$('detailDialog').close();go('markets');renderMarket();
  $('refreshMarket').disabled=true;toast('正在读取专用浏览器中的盘口，请稍候…');
  try{render(await rpc('inspect_market',marketRequest));toast('已读取页面快照，仅作观察')}
  catch(e){toast(e.message,true)}
  finally{marketBusy=false;$('refreshMarket').disabled=false;renderMarket()}
}
$('refreshMarket').onclick=()=>{if(marketRequest)inspectMarket(marketRequest.signal_id,marketRequest.source,marketRequest.leg_no)};
$('rehearseMarket').onclick=async()=>{
  if(!marketRequest||marketBusy)return;
  marketBusy=true;renderMarket();toast('正在读取当前盘口并独立试算…');
  try{render(await rpc('paper_rehearsal',marketRequest));toast('独立试算完成，正式模拟账本未改动')}
  catch(e){toast(e.message,true)}
  finally{marketBusy=false;renderMarket();renderRehearsal()}
};
document.addEventListener('click',async e=>{
  const b=e.target.closest('[data-cancel-paper]');if(!b)return;
  b.disabled=true;await act('cancel_paper',{order_id:Number(b.dataset.cancelPaper)});
});
document.addEventListener('click',e=>{
  const b=e.target.closest('[data-detail]');if(!b)return;
  const row=state?.signals.find(x=>x.id===b.dataset.detail&&x.source===b.dataset.source);
  if(!row||row.signal.type!=='PICK'||!row.signal.event_id)return;
  for(const leg of row.signal.legs.filter(l=>l.status==='PARSED')){
    const button=document.createElement('button');button.className='primary small';button.textContent='读取 '+(names[leg.market_type]||leg.market_type)+' '+leg.selection+(leg.line?' '+leg.line:'')+' / '+leg.side;
    button.onclick=()=>inspectMarket(row.id,row.source,leg.leg_no);$('detailContent').querySelector('.actions').append(button);
  }
});

function renderMarket(){
  $('rehearseMarket').disabled=marketBusy||!marketRequest;
  $('refreshMarket').disabled=marketBusy||!marketRequest;
  if(marketBusy){$('marketContent').innerHTML=empty('正在读取赛事页面','读取完成后显示新的快照。');return}
  const m=state?.market_observation;
  if(!m){$('marketContent').innerHTML=empty('尚未读取市场','进入信号记录 → 详情 → 读取盘口与报价。');return}
  if(!marketRequest){const source=m.source||state.signals.find(x=>x.id===m.signal_id)?.source;if(source)marketRequest={signal_id:m.signal_id,source,...(m.leg_no?{leg_no:m.leg_no}:{})}}
  $('refreshMarket').disabled=!marketRequest;
  $('rehearseMarket').disabled=!marketRequest;
  const isOld=Date.now()-Date.parse(m.observed_at)>30000;
  const selected=m.selected?escapeHTML(m.selected.group+' · '+m.selected.selection):'尚无唯一选中选项';
  const levels=rows=>rows.length?rows.map(r=>`<tr><td>${escapeHTML(r.price_cents)}¢</td><td>${escapeHTML(r.shares)}</td></tr>`).join(''):'<tr><td colspan="2">暂无可核对深度</td></tr>';
  const reasons={DISPLAY_MATCH:'显示选项已匹配',NO_VALID_UNIQUE_MATCH:'无有效的唯一报价',MAPPING_NOT_VERIFIED:'此玩法或方向映射待核查'};
  $('marketContent').innerHTML=`<div class="panel-head"><h2>${escapeHTML(m.home+' vs '+m.away)}</h2><span class="badge ${isOld?'demo':'real'}">${isOld?'历史快照':'刚刚读取'}</span></div>
  <div class="panel-body"><p class="hint">事件 ${escapeHTML(m.event_id)} · ${escapeHTML(m.signal_id)} · 读取时间 ${time(m.observed_at)}（北京时间）</p>
  <div class="notice subtle">${m.matches.map(x=>escapeHTML(x.selection+' / '+x.side+'：'+(reasons[x.reason]||x.reason))).join('<br>')}</div>
  <h3>当前可见盘口</h3><table><thead><tr><th>玩法</th><th>选项</th><th>页面价格</th><th>状态</th></tr></thead><tbody>${m.outcomes.map(o=>`<tr><td>${escapeHTML(o.group)}</td><td>${escapeHTML(o.selection)}</td><td>${escapeHTML(o.price_cents||'—')}¢</td><td>${o.valid?(o.selected?'已选中 · 仅观察':'仅观察'):'0¢ 或无效价格 · 不可使用'}</td></tr>`).join('')}</tbody></table>
  <h3 style="margin-top:24px">订单簿 · ${selected}</h3><p class="hint">${escapeHTML(m.book.reason)}</p>
  <div class="grid2 section"><div><h3>卖盘 Asks · 最低价优先</h3><table><thead><tr><th>价格</th><th>页面份额</th></tr></thead><tbody>${levels(m.book.asks)}</tbody></table></div><div><h3>买盘 Bids · 最高价优先</h3><table><thead><tr><th>价格</th><th>页面份额</th></tr></thead><tbody>${levels(m.book.bids)}</tbody></table></div></div>
  <p class="footnote">${m.blockers.map(escapeHTML).join('；')}。此页面不提供真实下单操作。</p></div>`;
}

function renderRehearsal(){
  const r=state?.rehearsal;
  if(!r){$('rehearsalContent').innerHTML=empty('用当前盘口检查保存参数','独立试算忽略历史信号的过期时间，不计入正式模拟账本。');return}
  const orders=r.ledger.orders;
  $('rehearsalContent').innerHTML=`<div class="panel-head"><h2>当前行情独立试算</h2><span class="badge demo">不计入账本</span></div><div class="panel-body"><p class="hint">${escapeHTML(r.original_signal_id)} · 行情读取 ${time(r.observed_at)}</p><p class="notice">${escapeHTML(r.notice)}</p><p>${r.decisions.map(d=>escapeHTML('盘口 '+d.leg_no+'：'+d.label)).join(' / ')}</p>${orders.map(o=>`<div class="grid2"><div><h3>${escapeHTML(o.home+' vs '+o.away)}</h3><p class="hint">${escapeHTML(JSON.parse(o.leg_identity).join(' · '))}</p><p>预算 ${money(o.budget)} USDT · 限价 ${(Number(o.limit_price)*100).toFixed(2)}¢</p><p>已成交 ${escapeHTML(o.filled)} / ${escapeHTML(o.size)} 份</p></div><div><p>含费成本 ${money(o.cost)} USDT</p><p>模拟费率 ${(Number(o.fee_rate)*100).toFixed(2)}%</p></div></div>`).join('')}<p class="footnote">仅使用读取时卖一档深度；本机时间与本地语义标识不能证明服务端时效或实盘可成交性。费用按成交额额外计收，仅为保守模拟假设。</p></div>`;
}
