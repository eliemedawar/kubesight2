const paths = {
  overview: '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
  cluster: '<path d="m12 3 8 4.5v9L12 21l-8-4.5v-9L12 3Z"/><path d="m4 7.5 8 4.5 8-4.5M12 12v9M8 5.3l8 4.5"/>',
  box: '<rect x="4" y="4" width="16" height="16" rx="3"/><path d="M4 9h16M9 9v11"/>',
  storage: '<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14c0 1.7 3.6 3 8 3s8-1.3 8-3V5M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/>',
  bell: '<path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9ZM10 21h4"/>',
  activity: '<path d="M3 12h4l3-8 4 16 3-8h4"/>',
  settings: '<path d="M4 7h16M4 17h16"/><circle cx="8" cy="7" r="3" fill="currentColor" stroke="none"/><circle cx="16" cy="17" r="3" fill="currentColor" stroke="none"/>',
  refresh: '<path d="M20 7v5h-5M4 17v-5h5"/><path d="M6.1 6.1A8 8 0 0 1 20 12M4 12a8 8 0 0 0 13.9 5.9"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7h.01"/>',
  cpu: '<rect x="5" y="5" width="14" height="14" rx="2"/><path d="M9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3"/><rect x="9" y="9" width="6" height="6" rx="1"/>',
  memory: '<rect x="3" y="6" width="18" height="12" rx="2"/><path d="M7 10v4M12 10v4M17 10v4M7 18v3M12 18v3M17 18v3"/>',
  node: '<rect x="4" y="3" width="16" height="7" rx="2"/><rect x="4" y="14" width="16" height="7" rx="2"/><path d="M8 6.5h.01M8 17.5h.01M13 6.5h3M13 17.5h3"/>',
  search: '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',
  filter: '<path d="M4 5h16l-6 7v7l-4 2v-9L4 5Z"/>',
  check: '<path d="m5 12 4 4L19 6"/>',
  arrow: '<path d="M4 12h16m-5-5 5 5-5 5"/>',
  chevron: '<path d="m9 5 7 7-7 7"/>',
  warning: '<path d="m10.3 4-8 14a2 2 0 0 0 1.7 3h16a2 2 0 0 0 1.7-3l-8-14a2 2 0 0 0-3.4 0Z"/><path d="M12 9v4M12 17h.01"/>',
  close: '<path d="m6 6 12 12M6 18 18 6"/>'
};
const icon = name => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[name] || paths.node}</svg>`;
document.querySelectorAll('[data-icon]').forEach(el => el.innerHTML = icon(el.dataset.icon));
const $ = selector => document.querySelector(selector);
const colors = { memory: '#647dbe', cpu: '#59a393', storage: '#9784bd' };
const production = [
  { name: 'worker-eu-01', cpu: 62, memory: 87, storage: 62, pods: 38, zone: 'eu-west-1a', ip: '10.0.1.21' },
  { name: 'worker-eu-02', cpu: 48, memory: 69, storage: 84, pods: 32, zone: 'eu-west-1b', ip: '10.0.2.22' },
  { name: 'worker-eu-03', cpu: 39, memory: 64, storage: 48, pods: 29, zone: 'eu-west-1c', ip: '10.0.3.23' },
  { name: 'worker-eu-04', cpu: 43, memory: 62, storage: 54, pods: 31, zone: 'eu-west-1a', ip: '10.0.1.24' },
  { name: 'worker-eu-05', cpu: 37, memory: 59, storage: 46, pods: 28, zone: 'eu-west-1b', ip: '10.0.2.25' },
  { name: 'worker-eu-06', cpu: 35, memory: 67, storage: 60, pods: 28, zone: 'eu-west-1c', ip: '10.0.3.26' }
].map(n => ({ ...n, memoryTotal: 64, storageTotal: 500, cpuTotal: 16 }));
const staging = [
  { ...production[2], name: 'staging-eu-01', memory: 42, cpu: 23, storage: 29, pods: 14 },
  { ...production[3], name: 'staging-eu-02', memory: 38, cpu: 17, storage: null, pods: 12 }
];
const state = { cluster: 'production', metric: 'memory', range: '6h', query: '', attention: false, sort: null, descending: true };
const allNodes = () => state.cluster === 'production' ? production : staging;
const needsAttention = n => ['cpu', 'memory', 'storage'].some(key => n[key] != null && n[key] >= 80);
const percent = key => { const known = allNodes().filter(n => n[key] != null); return Math.round(known.reduce((sum, n) => sum + n[key], 0) / known.length); };
const fixed = n => Number(n.toFixed(1)).toLocaleString('en-US');
const capacity = key => {
  const known = allNodes().filter(n => n[key] != null);
  return { total: known.reduce((sum, n) => sum + n[`${key}Total`], 0), used: known.reduce((sum, n) => sum + n[`${key}Total`] * n[key] / 100, 0), known: known.length };
};
function metricCard(label, key, value, unit, sub, fill, note = '') {
  return `<article class="metric"><div class="metric-top"><span>${label}</span>${icon(key)}</div><div class="metric-value">${value}<span>${unit}</span></div><div class="metric-sub">${sub}</div>${note ? `<span class="metric-note">${note}</span>` : ''}<div class="metric-track"><i style="width:${fill}%;background:${colors[key] || '#76a78e'}"></i></div></article>`;
}
function renderMetrics() {
  const cpu = capacity('cpu'), mem = capacity('memory'), disk = capacity('storage');
  const running = allNodes().reduce((sum, n) => sum + n.pods, 0);
  const pending = state.cluster === 'production' ? 2 : 0;
  $('#metrics').innerHTML = [
    metricCard('CPU usage', 'cpu', percent('cpu'), '%', `<b>${fixed(cpu.used)}</b> / ${cpu.total} cores in use`, percent('cpu')),
    metricCard('Memory usage', 'memory', percent('memory'), '%', `<b>${fixed(mem.used)}</b> / ${mem.total} GiB in use`, percent('memory')),
    metricCard('Local storage', 'storage', percent('storage'), '%', `<b>${fixed(disk.used)}</b> / ${disk.total.toLocaleString('en-US')} GiB${disk.known < allNodes().length ? ' · 1/2 reporting' : ' in use'}`, percent('storage')),
    metricCard('Running pods', 'box', running, `/ ${running + pending}`, `<b>${running} running</b><span class="pod-status">${pending} pending</span>`, running / (running + pending) * 100)
  ].join('');
  $('#ready-count').textContent = `${allNodes().length} / ${allNodes().length} nodes ready`;
  $('#node-count').textContent = allNodes().length;
  $('.environment').innerHTML = `<i></i>${state.cluster === 'production' ? 'Production' : 'Staging'}`;
  $('.alert-count').textContent = allNodes().filter(needsAttention).length;
}
function usage(n, key) {
  if (n[key] == null) return '<div class="usage"><div class="usage-line">Unavailable</div><div class="usage-meta">Filesystem metrics not reported</div></div>';
  const total = n[`${key}Total`], used = total * n[key] / 100;
  const unit = key === 'cpu' ? 'cores' : 'GiB';
  return `<div class="usage ${n[key] >= 80 ? 'warning' : ''}"><div class="usage-line"><span>${n[key]}%</span>${n[key] >= 80 ? '<small>High usage</small>' : ''}</div><div class="usage-bar" style="--usage:${n[key]}%;--bar-color:${colors[key]}"><i></i></div><div class="usage-meta">${fixed(used)} / ${total} ${unit}${key !== 'cpu' ? ` · ${fixed(total - used)} free` : ''}</div></div>`;
}
function renderNodes() {
  let nodes = allNodes().filter(n => n.name.includes(state.query.toLowerCase()) && (!state.attention || needsAttention(n)));
  if (state.sort) nodes = [...nodes].sort((a, b) => {
    if (a[state.sort] == null) return 1;
    if (b[state.sort] == null) return -1;
    return (a[state.sort] - b[state.sort]) * (state.descending ? -1 : 1);
  });
  $('#nodes').innerHTML = nodes.map(n => `<tr><td><button class="node-link" data-node="${n.name}"><span>${icon('node')}</span><span><strong>${n.name}</strong><small>${n.zone}</small></span></button></td><td><span class="status"><i></i>Ready</span></td><td data-label="CPU">${usage(n, 'cpu')}</td><td data-label="MEMORY">${usage(n, 'memory')}</td><td data-label="LOCAL STORAGE">${usage(n, 'storage')}</td><td class="pods-number" data-label="PODS">${n.pods}</td><td><button class="row-arrow" data-node="${n.name}" aria-label="Inspect ${n.name}">${icon('chevron')}</button></td></tr>`).join('');
  $('#empty').hidden = nodes.length > 0;
  $('#table-count').textContent = nodes.length === allNodes().length ? `Showing all ${nodes.length} nodes` : `Showing ${nodes.length} of ${allNodes().length} nodes`;
  $('#attention-filter').setAttribute('aria-pressed', state.attention);
  document.querySelectorAll('[data-sort]').forEach(button => {
    button.closest('th').removeAttribute('aria-sort');
    if (state.sort === button.dataset.sort) button.closest('th').setAttribute('aria-sort', state.descending ? 'descending' : 'ascending');
  });
}
function renderAttention() {
  const affected = allNodes().filter(needsAttention);
  $('#attention-count').textContent = affected.length;
  $('#attention').innerHTML = affected.length ? affected.map(n => {
    const key = ['memory', 'storage', 'cpu'].find(k => n[k] >= 80);
    const label = { memory: 'Memory', storage: 'Local storage', cpu: 'CPU' }[key];
    return `<article class="attention-item"><span class="warning-icon">${icon(key)}</span><div class="attention-copy"><h3>${label} usage at ${n[key]}%</h3><p>${n.name} · ${fixed(n[`${key}Total`] * (100 - n[key]) / 100)} ${key === 'cpu' ? 'cores' : 'GiB'} free</p><button data-node="${n.name}">Inspect node ${icon('arrow')}</button></div><span class="attention-time">${key === 'memory' ? '8m' : '12m'}</span></article>`;
  }).join('') : '<div class="healthy-empty">All reporting resources are below 80%.<p>One node is missing filesystem metrics. Open staging-eu-02 to inspect reporting status.</p></div>';
}
function renderChart() {
  const current = percent(state.metric);
  const width = Math.max(270, Math.round($('#chart').clientWidth - 35)), height = 155, left = 30, right = 8, top = 9, bottom = 27;
  const w = width - left - right, h = height - top - bottom;
  const hours = Number.parseInt(state.range);
  const points = Array.from({ length: 55 }, (_, i) => {
    const smooth = state.metric === 'storage' ? .35 : state.metric === 'cpu' ? 3.4 : 1.3;
    const delta = (54 - i) / 54;
    const value = current - delta * (hours === 24 ? 16 : hours === 6 ? 10 : 4) + (Math.sin(i * .65) + Math.sin(i * 1.65) * .35) * smooth * Math.min(1, delta * 8);
    return [left + i / 54 * w, top + h * (1 - value / 100)];
  });
  const line = points.map((p, i) => `${i ? 'L' : 'M'}${p[0].toFixed(2)} ${p[1].toFixed(2)}`).join(' ');
  const grid = [0, 25, 50, 75, 100].map(value => { const y = top + h * (1 - value / 100); return `<line x1="${left}" x2="${width-right}" y1="${y}" y2="${y}" stroke="#eef1f5" stroke-dasharray="3 4"/><text x="0" y="${y+3}" fill="#a5adba" font-size="8" font-family="Instrument,Arial">${value}%</text>`; }).join('');
  const ticks = width < 400 ? 3 : 6;
  const labels = Array.from({ length: ticks + 1 }, (_, i) => { const ago = hours * (1-i/ticks); const label = i === ticks ? 'Now' : hours === 1 ? `${Math.round(ago*60)}m ago` : `${fixed(ago)}h ago`; return `<text x="${left+i/ticks*w}" y="${height-3}" text-anchor="${i === 0 ? 'start' : i === ticks ? 'end' : 'middle'}" fill="#a5adba" font-size="8" font-family="Instrument,Arial">${label}</text>`; }).join('');
  const last = points.at(-1);
  $('#chart').innerHTML = `<svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" role="img" aria-label="Illustrative ${state.metric} usage over ${state.range}, ending at ${current} percent. ${state.metric === 'storage' && state.cluster === 'staging' ? 'Only one of two nodes reports storage.' : ''}"><defs><linearGradient id="area" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="${colors[state.metric]}" stop-opacity=".18"/><stop offset="1" stop-color="${colors[state.metric]}" stop-opacity=".01"/></linearGradient></defs>${grid}<path d="${line} L${last[0]} ${top+h} L${left} ${top+h}Z" fill="url(#area)"/><path d="${line}" fill="none" stroke="${colors[state.metric]}" stroke-width="2" vector-effect="non-scaling-stroke" stroke-linejoin="round"/><circle cx="${last[0]}" cy="${last[1]}" r="3" fill="${colors[state.metric]}" stroke="white" stroke-width="1.5"/>${labels}</svg>`;
  $('#chart-value').textContent = `${current}%`;
  $('#chart-caption').textContent = { memory: 'Memory used / allocatable memory', cpu: 'CPU used / allocatable CPU', storage: `Filesystem used / filesystem capacity${state.cluster === 'staging' ? ' · 1/2 nodes reporting' : ''}` }[state.metric];
}
function openDetails(title, html) {
  $('#detail-eyebrow').textContent = title;
  $('#detail-body').innerHTML = html;
  if (!$('#details').open) $('#details').showModal();
}
function inspectNode(name) {
  const n = allNodes().find(node => node.name === name);
  if (!n) return;
  openDetails('NODE DETAILS', `<h2>${n.name}</h2><p><span class="status"><i></i>Ready</span> &nbsp; Worker node · ${n.zone}</p><div class="detail-specs"><div class="detail-spec"><span>Internal IP</span><b>${n.ip}</b></div><div class="detail-spec"><span>Running pods</span><b>${n.pods} / 110 capacity</b></div><div class="detail-spec"><span>Node readiness</span><b>Accepting workloads</b></div></div>${['cpu', 'memory', 'storage'].map(key => `<div class="detail-resource"><h3>${{ cpu: 'CPU', memory: 'Memory', storage: 'Local storage · node filesystem' }[key]}</h3>${usage(n, key)}</div>`).join('')}${needsAttention(n) ? `<div class="detail-advice">${n.memory >= 80 ? 'Memory usage is above the 80% warning threshold. Review the largest workloads and their memory requests before scheduling more pods here.' : 'Local storage is above the 80% warning threshold. Inspect container logs, unused images, and ephemeral workload data before planning cleanup.'}</div>` : ''}${n.storage == null ? '<div class="detail-advice">Filesystem usage is unavailable. The preview keeps this value unknown instead of treating missing telemetry as empty storage.</div>' : ''}<p>Memory and CPU are compared with allocatable capacity. Local storage is the node filesystem; persistent volumes are tracked separately.</p>`);
}
let toastTimer;
function toast(message) { $('#toast').textContent = message; $('#toast').classList.add('visible'); clearTimeout(toastTimer); toastTimer = setTimeout(() => $('#toast').classList.remove('visible'), 3500); }
function render() { renderMetrics(); renderChart(); renderAttention(); renderNodes(); }
$('#search').addEventListener('input', event => { state.query = event.target.value; renderNodes(); });
$('#attention-filter').addEventListener('click', () => { state.attention = !state.attention; renderNodes(); });
$('#clear-filters').addEventListener('click', () => { state.query = ''; state.attention = false; $('#search').value = ''; renderNodes(); });
$('#cluster').addEventListener('change', event => { state.cluster = event.target.value; state.query = ''; state.attention = false; $('#search').value = ''; $('#updated').textContent = 'Preview snapshot'; render(); });
document.addEventListener('click', event => {
  const node = event.target.closest('[data-node]');
  if (node) inspectNode(node.dataset.node);
  const sort = event.target.closest('[data-sort]');
  if (sort) { state.descending = state.sort === sort.dataset.sort ? !state.descending : true; state.sort = sort.dataset.sort; renderNodes(); }
  for (const key of ['metric', 'range']) {
    const button = event.target.closest(`[data-${key}]`);
    if (!button) continue;
    state[key] = button.dataset[key];
    document.querySelectorAll(`[data-${key}]`).forEach(item => { item.classList.toggle('selected', item === button); item.setAttribute('aria-pressed', item === button); });
    renderChart();
  }
  const section = event.target.closest('[data-section]');
  if (section) showSection(section.dataset.section);
});
$('#refresh').addEventListener('click', () => {
  const button = $('#refresh');
  button.disabled = true; button.lastElementChild.textContent = 'Refreshing…';
  setTimeout(() => { render(); button.disabled = false; button.lastElementChild.textContent = 'Refresh'; $('#updated').textContent = 'Preview refreshed just now'; toast('Sample snapshot refreshed. No live cluster is connected.'); }, 550);
});
$('#close-details').addEventListener('click', () => $('#details').close());
$('#details').addEventListener('click', event => { if (event.target === $('#details') && event.clientX < $('#details').getBoundingClientRect().left) $('#details').close(); });
$('#cluster-details').addEventListener('click', () => showSection('Clusters'));
document.addEventListener('keydown', event => { if (event.key === '/' && !['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement.tagName) && !$('#details').open) { event.preventDefault(); $('#search').focus(); } });
function showSection(section) {
  const content = {
    Clusters: `<h2>Cluster details</h2><p>${$('#cluster').selectedOptions[0].textContent}</p><div class="detail-specs"><div class="detail-spec"><span>Provider</span><b>AWS</b></div><div class="detail-spec"><span>Region</span><b>eu-west-1</b></div><div class="detail-spec"><span>Environment</span><b>${state.cluster}</b></div><div class="detail-spec"><span>Worker nodes ready</span><b>${allNodes().length} / ${allNodes().length}</b></div></div><p>Cluster metadata and upgrade actions live here, keeping the overview focused on current health and capacity.</p>`,
    Workloads: '<h2>Workloads</h2><p>Workload inventory, deployments, namespaces, and pod details belong in this dedicated view.</p><p>The overview keeps only running and pending pod counts. This concept focuses on the dashboard; the full workload screen is outside this preview.</p>',
    Storage: '<h2>Storage</h2><p>The overview shows local filesystem usage per node, so disk capacity is visible alongside memory.</p><div class="detail-resource"><h3>Local node storage</h3><p>Filesystem space used by container images, logs, and ephemeral data.</p></div><div class="detail-resource"><h3>Persistent storage</h3><p>Volume claims, provisioned capacity, and storage classes belong in this dedicated view. Provisioned volume size is not actual disk usage.</p></div>',
    Alerts: `<h2>Resource alerts</h2><p>Sample capacity warnings for this cluster.</p>${allNodes().filter(needsAttention).map(n => `<div class="detail-resource"><h3>${n.name}</h3><p>${n.memory >= 80 ? `Memory usage is ${n.memory}%` : `Local storage usage is ${n.storage}%`}.</p><button class="button" data-node="${n.name}">Inspect node ${icon('arrow')}</button></div>`).join('') || '<p>No active capacity warnings in the sample snapshot.</p>'}`,
    Activity: '<h2>Activity</h2><p>Audit events and deployment history move into a dedicated activity view.</p><p>The dashboard surfaces current issues that need action. Routine events no longer compete with node health.</p>',
    Settings: '<h2>Workspace settings</h2><p>Cluster connections, access management, and alert thresholds remain available from settings.</p><p>This preview uses an illustrative 80% resource warning threshold.</p>'
  };
  openDetails('DESIGN PREVIEW', content[section]);
}
document.querySelectorAll('[data-range], [data-metric]').forEach(button => button.setAttribute('aria-pressed', button.classList.contains('selected')));
render();

new ResizeObserver(() => renderChart()).observe($('#chart'));
