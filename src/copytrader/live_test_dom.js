() => {
 const visible=n=>n.getClientRects().length>0;
 const clean=s=>(s||'').replace(/\s+/g,' ').trim();
 const tables=Array.from(document.querySelectorAll('table')).filter(visible).filter(t=>Array.from(t.querySelectorAll('thead th')).map(n=>clean(n.innerText)).join('|')==='Market|Market Type|Type|Shares|Date|Price|Value/Fees|Tx');
 return {url:location.origin+location.pathname,table_count:tables.length,headers:tables.length===1?Array.from(tables[0].querySelectorAll('thead th')).map(n=>clean(n.innerText)):[],rows:tables.length===1?Array.from(tables[0].querySelectorAll('tbody tr')).map(tr=>({cells:Array.from(tr.querySelectorAll('td')).map(n=>clean(n.innerText)),links:Array.from(tr.querySelectorAll('a[href]')).map(a=>({text:clean(a.innerText),href:a.getAttribute('href')}))})):[]};
}
