'use strict';
const esc = value => String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
const modeLabel = mode => mode === 'LIMIT' ? '限价单' : '市价单';
const quote = p => Number((p * 100).toFixed(3)) + '¢';
const archiveTypes = {PICK:'交易信号',RESULT:'频道赛果',SUMMARY:'频道汇总',OTHER:'其他消息'};
const reasons = {MONEYLINE_WITH_HANDICAP:'胜负盘包含让分',MISSING_OR_MALFORMED_PROFIT:'预期收益字段缺失或格式异常'};
const $ = selector => document.querySelector(selector);
const stamp = date => new Date(date).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false});
const signalsById = new Map(signalArchive.map(s=>[s.signal_id.split(':')[1],s]));
const idOf = s => s.signal_id.split(':')[1];
const main=$('main'),nav=$('nav'),sheet=$('#sheet'),inner=$('#inner');
let timer;
const shapes = {
  down:'<path d="M12 3v13m-5-5 5 5 5-5M4 16v4h16v-4"/>',
  up:'<path d="M12 16V3m-5 5 5-5 5 5M4 16v4h16v-4"/>',
  shield:'<path d="m12 2 8 3v6c0 5-4 9-8 11-4-2-8-6-8-11V5z"/><path d="m8 12 3 3 5-6"/>',
  next:'<path d="m9 5 7 7-7 7"/>',
  search:'<circle cx="10" cy="10" r="6.5"/><path d="m15 15 5 5"/>',
  close:'<path d="m6 6 12 12M18 6 6 18"/>',
  markets:'<path d="M3 20h18M6 16v-6m6 6V4m6 12V8"/>',
  funds:'<rect x="3" y="6" width="18" height="14" rx="3"/><path d="M4 6l12-4v4m5 6h-6v4h6"/>',
  home:'<rect x="3" y="3" width="7" height="7" rx="1.4"/><rect x="14" y="3" width="7" height="7" rx="1.4"/><rect x="3" y="14" width="7" height="7" rx="1.4"/><rect x="14" y="14" width="7" height="7" rx="1.4"/>',
  orders:'<path d="M5 3h14v18l-3-2-4 2-4-2-3 2z M8 8h8 M8 12h6"/>',
  account:'<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',
  telegram:'<path d="m3 10 18-7-4 18-6-5-3 3v-6l9-7-11 6z"/>',
  settings:'<path d="M3 7h6m4 0h8M3 17h10m4 0h4"/><circle cx="11" cy="7" r="2"/><circle cx="15" cy="17" r="2"/>',
  wave:'<path d="M2 13h5l3-8 4 14 3-8 2 2h3"/>',
  football:'<circle cx="12" cy="12" r="9"/><path d="m12 8 4 3-1.5 4h-5L8 11z M12 8V3m4 8 4-2m-5.5 6 3 4m-8-4-3 4M8 11 4 9"/>',
  basketball:'<circle cx="12" cy="12" r="9"/><path d="m6 5 13 14M4 16l15-10M5 7q8 0 13-2M8 20q-1-7 11-8"/>',
  tennis:'<circle cx="12" cy="12" r="9"/><path d="M4 8q10 2 10-5M10 21q0-10 11-11"/>',
  arrow:'<path d="M4 12h15m-6-6 6 6-6 6"/>',
  clock:'<circle cx="12" cy="12" r="9"/><path d="M12 6v6l4 2"/>'
};
function icon(name) {
  return '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round" stroke-linecap="round" aria-hidden="true">'+shapes[name]+'</svg>';
}
document.querySelector('#tgIcon').innerHTML=icon('telegram');
const btn=(action,title,cls='primary')=>'<button type="button" class="'+cls+'" data-act="'+action+'">'+title+'</button>';
const badge=(txt,on=false)=>'<span class="badge '+(on?'active':'')+'">'+txt+'</span>';
const pair=(k,v)=>'<div class="pair"><span>'+k+'</span><b>'+v+'</b></div>';
function toast(msg){const t=document.querySelector('#toast');t.textContent=msg;t.classList.add('on');clearTimeout(timer);timer=setTimeout(()=>t.classList.remove('on'),2200)}
let modalTrigger = null;
function modal(title,content){
 modalVersion++;
 plan=null;
 inner.innerHTML='<div class="head sheet-head"><h2 id="modalTitle" tabindex="-1">'+title+'</h2><button class="close" aria-label="关闭" data-act="close">'+icon('close')+'</button></div>'+content;
 sheet.setAttribute('aria-labelledby','modalTitle');
 if(!sheet.open)sheet.showModal();
 sheet.scrollTop=0;
 $('#modalTitle').focus({preventScroll:true});
}
function courtArt() {
  return `<svg class="court" viewBox="0 0 400 260" fill="none" aria-hidden="true" preserveAspectRatio="xMidYMid slice">
    <defs><pattern id="dots" width="10" height="10" patternUnits="userSpaceOnUse"><circle cx="1" cy="1" r="1" fill="#ffffff" opacity=".16"/></pattern></defs>
    <rect width="400" height="260" fill="url(#dots)"/>
    <g transform="rotate(-22 200 140)" stroke="#fb7955" stroke-width="2">
      <rect x="75" y="35" width="310" height="195" rx="4"/><path d="M230 35v195 M75 75h45v115H75 M385 75h-45v115h45"/>
      <circle cx="230" cy="132" r="35"/><circle cx="230" cy="132" r="3" fill="#ff6946"/>
      <path d="M125 159q45-78 123-36t85-38" stroke="#fff0d2" stroke-dasharray="5 7"/>
      <path d="m323 88 11-7 1 13" stroke="#fff0d2"/><circle cx="125" cy="159" r="8" fill="#ff6946"/>
    </g></svg>`;
}
const field = (name,label,value,attrs='') => '<label>'+label+'<input name="'+name+'" type="number" value="'+esc(value)+'" required '+attrs+'></label>';
const seg = (action,label,selected) => btn(action,label,selected?'sel':'');
function featuredSignals() { return ['1229','1202','1224'].map(id=>signalsById.get(id)); }
function signalTitle(s) { return s.home && s.away ? s.home+' vs '+s.away : s.raw_text.split('\n')[0]; }
function signalCard(s) {
  const problem=s.legs.some(l=>l.status!=='PARSED');
  return '<article class="card signal-card"><div class="meta"><span class="sport"><span class="signal-mark">'+icon('wave')+'</span>'+esc(s.league)+'</span><time>'+stamp(s.date)+'</time></div><h3>'+esc(signalTitle(s))+'</h3><div class="signal-info"><span>#'+idOf(s)+' · '+s.legs.length+' 个交易项</span><span class="parse-state'+(problem?' warn':'')+'">'+(problem?'部分解析失败':'已解析')+'</span></div>'+btn('signalinfo-'+idOf(s),'查看信号 '+icon('next'),'signal-link')+'</article>';
}
function archive() {
  const rows=signalArchive.filter(s=>archiveType==='全部'||s.type===archiveType);
  modal('历史信号', '<div class="filters">'+['全部',...Object.keys(archiveTypes)].map(t=>seg('archiveType-'+t,archiveTypes[t]||t,t===archiveType)).join('')+'</div><p class="notice">FixtureSignals · 北京时间 · '+rows.length+' 条</p>'+rows.slice(archivePage*8,archivePage*8+8).map(s=>'<button class="listrow" data-act="signalinfo-'+idOf(s)+'"><small>#'+idOf(s)+' · '+stamp(s.date)+' · '+archiveTypes[s.type]+'</small><br>'+esc(signalTitle(s))+'</button>').join('')+pager('archivePage',archivePage,rows.length,8));
}
function signalInfo(s) {
  const legs=s.legs.map(l=>'<div class="book"><b>交易项 '+l.leg_no+'</b><p>'+esc(l.raw)+'</p><small>'+esc(l.status==='PARSED'?'已解析 · '+l.selection+' '+l.side+(l.risk_flag==='high'?' · 高风险':''):'已跳过 · '+(reasons[l.reason]||l.reason))+'</small></div>').join('');
  modal('频道消息 #'+idOf(s),'<p class="notice">'+stamp(s.date)+' · '+archiveTypes[s.type]+'</p><pre class="raw">'+esc(s.raw_text)+'</pre>'+legs+'<div class="actions">'+btn('archive','返回历史信号','secondary')+'</div>');
}
function pager(prefix,index,count,size) {
  return '<div class="pager">'+(index?btn(prefix+'-'+(index-1),'上一页','secondary'):'')+'<small>'+(index+1)+' / '+Math.max(1,Math.ceil(count/size))+'</small>'+(count>(index+1)*size?btn(prefix+'-'+(index+1),'下一页','secondary'):'')+'</div>';
}

const pages={markets:'市场',orders:'订单',home:'工作单',funds:'资金',account:'我的'};
const sports={
  Soccer:['足球','⚽'], Basketball:['篮球','🏀'], Tennis:['网球','🎾'], TableTennis:['乒乓球','🏓'],
  Football:['美式橄榄球','🏈'], Hockey:['冰球','🏒'], Volleyball:['排球','🏐'], Darts:['飞镖','🎯'],
  Cricket:['板球','🏏'], Pickleball:['匹克球','<svg viewBox="0 0 24 24"><path d="M8 14c-4-4 1-11 6-11 5 0 7 5 4 9-2 3-5 5-8 4l-5 6-2-2z" fill="#eea93a"/><circle cx="18" cy="19" r="4" fill="#a6c531"/><path d="M17 18h1m1 2h1" stroke="#456326"/></svg>'],
  CS2:['CS2','<b class="game cs">CS</b>'],Valorant:['无畏契约','<svg viewBox="0 0 24 24"><path fill="#fe4655" d="m2 4 13 15H9L2 11zm20 0v7l-5 6-4-5z"/></svg>'],
  LOL:['英雄联盟','<b class="game lol">L</b>'],'Dota 2':['Dota 2','<svg viewBox="0 0 24 24"><path fill="#b44634" fill-rule="evenodd" d="m2 3 19-1 1 19-20 1zm3 2 10 13 4 1L8 6zm10 1 4 5V5zM5 14v5l5-1z"/></svg>']
};
const sportTile=k=>'<span class="sporticon" aria-hidden="true">'+(sports[k]?.[1]||'•')+'</span>';
const defaults={mode:'LIMIT',amount:2,eventLimit:2,limit:10,maxPrice:85,ttl:600};
let rules={...defaults}, category='体育', subcat='', keyword='', marketPageNo=0, archivePage=0, archiveType='全部';
try{Object.assign(rules,JSON.parse(localStorage.getItem('desk.ruleDraft.v2')))}catch{}
let ticket=null,account=null,security=null,wallet=null,orderRows=null,followActive=false,modalVersion=0;
const service=()=>window.signalDeskAccount;
async function request(action,payload={}) {
  if(!service()?.request) throw Error('账户服务尚未连接。请从已接入服务的 Telegram 小程序继续。');
  return await service().request(action,payload);
}
// Telegram passes signed launch data in the URL fragment before the app starts routing.
const launchData=new URLSearchParams(location.hash.slice(1)).get('tgWebAppData');
function tg(){
 if(window.Telegram?.WebApp?.initData)return window.Telegram.WebApp;
 if(!launchData)return null;
 try{return {initData:launchData,initDataUnsafe:{user:JSON.parse(new URLSearchParams(launchData).get('user'))}}}catch{return null}
}
const errText=e=>esc(e?.message || '请求未完成，请重试。');
const errBox='<p class="sheeterr" role="alert"></p>';
const submit=label=>errBox+'<button type="submit" class="primary wide">'+label+'</button>';
const heads=(name,ico,tail='')=>'<div class="head"><h2>'+icon(ico)+name+'</h2>'+tail+'</div>';
const section=(content,cls='')=>'<section class="card '+cls+'">'+content+'</section>';
const openPage=name=>{if(sheet.open)sheet.close();location.hash=name};
const current=()=>location.hash.slice(1);
const signed=()=>Boolean(account);
const titleByPage={home:'跟单工作台',markets:'赛事市场',funds:'我的资金',orders:'我的订单',account:'个人中心'};
const mById=new Map(markets.map(m=>[m.id,m]));
function menuRow(action,symbol,label,detail=''){
 return btn(action,'<span class="row-icon">'+icon(symbol)+'</span><span class="row-copy"><strong>'+label+'</strong>'+(detail?'<small>'+detail+'</small>':'')+'</span>'+icon('next'),'menu-row');
}
function metric(label,value,unit){
 return '<div><dt>'+label+'</dt><dd>'+esc(value)+' <small>'+unit+'</small></dd></div>';
}
function home(){
 const metrics=metric('单笔上限',rules.amount,'USDT')+metric('每日上限',rules.limit,'USDT')+
   (rules.mode==='LIMIT'?metric('等待撤单',rules.ttl,'秒'):metric('单赛事上限',rules.eventLimit,'USDT'));
 return '<div class="topgrid"><section class="rule-hero">'+courtArt()+'<div class="rule-top"><h2>'+icon('settings')+'跟单规则</h2>'+badge(followActive?'服务端已启用':'本机草稿',followActive)+'</div><strong class="rule-mode">'+modeLabel(rules.mode)+'</strong><dl class="rule-metrics">'+metrics+'</dl><div class="rule-actions">'+btn('settings','编辑规则 '+icon('arrow'))+(followActive?btn('pause','暂停跟单','secondary'):'')+'</div></section>'+
 section('<h2>交易入口</h2>'+menuRow('markets','markets','浏览赛事市场','体育与电竞盘口')+menuRow('orders','orders','我的订单','手动委托与信号跟单'),'quick-card')+'</div>'+
 '<section class="feed">'+heads('信号动态','wave',btn('archive','全部 '+signalArchive.length+' 条 '+icon('next'),'textbtn'))+'<p class="sub">FixtureSignals · 历史测试记录</p><div class="signals">'+featuredSignals().map(signalCard).join('')+'</div></section>';
}
function segmented(prefix,items,active){
  return '<div class="segment">'+items.map(([v,label])=>'<button type="button" aria-pressed="'+(v===active)+'" data-act="'+esc(prefix+v)+'">'+esc(label)+'</button>').join('')+'</div>';
}
function settings(){
  modal('跟单规则','<form id="rules"><label>下单方式</label>'+segmented('ruleMode-',[['LIMIT','限价单'],['MARKET','市价单']],rules.mode)+'<input type="hidden" name="mode" value="'+esc(rules.mode)+'"><div class="formgrid">'+
    field('amount','单笔上限 · USDT',rules.amount,'min="0.01" step="0.01"')+field('eventLimit','单赛事上限 · USDT',rules.eventLimit,'min="0.01" step="0.01"')+field('limit','每日上限 · USDT',rules.limit,'min="0.01" step="0.01"')+'</div><fieldset class="limitgrid" id="limitFields">'+
    field('maxPrice','最高买价 · ¢/份',rules.maxPrice,'min="0.01" max="99.99" step="0.01"')+field('ttl','等待撤单 · 秒',rules.ttl,'min="0" max="3600" step="1"')+'</fieldset><p class="notice" id="modeNote"></p>'+submit('保存本机草稿')+(signed()?btn('activate','保存并启用服务端跟单','secondary wide'):'')+'</form>');ruleMode(rules.mode);
}
function ruleMode(mode){
 const f=$('#rules');f.elements.mode.value=mode;
 f.querySelectorAll('[aria-pressed]').forEach(b=>b.setAttribute('aria-pressed',b.dataset.act==='ruleMode-'+mode));
 const limited=mode==='LIMIT';$('#limitFields').hidden=$('#limitFields').disabled=!limited;
 $('#modeNote').textContent=limited?'按卖一价挂单。默认等待 600 秒，未成交部分到期撤单。填 0 表示保留挂单。':'按可用预算提交市价买入。';
}
function readRules(f){
 const draft={...rules};for(const [k,v] of new FormData(f))draft[k]=k==='mode'?v:Number(v);
 if(draft.mode==='LIMIT'&&draft.ttl>0&&draft.ttl<30)throw Error('等待撤单请填 0 或 30–3600 秒。');
 return draft;
}
function marketPage(){
 const groups=Object.keys(sports).filter(k=>markets.some(m=>m.cat===category&&m.sport===k));
 return '<div class="browse-head">'+segmented('cat-',[['体育','体育'],['电竞','电竞']],category)+'<div class="search-field">'+icon('search')+'<input type="search" id="searchMarkets" aria-label="搜索市场" placeholder="搜索赛事、联赛或市场编号" value="'+esc(keyword)+'"></div></div>'+
 '<div class="chips" aria-label="项目分类">'+[['','全部项目'],...groups.map(k=>[k,sports[k][0]])].map(([k,label])=>'<button type="button" aria-pressed="'+(k===subcat)+'" data-act="sport-'+esc(k)+'">'+(k?sportTile(k):icon('markets'))+esc(label)+'</button>').join('')+'</div><div id="marketResults">'+marketResults()+'</div>';
}
function marketResults(){
 const term=keyword.trim().toLowerCase();
 const rows=markets.filter(m=>m.cat===category&&(!subcat||m.sport===subcat)&&(!term||[m.event,m.id,m.league,...m.options.map(o=>o.group)].join(' ').toLowerCase().includes(term)))
 .sort((a,b)=>Number(Boolean(b.historical))-Number(Boolean(a.historical))||Number(Number(b.end)>Date.now())-Number(Number(a.end)>Date.now())||Number(b.volume||0)-Number(a.volume||0));
 return '<div class="resultsline"><b>'+rows.length+' 个市场</b><small>ForeGate 公开目录 · 2026-11-24 快照</small></div><div class="library">'+rows.slice(marketPageNo*12,marketPageNo*12+12).map(m=>{
 const first=m.options.find(o=>o.ask>0)||m.options[0];
 const group=first?m.options.filter(o=>o.group===first.group).slice(0,2):[];
 const prices=group.map(o=>'<span class="card-quote"><span>'+esc(o.label)+'</span><b>'+(o.ask>0?quote(o.ask):'—')+'</b></span>').join('');
 return '<a class="card marketcard" href="#market/'+m.id+'"><div class="meta"><span class="sport">'+sportTile(m.sport)+esc(m.league)+'</span>'+badge(m.historical?'历史盘口':m.options.length+' 个选项')+'</div><h3>'+esc(m.event)+'</h3><div class="price-strip"><small>快照卖一'+(group.length&&m.options.some(o=>o.group!==first.group)?' · '+esc(first.group):'')+'</small><div>'+prices+'</div></div></a>';
 }).join('')+'</div>'+(rows.length?pager('marketPage',marketPageNo,rows.length,12):'<div class="empty"><p>未找到匹配的市场</p>'+btn('clearMarkets','清除搜索和项目筛选','secondary')+'</div>');
}
function chooseOption(m,index){
 return '<details class="picker"><summary aria-label="选择盘口与选项">'+esc(m.options[index].group)+' · <b>'+esc(m.options[index].label)+'</b><i>⌄</i></summary><div class="options">'+m.options.map((o,i)=>'<button type="button" aria-pressed="'+(i===index)+'" data-act="pick-'+i+'"><small>'+esc(o.group)+'</small><b>'+esc(o.label)+(i===index?' ✓':'')+'</b></button>').join('')+'</div></details>';
}
function bookTable(side,rows){
 const isBuy=side==='bids',list=(rows||[]).filter(r=>Number(r.price)>0).slice().sort((a,b)=>isBuy?b.price-a.price:a.price-b.price).slice(0,8);
 const max=Math.max(1,...list.map(r=>Number(r.size)||0));
 const title=isBuy?'买盘':'卖盘';
 const body=list.map(r=>{
   const price=Number((Number(r.price)*100).toFixed(3));
   const size=r.size==null?'—':Number(r.size).toLocaleString('zh-CN',{maximumFractionDigits:4});
   const width=Math.min(100,Math.max(0,Number(r.size)||0)/max*100);
   return '<tr style="--depth:'+width+'%"><td><strong>'+price+'</strong></td><td>'+esc(size)+'</td></tr>';
 }).join('');
 return '<table class="levels '+(isBuy?'buy':'sell')+'"><caption>'+title+'</caption><thead><tr><th scope="col">价格 ¢</th><th scope="col">数量 · 份</th></tr></thead><tbody>'+(body||'<tr><td colspan="2" class="no-level">暂无档位</td></tr>')+'</tbody></table>';
}
function depth(m,opt,live){
 const data=live||opt.book||{bids:[],asks:opt.ask>0?[{price:opt.ask,size:null}]:[]};
 const bid=Math.max(0,...data.bids.map(r=>Number(r.price)).filter(p=>p>0));
 const ask=Math.min(Infinity,...data.asks.map(r=>Number(r.price)).filter(p=>p>0));
 const metrics=[['买一',bid>0?quote(bid):'—'],['卖一',Number.isFinite(ask)?quote(ask):'—'],['买卖价差',bid>0&&Number.isFinite(ask)&&ask>=bid?quote(ask-bid):'—']];
 const source=live?'最新查询':m.historical?'历史深度':'目录快照';
 const note=live?'更新于 '+new Date().toLocaleTimeString('zh-CN'):m.historical?(opt.book?'来源于此前测试时采集的 ForeGate 盘口。':'此次测试未记录所选项的深度。'):'目录仅提供最优卖价。完整深度需读取此选项的订单簿。';
 return heads('订单簿','markets',badge(source))+'<p class="sub">'+esc(opt.group)+' · '+esc(opt.label)+'</p><dl class="top-quotes">'+metrics.map(([label,value])=>'<div><dt>'+label+'</dt><dd>'+value+'</dd></div>').join('')+'</dl><div class="bookgrid">'+bookTable('bids',data.bids)+bookTable('asks',data.asks)+'</div><p class="notice">'+note+'</p>'+(!m.historical?btn('refreshbook','刷新盘口','secondary')+errBox:'');
}
function orderCard(m){
 const {mode,side}=ticket, budget=mode==='MARKET'&&side==='BUY';
 return heads('下单','orders')+'<p class="ticketpick">'+esc(m.options[ticket.index].label)+'</p><form id="ticket">'+segmented('side-',[['BUY','买入'],['SELL','卖出']],side)+segmented('mode-',[['LIMIT','限价单'],['MARKET','市价单']],mode)+
 (mode==='LIMIT'?field('price','限价 · ¢/份',ticket.price,'placeholder="输入限价" min="0.001" max="99.999" step="0.001"'):'')+
 field('quantity',budget?'支出金额 · USDT':'委托数量 · 份',ticket.quantity,'placeholder="0.00" min="0.01" step="0.01"')+
 '<div class="pair"><span>'+(budget?'买入预算':mode==='LIMIT'?'委托金额':'估算金额')+'</span><b id="estimate">—</b></div>'+ (m.historical||Number(m.end)>0&&Number(m.end)<=Date.now()?'<p class="notice">此市场已截止。</p><button class="primary wide" disabled>交易已截止</button>':submit('核对委托'))+'</form>';
}
function refreshEstimate(){
 const f=$('#ticket');if(!f)return;ticket.quantity=f.elements.quantity.value;ticket.price=f.elements.price?.value??ticket.price;
 const qty=Number(ticket.quantity),price=Number(ticket.price);
 const n=ticket.mode==='LIMIT'?qty*price/100:ticket.side==='BUY'?qty:null;
 $('#estimate').textContent=n>0?n.toLocaleString('zh-CN',{maximumFractionDigits:4})+' USDT':'—';
}
function marketDetail(m){
 if(ticket?.id!==m.id)ticket={id:m.id,index:Math.max(0,m.options.findIndex(o=>o.book)),mode:'LIMIT',side:'BUY',quantity:'',price:'',live:null};
 const opt=m.options[ticket.index];
 const title=esc(m.event).replace(/ vs\.? /i,match=>'<span class="versus">'+match.trim()+'</span>');
 const ended=m.historical||(Number(m.end)>0&&Number(m.end)<=Date.now());
 return '<a href="#markets" class="back">← 返回市场</a><section class="eventhead"><div class="event-meta"><span class="sport">'+sportTile(m.sport)+esc(sports[m.sport]?.[0])+' / '+esc(m.league)+'</span>'+(ended?badge('已截止'):'')+'</div><h1>'+title+'</h1><div class="metarow">'+(m.start?'<span>'+icon('clock')+'开赛 '+stamp(Number(m.start))+'</span>':'')+'<a target="_blank" rel="noopener" href="https://www.foregate.com/zh/event/'+m.id+'">ForeGate 市场 ↗</a></div></section>'+
 '<div class="detail-jump">'+btn('jump-depth','查看订单簿','secondary')+btn('jump-orderCard','查看下单卡片','secondary')+'</div><div class="detailgrid"><div class="bookcolumn">'+section('<h2>盘口与选项</h2>'+chooseOption(m,ticket.index),'optioncard')+'<section class="card" id="depth" tabindex="-1">'+depth(m,opt,ticket.live)+'</section></div><aside class="card ticket" id="orderCard" tabindex="-1">'+orderCard(m)+'</aside></div>';
}

function auth(){
 const user=tg()?.initDataUnsafe?.user;
 const name=user?[user.first_name,user.last_name].filter(Boolean).join(' ')||user.username:'';
 modal('使用 Telegram 身份继续','<div class="connect"><span class="tile">'+icon('telegram')+'</span>'+icon('arrow')+'<b>Signal Desk</b></div>'+
 '<p class="notice">授权后将使用你的 Telegram 身份创建平台用户，并自动开通 ForeGate 交易账户。</p>'+
 (tg()?.initData?'<form id="authorize"><label>平台昵称<input name="nickname" maxlength="32" required value="'+esc(name)+'"></label>'+submit('授权并继续')+'</form>':'<p class="book">请从 Telegram 机器人打开小程序，以读取账户昵称。</p>'+btn('markets','浏览赛事市场','secondary')));
}
const factorStatus=()=>security?.locked||security?.forceReset?'需处理账户安全限制':security?.totpEnabled===true?'已绑定':security?.totpEnabled===false?'未绑定':signed()?'尚未读取':'授权后可查看';
function profile(){
 const name=esc(account?.nickname||'尚未授权');
 return '<div class="account-grid">'+section('<div class="profile-heading"><span class="avatar">'+icon('account')+'</span><div><small>Telegram 身份</small><h2>'+name+'</h2></div></div>'+pair('ForeGate 账户',esc(account?.foregateId||'—'))+(!signed()?btn('auth',icon('telegram')+'使用 Telegram 授权','primary wide'):''),'profile-card')+
 section('<h2>账户安全</h2>'+menuRow('bind-profile','shield',security?.totpEnabled?'查看验证方式':'绑定二次验证码',factorStatus()),'quick-card')+'</div>';
}
function funds(){
 return '<div class="account-grid"><section class="fundhero"><div class="balance-label">'+icon('funds')+'可用余额</div><div class="balance">'+esc(wallet?.available??'—')+' <small>USDT</small></div><div class="wallet-actions">'+btn('deposit',icon('down')+'充值')+btn('withdraw',icon('up')+'提现','secondary')+'</div></section>'+
 section('<h2>资金记录</h2>'+menuRow('deposits','down','充值记录')+menuRow('withdrawals','up','提现记录'),'quick-card')+'</div>';
}
function orders(){
 const refresh=btn('refreshorders','刷新订单','secondary');
 if(orderRows?.length)return section(heads('订单记录','orders',refresh)+orderRows.map(r=>pair(esc(r.title||r.orderId),esc(r.status))).join(''),'board');
 const title=signed()?(orderRows?'暂无订单':'尚未读取订单'):'授权后查看订单';
 return section('<div class="empty-state"><span class="empty-icon">'+icon('orders')+'</span><h2>'+title+'</h2><p>在这里查看手动委托和跟单订单的处理状态。</p>'+(signed()?refresh:btn('auth',icon('telegram')+'使用 Telegram 授权'))+'</div>','board orders-board');
}
function render(){
 const route=current(), m=route.startsWith('market/')?mById.get(route.slice(7)):null;
 const page=m?'markets':Object.hasOwn(pages,route)?route:'home';
 nav.innerHTML=Object.entries(pages).map(([k,v])=>'<a class="navbtn" href="#'+k+'" '+(k===page?'aria-current="page"':'')+'>'+'<span class="nav-icon">'+icon(k)+'</span><span>'+v+'</span></a>').join('');
 $('.identity-name').textContent=account?.nickname||'个人中心';
 main.innerHTML=m?marketDetail(m):'<h1>'+titleByPage[page]+'</h1>'+({home,markets:marketPage,account:profile,funds,orders}[page])();
 if(m){category=m.cat;refreshEstimate()}
}
function qr(value,label){
 const code=new window.DeskQR(-1,0);code.addData(value);code.make();
 const n=code.getModuleCount();let path='';
 for(let row=0;row<n;row++)for(let col=0;col<n;col++)if(code.isDark(row,col))path+='M'+(col+4)+' '+(row+4)+'h1v1h-1z';
 return '<svg class="qr" role="img" aria-label="'+esc(label)+'" viewBox="0 0 '+(n+8)+' '+(n+8)+'" shape-rendering="crispEdges"><rect width="100%" height="100%" fill="white"/><path d="'+path+'"/></svg>';
}
function validSolana(value){
 if(!/^[1-9A-HJ-NP-Za-km-z]{32,44}$/.test(value))return false;
 const alphabet='123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz';
 let n=0n;for(const c of value)n=n*58n+BigInt(alphabet.indexOf(c));
 return (value.match(/^1*/)[0].length)+(n===0n?0:Math.ceil(n.toString(16).length/2))===32;
}
function deposit(){
 modal('充值 USDT',pair('网络','Solana')+'<div id="depositResult"></div><div class="actions">'+btn('getaddress','获取账户充值地址')+'</div>'+
 '<details class="manualqr"><summary>已有充值地址？生成二维码</summary><form id="addressQR"><label>从 ForeGate 复制的 Solana 充值地址<input name="address" autocomplete="off" spellcheck="false" required></label>'+submit('生成地址二维码')+'</form></details>'+errBox);
 if(signed())void runButton(inner.querySelector('[data-act=getaddress]'),()=>getAddress());
}
function showAddress(value,source){
 if(!validSolana(value))throw Error('请输入完整的 Solana 地址。');
 $('#depositResult').innerHTML='<div class="qrpanel">'+qr(value,'充值地址二维码')+'<small>'+source+'</small><code id="depositAddress">'+esc(value)+'</code>'+btn('copyaddress','复制地址','textbtn')+'</div>';
}
async function getAddress(){
 const version=modalVersion;
 const data=await request('deposit_address');
 if(version!==modalVersion)return;
 if(!['solana','sol'].includes(String(data.chain).toLowerCase()))throw Error('返回地址的网络与所选 Solana 网络不符。');
 showAddress(data.walletAddress,'账户充值地址 · USDT / Solana');
}
const otpField='<label>验证器的 6 位验证码<input name="code" required pattern="[0-9]{6}" maxlength="6" inputmode="numeric" autocomplete="one-time-code" placeholder="000000"></label>';
function bindingSteps(currentStep){
 return '<ol class="steps" aria-label="绑定进度">'+['添加验证器','验证','保存恢复码'].map((step,i)=>'<li'+(i===currentStep?' aria-current="step"':'')+'>'+(i+1)+' · '+step+'</li>').join('')+'</ol>';
}
function binding(origin){
 modal('绑定二次验证码',bindingSteps(0)+'<p class="notice">准备好验证器应用，例如 Google Authenticator。获取密钥后，可扫码或手动添加账户。</p>'+pair('账户状态',factorStatus())+errBox+
 (security?.totpEnabled||security?.locked||security?.forceReset?'':btn('setup-'+origin,'获取绑定二维码'))+(origin==='withdraw'?btn('withdraw','返回提现','textbtn'):''));
}
async function setup(origin){
 const version=modalVersion, status=await request('mfa_status');
 if(version!==modalVersion)return;
 security=status;
 if(status?.locked||status?.forceReset||status?.totpEnabled!==false){binding(origin);return}
 const data=await request('mfa_setup');
 if(version!==modalVersion)return;
 const secret=String(data.key||'').trim().replace(/\s/g,'').toUpperCase();
 if(!/^[A-Z2-7]+=*$/.test(secret))throw Error('服务未返回有效的验证器密钥。');
 const issuer=data.issuer||'ForeGate', label=data.account||account?.nickname||'ForeGate';
 const uri='otpauth://totp/'+encodeURIComponent(issuer)+':'+encodeURIComponent(label)+'?secret='+encodeURIComponent(secret)+'&issuer='+encodeURIComponent(issuer)+'&digits=6&period=30';
 modal('添加验证器',bindingSteps(1)+'<div class="qrpanel">'+qr(uri,'验证器绑定二维码')+'<details><summary>手动输入密钥</summary><code>'+esc(secret)+'</code></details></div><form id="bindCode" data-origin="'+origin+'">'+otpField+submit('验证并绑定')+'</form>');
}
let withdrawDraft=null,plan=null;
function withdraw(){
 const draft=withdrawDraft||{};
 modal('提现 USDT',pair('网络','Solana')+pair('验证器',factorStatus())+
 (security?.totpEnabled?'':btn('bind-withdraw','绑定二次验证码','textbtn'))+
 '<form id="withdrawForm"><label>收款地址<input name="address" required autocomplete="off" value="'+esc(draft.address||'')+'" placeholder="Solana 地址"></label>'+
 field('amount','提现金额 · USDT',draft.amount||'','min="0.01" step="0.01" placeholder="0.00"')+submit('下一步 · 验证身份')+'</form>');
}
async function withdrawVerify(draft){
 if(!validSolana(draft.address))throw Error('请核对 Solana 收款地址。');
 withdrawDraft=draft;
 const version=modalVersion;
 const status=await request('mfa_status');if(version!==modalVersion)return;security=status;
 if(status.locked||status.forceReset)throw Error('账户验证已锁定，请先处理安全限制。');
 if(status.totpEnabled!==true){binding('withdraw');return}
 const session=await request('mfa_begin');if(version!==modalVersion)return;
 if(!session.methods?.includes('TOTP'))throw Error('本次提现验证未提供验证器方式。');
 modal('验证本次提现',pair('提现金额',esc(draft.amount)+' USDT')+'<p class="address">'+esc(draft.address)+'</p><form id="withdrawCode">'+otpField+submit('验证并核对提现')+'</form>');
}
function ids(){
 const m=mById.get(ticket.id),o=m.options[ticket.index];
 return {marketId:m.id,outcomeId:o.outcomeId,optionId:o.id};
}
async function refreshBook(){
 const selected=ticket, index=ticket.index, m=mById.get(ticket.id);
 if(!service()?.request)throw Error('此预览仅有目录快照，刷新深度需连接行情服务。');
 const result=await request('book',ids());
 if(ticket!==selected||ticket.index!==index||current()!=='market/'+m.id)return;
 if(!Array.isArray(result.asks)||!Array.isArray(result.bids))throw Error('订单簿返回不完整。');
 ticket.live=result;$('#depth').innerHTML=depth(m,m.options[index],result);
}
function reviewOrder(){
 const m=mById.get(ticket.id), opt=m.options[ticket.index];
 if(m.historical||Number(m.end)>0&&Number(m.end)<=Date.now())throw Error('该市场已截止。');
 modal('核对委托','<h3>'+esc(m.event)+'</h3>'+pair('盘口',esc(opt.group)+' · '+esc(opt.label))+pair('方向 / 类型',(ticket.side==='BUY'?'买入':'卖出')+' / '+modeLabel(ticket.mode))+
 (ticket.mode==='LIMIT'?pair('限价',esc(ticket.price)+' ¢/份'):'')+pair(ticket.mode==='MARKET'&&ticket.side==='BUY'?'支出金额':'委托份额',esc(ticket.quantity)+(ticket.mode==='MARKET'&&ticket.side==='BUY'?' USDT':' 份'))+
 errBox+btn(signed()?'prepareorder':'auth',signed()?'获取服务端确认信息':'使用 Telegram 授权'));
}
async function prepare(payload){
 const version=modalVersion, data=await request('prepare',payload);if(version!==modalVersion)return;
 if(!data?.token||!data.summary||data.kind!==payload.kind)throw Error('服务未返回有效的确认信息。');
 modal(payload.kind==='withdraw'?'确认提现':'确认下单','<p class="raw">'+esc(data.summary)+'</p>'+errBox+btn('execute',payload.kind==='withdraw'?'确认提交提现':'确认提交委托'));
 plan={token:data.token,summary:data.summary,kind:data.kind};
}
async function historyRows(kind){
 const version=modalVersion, data=await request(kind);if(version!==modalVersion)return;
 if(!Array.isArray(data?.rows))throw Error('服务未返回记录。');
 modal(kind==='deposits'?'充值记录':'提现记录',data.rows.length?data.rows.map(r=>pair(esc(r.amount)+' USDT',esc(r.status||r.createdAt))).join(''):'<p class="empty">暂无记录</p>');
}

function restoreControl(action){
 main.querySelector('[data-act="'+CSS.escape(action)+'"]')?.focus({preventScroll:true});
}
async function action(a){
 if(Object.hasOwn(pages,a)){openPage(a);return}
 const [verb,...rest]=a.split('-'),arg=rest.join('-');
 switch(verb){
 case 'close':sheet.close();return;
 case 'auth':auth();return;
 case 'settings':settings();return;
 case 'ruleMode':ruleMode(arg);return;
 case 'archive':archive();return;
 case 'archiveType':archiveType=arg;archivePage=0;archive();return;
 case 'archivePage':archivePage=Number(arg);archive();return;
 case 'signalinfo':signalInfo(signalsById.get(arg));return;
 case 'clearMarkets':keyword='';subcat='';marketPageNo=0;render();$('#searchMarkets').focus();return;
 case 'jump':{const target=document.getElementById(arg);target.focus({preventScroll:true});target.scrollIntoView({block:'start'});return}
 case 'cat':category=arg;subcat='';marketPageNo=0;render();restoreControl(a);return;
 case 'sport':subcat=arg;marketPageNo=0;render();restoreControl(a);return;
 case 'marketPage':marketPageNo=Number(arg);render();window.scrollTo(0,0);return;
 case 'pick':ticket.index=Number(arg);ticket.live=null;ticket.price='';render();$('.picker summary').focus();return;
 case 'side':case 'mode':if(ticket[verb]!==arg){ticket[verb]=arg;ticket.quantity='';render();restoreControl(a)}return;
 case 'refreshbook':await refreshBook();return;
 case 'deposit':deposit();return;
 case 'getaddress':await getAddress();return;
 case 'copyaddress':await navigator.clipboard.writeText($('#depositAddress').textContent);toast('地址已复制');return;
 case 'withdraw':withdraw();return;
 case 'bind':
   if($('#withdrawForm'))withdrawDraft=Object.fromEntries(new FormData($('#withdrawForm')));
   binding(arg);return;
 case 'setup':await setup(arg);return;
 case 'deposits':case 'withdrawals':modal(a==='deposits'?'充值记录':'提现记录','<p>'+ (signed()?'正在读取记录…':'授权后可查看资金记录。')+'</p>'+errBox+(!signed()?btn('auth','使用 Telegram 授权'):''));
   if(signed())await historyRows(a);return;
 case 'refreshorders':{
   const data=await request('orders');if(!Array.isArray(data.rows))throw Error('订单记录不可用。');
   orderRows=data.rows;render();return;
 }
 case 'activate':{
   const form=$('#rules');if(!form.reportValidity())return;
   const draft=readRules(form);
   modal('启用服务端跟单',pair('下单方式',modeLabel(draft.mode))+pair('单笔上限',esc(draft.amount)+' USDT')+pair('每日上限',esc(draft.limit)+' USDT')+errBox+btn('enable','确认启用'));
   inner.querySelector('[data-act=enable]').ruleDraft=draft;return;
 }
 case 'pause':
   modal('暂停服务端跟单','<p>暂停后将停止接收新信号。</p>'+errBox+btn('confirmpause','确认暂停'));return;
 case 'confirmpause':{
   const res=await request('follow_pause');if(res?.enabled!==false)throw Error('服务尚未确认暂停。');
   followActive=false;sheet.close();render();toast('服务端跟单已暂停');return;
 }
 case 'enable':{
   const draft=inner.querySelector('[data-act=enable]').ruleDraft;
   const res=await request('follow_enable',draft);if(!res?.enabled)throw Error('服务尚未确认启用。');
   followActive=true;rules=draft;sheet.close();render();toast('服务端跟单已启用');return;
 }
 case 'prepareorder':{
   const payload={kind:'order',...ids(),direction:ticket.side,orderType:ticket.mode};
   if(ticket.mode==='LIMIT')payload.limitPrice=Number(ticket.price)/100;
   if(ticket.side==='BUY'&&ticket.mode==='MARKET')payload.price=ticket.quantity;else payload.share=ticket.quantity;
   await prepare(payload);return;
 }
 case 'execute':{
   const payload=plan;plan=null;if(!payload)throw Error('请重新核对后提交。');
   const version=modalVersion;
   try{
     const res=await request('execute',payload);
     if(version!==modalVersion)return;
     if(!res?.message||res.error)throw Error(res?.message||'服务未返回提交回执。');
     if(payload.kind==='withdraw')withdrawDraft=null;
     modal('提交结果','<p>'+esc(res.message)+'</p>'+btn('close','完成'));
   }catch(e){if(version===modalVersion)modal('请核对提交状态','<p>'+errText(e)+'</p><p class="notice">重新提交前，请先查看相关记录。</p>'+btn(payload.kind==='withdraw'?'withdrawals':'orders','查看记录'));}return;
 }
 }
}
async function handleForm(f){
 const data=Object.fromEntries(new FormData(f)),version=modalVersion;
 switch(f.id){
 case 'rules':
   rules=readRules(f);localStorage.setItem('desk.ruleDraft.v2',JSON.stringify(rules));sheet.close();render();toast('跟单规则草稿已保存');return;
 case 'ticket':refreshEstimate();reviewOrder();return;
 case 'authorize':{
   const nickname=data.nickname.trim();if(!nickname||!tg()?.initData)throw Error('请核对 Telegram 身份和昵称。');
   const res=await request('telegram_authorize',{initData:tg().initData,nickname});
   if(!res?.nickname||!res.foregateId)throw Error('账户服务尚未确认开户。');
   account=res;if(version===modalVersion)sheet.close();render();void loadPrivate();return;
 }
 case 'addressQR':showAddress(data.address.trim(),'手动输入的地址 · 请与 ForeGate 核对');return;
 case 'bindCode':{
   const result=await request('mfa_setup_complete',{code:data.code});if(version!==modalVersion)return;
   if(!Array.isArray(result?.recoveryCodes))throw Error('服务未返回绑定结果。');
   security={...security,totpEnabled:true};
   modal('验证器已绑定',bindingSteps(2)+'<p class="notice">'+(result.recoveryCodes.length?'请将以下恢复码保存在安全的位置。关闭后无法再次查看。':'服务端未返回恢复码。')+'</p><div class="codes">'+result.recoveryCodes.map(s=>'<code>'+esc(s)+'</code>').join('')+'</div>'+btn(f.dataset.origin==='withdraw'?'withdraw':'close',f.dataset.origin==='withdraw'?'已保存，继续提现':'完成'));render();return;
 }
 case 'withdrawForm':await withdrawVerify({address:data.address.trim(),amount:data.amount});return;
 case 'withdrawCode':{
   const result=await request('mfa_verify',{method:'TOTP',code:data.code});
   if(version!==modalVersion)return;
   if(result?.verified!==true)throw Error('验证码未通过，请重试。');
   await prepare({kind:'withdraw',toAddress:withdrawDraft.address,amount:withdrawDraft.amount});return;
 }
 }
}
async function runButton(button,fn){
 if(button?.disabled)return;
 const form=button?.closest('form');
 if(button){button.disabled=true;button.setAttribute('aria-busy','true')}
 const pane=sheet.open?inner:main;
 const errorSlot=form?.querySelector('.sheeterr')||button?.closest('#depth')?.querySelector('.sheeterr')||[...pane.querySelectorAll('.sheeterr')].find(el=>!el.closest('details:not([open])'));
 if(errorSlot)errorSlot.textContent='';
 try{await fn()}catch(e){
   const out=errorSlot?.isConnected?errorSlot:sheet.open?[...inner.querySelectorAll('.sheeterr')].find(el=>!el.closest('details:not([open])')):null;
   if(out?.isConnected)out.textContent=e.message||'请求未完成。';else toast(e.message||'请求未完成。');
 }finally{if(button?.isConnected){button.disabled=false;button.removeAttribute('aria-busy')}}
}
document.addEventListener('click',e=>{
 const card=e.target.closest('.marketcard');
 if(card)marketReturn={id:card.hash,top:window.scrollY};
 const b=e.target.closest('[data-act]');
 if(b&&!sheet.open)modalTrigger=b;
 if(b)void runButton(b,()=>action(b.dataset.act));
 const picker=$('.picker[open]');if(picker&&!picker.contains(e.target))picker.open=false;
 if(e.target===sheet){const r=sheet.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)sheet.close()}
});
document.addEventListener('submit',e=>{e.preventDefault();void runButton(e.submitter,()=>handleForm(e.target))});
document.addEventListener('input',e=>{
 if(e.target.closest('#addressQR'))$('#depositResult').replaceChildren();
 if(e.target.id==='searchMarkets'){keyword=e.target.value;marketPageNo=0;$('#marketResults').innerHTML=marketResults()}
 if(e.target.closest('#ticket'))refreshEstimate();
});
document.addEventListener('keydown',e=>{
 const picker=e.target.closest('.picker');if(!picker)return;
 if(e.key==='Escape'){picker.open=false;picker.querySelector('summary').focus();return}
 if(!['ArrowDown','ArrowUp','Home','End'].includes(e.key))return;
 e.preventDefault();picker.open=true;const opts=[...picker.querySelectorAll('button')],i=opts.indexOf(e.target);
 const next=e.key==='ArrowDown'?i+1:i<0?opts.length-1:i-1;
 opts[e.key==='Home'?0:e.key==='End'?opts.length-1:(next+opts.length)%opts.length].focus();
});
sheet.addEventListener('close',()=>{
 if(sheet.open)return;
 modalVersion++;inner.replaceChildren();plan=null;
 if(modalTrigger?.isConnected)modalTrigger.focus({preventScroll:true});
 else if(modalTrigger)restoreControl(modalTrigger.dataset.act);
 modalTrigger=null;
});
let marketReturn=null;
window.addEventListener('hashchange',event=>{
 if(sheet.open){modalTrigger=null;sheet.close()}
 render();
 const oldRoute=new URL(event.oldURL).hash;
 if(current()==='markets'&&marketReturn?.id===oldRoute){
   const card=main.querySelector('.marketcard[href="'+CSS.escape(oldRoute)+'"]');
   card?.focus({preventScroll:true});window.scrollTo(0,marketReturn.top);
 }else{main.focus({preventScroll:true});window.scrollTo(0,0)}
});
async function loadPrivate(){
 if(!signed())return;
 const result=await Promise.allSettled([request('mfa_status'),request('balance'),request('orders')]);
 if(result[0].status==='fulfilled')security=result[0].value;
 if(result[1].status==='fulfilled')wallet=result[1].value;
 if(result[2].status==='fulfilled')orderRows=result[2].value?.rows;
 if(['account','funds','orders'].includes(current()))render();
}
async function boot(){
 render();
 if(service())try{const session=await request('session');if(session?.foregateId){account=session;followActive=session.followEnabled===true;render();void loadPrivate()}}catch{}
 if(tg()?.initData){tg().ready?.();if(!signed()&&!sheet.open)auth()}
}
void boot();
