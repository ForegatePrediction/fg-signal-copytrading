titles.execution='实盘工作台';
const executionBaseRender=render;let executionFormLoaded=false,executionAlertSeen=null;
render=function(s){executionBaseRender(s);renderExecution()};
const execKeys=['order_type','amount_mode','amount','stake_pct','max_order_usd','price_source','max_price_cents','fee_buffer','session_budget'];
const execLabels={QUEUED:'等待处理',PREFLIGHT:'提交前检查',READY:'准备完成',SUBMITTING:'真实提交中',UNKNOWN:'结果不明',OPEN:'挂单中',PARTIAL:'部分成交',FILLED:'已成交',CANCEL_PENDING:'撤单中',CANCEL_UNKNOWN:'撤单结果不明',CANCELLED:'已撤销',EXPIRED:'平台已到期',ACTIVITY_CONFIRMED:'新增买入已确认 · 终态待核对',SETTLED:'已核验结算',SKIPPED:'已跳过',REJECTED:'平台拒绝'};
async function execAct(action,data={}){
 $('execError').classList.add('hide');try{const r=await rpc(action,data);if(r?.settings)render(r);return r}catch(e){$('execError').textContent=e.message;$('execError').classList.remove('hide');return null}
}
function positionDetails(order){
 if(!order.committed_at)return '';
 if(!order.position_target)return order.position_before?.error?'<p class="hint">持仓待核对 · '+escapeHTML(order.position_before.error)+'</p>':'';
 const text=order.position_summary||'提交约一分钟后读取 My Position；中断后可手动核对。';
 return '<p class="hint">详情页持仓 · '+escapeHTML(text)+'</p><button class="small" data-live-position="'+escapeHTML(order.id)+'">只读查看持仓</button>';
}
function submissionDetails(order){
 if(!Array.isArray(order.post_submit_feedback))return '';
 const lines=[...order.post_submit_feedback,...(order.post_submit_dialogs||[])];
 if(!lines.length)lines.push('未读到页面反馈。');
 if(order.post_submit_screenshot)lines.push('本机截图 '+order.post_submit_screenshot);
 return '<details><summary>提交后的页面反馈</summary>'+lines.map(line=>'<p class="hint">'+escapeHTML(line)+'</p>').join('')+'</details>';
}
function renderExecution(){
 const x=state?.execution;if(!x)return;const c=x.config;
 if(!executionFormLoaded){for(const k of execKeys)$('exec_'+k).value=c[k];$('exec_auto_cancel').checked=c.auto_cancel;$('exec_notify').checked=c.notify;$('execAliases').value=Object.entries(c.aliases||{}).map(([k,v])=>k+' = '+v).join('\n');$('execPoll').value=state.settings.poll_interval_sec;executionFormLoaded=true}
 $('execBadge').textContent=x.armed?'真实资金 · 已启用':x.busy?'只读/核对中':'未启用';
 $('execArm').disabled=x.armed||x.busy||!x.prepared||Date.now()-Date.parse(x.prepared.at)>180000;
 for(const id of ['execSave','execPrepare','execRead'])$(id).disabled=x.armed||x.busy;
 $('execSession').textContent=(x.prepared?'会话准备于 '+time(x.prepared.at)+'，3 分钟有效。准备不下单；启用后跟随新采集且未过期的信号。':'先保存参数并准备会话。启动时实盘关闭，不恢复提交旧队列。')+(Number(x.ledger.totals.single_test_reserved)>0?' 单次自动测试已预留 '+x.ledger.totals.single_test_reserved+' USDT，已计入本轮累计额度；结果不明不释放额度。':'');
 const lastAlert=x.ledger.alerts[0];if(executionAlertSeen!==null&&lastAlert&&lastAlert.id>executionAlertSeen&&c.notify)toast(lastAlert.message,lastAlert.level==='error');executionAlertSeen=lastAlert?.id||0;
 if(!x.armed&&!x.busy&&x.ledger.orders.some(o=>['UNKNOWN','CANCEL_UNKNOWN','ACTIVITY_CONFIRMED'].includes(o.state)))$('executionBadge').textContent='实盘结果待核对';
 if(x.armed)$('executionBadge').textContent='频道实盘已启用';else if(x.busy)$('executionBadge').textContent='真实账户核对中';
 const t=x.ledger.totals;$('execMetrics').innerHTML=[['本轮累计预留',t.spent],['今日累计预留',t.daily],['本机管理的未结算投入上界',t.exposure],['已核验今日盈亏',t.pnl]].map(([k,v])=>`<div class="card metric"><div class="metric-label">${escapeHTML(k)} · USDT</div><div class="metric-value">${money(v)}</div></div>`).join('');
 $('execOrders').innerHTML=x.ledger.orders.length?`<div class="tablewrap"><table><thead><tr><th>事件 / 玩法</th><th>本机 / 平台编号</th><th>状态</th><th>预算 / 成本 / 费用</th><th>已成交份额</th><th>操作与原因</th></tr></thead><tbody>${x.ledger.orders.map(o=>`<tr><td>${escapeHTML(o.signal.home+' vs '+o.signal.away)}<br>${escapeHTML((names[o.leg.market_type]||o.leg.market_type)+' · '+o.leg.selection+' '+o.leg.line+' / '+o.leg.side)}</td><td>${escapeHTML(o.id.slice(0,8))}<br>${escapeHTML(o.order_id||'平台未提供明确编号')}</td><td>${escapeHTML(execLabels[o.state]||o.state)}</td><td>${money(o.budget)} / ${money(o.cost)} / ${money(o.fee)}</td><td>${escapeHTML(o.filled)}</td><td>${o.committed_at?`<button class="small" data-live-reconcile="${o.id}" ${x.armed||x.busy?'disabled':''}>只读核对</button>`:''}${['OPEN','PARTIAL'].includes(o.state)&&o.order_id?`<button class="small" data-live-cancel="${o.id}" ${x.armed||x.busy?'disabled':''}>撤销剩余</button>`:''}<p class="hint">${escapeHTML(o.reason||'')}</p>${positionDetails(o)}${submissionDetails(o)}${o.channel_report?`<p class="hint">频道报告（未验证）：${escapeHTML(o.channel_report)}</p>`:''}</td></tr>`).join('')}</tbody></table></div>`:empty('暂无真实订单','未启用时不会发送订单；这里与模拟账本分开。');
 const a=x.account;$('execAccount').innerHTML=a?`<p class="hint">只读更新 ${time(a.observed_at)} · 可用余额 ${escapeHTML(a.balance)} USDT</p>`+a.views.map(v=>`<h3 class="section">${escapeHTML(v.name)}</h3>${v.tables.map(t=>`<div class="tablewrap"><table><thead><tr>${t.headers.map(h=>`<th>${escapeHTML(h)}</th>`).join('')}</tr></thead><tbody>${t.rows.map(r=>`<tr>${r.cells.map(c=>`<td>${escapeHTML(c)}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`).join('')||'<p class="hint">当前页无可读取表格；不表示没有订单。</p>'}`).join(''):empty('尚未读取账户','点击只读刷新账户；页面未提供的订单号或状态保留未知。');
 const stats=x.ledger.statistics;$('execStats').innerHTML=stats.length?`<table><tr><th>分类</th><th>项目</th><th>已结算笔数</th><th>盈利比例</th><th>净盈亏</th></tr>${stats.map(s=>`<tr><td>${escapeHTML(s.kind)}</td><td>${escapeHTML(s.key)}</td><td>${s.count}</td><td>${s.win_rate}%</td><td>${money(s.pnl)}</td></tr>`).join('')}</table>`:empty('暂无可核验结算','只使用平台明确的结算字段计算统计；频道自称输赢只存档，不计入真实盈亏。');
 $('execAlerts').innerHTML=x.ledger.alerts.length?x.ledger.alerts.map(a=>`<div class="logline"><span class="logtime">${time(a.at)}</span><span>${a.read?'':'● '}${escapeHTML(a.message)}</span></div>`).join(''):empty('暂无告警','异常、状态不明及风控暂停会记录在这里。');
}
$('execSave').onclick=async()=>{
 const config={...state.execution.config};for(const k of execKeys)config[k]=$('exec_'+k).value;config.auto_cancel=$('exec_auto_cancel').checked;config.notify=$('exec_notify').checked;config.aliases={};for(const line of $('execAliases').value.split('\n').map(x=>x.trim()).filter(Boolean)){const parts=line.split('=');if(parts.length!==2||!parts[0].trim()||!parts[1].trim()||config.aliases[parts[0].trim()]){$('execError').textContent='映射应为每行 原缩写 = 页面缩写，不能重复。';$('execError').classList.remove('hide');return}config.aliases[parts[0].trim()]=parts[1].trim()}
 let r=await execAct('live_save',{config});if(!r)return;
 const settings=JSON.parse(JSON.stringify(state.settings));delete settings.api_hash_set;settings.poll_interval_sec=Number($('execPoll').value);
 r=await execAct('save_settings',settings);if(r)toast('实盘参数与采集间隔已保存')
};
$('execPrepare').onclick=()=>execAct('live_prepare_session');
$('execArm').onclick=()=>execAct('live_arm',{plan_id:state.execution.prepared?.id,summary:`@${state.settings.channel} · ${state.execution.config.order_type} · 累计 ${state.execution.config.session_budget} USDT`});
$('execStop').onclick=()=>execAct('live_stop');$('execRead').onclick=()=>execAct('live_read_account');$('execAck').onclick=()=>execAct('live_ack_alerts');$('execExport').onclick=async()=>{const r=await execAct('live_export');if(r?.saved)toast('真实订单 CSV 已保存')};
document.addEventListener('click',e=>{const b=e.target.closest('[data-live-reconcile],[data-live-cancel],[data-live-position]');if(!b)return;if(b.dataset.livePosition)execAct('live_position',{order_id:b.dataset.livePosition});else if(b.dataset.liveReconcile)execAct('live_reconcile',{order_id:b.dataset.liveReconcile});else execAct('live_cancel',{order_id:b.dataset.liveCancel})});
