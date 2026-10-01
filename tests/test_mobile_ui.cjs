const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const root = path.resolve(__dirname, '..');

function classList(initial = []) {
  const values = new Set(initial);
  return {
    add: (...items) => items.forEach(item => values.add(item)),
    contains: item => values.has(item),
    toggle: (item, force) => {
      if (force === false) values.delete(item);
      else if (force === true) values.add(item);
      else if (values.has(item)) values.delete(item);
      else values.add(item);
      return values.has(item);
    },
  };
}

function cell(textContent, { colSpan = 1, rowSpan = 1 } = {}) {
  return {
    textContent,
    colSpan,
    rowSpan,
    dataset: {},
    removeAttribute(name) {
      if (name === 'data-label') delete this.dataset.label;
    },
  };
}

test('mobile tables receive readable labels and empty rows stay unlabeled', () => {
  const table = {
    classList: classList(),
    tHead: {
      rows: [
        { cells: [cell('기본 정보', { colSpan: 2 })] },
        { cells: [cell('순위 ↕'), cell('종목')] },
      ],
    },
    tBodies: [{
      rows: [
        { classList: classList(), cells: [cell('1'), cell('미래산업')] },
        { classList: classList(), cells: [cell('자료 없음', { colSpan: 2 })] },
      ],
    }],
  };
  const shell = {};
  let observed = null;

  class MutationObserver {
    constructor(callback) { this.callback = callback; }
    observe(target, options) { observed = { target, options }; }
  }

  const context = vm.createContext({
    document: {
      readyState: 'complete',
      querySelector: selector => selector === '.shell' ? shell : null,
      querySelectorAll: selector => selector === '.tablewrap table, .issue-tablewrap table' ? [table] : [],
    },
    MutationObserver,
    requestAnimationFrame: callback => callback(),
  });
  vm.runInContext(fs.readFileSync(path.join(root, 'mobile.js'), 'utf8'), context);

  assert.equal(table.classList.contains('mobile-card-table'), true);
  assert.equal(table.tBodies[0].rows[0].cells[0].dataset.label, '기본 정보 · 순위');
  assert.equal(table.tBodies[0].rows[0].cells[1].dataset.label, '기본 정보 · 종목');
  assert.equal(table.tBodies[0].rows[1].classList.contains('mobile-empty-row'), true);
  assert.equal(table.tBodies[0].rows[1].cells[0].dataset.label, undefined);
  assert.equal(observed.target, shell);
  assert.equal(observed.options.childList, true);
  assert.equal(observed.options.subtree, true);
});

test('main page loads the mobile layout assets after feature styles', () => {
  const html = fs.readFileSync(path.join(root, 'index.html'), 'utf8');
  assert.ok(html.indexOf('issue-spread.css') < html.indexOf('mobile.css'));
  assert.ok(html.indexOf('issue-spread.js') < html.indexOf('mobile.js'));
});
