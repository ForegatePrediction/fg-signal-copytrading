() => {
  const visible=n=>n.getClientRects().length>0;
  const clean=s=>(s||'').replace(/\s+/g,' ').trim();
  // Only visible trade controls: never read cookies, storage or login field values.
  const roots=[];
  for(const field of Array.from(document.querySelectorAll('input[placeholder="0.00"],input[type="number"],input[inputmode="decimal"],input[inputmode="numeric"]')).filter(visible)){
    let root=field.parentElement;
    for(let i=0;root&&i<12;i++,root=root.parentElement){
      if(Array.from(root.querySelectorAll('[role="tab"]')).some(t=>clean(t.innerText)==='Buy')&&/Amount|Shares|Quantity/.test(root.innerText)){roots.push(root);break}
    }
  }
  const panels=Array.from(new Set(roots));
  const fields=panels.flatMap(p=>Array.from(p.querySelectorAll('input'))).filter(n=>visible(n)&&
    (n.type==='number'||n.placeholder==='0.00'||['decimal','numeric'].includes(n.inputMode))).map(n=>({
      label:clean(n.getAttribute('aria-label')||n.placeholder||n.name||n.id),
      value:n.value,min:n.min,max:n.max,step:n.step,type:n.type,inputmode:n.inputMode}));
  const controls=Array.from(document.querySelectorAll('button,[role="tab"]')).filter(visible)
    .map(n=>({text:clean(n.innerText||n.getAttribute('aria-label')),disabled:!!n.disabled,selected:n.getAttribute('aria-selected')==='true'}))
    .filter(n=>/^(Buy|Sell|Trade|Market|Limit|Amount|Shares|Open Orders|Order History|Positions|Activity|买入|卖出|交易|市价|限价|订单|持仓)/i.test(n.text)).slice(0,35);
  const feedback=Array.from(document.querySelectorAll('[aria-label="Trade feedback notifications"]')).filter(visible).map(n=>clean(n.innerText)).filter(Boolean);
  const dialogs=Array.from(document.querySelectorAll('[role="dialog"],[role="alertdialog"]')).filter(visible).map(n=>clean(n.innerText).slice(0,6000));
  const tables=Array.from(document.querySelectorAll('table,[role="table"]')).filter(visible).map(n=>clean(n.innerText)).filter(t=>/Order|Filled|Status|订单|成交|状态/i.test(t)).map(t=>t.slice(0,6000)).slice(0,3);
  // Orders may use cards rather than semantic tables. Preserve visible text as
  // explicitly unparsed evidence; never infer FILLED from a balance or a toast.
  const lines=[];
  const walker=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);
  while(walker.nextNode()){
    const n=walker.currentNode,p=n.parentElement,t=clean(n.textContent);
    if(p&&visible(p)&&t&&t.length<1000&&!p.closest('[aria-label="Orderbook"],nav,header,footer,script,style'))lines.push(t);
  }
  const anchors=lines.map((t,i)=>/^(Order\s*(ID|No\.?|Number|#)|Filled|Status|Filled Shares|Avg\.? Price|Average Price|Cost|Total Cost|Size|订单号|成交份额|成交数量|状态|成本)(?:\s|:|：|$)/i.test(t)?i:-1).filter(i=>i>=0);
  if(anchors.length){const indexes=new Set();for(const i of anchors)for(let j=Math.max(0,i-2);j<Math.min(lines.length,i+5);j++)indexes.add(j);tables.unshift('订单字段附近原文（未解析）：\n'+Array.from(indexes).sort((a,b)=>a-b).map(i=>lines[i]).join('\n').slice(0,5000))}
  const visibleText=lines.join('\n').slice(0,16000);
  const orderLinks=Array.from(document.querySelectorAll('a[href]')).filter(visible)
    .filter(a=>/order|position|portfolio|history|订单|持仓|交易记录/i.test(a.innerText))
    .map(a=>({text:clean(a.innerText),url:a.href})).filter(a=>a.url.startsWith('https://www.foregate.com/')).slice(0,20);
  tables.push('页面可见原文（未解析，不等同于订单状态）：\n'+visibleText+(orderLinks.length?'\n可见记录入口：\n'+orderLinks.map(a=>a.text+' '+a.url).join('\n'):''));
  const eventLinks=Array.from(document.querySelectorAll('a[href]')).filter(visible)
    .map(a=>({text:clean(a.innerText),url:new URL(a.getAttribute('href'),location.href)}))
    .filter(a=>a.text&&a.url.origin==='https://www.foregate.com'&&/^\/en\/event\/\d+$/.test(a.url.pathname))
    .map(a=>({text:a.text,url:a.url.origin+a.url.pathname}));
  const uniqueEvents=Array.from(new Map(eventLinks.map(a=>[a.url,a])).values()).slice(0,20);
  if(uniqueEvents.length)tables.push('当前页赛事入口（只读，是否可交易需进入赛事核对）：\n'+uniqueEvents.map(a=>a.text+' '+a.url).join('\n'));
  return {url:location.origin+location.pathname,title:document.title,fields,controls,feedback,dialogs,tables,panel_text:panels.map(p=>p.innerText.slice(0,8000))};
}
