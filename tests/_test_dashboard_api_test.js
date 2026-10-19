/* API 连通性测试面板（前端接线）：抽真实脚本块，验证模型下拉、结果渲染与提交参数。
 *
 *   node tests/_test_dashboard_api_test.js
 */
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const html = fs.readFileSync(path.join(__dirname, '..', 'dashboard.html'), 'utf8');
console.log('SUITE: ' + path.basename(__filename) + ' md5='
  + crypto.createHash('md5').update(fs.readFileSync(__filename)).digest('hex'));
console.log('FILE: dashboard.html md5='
  + crypto.createHash('md5').update(html).digest('hex'));

let PASS = 0, FAIL = 0;
const check = (label, ok, detail) => {
  if (ok) { PASS++; console.log('  [PASS] ' + label); }
  else { FAIL++; console.log('  [FAIL] ' + label + '  ' + JSON.stringify(detail)); }
};

const blocks = [...html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)].map(m => m[1]);
const code = blocks.join('\n');
const start = code.indexOf('function renderApiTestModels(){');
const end = code.indexOf("let CURRENT_GROWTH_UID = 'all';", start);
const block = (start >= 0 && end > start) ? code.slice(start, end) : '';
check('能抽取 API 连通性测试脚本块', block.length > 2000, block.length);

const esc = value => String(value ?? '').replace(/[&<>"]/g,
  ch => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[ch]));
const els = {
  apiTestModel: {innerHTML: '', value: ''},
  apiTestModelHint: {textContent: ''},
  apiTestState: {textContent: ''},
  apiTestMessage: {value: '你是谁，出来干活了'},
  apiTestResult: {innerHTML: ''},
};
global.document = {getElementById: id => els[id] || null};
global.window = {DIAG_DEFAULT_MODEL: 'Qwen3.8-Flash'};
const toasts = [];
const toast = (msg, kind) => toasts.push([msg, kind]);
let posted = null;
const postJSON = async (url, body) => {
  posted = {url, body};
  return {
    ok: true, status: 200, elapsed_ms: 123, model: body.model,
    message: body.message, realm: 'intl', reply: '你好，我在',
    response: {choices: [{message: {content: '你好，我在'}}]},
  };
};
const loadModels = async () => {};

let api = null;
try {
  api = new Function('document', 'MODELS_DATA', 'esc', 'toast', 'postJSON',
    'loadModels', block + `
    return {
      renderApiTestModels, renderApiTestResult, runApiConnectivityTest,
      setModels: value => { MODELS_DATA = value; },
    };`)(global.document, [], esc, toast, postJSON, loadModels);
} catch (err) {
  check('脚本块可执行', false, String(err));
}
check('API 连通性函数可提取', api !== null);

(async () => {
  if (api) {
    // 顺序故意让付费模型排在前面：默认必须是免费模型，不是「第一个」
    api.setModels([
      {id: 'Paid-Ultimate', enabled: true, is_free: false},
      {id: 'Qwen3.8-Flash', enabled: true, is_free: true},
      {id: 'Qwen3.8-Max', enabled: false, is_free: true},
    ]);
    api.renderApiTestModels();
    check('模型下拉包含全部模型',
      els.apiTestModel.innerHTML.includes('Qwen3.8-Flash')
      && els.apiTestModel.innerHTML.includes('Qwen3.8-Max'),
      els.apiTestModel.innerHTML);
    check('默认选中免费模型 Qwen3.8-Flash（不是第一个付费模型）',
      els.apiTestModel.value === 'Qwen3.8-Flash', els.apiTestModel.value);
    check('免费模型提示不消耗付费额度',
      els.apiTestModelHint.textContent.includes('免费模型'),
      els.apiTestModelHint.textContent);
    check('未开通模型有标记',
      els.apiTestModel.innerHTML.includes('未开通'),
      els.apiTestModel.innerHTML);
    check('模型数量状态显示', els.apiTestState.textContent.includes('3 个模型'),
      els.apiTestState.textContent);

    els.apiTestModel.value = 'Qwen3.8-Flash';
    els.apiTestMessage.value = '你是谁，出来干活了';
    const btn = {disabled: false, textContent: ''};
    await api.runApiConnectivityTest(btn);
    check('提交到 /diag/connectivity',
      posted && posted.url === '/diag/connectivity', posted && posted.url);
    check('提交模型、消息和 max_tokens=256',
      posted.body.model === 'Qwen3.8-Flash'
      && posted.body.message === '你是谁，出来干活了'
      && posted.body.max_tokens === 256, posted && posted.body);
    check('成功结果渲染回复',
      els.apiTestResult.innerHTML.includes('你好，我在'),
      els.apiTestResult.innerHTML.slice(0, 200));
    check('按钮恢复',
      btn.disabled === false && btn.textContent === '🚀 发送测试请求',
      {disabled: btn.disabled, text: btn.textContent});

    const errorHtml = api.renderApiTestResult({
      ok: false, status: 403, model: 'Qwen3.8-Flash',
      elapsed_ms: 500, error: '上游排队/资源受限（10605）',
      response: {error: {message: 'queued'}},
    });
    check('失败结果渲染错误文案', errorHtml.includes('排队'), errorHtml.slice(0, 200));
  }

  console.log('');
  console.log('SUMMARY: TOTAL ' + (PASS + FAIL) + ' checks, ' + PASS
    + ' passed, ' + FAIL + ' failed');
  console.log('RESULT: ' + (FAIL ? 'RED' : 'GREEN') + ' (exit ' + (FAIL ? 1 : 0) + ')');
  process.exit(FAIL ? 1 : 0);
})();
