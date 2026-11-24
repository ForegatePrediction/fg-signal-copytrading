() => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const heading = /^(Moneyline|Spreads|Totals|Map Handicap|Game Handicap|Total Maps|Total Games|Both Teams to Score\??|(?:Map|Game) \d+ Winner)(?:\s|$)/i;
  const headers=Array.from(document.querySelectorAll('p')).filter(n=>n.getClientRects().length&&heading.test(clean(n.innerText)));
  const arrows=Array.from(document.querySelectorAll('[aria-label="next line"]'));
  const groups=headers.map(h=>{
    let root=h.parentElement;
    for(let i=0;root&&i<5;i++,root=root.parentElement){
      const localHeaders=Array.from(root.querySelectorAll('p')).filter(p=>heading.test(clean(p.innerText)));
      if(localHeaders.length>1)break;
      const next=root.querySelector('[aria-label="next line"]');
      if(next)return {group:clean(h.innerText).replace(/\s*✨\s*AI\s*$/,''),next_index:arrows.indexOf(next),can_next:!next.disabled};
    }
    return {group:clean(h.innerText).replace(/\s*✨\s*AI\s*$/,''),next_index:-1,can_next:false};
  });
  const allButtons=Array.from(document.querySelectorAll('[data-outcome-button]'));
  const buttons = allButtons.filter(n => n.getClientRects().length);
  const outcomes = buttons.map((n, index) => {
    let group = n.parentElement, label = '';
    for (let i = 0; group && i < 5; i++, group = group.parentElement) {
      const headers = Array.from(group.querySelectorAll('p')).filter(p => heading.test(clean(p.innerText)));
      if (headers.length === 1) { label = clean(headers[0].innerText).replace(/\s*✨\s*AI\s*$/, ''); break; }
      if (headers.length > 1) break;
    }
    return {index:allButtons.indexOf(n), group:label, text:clean(n.innerText), selected:n.getAttribute('aria-pressed') === 'true'};
  });
  const books = Array.from(document.querySelectorAll('[aria-label="Orderbook"]')).filter(n => n.getClientRects().length);
  const book = {count:books.length, asks:[], bids:[]};
  if (books.length === 1) {
    for (const [side, field] of [['ask','asks'], ['bid','bids']]) {
      const region = books[0].querySelector(`[data-orderbook-scroll-side="${side}"]`);
      if (region) book[field] = Array.from(region.querySelectorAll('[data-orderbook-row-key]')).map(n => ({key:n.getAttribute('data-orderbook-row-key'), text:clean(n.innerText)}));
    }
  }
  return {url:location.origin + location.pathname, title:document.title, outcomes, book, groups};
}
