// Regression fixtures for the visible Activity table and pagination sentinel.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync(require('node:path').join(__dirname, '../src/copytrader/account_dom.js'), 'utf8');
const node = (text = '', selectors = {}) => ({
  innerText: text, getClientRects: () => [{}], getAttribute: () => null,
  querySelectorAll: s => selectors[s] || []
});
function read(rows, busy = false) {
  const table = node('', {
    'thead th': ['Market', 'Side', 'Price'].map(s => node(s)),
    'tbody tr': rows.map(cells => node('', {td: cells.map(s => node(s))}))
  });
  const document = { title: 'Portfolio', querySelectorAll: s => ({
    table: [table], button: [node('Available $21.029')],
    '[aria-busy="true"],[role="progressbar"]': busy ? [node()] : []
  }[s] || []) };
  return vm.runInNewContext('(' + source + ')()', {
    document, location: {origin: 'https://www.foregate.com', pathname: '/en/portfolio'}
  });
}
assert.equal(read([['Match Winner TS8', 'Buy', '79.21¢'], ['']]).loading, false);
assert.equal(read([['', '', '']]).loading, true);
assert.equal(read([['Match Winner TS8', 'Buy', '79.21¢']], true).loading, true);
assert.equal(read([['No orders']]).loading, false);
console.log('4 account DOM regressions passed');
