// Locate only the detail card that owns both the named market and its My Position tab.
({group, mode}) => {
  const clean = value => (value || '').replace(/\s+/g, ' ').trim();
  const text = el => clean(el.innerText);
  const visible = el => el.getClientRects().length > 0;
  const within = (root, selector) => Array.from(root.querySelectorAll(selector)).filter(visible);
  const market = /^(Moneyline|Spreads|Totals|Map Handicap|Game Handicap|Total Maps|Total Games|Both Teams to Score\??|(?:Map|Game) \d+ Winner)$/i;
  const heading = el => text(el).replace(/\s*✨?\s*AI$/i, '').trim();
  const cards = [];
  const controls = 'button,[role="button"],[role="tab"],summary';
  for (const tab of within(document, controls).filter(el => /^My Position(?:\s*\d+)?$/i.test(text(el)))) {
    let card = tab.parentElement;
    while (card && card !== document.body) {
      const headers = within(card, 'p,h2,h3,h4').map(heading).filter(h => market.test(h));
      if (headers.length) {
        if (headers.length === 1 && headers[0].toLowerCase() === group.toLowerCase()) cards.push({tab, card});
        break;
      }
      card = card.parentElement;
    }
  }
  if (cards.length !== 1) return null;
  const {tab, card} = cards[0];
  if (mode === 'tab') return tab;
  if (tab.getAttribute('aria-selected') === 'false' || within(card, '[aria-busy="true"],[role="progressbar"]').length) return null;
  const contents = clean(card.innerText);
  const headings = within(card, 'th,[role="columnheader"],span,div,p').filter(el =>
    text(el) === 'Outcome' && !Array.from(el.children).some(child => text(child) === 'Outcome'));
  if (mode === 'expand') {
    if (headings.length) return null;
    const buttons = within(card, controls).filter(el => {
      const name = text(el).replace(/^[▾▸▼▶]\s*/, '').replace(/\s*[·•]\s*\d+$/, '');
      return name.toLowerCase() === group.toLowerCase() && el.getAttribute('aria-expanded') !== 'true';
    });
    return buttons.length === 1 ? buttons[0] : null;
  }
  if (headings.length === 0 && /\b(?:no (?:open |current )?positions?(?: yet)?|you (?:have|hold) no positions?)\b/i.test(contents))
    return {empty:true, rows:[]};
  if (headings.length !== 1) return null;
  // Support semantic tables and the detail page's visual grid. Use the Shares column by name.
  let header = headings[0].parentElement;
  while (header && header !== card) {
    const columns = Array.from(header.children).map(text);
    if (columns[0] === 'Outcome' && columns.includes('Avg Price') && columns.includes('Shares')) {
      const index = columns.indexOf('Shares');
      const rows = [];
      for (const row of within(card, 'tr,[role="row"],div,li')) {
        const cells = Array.from(row.children).map(text);
        if (cells.length > index && cells[0] && /^\d+(?:\.\d+)?¢$/.test(cells[columns.indexOf('Avg Price')]) && /^\d+(?:\.\d+)?$/.test(cells[index]))
          rows.push({outcome:cells[0], shares:cells[index]});
      }
      return rows.length ? {rows} : null;
    }
    header = header.parentElement;
  }
  return null;
}
