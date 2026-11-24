'use strict';

const $ = selector => document.querySelector(selector);
const html = value => String(value ?? '—').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c]);
const titles = {accounts:'ForeGate API 账户',overview:'账户概览',market:'市场交易',positions:'持仓',orders:'订单与成交',funds:'充值提现',follow:'自动跟单',connection:'连接设置'};
const statusNames = {ACCEPTED:'平台已受理',OPEN:'挂单中',PENDING:'等待处理',NEW:'待撮合',MATCHING:'撮合中',FILLED:'已成交',PARTIAL_FILLED:'部分成交',PARTIALLY_FILLED:'部分成交',CANCELED:'已撤销',CANCELLED:'已撤销',REJECTED:'已拒绝',SUBMITTING:'提交中',SUBMITTED:'平台已受理',QUEUED:'已排队',PRECHECK:'核对中',SKIPPED:'已跳过',UNKNOWN:'待人工核对',MANUALLY_REVIEWED:'已人工核对',CLEARED_NO_ORDER:'核对为未执行',EXPIRED:'已过期',FAILED:'失败',open:'交易中',closed:'已关闭',resolved:'已结算',BUY:'买入',SELL:'卖出',CLAIM:'申领'};
const cycleStatusNames = {UPCOMING_TRADABLE:'即将开始', ACTIVE:'进行中', QUIET:'已截止', PRICE_PENDING:'等待结算价格',
  TRADING_SUSPENDED:'暂停交易', RESOLVING:'正在结算', DISPUTE:'争议中', PAYOUT_PROCESSING:'正在派发',
  CORRECTING:'正在修正', SETTLED:'已结算', VOIDED:'已作废'};
const operationsLabels = {order:'交易',cancel:'撤单',claim:'申领',withdraw:'提现'};
const credentialStorage = window.desktop?.credentialStorage || 'macOS 钥匙串';
const state = {page:'accounts', version:0, accountBusy:false, enrollMode:'', orderTab:'local', fundTab:'deposit', marketPage:1, marketSearch:'', selected:null,
  marketTree:null, marketCategory:[], cutoffTimer:null,
  direction:'BUY', orderType:'LIMIT', position:null, status:null, assets:null, fundsAddress:null, mfaMethods:[], mfaVerified:false};
const pending = new Map();
let sequence = 0;

window.receive = response => {
  const task = pending.get(response.id);
  if (!task) return;
  pending.delete(response.id);
  if (response.ok) task.resolve(response.data);
  else task.reject(new Error(response.error || '请求未完成。'));
};

function rpc(action, data = {}) {
  if (window.desktop) return window.desktop.invoke(action, data).then(response => {
    if (response.ok) return response.data;
    throw new Error(response.error || '请求未完成。');
  });
  if (!window.webkit?.messageHandlers?.native) return Promise.reject(new Error('请在桌面客户端打开工作台。'));
  return new Promise((resolve, reject) => {
    const id = ++sequence;
    pending.set(id, {resolve, reject});
    window.webkit.messageHandlers.native.postMessage({id, action, data});
  });
}

const api = (action, data = {}) => rpc('api_' + action, {...data, account_context:state.status?.account_context ?? null});

function toast(message, error = false) {
  const node = $('#notice');
  node.hidden = !message;
  node.classList.toggle('error', error);
  node.textContent = message || '';
}
window.toast = toast;

function statusBadge(value) {
  let color = '';
  if (['FILLED','open','BUY'].includes(value)) color = 'green';
  if (['REJECTED','FAILED','SELL'].includes(value)) color = 'red';
  if (['UNKNOWN','SKIPPED'].includes(value)) color = 'amber';
  return `<span class="badge ${color}">${html(statusNames[value] ?? cycleStatusNames[value] ?? value)}</span>`;
}

const btn = (label, id, style = '') => `<button type="button" id="${id}" class="${style}">${html(label)}</button>`;
const empty = label => `<p class="empty">${html(label)}</p>`;
const field = (label, name, value = '', extra = '') =>
  `<label class="field">${html(label)}<input name="${name}" value="${html(value ?? '')}" ${extra}></label>`;
const panel = (title, body, trailing = '') =>
  `<section class="panel"><header class="panel-head"><h2>${html(title)}</h2>${trailing}</header><div class="panel-body">${body}</div></section>`;
const rowText = value => html(value);
const table = (head, rows, message = '暂无记录') => rows.length ?
  `<div class="table-scroll"><table><thead><tr>${head.map(x => `<th scope="col">${html(x)}</th>`).join('')}</tr></thead><tbody>${rows.map(row =>
    `<tr>${row.map(cell => `<td>${cell}</td>`).join('')}</tr>`).join('')}</tbody></table></div>` : empty(message);
const metric = (label, value, suffix = '') =>
  `<section class="metric"><small>${html(label)}</small><strong>${decimalText(value)} <small>${html(suffix)}</small></strong></section>`;
const decimalDisplay = new Intl.NumberFormat('en-US', {
  useGrouping:false, minimumFractionDigits:3, maximumFractionDigits:3
});
function decimalText(value) {
  if (typeof value !== 'number' && (typeof value !== 'string' || !value.trim())) return '—';
  if (!Number.isFinite(Number(value))) return '—';
  const text = decimalDisplay.format(value);
  return text === '-0.000' ? '0.000' : text;
}
const decimalPair = (left, right) => left == null && right == null ? '—' : `${decimalText(left)} / ${decimalText(right)}`;
const formData = form => Object.fromEntries(new FormData(form));
const idString = x => typeof x === 'string' && /^\d{1,19}$/.test(x);
const choiceIds = () => {
  const selected = state.selected;
  if (!selected?.option) return null;
  const {detail, group, option} = selected;
  return {marketId:detail.marketId, outcomeId:group.outcomeId, optionId:option.optionId, ...(detail.cycle ? {cycle:true} : {})};
};

function formatTime(value) {
  if (value == null || value === '') return '—';
  const parsed = typeof value === 'string' && /^[0-9]{13}$/.test(value) ? Number(value) : value;
  const date = new Date(parsed);
  return Number.isNaN(date.getTime()) ? String(value) : new Intl.DateTimeFormat('zh-CN', {
    year:'numeric', month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit', second:'2-digit', hourCycle:'h23'
  }).format(date);
}

function on(id, work) {
  const node = document.getElementById(id);
  if (!node) return;
  node.addEventListener(node.tagName === 'FORM' ? 'submit' : 'click', async event => {
    event.preventDefault();
    if (node.dataset.busy) return;
    node.dataset.busy = '1';
    if (node.tagName === 'BUTTON') node.disabled = true;
    toast('');
    try { await work(node, event); }
    catch (error) { toast(error.message, true); }
    finally { node.disabled = false; delete node.dataset.busy; }
  });
}

function at(index, label, action) {
  return `<button class="compact" type="button" data-act="${action}" data-index="${index}">${html(label)}</button>`;
}

function bindRows(action, rows, callback) {
  document.querySelectorAll(`[data-act="${action}"]`).forEach((button, index) => {
    button.id = `row-${action}-${index}`;
    on(button.id, () => callback(rows[Number(button.dataset.index)]));
  });
}

function pager(data, id) {
  return `<div class="pager">${data.page > 1 ? btn('上一页', id+'-prev','compact') : ''}
    <span class="subtle">第 ${html(data.page)} 页</span>${data.more ? btn('下一页', id+'-next','compact') : ''}</div>`;
}

function bindPager(data, id, work) {
  on(id+'-prev', () => work(data.page-1));
  on(id+'-next', () => work(data.page+1));
}

function readError(reason) {
  return `<p class="amber">${html(reason.message || reason)}</p>`;
}

function updateHeader(status) {
  state.status = status;
  $('#account').textContent = status.account_id ? 'UID ' + status.account_id : '';
  $('#account').hidden = !status.account_id;
  $('#account-picker').textContent = status.account_id ? '切换账户' : '账户';
  $('#connection-status').textContent = status.verified ? 'API 已验证' : status.account_id ? 'API 未验证' : 'API 未连接';
  $('#connection-status').classList.toggle('green', status.verified);
  for (const nav of document.querySelectorAll('[data-page]')) nav.disabled = !status.account_context;
}

async function go(page) {
  if (!state.status?.account_context) page = 'accounts';
  clearTimeout(state.cutoffTimer);
  state.page = page;
  const version = ++state.version;
  if (page !== 'funds') {
    state.mfaMethods = [];
    state.mfaVerified = false;
  }
  $('#title').textContent = titles[page];
  document.querySelectorAll('[data-page]').forEach(node => {
    if (node.dataset.page === page) node.setAttribute('aria-current','page');
    else node.removeAttribute('aria-current');
  });
  $('#content').setAttribute('aria-busy','true');
  $('#content').innerHTML = empty('正在读取…');
  toast('');
  try { await pages[page](version); }
  catch (error) { if (version === state.version) $('#content').innerHTML = panel('读取失败', readError(error)); }
  finally { if (version === state.version) $('#content').setAttribute('aria-busy','false'); }
}

const activeView = version => version === state.version;
const setContent = (version, markup) => {
  if (!activeView(version)) return false;
  $('#content').innerHTML = markup;
  return true;
};

async function connect() {
  const context = state.status?.account_context;
  const connected = await api('connect');
  const status = await api('status');
  if (status.account_context === context && state.status?.account_context === context) updateHeader(status);
  return connected;
}

async function changeAccount(action, data) {
  if (state.accountBusy) return;
  state.accountBusy = true;
  const view = $('#account-view');
  for (const input of view.querySelectorAll('button, select, input')) input.disabled = true;
  try {
    const result = await api(action, data);
    Object.assign(state, {selected:null, position:null, fundsAddress:null, assets:result.assets,
      orderTab:'local', fundTab:'deposit', direction:'BUY', orderType:'LIMIT', enrollMode:''});
    updateHeader(result);
    await go('overview');
    toast('已连接 ' + result.label + ' · UID ' + result.account_id);
  } catch (error) {
    const connected = await api('status').catch(() => null);
    if (connected) updateHeader(connected);
    throw error;
  } finally {
    state.accountBusy = false;
    for (const input of view.querySelectorAll('button, select, input')) input.disabled = false;
  }
}

const guideSteps = [
  ['连接你的 API 账户', '已保存过账户，可直接选择并进入。首次连接请准备 APP Key、APP Secret 和 API Key。已有 API 账户请填写原商户用户标识和 UID。需要注册时，选择「创建新 API 账户」。'],
  ['核对账户，再查看市场', '进入「账户概览」，核对顶部 UID 和可用余额。打开「市场交易」，搜索赛事名称或 Event ID，选择盘口和方向即可查看订单簿。'],
  ['核对委托和成交', '下单前核对市价或限价、买卖方向和金额。提交后在「订单与成交」查看平台状态，在「持仓」查看份额。提交结果不明时，先核对记录，避免重复下单。'],
  ['需要自动跟单时，先仅监听', '在「连接设置」保存 Telegram 频道，再到「自动跟单」点「仅监听信号」。确认收到信号后，选择市价或限价并保存金额上限。准备使用真实资金时，再允许并确认启用实盘跟单。']
];
let guideIndex = 0;

function renderGuide() {
  const [title, body] = guideSteps[guideIndex];
  $('#guide-progress').textContent = '新手指引 · ' + (guideIndex + 1) + ' / ' + guideSteps.length;
  $('#guide-title').textContent = title;
  $('#guide-body').textContent = body;
  $('#guide-back').hidden = guideIndex === 0;
  $('#guide-next').textContent = guideIndex === guideSteps.length - 1 ? '完成指引' : '下一步';
}

function openGuide() {
  if ($('#guide').open) return;
  guideIndex = 0;
  $('#guide-error').hidden = true;
  renderGuide();
  $('#guide').showModal();
  $('#guide-next').focus();
}

async function dismissGuide() {
  try {
    await api('guide_dismiss');
    $('#guide').close();
  } catch (error) {
    $('#guide-error').textContent = error.message;
    $('#guide-error').hidden = false;
  }
}

async function accounts(version) {
  const catalog = await api('account_catalog');
  if (!activeView(version)) return;
  updateHeader(catalog);
  if (catalog.guide_seen === false) openGuide();
  const selected = catalog.profiles.find(profile => profile.uid === (catalog.account_id ?? catalog.last_uid));
  const options = catalog.profiles.map(profile => `<option value="${html(profile.slot)}" ${profile === selected ? 'selected' : ''}>
    ${html(profile.label)} · UID ${html(profile.uid)}</option>`).join('');
  const saved = catalog.profiles.length ? `<div class="account-saved">
    <form id="choose-account" class="forms"><label class="field">本机已保存账户<select name="slot" required>${options}</select></label>
    <button class="primary">进入所选账户</button>${catalog.account_context ? btn('返回当前账户','keep-account') : ''}</form>
    <p class="subtle">切换时会暂停自动跟单。回到原账户后继续核对该账户的挂单和成交。</p></div>` : '';
  const mode = state.enrollMode;
  const form = mode ? `<form id="enroll-account" class="forms account-enroll">
    ${field('账户昵称（选填）','label','','maxlength="50" autocomplete="off"')}
    ${mode === 'existing' ? field('已有 ForeGate UID','uid','','required maxlength="128" autocomplete="off"') : ''}
    ${field('商户用户标识 (marchantUserId)','reference',mode === 'new' ? catalog.draft_reference : '',
       mode === 'new' ? 'readonly' : 'required maxlength="128" autocomplete="off"')}
    ${field('Merchant ID（选填）','merchant_id','','maxlength="128" autocomplete="off"')}
    ${field('APP Key','appKey','','type="password" required maxlength="1024" autocomplete="off"')}
    ${field('APP Secret','appSecret','','type="password" required maxlength="1024" autocomplete="off"')}
    ${field('API Key','apiKey','','type="password" required maxlength="1024" autocomplete="off"')}
    <p class="subtle full">${mode === 'new' ? '使用此标识创建 ForeGate API 账户。换电脑时可用原标识和 UID 重新连接。' : '填写创建该 API 账户时使用的商户用户标识。连接后会核对返回的 UID。'}</p>
    <div class="full flex"><button class="primary">${mode === 'new' ? '注册并进入' : '验证并进入'}</button>
    ${btn('取消','cancel-enroll')}</div></form>` : '';
  const legacy = catalog.has_legacy_journal ? '<p class="subtle">旧版本机记录已保留。升级后，请先通过「连接已有账户」连接原来的 ForeGate API 账户。</p>' : '';
  setContent(version, `<section class="panel account-panel" id="account-view">
    <header class="panel-head"><h2>${catalog.profiles.length ? '选择或添加账户' : '连接你的 ForeGate API 账户'}</h2></header>
    <div class="panel-body">${saved}<p>使用 ForeGate 开通的 APP Key、APP Secret 和 API Key。凭证保存在${html(credentialStorage)}。</p>
    ${legacy}<div class="flex account-actions">${btn('连接已有账户','enroll-existing',mode === 'existing' ? 'primary' : '')}
    ${btn('创建新 API 账户','enroll-new',mode === 'new' ? 'primary' : '')}</div>${form}
    </div></section>`);
  on('choose-account', form => changeAccount('account_select', formData(form)));
  on('keep-account', () => go('overview'));
  on('enroll-existing', () => { state.enrollMode = 'existing'; return go('accounts'); });
  on('enroll-new', () => { state.enrollMode = 'new'; return go('accounts'); });
  on('cancel-enroll', () => { state.enrollMode = ''; return go('accounts'); });
  on('enroll-account', form => {
    const data = formData(form);
    for (const key of Object.keys(data)) data[key] = data[key].trim();
    return changeAccount('account_enroll', {...data, mode});
  });
}

function activity(data) {
  return table(['市场 / Event ID','类型','份额','成交价','金额 / 费用','本机时间'], data.rows.map(item => {
    const eventId = [item.eventId, item.marketId].find(idString);
    const title = [item.marketTitle ?? item.title, item.outcomeTitle, item.optionTitle].filter(Boolean).join(' · ');
    return [html(title || '市场详情') + `<div class="subtle mono event-id">Event ID ${html(eventId)}</div>`,
      statusBadge(String(item.type ?? item.tradeType ?? '—').toUpperCase()), decimalText(item.shares ?? item.share),
      decimalText(item.price), decimalPair(item.value ?? item.amount, item.fees ?? item.fee),
      html(formatTime(item.date ?? item.createdAt))];
  }));
}

async function overview(version) {
  const [wallet] = await Promise.allSettled([connect()]);
  if (!activeView(version)) return;
  const [history] = wallet.status === 'fulfilled' ? await Promise.allSettled([api('trades')]) : [wallet];
  if (!activeView(version)) return;
  const assets = wallet.status === 'fulfilled' ? wallet.value.assets : null;
  setContent(version, `<div class="cards">${metric('可用余额', assets?.totalAvailableAmount ?? assets?.availableAmount ?? '—','USDT')}
    ${metric('冻结金额', assets?.frozenAmount ?? assets?.holdingOrderAmount ?? '—','USDT')}${metric('持仓价值', assets?.positionValue ?? '—','USDT')}</div>` +
    (wallet.status === 'rejected' ? panel('API 连接', readError(wallet.reason), btn('重新连接','retry-connect')) : '') +
    panel('最近成交与申领', history.status === 'fulfilled' ? activity(history.value) : readError(history.reason), btn('全部记录','overview-history','compact')));
  on('retry-connect', () => go('overview'));
  on('overview-history', () => {state.orderTab = 'trades'; return go('orders');});
}

async function openMarket(marketId, holding = null, locator = {}) {
  const previousVersion = state.version;
  const detail = await api('detail', {marketId, ...locator});
  if (!activeView(previousVersion)) return;
  const options = (detail.outcomes || []).flatMap(group => (group.options || []).map(option => ({group, option})));
  const requested = holding ?? locator;
  const next = requested.optionId ? options.find(entry => entry.group.outcomeId === (requested.outcomeId ?? locator.windowId)
    && entry.option.optionId === requested.optionId) : options[0];
  if (holding && !next) throw new Error('当前市场里找不到这条持仓所属的选项。');
  state.selected = {detail, options, group:next?.group, option:next?.option};
  state.position = holding;
  state.direction = holding ? 'SELL' : 'BUY';
  state.orderType = 'LIMIT';
  await go('market');
}

function optionLabel(item) {
  return [item?.group?.title || item?.group?.name, item?.option?.title].filter(Boolean).join(' · ');
}

function marketList(data) {
  return table(['市场','状态',''], data.rows.map((item, index) => [
    `<strong>${html(item.title)}</strong><div class="subtle mono event-id">Event ID ${html(item.marketId)}</div>`,
    statusBadge(item.cycle ? (item.windowId ? '加密周期盘' : '暂无当前窗口') : item.status),
    idString(item.marketId) && (!item.cycle || idString(item.windowId)) ? at(index,'打开','open') : ''
  ]), data.empty_message || '暂无匹配市场') + (data.series_error ? readError('部分周期市场暂时无法读取。') : '') +
    (data.rows.length || data.page > 1 || data.more ? pager(data,'markets') : '');
}

function bookMarkup(book) {
  const level = (entry, side) => `<tr><td class="${side}">${decimalText(entry.price)}</td><td class="numeric">${decimalText(entry.size ?? entry.quantity ?? entry.share)}</td></tr>`;
  const asks = [...book.asks].sort((a,b) => Number(a.price)-Number(b.price)).slice(0,6).reverse();
  const bids = [...book.bids].sort((a,b) => Number(b.price)-Number(a.price)).slice(0,6);
  return `<table class="book"><thead><tr><th>价格（USDT/份）</th><th class="numeric">份额</th></tr></thead><tbody>
    ${asks.map(ask => level(ask,'red')).join('') || '<tr><td colspan="2" class="subtle">暂无卖盘</td></tr>'}
    <tr><td class="mid" colspan="2">卖盘 ↑　买盘 ↓</td></tr>
    ${bids.map(bid => level(bid,'green')).join('') || '<tr><td colspan="2" class="subtle">暂无买盘</td></tr>'}</tbody></table>`;
}

function openId(row) { return row.id ?? row.orderId; }

async function actionSubmit(payload) {
  const plan = await api('prepare', payload);
  const result = await api('execute', {token:plan.token, summary:plan.summary, kind:plan.kind});
  const parts = [statusNames[result.state] ?? result.state, result.orderId ? '订单号 ' + result.orderId : '', result.message].filter(Boolean);
  return {message:parts.join(' · '), error:['UNKNOWN','REJECTED'].includes(result.state)};
}

function outcomeFields(holding) {
  return {marketId:holding.marketId,outcomeId:holding.outcomeId,optionId:holding.optionId,positionId:holding.positionId};
}

async function tradePanel(version) {
  const selected = state.selected;
  if (!selected) return;
  const ids = choiceIds();
  const cycle = selected.detail.cycle;
  const untilCutoff = cycle ? Date.parse(selected.detail.endTime) - Date.now() : 0;
  const tradeable = ids && selected.detail.modelType === 'orderbook'
    && (cycle ? selected.detail.tradingEnabled && untilCutoff > 0 : selected.detail.status === 'open');
  const showOrders = tradeable || cycle && ids;
  const options = selected.options.map((entry, index) => {
    const active = entry.option === selected.option;
    return `<button type="button" data-option="${index}" class="${active ? 'selected' : ''}" aria-pressed="${active}">
      <span class="option-label">${html(optionLabel(entry))}</span>
      <span class="option-price">${decimalText(entry.option.priceBuy ?? entry.option.price)}</span></button>`;
  }).join('');
  const markup = panel(selected.detail.title, `<div class="flex subtle"><span class="mono">${html(selected.detail.marketId)}</span>
    ${cycle ? '<span class="mono">窗口 '+html(selected.detail.windowId)+'</span>' : ''}
    ${statusBadge(cycle && selected.detail.tradingEnabled && untilCutoff <= 0 ? 'QUIET' : selected.detail.status)}
    ${selected.detail.endTime ? '<span>交易截止 '+html(formatTime(selected.detail.endTime))+'</span>' : ''}</div>
    <div class="within option-list">${options || empty('市场没有可选盘口')}</div>`, btn('返回市场','market-back','compact'));
  const trading = (tradeable ? panel('订单簿', '<div id="book-slot">'+empty('正在读取…')+'</div>', btn('刷新深度','book-refresh','compact')) : '') +
    (tradeable ? panel('交易委托', `<div class="flex trade-side">${btn('买入','side-buy',state.direction === 'BUY' ? 'primary' : '')}
      ${btn('卖出','side-sell',state.direction === 'SELL' ? 'primary' : '')}</div><div id="ticket"></div>`) :
      panel('交易委托', empty(cycle ? '这一期当前不可交易。可返回市场查看最新窗口。' : '此市场当前不可提交订单簿委托。')));
  const orders = showOrders ? panel('该选项的挂单','<div id="open-slot">'+empty('正在读取…')+'</div>',btn('刷新','open-refresh','compact')) : '';
  const cycleRecords = cycle && ids ? panel('该选项的持仓与成交','<div id="cycle-records">'+empty('正在读取…')+'</div>',
    btn('刷新','cycle-records-refresh','compact')) : '';
  if (!setContent(version, `<div class="columns market-columns"><div>${markup}${orders}${cycleRecords}</div><div>${trading}</div></div>`)) return;
  if (tradeable && cycle) {
    state.cutoffTimer = setTimeout(() => {if (activeView(version)) void go('market');}, Math.min(untilCutoff + 25, 2147483647));
  }
  const optionList = $('.option-list');
  const activeOption = optionList.querySelector('.selected');
  if (activeOption && activeOption.offsetTop + activeOption.offsetHeight > optionList.clientHeight) {
    optionList.scrollTop = activeOption.offsetTop - (optionList.clientHeight - activeOption.offsetHeight) / 2;
  }
  on('market-back', () => {state.selected = null;state.position = null; return go('market');});
  document.querySelectorAll('[data-option]').forEach(button => {
    button.id = 'choice-'+button.dataset.option;
    on(button.id, () => {
      Object.assign(selected, selected.options[Number(button.dataset.option)]);
      state.position = null;
      return go('market');
    });
  });
  on('side-buy', () => {state.direction = 'BUY'; return go('market');});
  on('side-sell', () => {state.direction = 'SELL'; return go('market');});
  const updateBook = async () => {
    try {
      const book = await api('book', ids);
      if (activeView(version)) $('#book-slot').innerHTML = bookMarkup(book);
    } catch (error) { if (activeView(version)) $('#book-slot').innerHTML = readError(error); }
  };
  const updateOpen = async (page = 1) => {
    try {
      const data = await api('open_orders',{...ids,page});
      if (!activeView(version)) return;
      $('#open-slot').innerHTML = table(['订单编号','方向','价格','已成交 / 总份额',''], data.rows.map((item,index) => [
        `<span class="mono">${html(openId(item))}</span>`, statusBadge(item.side ?? item.direction),
        decimalText(item.price ?? item.limitPrice), decimalPair(item.filled ?? item.currentShare,item.share ?? item.total),
        idString(openId(item)) ? at(index,'撤单','cancel') : ''
      ])) + pager(data,'market-open');
      bindRows('cancel', data.rows, async item => {
        const result = await actionSubmit({kind:'cancel',...ids,orderId:openId(item)});
        if (activeView(version)) {
          await Promise.all([updateOpen(page), cycle && updateCycleRecords()]);
          toast(result.message,result.error);
        }
      });
      bindPager(data,'market-open',updateOpen);
    } catch (error) { if (activeView(version)) $('#open-slot').innerHTML = readError(error); }
  };
  const updateCycleRecords = async (page = 1) => {
    const [holdings, activity] = await Promise.allSettled([api('sell_positions',ids), api('cycle_activity',{...ids,page})]);
    if (!activeView(version)) return;
    const positionRows = holdings.status === 'fulfilled' ? table(['持仓编号','份额','可卖','均价'],holdings.value.map(item => [
      rowText(item.positionId), decimalText(item.shares), decimalText(item.availableShare), decimalText(item.averagePrice)
    ]),'暂无持仓') : readError(holdings.reason);
    const activityRows = activity.status === 'fulfilled' ? table(['类型','份额','价格','金额 / 费用','时间'],activity.value.rows.map(item => [
      statusBadge(item.type), decimalText(item.shares), decimalText(item.price), decimalPair(item.value,item.fees),
      html(formatTime(typeof item.date === 'string' && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?$/.test(item.date) ? item.date+'Z' : item.date))
    ]),'暂无成交') + pager(activity.value,'cycle-activity') : readError(activity.reason);
    $('#cycle-records').innerHTML = positionRows + '<div class="within">'+activityRows+'</div>';
    if (activity.status === 'fulfilled') bindPager(activity.value,'cycle-activity',updateCycleRecords);
  };
  const loadTicket = async () => {
    const sell = state.direction === 'SELL';
    const limit = state.orderType === 'LIMIT';
    $('#ticket').innerHTML = '<p class="subtle">'+html(optionLabel(selected))+'</p>' +
      `<form id="ticket-form" class="forms"><label class="field full">委托类型<select id="order-type" name="orderType">
      <option value="LIMIT" ${limit ? 'selected' : ''}>限价</option><option value="MARKET" ${limit ? '' : 'selected'}>市价</option></select></label>
      ${limit ? field('限价（USDT/份）','limitPrice','','inputmode="decimal" required placeholder="例如 0.65"') : ''}
      ${sell || limit ? field('份额','share','','inputmode="decimal" required') : field('支出金额（USDT）','price','','inputmode="decimal" required')}
      ${sell ? '<div id="sell-holdings" class="full">'+empty('正在读取可卖持仓…')+'</div>' : ''}
      <div class="full"><button class="primary" type="submit" ${sell ? 'disabled' : ''}>核对并${sell ? '卖出' : '买入'}</button></div></form>`;
    $('#order-type').addEventListener('change', e => {state.orderType = e.target.value; void loadTicket();});
    const ticket = $('#ticket-form');
    on('ticket-form', async form => {
      const result = await actionSubmit({kind:'order',...ids,...formData(form), direction:sell ? 'SELL' : 'BUY'});
      if (activeView(version)) {
        if (!result.error) form.querySelectorAll('input').forEach(input => {input.value = '';});
        await Promise.all([updateBook(),updateOpen(),cycle && updateCycleRecords()]);
        toast(result.message, result.error);
      }
    });
    if (!sell) return;
    try {
      const rows = await api('sell_positions', ids);
      if (!activeView(version) || ticket !== $('#ticket-form')) return;
      const matches = rows.filter(item => idString(item.positionId) && Number(item.availableShare) > 0);
      const current = state.position?.positionId;
      $('#sell-holdings').innerHTML = matches.length ? `<label class="field">可卖持仓<select name="positionId">${matches.map(item =>
        `<option value="${html(item.positionId)}" ${item.positionId === current ? 'selected':''}>${decimalText(item.availableShare)} 份 · ${html(item.positionId)}</option>`).join('')}</select></label>` : empty('该账户在此选项暂无可卖持仓。');
      ticket.querySelector('[type=submit]').disabled = !matches.length;
    } catch (error) { if (activeView(version) && ticket === $('#ticket-form')) $('#sell-holdings').innerHTML = readError(error); }
  };
  on('book-refresh', updateBook);
  on('open-refresh', () => updateOpen());
  on('cycle-records-refresh', () => updateCycleRecords());
  await Promise.all([tradeable && updateBook(), showOrders && updateOpen(), tradeable && loadTicket(),
    cycle && ids && updateCycleRecords()]);
}

async function market(version) {
  if (state.selected) return tradePanel(version);
  const search = `<form id="market-search" class="form-inline"><input aria-label="市场名称或编号" name="query" value="${html(state.marketSearch)}" placeholder="市场名称、编号或 ForeGate 赛事链接"><button type="submit" class="primary">搜索</button></form>`;
  if (!setContent(version,panel('查找市场',search + '<div id="market-categories" class="within" aria-live="polite">'+empty('正在读取市场分类…')+'</div>') + '<div id="market-results"></div>')) return;
  on('market-search', form => {
    const query = String(formData(form).query || '').trim();
    const event = /^https:\/\/(?:www\.)?foregate\.com\/(?:[a-z]{2}\/)?event\/(\d{1,19})(?:[/?#]|$)/i.exec(query);
    const id = event?.[1] ?? query;
    if (idString(id)) {
      const windowId = event && new URL(query).searchParams.get('windowId');
      return openMarket(id, null, idString(windowId) ? {windowId} : {});
    }
    state.marketSearch = query; state.marketCategory = []; state.marketPage = 1;
    return go('market');
  });
  const loadList = async (title, params) => {
    $('#market-results').innerHTML = panel(title,'<div id="market-list">'+empty('正在读取市场…')+'</div>');
    try {
      const list = await api('markets',{...params,page:state.marketPage});
      if (!activeView(version)) return;
      $('#market-list').innerHTML = marketList(list);
      bindRows('open',list.rows,item => openMarket(item.marketId, null, item.cycle ? {cycle:true,windowId:item.windowId} : {}));
      bindPager(list,'markets',page => {state.marketPage = page;return go('market');});
    } catch (error) { if (activeView(version)) $('#market-list').innerHTML = readError(error); }
  };
  const loadCategories = async () => {
    try {
      if (!state.marketTree) state.marketTree = await api('market_categories');
      if (!activeView(version)) return;
      const path = [];
      const labels = ['一级分类','二级分类','三级分类'];
      let choices = state.marketTree;
      const controls = labels.map((label, level) => {
        const option = choices.find(item => item.id === state.marketCategory[level]);
        if (option) path.push(option);
        const markup = `<label class="field">${label}<select id="category-${level}" ${choices.length ? '' : 'disabled'}>
          <option value="">${choices.length ? (level ? '全部' : '选择一级分类') : '无下级分类'}</option>${choices.map(item =>
          `<option value="${html(item.id)}" ${item === option ? 'selected' : ''}>${html(item.label)}</option>`).join('')}</select></label>`;
        choices = option?.children ?? [];
        return markup;
      });
      state.marketCategory = path.map(option => option.id);
      $('#market-categories').innerHTML = `<div class="category-controls">${controls.join('')}</div>`;
      labels.forEach((_, level) => $('#category-'+level).addEventListener('change', event => {
        const id = event.target.value;
        state.marketCategory = state.marketCategory.slice(0, level);
        if (id) state.marketCategory.push(id);
        state.marketSearch = '';
        state.marketPage = 1;
        void go('market');
      }));
      if (path.length) {
        await loadList(path.map(option => option.label).join(' / '),{categoryId:path[path.length-1].id});
      }
    } catch (error) { if (activeView(version)) $('#market-categories').innerHTML = readError(error); }
  };
  await Promise.all([loadCategories(), state.marketSearch ? loadList('搜索结果',{search:state.marketSearch}) : null]);
}

async function positions(version, page = 1) {
  const data = await api('positions',{page});
  if (!activeView(version)) return;
  const markets = [...new Set(data.rows.map(item => item.marketId).filter(idString))];
  const details = new Map();
  for (let index = 0; index < markets.length; index+=6) {
    await Promise.all(markets.slice(index,index+6).map(async id => {
      try { details.set(id,await api('detail',{marketId:id})); }
      catch { /* Positions stay visible if market metadata is unavailable. */ }
    }));
    if (!activeView(version)) return;
  }
  if (!setContent(version,panel('API 账户持仓',table(['市场 / 选项','份额','均价','成本','操作'],data.rows.map((item,index) => {
    const detail = details.get(item.marketId);
    const group = detail?.outcomes?.find(entry => entry.outcomeId === item.outcomeId);
    const option = group?.options?.find(entry => entry.optionId === item.optionId);
    const claimable = detail && detail.status !== 'open' && !['CLAIMING','CLAIMED'].includes(item.orderStatus) &&
      (item.win === true || ['1','true','yes'].includes(String(option?.isWin).toLowerCase()));
    const tradeable = option && detail?.status === 'open' && detail.modelType === 'orderbook' && Number(item.availableShare) > 0;
    return [`<strong>${html(detail?.title ?? item.marketTitle ?? item.marketId)}</strong><div class="subtle">${html(option ? optionLabel({group,option}) : item.optionTitle ?? item.optionId)}</div>`,
      decimalText(item.quantity),decimalText(item.avgPrice),decimalText(item.cost ?? item.price),
      [tradeable ? at(index,'限价卖出','sell') : '',claimable ? at(index,'申领','claim') : '',
        idString(item.marketId) ? at(index,'市场','pmarket') : ''].filter(Boolean).join(' ')];
  })) + pager(data,'positions')))) return;
  bindPager(data,'positions',next => positions(version,next));
  bindRows('pmarket',data.rows,item => openMarket(item.marketId));
  bindRows('sell',data.rows,item => openMarket(item.marketId,item));
  bindRows('claim',data.rows,async item => {
    const result = await actionSubmit({kind:'claim',...outcomeFields(item)});
    if (activeView(version)) {await positions(version,page); toast(result.message,result.error);}
  });
}

function localOperations(rows) {
  return table(['操作','内容 / 订单号','状态','提交时间',''], rows.map((item,index) => [
    rowText(operationsLabels[item.kind] ?? item.kind),
    `<div>${html(item.label).replace(/\n/g,'<br>')}</div>${item.orderId ? '<div class="mono subtle">'+html(item.orderId)+'</div>' : ''}
    ${item.position_check ? '<div class="subtle">'+html(item.position_check)+'</div>' : ''}
    ${item.message ? '<div class="subtle">'+html(item.message)+'</div>' : ''}`,
    statusBadge(item.state),html(formatTime(item.created)),
    item.state === 'UNKNOWN' ? at(index,'人工核对','review') : item.cycle ? at(index,'打开窗口','inspect-order') :
      item.orderId ? at(index,'查订单','inspect-order') : ''
  ]));
}

function historyRows(data) {
  const states = {'-1':'失败','0':'已创建','1':'处理中','2':'成功'};
  return table(['类型','金额','状态','网络','时间','编号 / 交易哈希'],data.rows.map(item => [
    rowText(item.tranType ?? item.type ?? item.transactionType),
    decimalText(item.amount ?? item.tokenAmount),rowText(states[item.status] ?? item.status),
    rowText(item.chain ?? item.network),html(formatTime(item.createTime ?? item.createdAt ?? item.date)),
    `<span class="mono">${html(item.txHash ?? item.transactionHash ?? item.txId ?? item.orderId ?? item.id)}</span>`
  ]));
}

async function orders(version) {
  const tabs = {local:'本机提交', lookup:'按订单号查询', trades:'成交与申领', account_history:'资金流水'};
  const render = (title, body, trailing) => setContent(version, `<div class="tabs">${Object.entries(tabs).map(([key,label]) => btn(label,'orders-tab-'+key,state.orderTab === key ? 'selected' : '')).join('')}</div>
    <div class="within">${panel(title,body,trailing)}</div>`);
  const bindTabs = () => Object.keys(tabs).forEach(key => on('orders-tab-'+key, () => {state.orderTab = key; return go('orders');}));
  const showPage = async (page = 1) => {
    const tab = state.orderTab;
    const data = await api(tab,{page});
    if (!activeView(version)) return;
    render(tabs[tab],(tab === 'trades' ? activity(data) : historyRows(data))+pager(data,'orders'));
    bindTabs();
    bindPager(data,'orders',showPage);
  };
  if (['trades','account_history'].includes(state.orderTab)) {
    render(tabs[state.orderTab],'<div id="orders-view">'+empty('正在读取…')+'</div>');
    bindTabs();
    try { await showPage(); } catch (error) {if (activeView(version)) $('#orders-view').innerHTML = readError(error);}
    return;
  }
  if (state.orderTab === 'lookup') {
    render('查询平台订单',`<form id="order-query" class="form-inline"><input name="ids" aria-label="订单号" value="${html(state.queryOrder ?? '')}" placeholder="用空格或逗号分隔订单号" required><button class="primary">查询</button></form><div id="order-query-result" class="within"></div>`);
    bindTabs();
    const lookup = async ids => {
      const data = await api('query_orders',{ids});
      if (!activeView(version)) return;
      $('#order-query-result').innerHTML = table(['订单号','方向','类型','委托金额','限价','份额','平台当前份额','状态','更新'],data.rows.map(item => [
        rowText(item.orderId),statusBadge(item.direction),rowText(item.orderType),decimalText(item.amount),decimalText(item.limitPrice),
        decimalText(item.share),decimalText(item.currentShare),statusBadge(item.orderStatus),html(formatTime(item.updatedAt ?? item.createdAt))
      ]),'平台未返回对应订单');
    };
    on('order-query',form => lookup(formData(form).ids));
    if (state.queryOrder) {
      const id = state.queryOrder;
      state.queryOrder = '';
      try {await lookup(id);} catch (error) {if (activeView(version)) $('#order-query-result').innerHTML = readError(error);}
    }
    return;
  }
  const showLocal = data => {
    if (!activeView(version)) return;
    render('提交与核对记录',localOperations(data.operations),btn('查询最新订单状态','reconcile','compact'));
    bindTabs();
    on('reconcile', async () => showLocal(await api('reconcile')));
    bindRows('inspect-order',data.operations,item => {
      if (item.cycle) return openMarket(item.marketId, null, {cycle:true,windowId:item.outcomeId,optionId:item.optionId});
      state.queryOrder = item.orderId; state.orderTab = 'lookup'; return go('orders');
    });
    bindRows('review',data.operations,async item => {
      const result = await api('review_unknown',{id:item.id,label:item.label});
      if (activeView(version)) {showLocal(result);toast('已记录人工核对。');}
    });
  };
  showLocal(await api('status'));
}

function fundHistory(version, kind) {
  const action = kind === 'deposit' ? 'deposits' : 'withdrawals';
  const show = async (page = 1) => {
    try {
      const data = await api(action,{page});
      if (!activeView(version) || state.fundTab !== kind) return;
      $('#fund-history').innerHTML = historyRows(data) + pager(data,'fund');
      bindPager(data,'fund',show);
    } catch (error) {
      if (activeView(version) && state.fundTab === kind) $('#fund-history').innerHTML = readError(error);
    }
  };
  return show();
}

async function funds(version) {
  const deposit = state.fundTab === 'deposit';
  const nav = `<div class="tabs">${btn('充值','tab-deposit',deposit?'selected':'')}${btn('提现','tab-withdraw',deposit?'':'selected')}</div>`;
  const history = panel(deposit?'充值记录':'提现记录','<div id="fund-history">'+empty('正在读取…')+'</div>');
  if (deposit) {
    setContent(version,nav + '<div class="within">' + panel('USDT 充值地址',
      '<div id="address">'+empty('提取充值地址后，使用显示的网络从外部钱包转入 USDT。')+'</div>',
      btn('获取充值地址','read-address','primary')) + history+'</div>');
    const fillAddress = data => {
      if (!activeView(version)) return;
      $('#address').innerHTML = `<div class="subtle">网络 ${html(String(data.chain).toLowerCase() === 'sol' ? 'Solana' : data.chain)}</div>
        <div class="route-address mono">${html(data.walletAddress)}</div>${btn('复制地址','copy-address','compact')}`;
      on('copy-address', async () => {await rpc('copy_address',{address:data.walletAddress});toast('充值地址已复制。');});
    };
    if (state.fundsAddress) fillAddress(state.fundsAddress);
    on('read-address',async () => {
      const address = await api('deposit_address');
      if (activeView(version)) { state.fundsAddress = address; fillAddress(address); }
    });
  } else {
    setContent(version,nav+'<div class="within">'+panel('提现至 Solana 地址',
      '<div id="limits" class="subtle">正在读取单笔限额…</div><div id="mfa" class="within"></div><div id="withdraw-form-slot"></div>',
      btn('开启本次 MFA 验证','begin-mfa','primary')) +
      panel('账户验证方式','<div id="setup-info" class="subtle">正在读取验证方式…</div><div id="setup-slot" class="within"></div>',
        btn('绑定验证器','setup-totp','compact')) + history + '</div>');
    const methodLabels = {TOTP:'验证器验证码',EMAIL:'邮箱验证码',RECOVERY:'恢复码'};
    const renderFactors = data => {
      const label = value => value === true ? '已启用' : value === false ? '未启用' : '状态未知';
      const blocked = data.locked === true || data.forceReset === true;
      const needsSetup = data.totpEnabled === false && data.emailOtpEnabled === false &&
        data.recoveryCodesRemaining === 0;
      $('#setup-info').textContent = `验证器：${label(data.totpEnabled)}　邮箱验证码：${label(data.emailOtpEnabled)}。` +
        (blocked ? ' 平台已锁定验证或要求重置，请联系 ForeGate 处理此 API 账户。' :
          needsSetup ? ' 请先绑定验证器。需要开通邮箱验证码时，请联系 ForeGate 确认此 API 账户的邮箱。' : '');
      $('#setup-totp').hidden = blocked || data.totpEnabled !== false;
      $('#begin-mfa').disabled = blocked || needsSetup;
    };
    $('#setup-totp').hidden = true;
    $('#begin-mfa').disabled = true;
    const refreshFactors = async () => {
      try {
        const factors = await api('mfa_status');
        if (activeView(version)) renderFactors(factors);
      } catch (error) {
        if (activeView(version)) $('#setup-info').innerHTML = readError(error);
      }
    };
    const showWithdraw = () => {
      if (!activeView(version)) return;
      $('#withdraw-form-slot').innerHTML = state.mfaVerified ? '<hr class="divider"><form id="withdraw-form" class="forms">' +
        field('提现金额（USDT）','amount','','required inputmode="decimal"')+
        field('Solana 收款地址','toAddress','','required autocomplete="off"')+
        '<div class="full"><button class="primary">核对并提现</button></div></form>' : '';
      on('withdraw-form',async form => {
        const result = await actionSubmit({kind:'withdraw',...formData(form)});
        state.mfaVerified = false;
        state.mfaMethods = [];
        if (activeView(version)) {
          showMfa();
          void fundHistory(version,'withdraw');
          toast(result.message,result.error);
        }
      });
    };
    const showMfa = () => {
      if (!activeView(version)) return;
      if (state.mfaVerified) $('#mfa').innerHTML = '<p class="green">本次 MFA 验证已通过。</p>';
      else if (!state.mfaMethods.length) $('#mfa').innerHTML = empty('提现需要进行一次身份验证。');
      else {
        $('#mfa').innerHTML = `<form id="mfa-form" class="forms"><label class="field">验证方式<select name="method">${state.mfaMethods.map(method =>
          `<option value="${html(method)}">${html(methodLabels[method] ?? method)}</option>`).join('')}</select></label>
          ${field('验证码或恢复码','code','','required type="password" autocomplete="one-time-code"')}
          <div class="flex full"><button class="primary" type="submit">验证</button>
          ${state.mfaMethods.includes('EMAIL') ? btn('发送邮箱验证码','send-email','compact') : ''}</div></form>`;
        on('mfa-form',async form => {
          const result = await api('mfa_verify',formData(form));
          if (!activeView(version)) return;
          state.mfaVerified = result.verified;
          showMfa();
        });
        const form = $('#mfa-form');
        const sendEmail = $('#send-email');
        if (sendEmail) {
          const update = () => { sendEmail.hidden = form.elements.method.value !== 'EMAIL'; };
          form.elements.method.addEventListener('change', update);
          update();
          on('send-email', async () => {await api('mfa_email');if (activeView(version)) toast('平台已向此 API 账户的邮箱发送验证码。');});
        }
      }
      showWithdraw();
    };
    showMfa();
    on('begin-mfa', async () => {
      state.mfaVerified = false; state.mfaMethods = [];
      showMfa();
      const result = await api('mfa_begin');
      if (!activeView(version)) return;
      state.mfaMethods = result.methods.includes(result.recommended) ?
        [result.recommended, ...result.methods.filter(method => method !== result.recommended)] : result.methods;
      showMfa();
    });
    on('setup-totp',async () => {
      const setup = await api('mfa_setup');
      if (!activeView(version)) return;
      $('#setup-totp').hidden = true;
      $('#setup-slot').innerHTML = `<p>在验证器中为 ${html(setup.issuer)} 添加账户，手动输入密钥。</p><p class="mono route-address">${html(setup.key)}</p>
        <form id="totp-form" class="form-inline"><input name="code" aria-label="验证器的 6 位验证码" required pattern="[0-9]{6}" autocomplete="one-time-code" inputmode="numeric" placeholder="6 位验证码"><button class="primary">完成绑定</button></form>`;
      on('totp-form',async form => {
        const result = await api('mfa_setup_complete',formData(form));
        if (!activeView(version)) return;
        $('#setup-slot').innerHTML = '<p>请妥善保存本次显示的恢复码。</p>' + result.recoveryCodes.map(code=>`<div class="mono">${html(code)}</div>`).join('');
        $('#setup-info').textContent = '验证器已绑定';
        await refreshFactors();
      });
    });
    void api('withdraw_limit').then(data => {
      if (activeView(version)) $('#limits').textContent = `单笔限额 ${decimalText(data.singleWithdrawalMin)}–${decimalText(data.singleWithdrawalMax)} USDT`;
    }).catch(error=>{if (activeView(version)) $('#limits').innerHTML = readError(error);});
    void refreshFactors();
  }
  on('tab-deposit', () => {state.fundTab = 'deposit';state.mfaMethods=[];state.mfaVerified=false;return go('funds');});
  on('tab-withdraw', () => {state.fundTab = 'withdraw';state.mfaMethods=[];state.mfaVerified=false;return go('funds');});
  await fundHistory(version,state.fundTab);
}

function recentSignals(rows) {
  return table(['信号','赛事','选项','采集时间'],rows.map(({signal,seen}) => [
    rowText(signal.signal_id),
    html([signal.home,signal.away].filter(Boolean).join(' vs ') || signal.type),
    html(signal.legs?.map(leg => [leg.market_type,leg.selection,leg.side,leg.line].filter(Boolean).join(' ')).join(' / ') || signal.reason || signal.type),
    html(formatTime(seen))
  ]));
}

function followUpdates(status) {
  const mode = $('#follow-status');
  if (!mode) return;
  mode.innerHTML = statusBadge(status.armed ? '自动跟单已启用' : status.listening ? '仅监听' : '已暂停')+
    ' <span class="subtle">'+html(status.tg_status)+'</span>';
  $('#follow-error').textContent = status.error || '';
  const operations = new Map(status.operations.map(item=>[item.id,item]));
  $('#follow-log').innerHTML = table(['信号','收录时间','状态','订单 / 处理结果'],status.signals.map(item => {
    const op = operations.get(item.op_id);
    return [rowText(item.signal_id+' · '+item.leg),html(formatTime(item.seen)),statusBadge(item.state),
      [op?.orderId, item.note, op?.position_check].filter(Boolean).map(x=>`<div>${html(x)}</div>`).join('') || '—'];
  }),'等待启用后的新信号');
  $('#signal-log').innerHTML = recentSignals(status.recent_signals);
}

async function follow(version) {
  const status = await api('status');
  if (!activeView(version)) return;
  updateHeader(status);
  const cfg = status.config;
  const body = `<div class="flex"><div id="follow-status"></div><span class="subtle push">@${html(status.channel)} · 每 ${html(status.poll_seconds)} 秒检查</span></div><p class="amber within" id="follow-error"></p><hr class="divider">
  <form class="forms" id="follow-form"><label class="field">跟单下单方式<select name="order_type" ${status.armed?'disabled':''}>
    <option value="LIMIT" ${cfg.order_type === 'LIMIT'?'selected':''}>限价买入</option>
    <option value="MARKET" ${cfg.order_type === 'MARKET'?'selected':''}>市价买入</option></select></label>
  ${field('单笔上限（USDT）','amount',cfg.amount,'inputmode="decimal" required')}
  ${field('单赛事累计上限（USDT）','event',cfg.event,'inputmode="decimal" required')}
  ${field('当日累计上限（USDT）','daily',cfg.daily,'inputmode="decimal" required')}
  ${field('最高买价（美分/份）','max_price',cfg.max_price,'inputmode="decimal" required')}
  ${field('等待撤单（秒，0 为保留）','ttl',cfg.ttl,'inputmode="numeric" required')}
  <p id="follow-mode-note" class="subtle full"></p>
  <div class="full flex"><button ${status.armed?'disabled':''}>保存参数</button>
  ${status.listening || status.armed ? btn('暂停','pause-follow','danger') : btn('仅监听信号','listen-follow')}
  ${status.armed ? '' : btn('启用实盘跟单','arm-follow','primary')}</div></form>
  <p class="subtle within">启用后处理新信号，并继续核对已提交的委托。</p>
  <p class="subtle">当前信号筛选　${html(status.risk.max_signal_age_sec)} 秒内 · 收益率 ${html(status.risk.min_profit_pct)}–${html(status.risk.max_profit_pct)}% · ${status.risk.follow_high_risk?'包含':'过滤'}高风险信号</p>`;
  setContent(version,panel('Telegram 跟单设置',body)+panel('API 跟单记录','<div id="follow-log"></div>')+panel('最近采集的信号','<div id="signal-log"></div>'));
  followUpdates(status);
  const settings = $('#follow-form').elements;
  const updateMode = () => {
    const market = settings.order_type.value === 'MARKET';
    for (const name of ['max_price', 'ttl']) {
      settings[name].closest('label').hidden = market;
      settings[name].disabled = market;
    }
    $('#follow-mode-note').textContent = market ? '按 USDT 预算提交市价买入，实际成交价由平台撮合决定，无最高买价。' :
      '以可用最优卖价挂限价单。超过最高买价时跳过，可设置超时撤销未成交部分。';
  };
  settings.order_type.addEventListener('change', updateMode);
  updateMode();
  on('follow-form',async form => {
    await api('config',formData(form));
    if (activeView(version)) {await go('follow');toast('跟单参数已保存。');}
  });
  on('pause-follow',async () => {await api('follow_pause');if (activeView(version)) await go('follow');});
  on('listen-follow',async () => {await api('follow_listen');if (activeView(version)) await go('follow');});
  on('arm-follow', async () => {
    const values = formData($('#follow-form'));
    if (Object.keys(values).some(key => String(cfg[key]) !== String(values[key]))) throw new Error('请先保存当前跟单参数。');
    const summary = 'ForeGate API 账户 '+status.account_id+' · @'+status.channel+
      '\n单笔最多 '+cfg.amount+' USDT，单赛事 '+cfg.event+' USDT，当日 '+cfg.daily+' USDT。\n'+
      (cfg.order_type === 'MARKET' ? '市价买入 · 按金额下单，无最高买价。' : '限价买入 · 最高买价 '+cfg.max_price+'¢。'+
      (cfg.ttl ? '未成交部分在 '+cfg.ttl+' 秒后尝试撤单。' : '挂单保留，由你撤单。'));
    await api('follow_arm',{summary,config:cfg,channel:status.channel});
    if (activeView(version)) await go('follow');
  });
}

async function connection(version) {
  const data = await api('settings');
  if (!activeView(version)) return;
  const config = data.settings;
  const foregate = panel('ForeGate API 账户',`<p>UID <span id="bound-id" class="mono">${html(state.status?.account_id)}</span></p>
    <p class="subtle" id="account-result"></p>`,`<div class="flex">${btn('管理账户','manage-accounts')}${btn('验证账户','connect-account','primary')}</div>`);
  const telegram = panel('Telegram',`<p class="subtle" id="tg-status">${html(data.tg_status)}</p>
    <form id="telegram-settings" class="forms">${field('频道用户名','channel',config.channel,'required')}
    ${field('Telegram API ID','api_id',config.api_id,'inputmode="numeric"')}
    ${field('手机号（含国家码）','phone',config.phone,'type="tel" autocomplete="off"')}
    ${field(config.api_hash_set?'API Hash 已保存在' + credentialStorage:'Telegram API Hash','api_hash','','type="password" autocomplete="new-password"')}
    <label class="full subtle"><input type="checkbox" name="risk_enabled" ${config.risk_enabled?'checked':''}> 允许启用实盘跟单</label>
    <div class="full flex"><button class="primary">保存</button>${btn('获取登录验证码','tg-send')}</div></form>
    <div id="tg-login-slot" class="within"></div>`);
  setContent(version,foregate+telegram);
  on('manage-accounts', () => go('accounts'));
  on('connect-account',async () => {
    const value = await connect();
    if (activeView(version)) $('#account-result').textContent = '验证通过 · 可用余额 ' + decimalText(value.assets.totalAvailableAmount ?? value.assets.availableAmount) + ' USDT';
  });
  on('telegram-settings',async form => {
    const data = formData(form);
    data.risk_enabled = Boolean(data.risk_enabled);
    data.channel = data.channel.replace(/^@/,'').trim();
    await rpc('save_settings',data);
    if (activeView(version)) {await go('connection');toast('Telegram 设置已保存。');}
  });
  const loginResult = result => {
    if (!activeView(version)) return;
    $('#tg-status').textContent = result.state?.tg_status ?? result.tg_status;
    const slot = $('#tg-login-slot');
    const password = result.next === 'password';
    slot.innerHTML = result.next ? `<form class="form-inline" id="tg-login"><input required name="${password?'password':'code'}"
      type="${password?'password':'text'}" aria-label="${password?'两步验证密码':'验证码'}" placeholder="${password?'两步验证密码':'Telegram 验证码'}" autocomplete="off">
      <button class="primary">登录</button></form>` : '';
    on('tg-login',async form => loginResult(await rpc('tg_sign_in',formData(form))));
  };
  on('tg-send',async () => loginResult(await rpc('tg_send_code')));
}

const pages = {accounts,overview,market,positions,orders,funds,follow,connection};
if (window.desktop) $('#client-version').textContent = window.desktop.clientVersion;
document.querySelectorAll('[data-page]').forEach(node => {
  node.addEventListener('click', () => { if (!state.accountBusy) void go(node.dataset.page); });
});
on('guide-open', openGuide);
on('guide-skip', dismissGuide);
on('guide-back', () => { if (guideIndex > 0) guideIndex--; renderGuide(); });
on('guide-next', () => {
  if (guideIndex === guideSteps.length - 1) return dismissGuide();
  guideIndex++;
  renderGuide();
});
$('#guide').addEventListener('cancel', event => { event.preventDefault(); void dismissGuide(); });
on('account-picker', () => { if (!state.accountBusy) return go('accounts'); });
on('refresh', async () => {
  if (state.accountBusy) return;
  if (state.page === 'market' && state.selected?.detail.cycle) {
    const selected = state.selected;
    const version = state.version;
    const detail = await api('detail',{cycle:true,marketId:selected.detail.marketId,windowId:selected.detail.windowId});
    if (!activeView(version)) return;
    const options = detail.outcomes.flatMap(group => group.options.map(option => ({group,option})));
    const current = options.find(entry => entry.option.optionId === selected.option?.optionId) ?? options[0];
    state.selected = {detail,options,group:current?.group,option:current?.option};
  }
  if (state.page === 'market' && !state.selected) state.marketTree = null;
  return go(state.page);
});
let polling = false;
setInterval(async () => {
  if (polling || state.accountBusy) return;
  polling = true;
  try {
    const status = await api('status');
    const previous = state.status;
    if (state.accountBusy || status.account_context !== previous?.account_context) return;
    updateHeader(status);
    if (state.page === 'follow') {
      if ($('#follow-status') && previous && (previous.armed !== status.armed || previous.listening !== status.listening)) {
        await go('follow');
      } else followUpdates(status);
    }
  } catch { /* Current page surfaces read and connection failures. */ }
  finally {polling = false;}
},5000);
void go('accounts');
