const $ = (id) => document.getElementById(id);
const esc = (value) => String(value).replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const params = new URLSearchParams(location.search);
let releases = [], matched = [], limit = 20;
$('query').value = params.get('q') || '';
$('channel').value = params.get('channel') || '';

function filter() {
  const query = $('query').value.trim().toLowerCase();
  const version = $('version').value, channel = $('channel').value, arch = $('arch').value;
  matched = releases.flatMap((release) => {
    if (version && release.version !== version) return [];
    if (channel && !release.channels.includes(channel)) return [];
    const groups = release.groups.filter((group) => !arch || group.arch === arch || (arch === 'chr' && group.arch.startsWith('chr-')))
      .map((group) => ({...group, files: group.files.filter((file) => !query || release.version.toLowerCase().includes(query) || file.name.toLowerCase().includes(query))}))
      .filter((group) => group.files.length);
    return groups.length ? [{...release, groups}] : [];
  });
  limit = 20;
  const state = new URLSearchParams();
  if (version) state.set('version', version);
  if (query) state.set('q', query);
  if (channel) state.set('channel', channel);
  if (arch) state.set('arch', arch);
  history.replaceState(null, '', location.pathname + (state.size ? '?' + state : ''));
  render();
}

function render() {
  $('status').textContent = `${matched.length} 个版本 / 已归档 ${releases.length} 个版本`;
  $('results').innerHTML = matched.slice(0, limit).map((release) => `<article class="release">
    <div class="release-head"><div><h2 class="version"><a href="${esc(release.url)}">${esc(release.version)} ↗</a></h2>
    <div class="badges">${release.channels.map((c) => `<a class="badge" href="?channel=${encodeURIComponent(c)}">${esc(c)}</a>`).join('')}</div></div><span class="date">${esc(release.date.slice(0,10))}</span></div>
    ${release.groups.map((group) => `<details ${$('arch').value ? 'open' : ''}><summary>${esc(group.arch)}<span>${group.files.length} FILES</span></summary><div class="files">${group.files.map((file) => `<div class="file"><a href="${esc(file.url)}">${esc(file.name)} ↓</a><code title="${esc(file.sha256)}">${esc(file.sha256.slice(0,12))}…</code><button class="copy" data-sha="${esc(file.sha256)}">复制 SHA-256</button></div>`).join('')}</div></details>`).join('')}</article>`).join('') || '<p class="empty">没有匹配的版本，试试其他关键词或筛选条件。</p>';
  $('more').hidden = limit >= matched.length;
}

$('filters').addEventListener('submit', (event) => event.preventDefault());
$('query').addEventListener('input', filter);
$('version').addEventListener('change', filter);
$('channel').addEventListener('change', filter);
$('arch').addEventListener('change', filter);
$('reset').addEventListener('click', () => { $('version').value = ''; $('query').value = ''; $('channel').value = ''; $('arch').value = ''; filter(); });
$('more').addEventListener('click', () => { limit += 20; render(); });
$('results').addEventListener('click', async (event) => {
  const button = event.target.closest('[data-sha]');
  if (!button) return;
  try { await navigator.clipboard.writeText(button.dataset.sha); button.textContent = '已复制'; }
  catch { button.textContent = '请选取校验值'; }
});

try {
  const response = await fetch('catalog.json', {cache: 'no-store'});
  if (!response.ok) throw new Error('索引暂不可用');
  releases = (await response.json()).releases.sort((a,b) => b.date.localeCompare(a.date));
  $('version').innerHTML += ['7', '6'].map((major) => `<optgroup label="RouterOS v${major}">${releases.filter((release) => release.version.startsWith(major + '.')).map((release) => `<option>${esc(release.version)}</option>`).join('')}</optgroup>`).join('');
  const requestedVersion = params.get('version') || '';
  if (requestedVersion && !releases.some((release) => release.version === requestedVersion)) {
    $('version').innerHTML += `<option>${esc(requestedVersion)}</option>`;
  }
  $('version').value = requestedVersion;
  const arches = [...new Set(releases.flatMap((r) => r.groups.map((g) => g.arch)))].sort();
  $('arch').innerHTML += '<option value="chr">CHR（全部）</option>' + arches.map((arch) => `<option>${esc(arch)}</option>`).join('');
  $('arch').value = params.get('arch') || '';
  filter();
} catch (error) { $('status').textContent = `加载失败：${error.message}。请稍后刷新或直接打开 GitHub Releases。`; }
