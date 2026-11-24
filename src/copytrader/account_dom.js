() => {
 const visible=n=>n.getClientRects().length>0,clean=s=>(s||'').replace(/\s+/g,' ').trim();
 const tables=Array.from(document.querySelectorAll('table')).map((t,index)=>({t,index})).filter(({t})=>visible(t)).map(({t,index})=>({index,headers:Array.from(t.querySelectorAll('thead th')).map(n=>clean(n.innerText)),rows:Array.from(t.querySelectorAll('tbody tr')).filter(visible).map((tr,row_index)=>({row_index,cells:Array.from(tr.querySelectorAll('td')).map(n=>clean(n.innerText)),order_id:tr.getAttribute('data-order-id')||'',links:Array.from(tr.querySelectorAll('a[href]')).map(a=>({text:clean(a.innerText),href:a.getAttribute('href')})),buttons:Array.from(tr.querySelectorAll('button')).filter(visible).map(b=>({text:clean(b.innerText),disabled:b.disabled}))}))}));
 const fields=Array.from(document.querySelectorAll('input')).map((n,index)=>({n,index})).filter(({n})=>visible(n)&&!['password','email','hidden'].includes(n.type)&&(n.placeholder==='0.00'||n.type==='number'||['decimal','numeric'].includes(n.inputMode))).map(({n,index})=>{
  let context='',p=n.parentElement;
  for(let i=0;p&&i<5;i++,p=p.parentElement){if(p.querySelectorAll('input').length!==1)break;const t=clean(p.innerText);if(/Price|Amount|Shares|Quantity/i.test(t)&&t.length<180){context=t;break}}
  const label=n.labels?.length?Array.from(n.labels).map(x=>clean(x.innerText)).join(' '):n.getAttribute('aria-label')||'';
  return {index,label,context,placeholder:n.placeholder,value:n.value,min:n.min,max:n.max,step:n.step};
 });
 const balance=Array.from(document.querySelectorAll('button')).filter(visible).map(n=>clean(n.innerText)).filter(t=>/^Available\b/.test(t));
 // ForeGate appends a blank, single-cell pagination sentinel to Activity.
 // Actual loading skeletons have one blank cell per column. A footer must
 // not hide already-loaded activity, but complete skeleton rows still block.
 const loading=Array.from(document.querySelectorAll('[aria-busy="true"],[role="progressbar"]')).some(visible)||tables.some(t=>t.headers.length>1&&t.rows.some(r=>r.cells.length===t.headers.length&&r.cells.every(c=>!c)));
 return {url:location.origin+location.pathname,title:document.title,tables,fields,balance,loading,tabs:Array.from(document.querySelectorAll('[role="tab"],button')).filter(visible).map(n=>clean(n.innerText)).filter(t=>['Activity','Open Orders','Positions','My Positions','History','Order History','Closed Positions'].includes(t))};
}
