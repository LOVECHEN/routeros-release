const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {runInNewContext} = require('node:vm');

const source = readFileSync('site/app.js', 'utf8');
const makeRelease = (version, date, channels = ['stable']) => ({
  version, date, channels, url: `https://github.com/example/releases/tag/${version}`,
  groups: ['arm64', 'x86', 'chr-x86_64'].map((arch) => ({arch, files: [{
    name: `routeros-${version}-${arch}.npk`, sha256: 'a'.repeat(64), url: 'https://github.com/example/file',
  }]})),
});
const releases = [makeRelease('6.49.23', '2026-10-09'), makeRelease('7.25', '2026-10-23'),
  makeRelease('7.25.1', '2026-11-03'), makeRelease('7.26beta5', '2026-11-02', ['development'])];

async function load(search = '') {
  const elements = new Map();
  const get = (id) => {
    if (!elements.has(id)) elements.set(id, {value: '', innerHTML: '', listeners: {},
      addEventListener(event, handler) { this.listeners[event] = handler; }});
    return elements.get(id);
  };
  const state = {url: ''};
  await runInNewContext(`(async () => {${source}\n})()`, {
    document: {getElementById: get}, URLSearchParams,
    location: {search, pathname: '/'},
    history: {replaceState(_state, _title, url) { state.url = url; }},
    fetch: async () => ({ok: true, json: async () => ({releases: structuredClone(releases)})}),
  });
  return {get, state};
}

test('exact version does not include patch releases; URL restores all filters', async () => {
  const {get, state} = await load('?version=7.25&channel=stable&arch=arm64');
  assert.match(get('status').textContent, /^1 个版本/);
  assert.match(get('results').innerHTML, /routeros-7\.25-arm64/);
  assert.doesNotMatch(get('results').innerHTML, /7\.25\.1|### x86/);
  assert.equal(state.url, '/?version=7.25&channel=stable&arch=arm64');
  assert.match(get('version').innerHTML, /optgroup label="RouterOS v7"/);
  assert.match(get('version').innerHTML, /optgroup label="RouterOS v6"/);
});

test('version, keyword, channel and architecture compose, then reset newest first', async () => {
  const {get, state} = await load('?version=7.25.1&q=arm64&channel=stable&arch=x86');
  assert.match(get('status').textContent, /^0 个版本/);
  get('arch').value = 'arm64';
  get('arch').listeners.change();
  assert.match(get('status').textContent, /^1 个版本/);
  get('reset').listeners.click();
  assert.equal(state.url, '/');
  assert.equal(get('version').value, '');
  assert.match(get('status').textContent, /^4 个版本/);
  const html = get('results').innerHTML;
  assert.ok(html.indexOf('7.25.1 ↗') < html.indexOf('7.26beta5 ↗'));
  assert.ok(html.indexOf('7.26beta5 ↗') < html.indexOf('6.49.23 ↗'));
});

test('unarchived exact version stays explicit and returns no unrelated results', async () => {
  const {get, state} = await load('?version=7.99');
  assert.match(get('status').textContent, /^0 个版本/);
  assert.equal(state.url, '/?version=7.99');
});

test('legacy fuzzy version queries and CHR group remain supported', async () => {
  const {get} = await load('?q=7.25&arch=chr');
  assert.match(get('status').textContent, /^2 个版本/);
  assert.match(get('results').innerHTML, /chr-x86_64/);
  assert.doesNotMatch(get('results').innerHTML, /routeros-7\.25-arm64/);
});
