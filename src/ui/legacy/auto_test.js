// A separate one-shot live test, enabled by the user through the native confirmation.
titles.autotest='自动实盘测试';
const autoBaseRender=render;
render=function(s){autoBaseRender(s);renderAutoTest()};
const autoLabels={PREPARING:'准备中 · 未下单',READY:'已准备 · 等待你启用',COMMITTING:'已进入真实提交边界',AWAITING_ACTIVITY:'已提交一次 · 核对中',ACTIVITY_CONFIRMED:'新增买入活动已确认',PARTIAL_ACTIVITY:'仅确认部分金额 · 不重试',UNKNOWN:'结果不明 · 已锁定，禁止重试',ABORTED:'已停止 · 未提交'};
function renderAutoTest(){
 const a=state?.auto_test,busy=state?.auto_test_busy;
 $('executionBadge').textContent=busy?'自动实盘测试运行中':a?.state==='READY'?'实盘测试待启用':a?.state==='UNKNOWN'?'实盘结果待核对':'模拟模式';
 $('autoPlan').disabled=!!busy||!!a&&['READY','COMMITTING','AWAITING_ACTIVITY','UNKNOWN','ACTIVITY_CONFIRMED','PARTIAL_ACTIVITY'].includes(a.state);
 $('autoEnable').disabled=!!busy||!a||a.state!=='READY';$('autoCancel').disabled=!!busy||!a||a.state!=='READY';
 $('autoRead').disabled=!!busy;
 if(!a?.target){$('autoResult').innerHTML=empty('先准备，再由你启用','准备过程读取旧活动作为基线并核对网页，不点击 Trade。启用后客户端独立执行一笔。');return}
 const r=a.receipt?.activity;
 $('autoResult').innerHTML=`<div class="panel-head"><h2>${escapeHTML(autoLabels[a.state]||a.state)}</h2><span class="badge demo">真实资金 · 单次测试</span></div><div class="panel-body"><p>${escapeHTML(a.title||'事件 '+a.target.event_id)}</p><p>Moneyline · ${escapeHTML(a.target.selection)} · Buy / Market · 金额 ${escapeHTML(a.target.amount)} USDT</p><p class="hint">准备时间 ${time(a.prepared_at||a.updated_at)} · 提交前平均报价上限 ${escapeHTML(a.target.max_price_cents)}¢</p><p class="notice">${escapeHTML(a.reason||'')}</p>${r?`<table><tr><th>份额（页面显示）</th><th>活动均价</th><th>成交金额</th><th>手续费</th><th>总支出</th></tr><tr><td>${escapeHTML(r.shares)}</td><td>${escapeHTML(r.price_cents)}¢</td><td>${escapeHTML(r.notional)}</td><td>${escapeHTML(r.fee)}</td><td>${escapeHTML(r.total)} USDT</td></tr></table><p class="hint">${escapeHTML(r.displayed_date)} · 页面新增买入活动，不等同于按唯一订单号核验终态。</p>`:''}<p class="footnote">测试记录保存在本机，已提交或结果不明时重启也不会重试。自动测试额度 2 USDT，整个测试最多一笔。连续频道路径在实盘工作台，尚待真实验收。</p></div>`;
}
async function autoAct(action,data={}){
 const e=$('autoError');e.classList.add('hide');
 try{render(await rpc(action,data))}catch(err){e.textContent=err.message;e.classList.remove('hide');await refresh()}
}
$('autoPlan').onclick=async()=>{const b=$('autoPlan');b.disabled=true;await autoAct('auto_test_prepare',{event_id:$('autoEvent').value.trim(),selection:$('autoSelection').value.trim(),amount:'2',max_price_cents:$('autoPrice').value});renderAutoTest()};
$('autoEnable').onclick=async()=>{const a=state?.auto_test;if(!a||a.state!=='READY')return;$('autoEnable').disabled=true;await autoAct('auto_test_execute',{plan_id:a.id,selection:a.target.selection,title:a.title});renderAutoTest()};
$('autoCancel').onclick=()=>autoAct('auto_test_cancel');
$('autoRead').onclick=()=>autoAct('auto_test_reconcile');
