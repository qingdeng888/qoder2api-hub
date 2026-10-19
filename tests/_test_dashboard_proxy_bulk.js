/* 代理池多选批量操作（前端接线）：
 * 从 dashboard.html 抽真实脚本块，用元素桩验证「全选 / 单选 / 已选 ID / 批量按钮启用态」。
 *
 *   node tests/_test_dashboard_proxy_bulk.js
 */
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const htmlPath = path.join(__dirname, '..', 'dashboard.html');
const html = fs.readFileSync(htmlPath, 'utf8');
console.log('SUITE: ' + path.basename(__filename) + ' md5='
  + crypto.createHash('md5').update(fs.readFileSync(__filename)).digest('hex'));
console.log('FILE: dashboard.html md5='
  + crypto.createHash('md5').update(html).digest('hex')
  + ' bytes=' + Buffer.byteLength(html));

let PASS = 0, FAIL = 0;
const check = (label, ok, detail) => {
  if (ok) { PASS++; console.log('  [PASS] ' + label); }
  else { FAIL++; console.log('  [FAIL] ' + label + '  ' + JSON.stringify(detail)); }
};

const blocks = [...html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)].map(m => m[1]);
const code = blocks.join('\n');
const start = code.indexOf('let PROXY_POOL = null;');
const end = code.indexOf('/* ---- 页面区块导航');
const block = (start >= 0 && end > start) ? code.slice(start, end) : '';
check('能抽取代理池脚本块', block.length > 3000, block.length);

// ---- 元素桩：只覆盖多选逻辑用到的接口 ----
const checks = ['a', 'b', 'c'].map(id => ({
  id, checked: false, dataset: { id }, getAttribute(name) {
    return name === 'data-id' ? this.id : '';
  },
}));
const bulkButtons = [{ disabled: true }, { disabled: true }];
const master = { checked: false, indeterminate: false };
const count = { textContent: '' };
const accountBox = { innerHTML: '', className: '' };
const accountState = { textContent: '', style: {} };
const byId = {
  ppSelectAll: master, ppSelCount: count,
  accountProxyList: accountBox, setAccountProxyState: accountState,
};
global.document = {
  getElementById: id => byId[id] || null,
  querySelectorAll: selector => {
    if (selector === '#ppList input.pp-row-check') return checks;
    if (selector === '[data-pp-bulk]') return bulkButtons;
    return [];
  },
};

let api = null;
try {
  const esc = value => String(value ?? '').replace(/[&<>"]/g,
    ch => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[ch]));
  api = new Function('esc', block + `
    return {
      selectAllProxies, selectProxyCheckbox, selectedProxyIds,
      updateProxySelection, renderAccountProxyBindings,
      syncAccountProxyMode, ACCOUNT_PROXY_MODES,
      selected: () => Array.from(PROXY_SELECTED),
    };`)(esc);
} catch (err) {
  api = null;
  check('脚本块可执行', false, String(err));
}
check('多选函数可提取', api !== null);

if (api) {
  api.selectAllProxies(true);
  check('全选：3 个复选框全部勾选',
    checks.every(item => item.checked === true), checks.map(i => i.checked));
  check('全选：selectedProxyIds 返回 3 个',
    api.selectedProxyIds().length === 3, api.selectedProxyIds());
  check('全选：表头全选框为选中态', master.checked === true, master);
  check('全选：计数显示 3', count.textContent === '3', count.textContent);
  check('全选：批量按钮解除禁用',
    bulkButtons.every(btn => btn.disabled === false), bulkButtons);

  checks[0].checked = false;
  api.selectProxyCheckbox(checks[0]);
  check('取消单个：已选剩 2 个',
    api.selectedProxyIds().length === 2, api.selectedProxyIds());
  check('取消单个：计数显示 2', count.textContent === '2', count.textContent);
  check('取消单个：全选框进入 indeterminate',
    master.checked === false && master.indeterminate === true, master);

  api.selectAllProxies(false);
  check('清空：已选 0 个且按钮全部禁用',
    api.selectedProxyIds().length === 0
    && bulkButtons.every(btn => btn.disabled === true),
    { selected: api.selectedProxyIds(), buttons: bulkButtons });

  check('账号出口模式共 5 种（默认/auto/bind/rotate/direct）',
    api.ACCOUNT_PROXY_MODES.length === 5,
    api.ACCOUNT_PROXY_MODES.map(item => item.value));
  api.renderAccountProxyBindings([
    {uid: 'uid-1', nickname: '主号', realm: 'cn',
     proxyMode: 'bind', proxyId: 'p1'},
    {uid: 'uid-2', nickname: '备用', realm: 'intl',
     proxyMode: '', proxyId: ''},
  ], {proxies: [
    {id: 'p1', label: 'HK-01', address: 'http://***@1.2.3.4:8080', enabled: true},
    {id: 'p2', label: 'JP-02', address: 'socks5://***@5.6.7.8:1080', enabled: false},
  ]});
  check('账号绑定表渲染两行', (accountBox.innerHTML.match(/data-uid=/g) || []).length === 2);
  check('绑定模式回显 bind 且代理 p1 选中',
    /value="bind" selected/.test(accountBox.innerHTML)
    && /value="p1" selected/.test(accountBox.innerHTML));
  check('代理下拉含脱敏地址与停用标记',
    accountBox.innerHTML.includes('HK-01')
    && accountBox.innerHTML.includes('socks5://***@5.6.7.8:1080')
    && accountBox.innerHTML.includes('（已停用）'));
  check('账号数量状态显示 2 个', accountState.textContent.includes('2 个账号'),
    accountState.textContent);

  const proxySelect = {
    disabled: true, value: '',
    options: [{value: ''}, {value: 'p1'}],
  };
  const modeSelect = {
    value: 'bind',
    closest: () => ({querySelector: () => proxySelect}),
  };
  api.syncAccountProxyMode(modeSelect);
  check('切到 bind：代理下拉启用并自动选中第一个',
    proxySelect.disabled === false && proxySelect.value === 'p1', proxySelect);
  modeSelect.value = 'auto';
  api.syncAccountProxyMode(modeSelect);
  check('切到 auto：代理下拉禁用', proxySelect.disabled === true, proxySelect);
}

console.log('');
console.log('SUMMARY: TOTAL ' + (PASS + FAIL) + ' checks, ' + PASS
  + ' passed, ' + FAIL + ' failed');
console.log('RESULT: ' + (FAIL ? 'RED' : 'GREEN') + ' (exit ' + (FAIL ? 1 : 0) + ')');
process.exit(FAIL ? 1 : 0);
