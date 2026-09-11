// Read-only diagnostic harness for the reported max_tokens error.
// Components and API error parser are real; network and persistence are isolated fixtures.
const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {createRequire} = require('node:module');
const ts = require('../../frontend/vendor/typescript.cjs');

function helpers(relative) {
  const filename = path.resolve(__dirname, '../frontend', relative);
  const source = fs.readFileSync(filename, 'utf8');
  const localRequire = createRequire(filename);
  const exports = {};
  vm.runInNewContext(source + '\nObject.assign(exports, {harness, nodes, field, button, text});', {
    exports, __dirname: path.dirname(filename), console, performance, setImmediate, URL,
    require: Object.assign(id => id === 'node:test' ? () => {} : localRequire(id), {resolve: localRequire.resolve}),
  }, {filename});
  return exports;
}

const center = helpers('provider-center.test.cjs');
const forms = helpers('execution-center.test.cjs');
const provider = {id: 'a', revision_id: 'rev-a', name: '诊断供应商', enabled: true, protocol: 'openai_responses', base_url: 'https://example.invalid/v1'};
const legacy = {id: 'legacy', version: 1, provider_id: provider.id, name: '旧参数配置', model_id: 'legacy-id',
  enabled: true, supports_tools: true, allowed_parameters: ['temperature', 'top_p', 'max_tokens'], parameters: {max_tokens: 4096}};
const symptom = '适配器尚未实现参数: max_tokens';

function jsonField(tree) {
  return forms.nodes(tree).find(n => n.props?.label === '默认模型参数 · JSON');
}

test('new Responses ModelForm defaults to max_output_tokens, not max_tokens', async () => {
  const h = forms.harness('ModelForm', {props: {providers: [provider], suggested: {provider_id: provider.id, model_id: 'new-model'}}});
  h.render();
  const tree = await h.settle();
  assert.equal(JSON.parse(jsonField(tree).props.value).max_output_tokens, 4096);
  assert.ok(!Object.hasOwn(JSON.parse(jsonField(tree).props.value), 'max_tokens'));
  await tree.props.onSubmit();
  const call = h.calls.find(c => c.url === '/models');
  assert.equal(call.body.parameters.max_output_tokens, 4096);
  assert.ok(!call.body.allowed_parameters.includes('max_tokens'));
});

test('editing an existing Responses model preserves its previously stored max_tokens declaration and value', async () => {
  const h = forms.harness('ModelForm', {props: {providers: [provider], initial: legacy}});
  h.render();
  const tree = await h.settle();
  assert.equal(JSON.parse(jsonField(tree).props.value).max_tokens, 4096);
  await tree.props.onSubmit();
  const call = h.calls.find(c => c.url === '/models/legacy');
  assert.equal(call.body.parameters.max_tokens, 4096);
  assert.ok(call.body.allowed_parameters.includes('max_tokens'));
});

test('batch enable sends only model IDs and versions and renders the exact server error above the toolbar', async () => {
  const transport = [];
  const exports = {};
  const source = fs.readFileSync(path.resolve(__dirname, '../../frontend/src/api.ts'), 'utf8');
  const code = ts.transpileModule(source, {compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022}}).outputText;
  vm.runInNewContext(code, {exports, fetch: async (url, options) => {
    transport.push({url, options});
    return {ok: false, status: 400, json: async () => ({detail: symptom})};
  }});
  const h = center.harness({models: [legacy], providerRows: [provider], handler: (url, method, body) => {
    if (url.endsWith('/bulk-state/preview')) return exports.api(url, method, body);
  }});
  h.render();
  let tree = await h.settle();
  center.field(tree, '选择当前筛选下模型配置').props.onChange({target: {checked: true}});
  tree = h.render();
  center.button(tree, '批量启用').props.onClick();
  tree = await h.settle();
  assert.deepEqual(JSON.parse(transport[0].options.body), {
    revision_id: provider.revision_id, selections: [{id: legacy.id, version: legacy.version}], enabled: true,
  });
  assert.equal(transport[0].url, '/api/providers/a/models/bulk-state/preview');
  const rendered = center.nodes(tree);
  const errorIndex = rendered.findIndex(n => n.props?.role === 'alert' && center.text(n) === symptom);
  const toolbarIndex = rendered.findIndex(n => n.props?.['aria-label'] === '批量调整已配置模型');
  assert.ok(errorIndex >= 0 && errorIndex < toolbarIndex);
  assert.match(center.text(tree), /已选择 1 个配置/);
  assert.equal(center.field(tree, '选择模型配置 旧参数配置').props.checked, true);
  assert.equal(center.button(tree, '批量启用').props.disabled, false);
  assert.ok(!rendered.some(n => n.props?.['aria-label'] === '确认批量模型状态'));
  assert.ok(!h.calls.some(c => c.url.endsWith('/bulk-state')));
  assert.equal(h.refresh.length, 0);
});
