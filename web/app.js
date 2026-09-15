const state = {
  devices: [],
  device: null,
  apps: [],
  app: null,
  selectedBundle: null,
  capabilities: [],
  sessions: {},
  activeRunId: null,
  watch: null,
  devicePoll: null,
  timer: null,
  selectGen: 0,
  selectPendingId: null,
  metricTab: 'overview',
};
const $ = (id) => document.getElementById(id);

function fetchErrorText(error) {
  const message = String((error && error.message) || '');
  if (/failed to fetch|networkerror|load failed/i.test(message)) {
    return '本机 Agent 无响应。请确认黑窗口还在，稍后点刷新；连接 iPhone 时请解锁并点「信任」。';
  }
  return message || '请求失败';
}

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

async function api(path, options = {}) {
  let response;
  try {
    response = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...options });
  } catch (error) {
    throw new Error(fetchErrorText(error));
  }
  const data = await response.json();
  if (!response.ok) {
    const error = new Error(data.error || 'Agent request failed');
    error.payload = data;
    error.status = response.status;
    throw error;
  }
  return data;
}

function sessionList() {
  return Object.values(state.sessions);
}
function activeSession() {
  return state.sessions[state.activeRunId] || null;
}
function sessionForDevice(deviceId) {
  return sessionList().find((session) => session.device && session.device.id === deviceId);
}
function isLiveSession(session) {
  return Boolean(session && !session.closing && !session.stopping && session.status === 'running');
}
function isMonitorTab(session) {
  if (!session || session.closing) return false;
  if (session.stopping) return true;
  return session.status === 'running' || session.status === 'finalizing' || session.status === 'disconnected';
}
function liveSessionForDevice(deviceId) {
  const session = sessionList().find((item) => item.device && item.device.id === deviceId && isLiveSession(item));
  return session || null;
}
function hasSessions() {
  return sessionList().length > 0;
}
function visibleSessions() {
  return sessionList().filter(isMonitorTab);
}

function setView(view) {
  document.querySelectorAll('.view').forEach((el) => el.classList.remove('active-view'));
  $(`${view}-view`).classList.add('active-view');
  document.querySelectorAll('.nav-item').forEach((el) => el.classList.toggle('active', el.dataset.view === view));
  const titles = { overview: '监测首页', devices: '设备管理', monitor: '实时监控', reports: '检测报告', settings: '系统设置' };
  $('page-title').textContent = titles[view];
  if (view === 'devices') renderDevicesTable();
  if (view === 'reports') renderReports();
  if (view === 'monitor') renderActiveMonitor();
}

function deviceIds(list) {
  return list.map((device) => device.id).sort().join('\n');
}

function paintDeviceList() {
  const list = $('device-list');
  if (!state.devices.length) {
    list.innerHTML = '<div class="empty-state">未检测到设备，请检查 USB / ADB 连接</div>';
    $('device-state').textContent = '未连接';
    $('device-state').className = 'state-badge waiting';
    if (!hasSessions()) {
    state.app = null;
    state.selectedBundle = null;
    state.capabilities = [];
      $('app-list').innerHTML = '<div class="empty-state">先选择一个设备</div>';
      $('capability-list').innerHTML = '';
      $('start-button').disabled = true;
      $('connection-pill').textContent = '未检测到设备';
      $('connection-pill').className = 'pill muted';
    }
    return '';
  }
  $('device-state').textContent = '已连接';
  $('device-state').className = 'state-badge available';
  const selectedId = state.device && state.devices.some((device) => device.id === state.device.id) ? state.device.id : state.devices[0].id;
  list.innerHTML = state.devices.map((device) => {
    const live = liveSessionForDevice(device.id);
    return `<div class="device-card ${device.id === selectedId ? 'selected' : ''} ${live ? 'live' : ''}" data-device="${device.id}"><div class="device-icon">▯</div><div><strong>${device.name}</strong><small>${device.version} · ${device.connection}</small></div><div class="device-meta"><b>${live ? '● LIVE' : '● ONLINE'}</b><small>${device.id.slice(0, 12)}…</small></div></div>`;
  }).join('');
  document.querySelectorAll('#device-list .device-card').forEach((card) => card.addEventListener('click', () => selectDevice(card.dataset.device)));
  return selectedId;
}

function renderDevices() {
  const selectedId = paintDeviceList();
  if (!selectedId) return;
  if (!state.device || state.device.id !== selectedId) selectDevice(selectedId);
  else updateStartButton();
}

function applyDeviceSnapshot(devices, meta = {}) {
  const previousIds = deviceIds(state.devices);
  const nextIds = deviceIds(devices);
  state.devices = devices;
  if ($('devices-view') && $('devices-view').classList.contains('active-view')) renderDevicesTable();
  const removed = Array.isArray(meta.removed) ? meta.removed : null;
  if (removed) {
    removed.forEach((id) => {
      const session = sessionForDevice(id);
      if (session) stopOnUnplug(session.runId);
    });
  } else if (devices.length) {
    sessionList().forEach((session) => {
      if (!devices.some((device) => device.id === session.device.id)) stopOnUnplug(session.runId);
    });
  }
  if (previousIds === nextIds) {
    paintDeviceList();
    if (state.device) {
      state.device = devices.find((device) => device.id === state.device.id) || state.device;
      const active = activeSession();
      if (!active || active.status !== 'disconnected') {
        $('connection-pill').textContent = `${state.device.name} · Agent 已连接`;
        $('connection-pill').className = 'pill';
        $('connection-pill').style.background = '';
        $('connection-pill').style.color = '';
      }
    }
    return;
  }
  renderDevices();
}

function watchDevices() {
  if (state.watch) state.watch.close();
  state.watch = new EventSource('/api/v1/devices/stream');
  state.watch.onmessage = (message) => {
    const event = JSON.parse(message.data);
    if (event.type === 'devices' && Array.isArray(event.devices)) {
      applyDeviceSnapshot(event.devices, { removed: event.removed || [] });
    }
  };
  state.watch.onopen = () => {
    if (state.devicePoll) {
      clearInterval(state.devicePoll);
      state.devicePoll = null;
    }
  };
  state.watch.onerror = () => {
    if (state.watch && state.watch.readyState === EventSource.CLOSED && !state.devicePoll) state.devicePoll = setInterval(pollDevices, 2500);
  };
}

async function pollDevices() {
  try {
    const result = await api('/api/v1/devices');
    applyDeviceSnapshot(result.devices, { removed: [] });
  } catch (error) { /* 忽略瞬时网络错误，避免误停监测 */ }
}

function updateStartButton() {
  const button = $('start-button');
  if (!button) return;
  const live = state.device && liveSessionForDevice(state.device.id);
  if (live) {
    button.disabled = false;
    button.querySelector('span').textContent = '查看实时监控';
    console.info('[StartMonitor] button enable reason=live device=%s', state.device.id);
    return;
  }
  button.querySelector('span').textContent = '开始监测';
  button.disabled = !state.app;
  console.info('[StartMonitor] button sync hasApp=%s disabled=%s bundle=%s', Boolean(state.app), button.disabled, state.app && state.app.bundle);
}

async function selectDevice(id) {
  const device = state.devices.find((item) => item.id === id);
  if (!device) {
    console.warn('[IosApps] select skip, device not found', { id });
    return;
  }
  const gen = ++state.selectGen;
  const tag = device.platform === 'ios' ? '[IosApps]' : '[AndroidApps]';
  console.info(`${tag} select start`, { id, platform: device.platform, gen });
  const previousBundle = state.selectedBundle || (state.app && state.app.bundle);
  state.device = device;
  document.querySelectorAll('#device-list .device-card').forEach((card) => card.classList.toggle('selected', card.dataset.device === id));
  $('connection-pill').textContent = `${state.device.name} · Agent 已连接`;
  $('connection-pill').className = 'pill';
  $('connection-pill').style.background = '';
  $('connection-pill').style.color = '';
  try {
    const result = await api(`/api/v1/devices/${encodeURIComponent(id)}/applications?platform=${encodeURIComponent(device.platform || 'android')}`);
    if (gen !== state.selectGen || (state.device && state.device.id !== id)) {
      console.info(`${tag} select stale skip`, { id, gen, current: state.selectGen });
      return;
    }
    state.apps = result.applications || [];
    state.appsError = result.error || '';
    console.info(`${tag} select done`, { id, count: state.apps.length, error: state.appsError || null });
  } catch (error) {
    if (gen !== state.selectGen) return;
    const text = error.message || '读取应用列表失败';
    console.error(`${tag} select failed`, { id, err: text });
    if (!state.apps.length) state.appsError = text;
    $('connection-pill').textContent = text;
    $('connection-pill').className = 'pill muted';
  }
  renderApps();
  const restore = state.selectedBundle || previousBundle;
  if (restore && state.apps.some((app) => app.bundle === restore)) {
    const input = document.querySelector(`input[name="app"][value="${CSS.escape(restore)}"]`);
    if (input) input.checked = true;
    state.selectedBundle = restore;
    state.app = state.apps.find((app) => app.bundle === restore);
    console.info('[StartMonitor] restoreApp bundle=%s', restore);
  } else {
    state.app = null;
    state.capabilities = [];
    $('capability-list').innerHTML = '';
  }
  updateStartButton();
}

function appVersionText(app) {
  if (!app) return '';
  if (app.versionLabel) return app.versionLabel;
  const version = app.version || app.versionName || '';
  const code = app.versionCode || '';
  if (version && code && !String(version).includes(String(code))) return `${version} (${code})`;
  return version || code || '';
}

function sessionAppTitle(session) {
  if (!session || !session.app) return '';
  const name = session.app.name || session.app.bundle || '';
  const version = appVersionText(session.app);
  return version ? `${name} ${version}` : name;
}

function reportListTitle(report) {
  if (report && report.title) return report.title;
  const device = (report && report.device) || {};
  const app = (report && (report.appName || report.bundle || report.packageId)) || '未知应用';
  const rawVersion = (report && (report.versionName || report.appVersion)) || '';
  const version = String(rawVersion).split('(')[0].trim();
  const model = device.model || device.name || '未知机型';
  return [app, version, model].filter(Boolean).join('-');
}

function renderApps() {
  const rawQuery = $('app-search') ? $('app-search').value : '';
  const query = String(rawQuery || '').trim().toLowerCase();
  const matched = query
    ? state.apps.filter((app) => String(app.bundle || '').toLowerCase().includes(query))
    : state.apps;
  if (query) {
    console.info('[AppSearch] filter by package query=%s matched=%s total=%s', query, matched.length, state.apps.length);
  }
  const apps = matched.slice(0, 40);
  let emptyCopy = state.appsError
    || (state.device && state.device.platform === 'ios'
      ? '读不到 iOS 应用。请在手机上点「信任」，解锁屏幕，并确认已安装 Apple 设备支持。'
      : '读不到本机应用。请确认 USB 调试已授权，查看 Agent 窗口或 %LOCALAPPDATA%\\PerfPilot\\logs\\agent.log');
  if (!state.apps.length && !state.device) emptyCopy = '先选择一个设备';
  else if (query && !apps.length && state.apps.length) emptyCopy = '没有匹配该包名的应用';
  $('app-list').innerHTML = apps.length
    ? apps.map((app) => `<label class="app-row"><input type="radio" name="app" value="${escapeHtml(app.bundle)}" ${state.selectedBundle === app.bundle ? 'checked' : ''}><div><strong>${escapeHtml(app.name)}</strong><small>${escapeHtml(app.bundle)} · ${escapeHtml(appVersionText(app) || '版本未知')}</small></div></label>`).join('')
    : `<div class="empty-state">${emptyCopy}</div>`;
  document.querySelectorAll('input[name="app"]').forEach((input) => input.addEventListener('change', () => selectApp(input.value)));
}

async function selectApp(bundle) {
  state.selectedBundle = bundle;
  state.app = state.apps.find((app) => app.bundle === bundle) || { bundle, name: bundle, version: '' };
  console.info('[StartMonitor] selectApp bundle=%s version=%s versionCode=%s', bundle, state.app && state.app.version, state.app && state.app.versionCode);
  updateStartButton();
  try {
    const result = await api('/api/v1/capabilities', { method: 'POST', body: JSON.stringify({ deviceId: state.device.id, bundle, platform: state.device.platform }) });
    state.capabilities = result.indicators || [];
    renderCapabilities();
    console.info('[StartMonitor] capabilities ready=%s count=%s', result.ready !== false, state.capabilities.length);
  } catch (error) {
    state.capabilities = [];
    $('capability-list').innerHTML = `<div class="empty-state">能力预检失败，仍可开始监测。${error.message || ''}</div>`;
    console.error('[StartMonitor] capabilities failed', { bundle, err: error.message });
  }
  updateStartButton();
}

function renderCapabilities() {
  $('capability-list').innerHTML = state.capabilities.map((item) => `<div class="capability-row"><i class="${item.state === 'degraded' ? 'degraded' : ''}"></i><b>${item.label}</b><span>${item.state === 'available' ? '可用' : '降级'} · ${item.source}</span></div>`).join('');
}

function renderDevicesTable() {
  $('devices-table').innerHTML = state.devices.map((device) => {
    const live = liveSessionForDevice(device.id);
    return `<div class="device-card ${live ? 'live' : ''}"><div class="device-icon">▯</div><div><strong>${device.name}</strong><small>${device.model} · ${device.version} · ${device.connection}</small></div><div class="device-meta"><b>${live ? '● 监测中' : '● 已连接'}</b><small>${device.id}</small></div></div>`;
  }).join('') || '<div class="empty-state">暂无设备</div>';
}

function formatElapsed(session) {
  const end = session.endedAt || Date.now();
  const seconds = Math.max(0, Math.floor((end - session.startedAt) / 1000));
  return `${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`;
}

function tickElapsed() {
  const session = activeSession();
  if (!session || !$('elapsed')) return;
  if (session.status !== 'running' && session.status !== 'finalizing') return;
  $('elapsed').textContent = formatElapsed(session);
}

function ensureTimer() {
  if (state.timer) return;
  state.timer = setInterval(tickElapsed, 1000);
}

function stopTimerIfIdle() {
  if (visibleSessions().some((session) => session.status === 'running')) return;
  clearInterval(state.timer);
  state.timer = null;
}

function renderMonitorTabs() {
  const tabs = $('monitor-tabs');
  const sessions = visibleSessions();
  if (!sessions.length) {
    tabs.hidden = true;
    tabs.innerHTML = '';
    return;
  }
  tabs.hidden = false;
  tabs.innerHTML = sessions.map((session) => {
    const device = session.device || {};
    const label = device.name || device.model || device.id || '设备';
    const model = [device.model, device.version].filter(Boolean).join(' · ');
    const issue = session.issue === 'background' ? 'disconnected' : '';
    const hint = session.issue === 'background' ? '应用不在前台' : (sessionAppTitle(session) || session.app.name || '');
    return `<button type="button" class="monitor-tab ${session.runId === state.activeRunId ? 'active' : ''} ${issue}" data-run="${session.runId}"><b>${label}</b><small>${model ? `${model} · ` : ''}${hint}</small></button>`;
  }).join('');
  tabs.querySelectorAll('.monitor-tab').forEach((button) => button.addEventListener('click', () => setActiveRun(button.dataset.run)));
}

function setActiveRun(runId) {
  const session = state.sessions[runId];
  if (!session || session.closing) return;
  state.activeRunId = runId;
  renderMonitorTabs();
  renderActiveMonitor();
}

function resetMetricDisplay(session) {
  $('sample-count').textContent = '0';
  $('fps-value').textContent = '--';
  $('fps-average').textContent = '--';
  $('fps-min').textContent = '--';
  $('cpu-value').textContent = '--';
  $('cpu-bar').style.width = '0%';
  if ($('cpu-le25')) $('cpu-le25').textContent = '--';
  if ($('cpu-le50')) $('cpu-le50').textContent = '--';
  $('memory-value').textContent = '--';
  $('memory-peak').textContent = '--';
  if ($('memory-native')) $('memory-native').textContent = '--';
  if ($('memory-swap')) $('memory-swap').textContent = '--';
  syncMemoryExtrasUi(session || null);
  $('gpu-value').textContent = '--';
  $('chart-fps-label').textContent = '--';
  $('chart-resource-label').textContent = '等待数据';
  if ($('chart-memory-label')) $('chart-memory-label').textContent = '--';
  drawCharts([], (session && session.extras) || {}, inspectMarkerTime(session, []));
}

function paintMonitorStatus(session) {
  const status = $('monitor-status');
  const wrap = status && status.closest('.live-label');
  if (!status) return;
  if (!session) {
    status.textContent = '等待监测';
    status.style.color = '';
    if (wrap) wrap.classList.remove('is-error');
    return;
  }
  let text = '正在监测';
  let error = false;
  if (session.status === 'disconnected') {
    text = '设备已断开';
    error = true;
  } else if (session.status === 'finalizing' || session.stopping) {
    const count = (session.samples || []).length;
    text = `正在收尾 · 已落盘 ${count} 个样本`;
  } else if (session.status === 'completed') {
    text = '监测完成';
  } else if (session.status === 'failed') {
    text = '采集异常';
    error = true;
  } else if (session.issue === 'background') {
    text = '应用不在前台';
    error = true;
  }
  status.textContent = text;
  status.style.color = error ? '#c65345' : '';
  if (wrap) wrap.classList.toggle('is-error', error);
}
function emptyMonitor() {
  paintMonitorStatus(null);
  const tabs = $('monitor-tabs');
  if (tabs) {
    tabs.hidden = true;
    tabs.innerHTML = '';
  }
  $('monitor-app').textContent = '等待目标应用';
  $('monitor-device').textContent = '';
  $('event-count').textContent = '0';
  $('event-list').innerHTML = '<div class="empty-state">开始监测后，异常会出现在这里</div>';
  $('monitor-capabilities').innerHTML = '';
  $('stop-button').disabled = true;
  $('elapsed').textContent = '00:00';
  hideTimelineInspect();
  resetMetricDisplay();
  paintMetricTabs('overview');
  renderCollectorLog([]);
  stopTimerIfIdle();
}

function renderActiveMonitor() {
  renderMonitorTabs();
  const session = activeSession();
  if (!session || session.closing) {
    emptyMonitor();
    return;
  }
  paintMonitorStatus(session);
  $('monitor-app').textContent = sessionAppTitle(session) || session.app.name;
  $('monitor-device').textContent = `${session.device.model || session.device.name} · ${session.device.version} · ${session.app.bundle}`;
  console.info(
    '[MonitorTab] header runId=%s app=%s version=%s model=%s',
    session.runId,
    session.app && session.app.name,
    appVersionText(session.app),
    session.device && (session.device.model || session.device.name),
  );
  $('monitor-capabilities').innerHTML = session.capabilities.map((item) => `<div class="coverage-item"><i class="${item.state === 'degraded' ? 'degraded' : ''}"></i>${item.label}<span>${item.state === 'available' ? '可用' : '降级'}</span></div>`).join('');
  $('stop-button').disabled = session.status === 'disconnected' || session.status === 'finalizing' || session.stopping;
  $('event-count').textContent = String(session.eventCount);
  $('event-list').innerHTML = session.events.length
    ? session.events.map((item) => `<button type="button" class="event-item" data-inspect="${item.sampleIndex == null ? '' : item.sampleIndex}"><i></i><div><b>${escapeHtml(item.title)}</b><small>${escapeHtml(item.message)}</small></div></button>`).join('')
    : '<div class="empty-state">开始监测后，异常会出现在这里</div>';
  $('elapsed').textContent = formatElapsed(session);
  const shown = inspectSample(session) || session.samples.at(-1);
  paintMetricTabs(session.metricTab || state.metricTab || 'overview');
  renderCollectorLog(logcatEnabled(session) ? (session.logs || []) : []);
  if (shown) updateMetrics(session, shown, true);
  else resetMetricDisplay(session);
}

function attachStream(session) {
  const runId = session.runId;
  if (session.stream) session.stream.close();
  session.stream = new EventSource(`/api/v1/runs/${runId}/stream`);
  session.stream.onmessage = (message) => handleEvent(runId, JSON.parse(message.data));
  session.stream.onerror = () => {
    if (!state.sessions[runId] || state.sessions[runId].closing || state.sessions[runId].stopping) return;
    if (!state.devices.some((device) => device.id === session.device.id)) stopOnUnplug(runId);
  };
}

function adoptRun(run) {
  const runId = run.runId || run.sessionId;
  const status = run.status || '';
  const live = status === 'ready' || status === 'running' || status === 'finalizing';
  if (!runId || !live) {
    console.info('[MonitorTab] adopt skip runId=%s status=%s', runId, status || null);
    if (runId && state.sessions[runId] && !isMonitorTab(state.sessions[runId])) closeSession(runId);
    return null;
  }
  if (state.sessions[runId]) {
    const existing = state.sessions[runId];
    if (Array.isArray(run.samples) && run.samples.length >= (existing.samples || []).length) {
      existing.samples = run.samples;
    }
    if (Array.isArray(run.logs) && run.logs.length >= (existing.logs || []).length) {
      existing.logs = run.logs;
    }
    if (Array.isArray(run.capabilities) && run.capabilities.length) {
      existing.capabilities = run.capabilities;
    }
    if (run.extras) existing.extras = { ...defaultMemoryExtras(existing.device || run.device), ...existing.extras, ...run.extras };
    if (existing.status === 'running' && (!existing.stream || existing.stream.readyState !== EventSource.OPEN)) {
      console.info('[MonitorTab] reattach stream runId=%s readyState=%s', runId, existing.stream ? existing.stream.readyState : null);
      attachStream(existing);
    }
    return existing;
  }
  const device = run.device || {};
  const bundle = run.bundle || run.packageId || '';
  const session = {
    runId,
    device,
    app: {
      name: run.appName || bundle,
      bundle,
      version: run.versionName || run.appVersion || '',
      versionCode: run.versionCode || '',
      versionLabel: run.appVersion || '',
    },
    capabilities: run.capabilities || [],
    samples: Array.isArray(run.samples) ? run.samples : [],
    logs: Array.isArray(run.logs) ? run.logs.slice(-500) : [],
    startedAt: run.startedAtMs || Date.now(),
    endedAt: run.endedAtMs || null,
    stream: null,
    events: [],
    eventCount: 0,
    status: status === 'finalizing' ? 'finalizing' : 'running',
    stopping: status === 'finalizing',
    issue: null,
    issueAlerted: null,
    extras: { ...defaultMemoryExtras(device), ...(run.extras || {}) },
    inspectIndex: null,
  };
  state.sessions[runId] = session;
  console.info('[MonitorTab] adopt runId=%s status=%s device=%s extras=%s', runId, session.status, device.id || '', JSON.stringify(session.extras));
  if (session.status === 'running' || session.status === 'finalizing') attachStream(session);
  if (!state.activeRunId || !state.sessions[state.activeRunId] || !isMonitorTab(state.sessions[state.activeRunId])) {
    state.activeRunId = runId;
  }
  ensureTimer();
  return session;
}

async function adoptRemoteRun(runId) {
  const run = await api(`/api/v1/runs/${encodeURIComponent(runId)}`);
  return adoptRun(run);
}

async function restoreRuns() {
  try {
    const result = await api('/api/v1/runs/active');
    const remote = (result.runs || []).filter((run) => {
      const status = run.status;
      const live = status === 'ready' || status === 'running' || status === 'finalizing';
      if (!live) console.info('[MonitorTab] restore skip runId=%s status=%s', run.runId || run.sessionId, status);
      return live;
    });
    const remoteIds = new Set(remote.map((run) => run.runId || run.sessionId));
    console.info('[MonitorTab] restore active count=%s ids=%s', remote.length, [...remoteIds].join(','));
    sessionList()
      .filter((session) => !remoteIds.has(session.runId))
      .forEach((session) => closeSession(session.runId));
    remote.forEach(adoptRun);
    if (visibleSessions().length) {
      if (!state.activeRunId || !state.sessions[state.activeRunId] || !isMonitorTab(state.sessions[state.activeRunId])) {
        state.activeRunId = visibleSessions()[0].runId;
      }
      const view = document.querySelector('.nav-item.active')?.dataset.view || 'overview';
      if (view === 'overview' || view === 'monitor') {
        console.info('[MonitorTab] restore open monitor runId=%s view=%s samples=%s', state.activeRunId, view, (activeSession() && activeSession().samples.length) || 0);
        setView('monitor');
      } else {
        renderMonitorTabs();
      }
    } else if (!hasSessions()) {
      emptyMonitor();
    }
  } catch (error) {
    console.warn('[MonitorTab] restore failed err=%s', error && error.message);
    sessionList().forEach((session) => closeSession(session.runId));
  }
  paintDeviceList();
  updateStartButton();
}

async function startSession(retry = true) {
  if (!state.device || state.starting) return;
  const existing = sessionForDevice(state.device.id);
  if (existing && isLiveSession(existing)) {
    if (state.app && existing.app.bundle !== state.app.bundle) {
      await showAppDialog(
        `该设备正在监测「${existing.app.name}」，不能直接改测「${state.app.name}」。\n请先停止当前监测，并把要测的应用切到前台后再开始。\n否则 FPS 会采到前台应用，CPU / 内存仍是上一款应用。`,
        '请先停止当前监测',
      );
      return;
    }
    setActiveRun(existing.runId);
    setView('monitor');
    return;
  }
  if (!state.app) return;
  state.starting = true;
  $('start-button').disabled = true;
  try {
    if (state.device.platform !== 'ios') {
      const fg = await api(`/api/v1/devices/${encodeURIComponent(state.device.id)}/foreground`);
      if (fg.supported && fg.package && fg.package !== state.app.bundle) {
        await showAppDialog(
          `「${state.app.name}」当前不在前台。\n前台应用是 ${fg.label || fg.package}。\n请把要监测的应用切到前台后再开始，否则 FPS 会采到前台画面，CPU / 内存仍是所选应用。`,
          '请把应用放到前台',
        );
        return;
      }
    }
    const created = await api('/api/v1/runs', { method: 'POST', body: JSON.stringify({ device: state.device, bundle: state.app.bundle, source: 'manual', app: state.app, capabilities: state.capabilities.slice() }) });
    const runId = created.runId || created.sessionId;
    const session = {
      runId,
      device: state.device,
      app: {
        name: created.appName || state.app.name,
        bundle: state.app.bundle,
        version: created.versionName || state.app.version,
        versionCode: created.versionCode || state.app.versionCode,
        versionLabel: created.appVersion || appVersionText(state.app),
      },
      capabilities: state.capabilities.slice(),
      samples: [],
      logs: [],
      startedAt: Date.now(),
      stream: null,
      events: [],
      eventCount: 0,
      status: 'running',
      issue: null,
      issueAlerted: null,
      extras: { ...defaultMemoryExtras(state.device), ...(created.extras || {}) },
      inspectIndex: null,
    };
    console.info(
      '[StartMonitor] session ready runId=%s app=%s version=%s nativePss=%s swapPss=%s',
      runId,
      session.app.name,
      session.app.versionLabel || session.app.version,
      session.extras.nativePss,
      session.extras.swapPss,
    );
    await api(`/api/v1/runs/${runId}/start`, { method: 'POST', body: '{}' });
    state.sessions[runId] = session;
    attachStream(session);
    state.activeRunId = runId;
    ensureTimer();
    paintDeviceList();
    setView('monitor');
  } catch (error) {
    const payload = error.payload || {};
    if (error.status === 409 && payload.runId) {
      const occupiedBundle = payload.bundle || payload.packageId;
      if (occupiedBundle && state.app && occupiedBundle !== state.app.bundle) {
        await showAppDialog(
          `该设备正在监测「${occupiedBundle}」，不能直接改测「${state.app.name}」。\n请先停止当前监测，并把要测的应用切到前台后再开始。`,
          '请先停止当前监测',
        );
        return;
      }
      try {
        const adopted = await adoptRemoteRun(payload.runId);
        if (adopted) {
          if (state.app && adopted.app.bundle && adopted.app.bundle !== state.app.bundle) {
            await showAppDialog(
              `该设备正在监测「${adopted.app.name}」，不能直接改测「${state.app.name}」。\n请先停止当前监测，并把要测的应用切到前台后再开始。`,
              '请先停止当前监测',
            );
            return;
          }
          setActiveRun(adopted.runId);
          setView('monitor');
          return;
        }
      } catch (ignored) { /* fall through */ }
    }
    if (error.status === 409 && payload.status === 'finalizing' && retry) {
      await new Promise((resolve) => setTimeout(resolve, 700));
      state.starting = false;
      updateStartButton();
      return startSession(false);
    }
    if (error.status === 409 && payload.code === 'not_foreground') {
      await showAppDialog(
        `「${state.app.name}」当前不在前台。\n前台应用是 ${payload.foreground}。\n请把它切到前台后再开始监测。`,
        '请把应用放到前台',
      );
    } else if (error.status === 409) {
      window.alert('该设备上一轮监测尚未完全结束，请稍候再试。');
    } else {
      window.alert(error.message || '无法开始监测');
    }
  } finally {
    state.starting = false;
    updateStartButton();
  }
}

function readableText(text) {
  return String(text || '')
    .replace(/\uFFFD+/g, '')
    .replace(/[^\S\n]+/g, ' ')
    .trim();
}

function showAppDialog(message, title = '监测异常') {
  const text = readableText(message);
  return new Promise((resolve) => {
    const dialog = $('app-dialog');
    const button = $('app-dialog-ok');
    if (!dialog || !button) {
      window.alert(`${title}\n${text}`);
      resolve();
      return;
    }
    $('app-dialog-title').textContent = title;
    $('app-dialog-message').textContent = text;
    dialog.hidden = false;
    const onOk = () => {
      button.removeEventListener('click', onOk);
      dialog.hidden = true;
      resolve();
    };
    button.addEventListener('click', onOk);
    button.focus();
  });
}

async function abortMonitorChannel(runId, reason, message) {
  const session = state.sessions[runId];
  if (!session || session.closing || session.stopping) return;
  if (session.stopPrompting) dismissStopDialog(null);
  if (session.prompting) return;
  session.prompting = true;
  await showAppDialog(message);
  if (!state.sessions[runId]) {
    setView('overview');
    return;
  }
  session.prompting = false;
  await stopAndFinalize(runId, reason, { save: true, report: true });
  setView('overview');
}

function handleEvent(runId, event) {
  const session = state.sessions[runId];
  if (!session || session.closing) return;
  if (session.prompting && !session.stopping) return;
  if (event.type === 'sample') {
    session.samples.push(event.data);
    if (state.activeRunId === runId) {
      paintMonitorStatus(session);
      updateMetrics(session, inspectSample(session) || event.data, false);
    }
    return;
  }
  if (event.type === 'log') {
    appendCollectorLog(runId, event.data && event.data.line);
    return;
  }
  if (event.type === 'warning') {
    if (event.data.code === 'background') {
      const first = session.issue !== 'background';
      session.issue = 'background';
      if (first) addEvent(runId, '应用不在前台', '已暂停帧率采集，把应用切回前台后会自动继续。');
      console.info('[MonitorTab] background pause runId=%s first=%s', runId, first);
      if (state.activeRunId === runId) {
        paintMonitorStatus(session);
        renderMonitorTabs();
      } else {
        renderMonitorTabs();
      }
      return;
    }
    if (event.data.code === 'foreground') {
      session.issue = null;
      addEvent(runId, '应用已回到前台', '继续采集。');
      console.info('[MonitorTab] foreground resume runId=%s', runId);
      if (state.activeRunId === runId) {
        paintMonitorStatus(session);
        renderMonitorTabs();
      } else {
        renderMonitorTabs();
      }
      return;
    }
    return;
  }
  if (event.type === 'status') {
    const nextStatus = event.data && event.data.status;
    if (nextStatus) session.status = nextStatus;
    if (session.stopping) {
      session.finishPayload = event.data || {};
      if (state.activeRunId === runId) {
        paintMonitorStatus(session);
        $('stop-button').disabled = true;
      }
      console.info('[StopMonitor] status runId=%s status=%s samples=%s reportReady=%s', runId, nextStatus, session.samples.length, event.data && event.data.reportReady);
      return;
    }
    if (event.data.status === 'failed' && session.issueAlerted !== 'error') {
      session.status = 'failed';
      session.issueAlerted = 'error';
      abortMonitorChannel(
        runId,
        'error',
        `采集异常：${readableText(event.data.message || event.data.error || '数据采集失败')}。\n点击确定后将关闭该设备的实时监控并返回首页，请重新选择应用后再开始监测。`,
      );
    }
    return;
  }
  if (event.type === 'error' && session.issueAlerted !== 'error' && !session.stopping) {
    session.status = 'failed';
    session.issueAlerted = 'error';
    abortMonitorChannel(
      runId,
      'error',
      `采集异常：${readableText(event.data.message || '数据采集失败')}。\n点击确定后将关闭该设备的实时监控并返回首页，请重新选择应用后再开始监测。`,
    );
  }
}

function markDisconnected(runId) {
  stopOnUnplug(runId);
}

function stopOnUnplug(runId) {
  const session = state.sessions[runId];
  if (!session || session.closing || session.stopping) return;
  if (session.stopPrompting) dismissStopDialog(null);
  addEvent(runId, '设备断开', '正在保存本次监测报告');
  console.info('[StopMonitor] unplug auto-stop runId=%s samples=%s', runId, (session.samples || []).length);
  stopAndFinalize(runId, 'unplug', { save: true, report: true });
}

let memoryExtrasSyncing = false;

function defaultMemoryExtras(device) {
  const android = Boolean(device && device.platform === 'android');
  return { nativePss: android, swapPss: android, logcat: false };
}

function sessionExtras(session) {
  if (!session) return { nativePss: true, swapPss: true, logcat: false };
  return {
    nativePss: Boolean(session.extras && session.extras.nativePss),
    swapPss: Boolean(session.extras && session.extras.swapPss),
    logcat: Boolean(session.extras && session.extras.logcat),
  };
}

function logcatEnabled(session) {
  return Boolean(session && session.extras && session.extras.logcat);
}

function syncMemoryExtrasUi(session) {
  const nativeBox = $('opt-native-pss');
  const swapBox = $('opt-swap-pss');
  const logBox = $('opt-logcat');
  const hint = $('memory-ios-hint');
  const nativeRow = $('memory-native-row');
  const swapRow = $('memory-swap-row');
  const android = Boolean(session && session.device && session.device.platform === 'android');
  const ios = Boolean(session && session.device && session.device.platform === 'ios');
  const extras = sessionExtras(session);
  const canToggle = android && session && session.status !== 'finalizing' && session.status !== 'failed';
  memoryExtrasSyncing = true;
  if (nativeBox && swapBox) {
    nativeBox.disabled = !canToggle;
    swapBox.disabled = !canToggle;
    nativeBox.checked = extras.nativePss;
    swapBox.checked = extras.swapPss;
  }
  if (logBox) {
    logBox.disabled = !canToggle;
    logBox.checked = extras.logcat;
  }
  const logRow = $('logcat-opt-row');
  if (logRow) logRow.hidden = Boolean(ios);
  memoryExtrasSyncing = false;
  if (nativeRow) nativeRow.hidden = !extras.nativePss;
  if (swapRow) swapRow.hidden = !extras.swapPss;
  const legendNative = $('legend-native');
  const legendSwap = $('legend-swap');
  if (legendNative) legendNative.hidden = !extras.nativePss;
  if (legendSwap) legendSwap.hidden = !extras.swapPss;
  if (hint) hint.hidden = !ios;
}

async function persistMemoryExtras() {
  if (memoryExtrasSyncing) return;
  const session = activeSession();
  const nativeBox = $('opt-native-pss');
  const swapBox = $('opt-swap-pss');
  const logBox = $('opt-logcat');
  if (!session) return;
  if (!session.device || session.device.platform !== 'android') {
    syncMemoryExtrasUi(session);
    return;
  }
  const prev = sessionExtras(session);
  const extras = {
    nativePss: nativeBox ? nativeBox.checked : prev.nativePss,
    swapPss: swapBox ? swapBox.checked : prev.swapPss,
    logcat: logBox ? logBox.checked : prev.logcat,
  };
  session.extras = extras;
  console.info('[AndroidSample] extras ui runId=%s nativePss=%s swapPss=%s logcat=%s', session.runId, extras.nativePss, extras.swapPss, extras.logcat);
  if (prev.logcat !== extras.logcat) {
    console.info('[Logcat] toggle runId=%s enabled=%s package=%s', session.runId, extras.logcat, session.app && session.app.bundle);
  }
  syncMemoryExtrasUi(session);
  renderCollectorLog(extras.logcat ? (session.logs || []) : []);
  try {
    await api(`/api/v1/runs/${encodeURIComponent(session.runId)}/extras`, { method: 'POST', body: JSON.stringify(extras) });
  } catch (error) {
    console.error('[AndroidSample] extras update failed runId=%s err=%s', session.runId, error && error.message);
  }
  const shown = inspectSample(session) || session.samples.at(-1);
  if (shown) updateMetrics(session, shown, true);
  else updateDetailPane(session, shown);
}

function capabilityOf(session, id) {
  return ((session && session.capabilities) || []).find((item) => item.id === id) || null;
}

function metricSource(session, id) {
  const android = Boolean(session && session.device && session.device.platform === 'android');
  const bundle = (session && ((session.app && session.app.bundle) || session.packageId || session.bundle)) || '';
  if (id === 'logs') {
    if (!android) return 'iOS 无 logcat';
    return logcatEnabled(session)
      ? (bundle ? `logcat 过滤包名 ${bundle}` : 'logcat 过滤包名')
      : (bundle ? `需勾选后采集 · logcat 过滤包名 ${bundle}` : '需勾选后采集 logcat');
  }
  const cap = capabilityOf(session, id);
  if (cap && cap.source) return cap.source;
  const sources = {
    fps: android ? 'SurfaceFlinger / gfxinfo' : 'DVT graphics',
    cpu: '进程采样 · 全核占比',
    memory: android ? 'dumpsys meminfo TOTAL PSS' : 'physFootprint',
    gpu: android ? '未接入 Android GPU 计数器' : 'DVT Device Utilization',
    network: 'Phase 1 未采集 UID 流量',
    battery: 'Phase 1 未采集电池接口',
  };
  return sources[id] || '能力探测';
}

function metricInterval(id) {
  const intervals = { fps: '1s 窗口', cpu: '1s', memory: '约 1s', gpu: '1s', network: '—', battery: '—', logs: '实时' };
  return intervals[id] || '1s';
}

function metricQualityInfo(session, id, hasValue) {
  const cap = capabilityOf(session, id);
  if (hasValue) return { state: 'available', label: 'AVAILABLE' };
  if (cap && cap.state === 'degraded') return { state: 'degraded', label: 'DEGRADED' };
  if (cap && cap.state === 'available') return { state: 'degraded', label: 'DEGRADED' };
  if (!session && (id === 'fps' || id === 'cpu' || id === 'memory')) {
    return { state: 'available', label: 'AVAILABLE' };
  }
  if (!session && id === 'gpu') return { state: 'degraded', label: 'DEGRADED' };
  return { state: 'unsupported', label: 'UNSUPPORTED' };
}

function setDetailMeta(metaId, qualityId, session, id, hasValue) {
  const quality = metricQualityInfo(session, id, hasValue);
  const source = metricSource(session, id);
  const interval = metricInterval(id);
  if ($(metaId)) $(metaId).textContent = `source：${source} · 采样：${interval} · quality：${quality.label}`;
  const badge = $(qualityId);
  if (badge) {
    badge.textContent = quality.label;
    badge.className = `quality ${quality.state}`;
  }
}

function setStatValue(id, text, ok) {
  const node = $(id);
  if (!node) return;
  node.textContent = text;
  node.classList.toggle('unsupported', !ok);
}

function percentile(values, ratio) {
  if (!values.length) return null;
  const ordered = [...values].sort((a, b) => a - b);
  const index = Math.min(ordered.length - 1, Math.max(0, Math.round((ordered.length - 1) * ratio)));
  return ordered[index];
}

function paintMetricTabs(tab) {
  const current = tab || state.metricTab || 'overview';
  state.metricTab = current;
  document.querySelectorAll('.metric-subtab').forEach((button) => {
    button.classList.toggle('active', button.dataset.metric === current);
  });
  document.querySelectorAll('.metric-pane').forEach((pane) => {
    pane.hidden = pane.id !== `metric-pane-${current}`;
  });
}

function setMetricTab(tab) {
  const session = activeSession();
  const next = tab || 'overview';
  state.metricTab = next;
  if (session) session.metricTab = next;
  paintMetricTabs(next);
  console.info('[MetricTab] switch tab=%s runId=%s samples=%s', next, session && session.runId, session && session.samples.length);
  if (next === 'logs' && session) renderCollectorLog(logcatEnabled(session) ? (session.logs || []) : []);
  else if (next === 'logs') renderCollectorLog([]);
  const shown = session ? (inspectSample(session) || session.samples.at(-1)) : null;
  requestAnimationFrame(() => {
    if (shown) updateMetrics(session, shown, true);
    else {
      updateDetailPane(session, shown);
      drawDetailCharts(session, (session && session.samples) || []);
    }
  });
}

function appendCollectorLog(runId, line) {
  const text = String(line || '').replace(/\r/g, ' ').trim();
  if (!text) return;
  const session = state.sessions[runId];
  if (!session) return;
  if (!Array.isArray(session.logs)) session.logs = [];
  session.logs.push(text);
  if (session.logs.length > 500) session.logs = session.logs.slice(-500);
  if (session.logs.length <= 3 || session.logs.length % 200 === 0) {
    console.info('[Logcat] append runId=%s n=%s preview=%s', runId, session.logs.length, text.slice(0, 80));
  }
  if (state.activeRunId !== runId) return;
  if (!logcatEnabled(session)) return;
  if ((session.metricTab || state.metricTab) === 'logs') renderCollectorLog(session.logs);
}

function renderCollectorLog(lines) {
  const box = $('collector-log');
  if (!box) return;
  const session = activeSession();
  const android = Boolean(session && session.device && session.device.platform === 'android');
  if (session && !android) {
    box.innerHTML = '<div class="empty-state">iOS 无 logcat。</div>';
    return;
  }
  if (!logcatEnabled(session)) {
    box.innerHTML = '<div class="empty-state">勾选「采集 logcat」后，才会拉取并显示该应用日志，避免影响性能。</div>';
    return;
  }
  if (!lines || !lines.length) {
    box.innerHTML = '<div class="empty-state">已开启采集，等待该应用 logcat…</div>';
    return;
  }
  const pinBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  box.innerHTML = lines.map((line) => `<div class="log-line">${escapeHtml(line)}</div>`).join('');
  if (pinBottom) box.scrollTop = box.scrollHeight;
}

function updateDetailPane(session, sample) {
  const extras = (session && session.extras) || {};
  const nums = (session && session.samples) || [];
  const fps = nums.map((item) => item.fps).filter(Number.isFinite);
  const cpu = nums.map((item) => item.cpu).filter(Number.isFinite);
  const memory = nums.map((item) => item.memory).filter(Number.isFinite);
  const gpu = nums.map((item) => item.gpu).filter(Number.isFinite);
  const down = nums.map((item) => (Number.isFinite(item.networkDown) ? item.networkDown : item.networkDownMiB)).filter(Number.isFinite);
  const up = nums.map((item) => (Number.isFinite(item.networkUp) ? item.networkUp : item.networkUpMiB)).filter(Number.isFinite);

  setDetailMeta('cpu-detail-meta', 'cpu-detail-quality', session, 'cpu', cpu.length > 0);
  setStatValue('cpu-app-detail', Number.isFinite(sample && sample.cpu) ? `${sample.cpu.toFixed(1)}%` : '--', Number.isFinite(sample && sample.cpu));
  setStatValue('cpu-sys-detail', '不支持（未采集整机 CPU）', false);
  if ($('cpu-detail-label')) $('cpu-detail-label').textContent = Number.isFinite(sample && sample.cpu) ? `${sample.cpu.toFixed(1)}% CPU` : '--';
  if ($('cpu-core-hint')) {
    $('cpu-core-hint').textContent = '未采集各核占用，避免用 0 填充。当前仅提供目标进程 App CPU。';
  }
  if ($('cpu-core-list')) $('cpu-core-list').innerHTML = '';

  setDetailMeta('mem-detail-meta', 'mem-detail-quality', session, 'memory', memory.length > 0);
  const memText = Number.isFinite(sample && sample.memory) ? `${sample.memory.toFixed(1)} MB` : '--';
  setStatValue('mem-app-detail', memText, Number.isFinite(sample && sample.memory));
  setStatValue('mem-pss-detail', memText, Number.isFinite(sample && sample.memory));
  const nativeOn = Boolean(extras.nativePss);
  const swapOn = Boolean(extras.swapPss);
  setStatValue(
    'mem-native-detail',
    nativeOn ? (Number.isFinite(sample && sample.nativePss) ? `${sample.nativePss.toFixed(1)} MB` : '--') : (session && session.device && session.device.platform === 'ios' ? 'iOS 不支持' : '未勾选采集'),
    nativeOn && Number.isFinite(sample && sample.nativePss),
  );
  setStatValue(
    'mem-swap-detail',
    swapOn ? (Number.isFinite(sample && sample.swapPss) ? `${sample.swapPss.toFixed(1)} MB` : '--') : (session && session.device && session.device.platform === 'ios' ? 'iOS 不支持' : '未勾选采集'),
    swapOn && Number.isFinite(sample && sample.swapPss),
  );
  if ($('mem-detail-label')) $('mem-detail-label').textContent = memText;

  setDetailMeta('fps-detail-meta', 'fps-detail-quality', session, 'fps', fps.length > 0);
  setStatValue('fps-now-detail', Number.isFinite(sample && sample.fps) ? sample.fps.toFixed(1) : '--', Number.isFinite(sample && sample.fps));
  setStatValue('fps-avg-detail', fps.length ? (fps.reduce((a, b) => a + b, 0) / fps.length).toFixed(1) : '--', fps.length > 0);
  setStatValue('fps-min-detail', fps.length ? Math.min(...fps).toFixed(1) : '--', fps.length > 0);
  setStatValue('fps-max-detail', fps.length ? Math.max(...fps).toFixed(1) : '--', fps.length > 0);
  const p95 = percentile(fps, 0.95);
  setStatValue('fps-p95-detail', p95 == null ? '--' : p95.toFixed(1), p95 != null);
  if ($('fps-detail-label')) $('fps-detail-label').textContent = Number.isFinite(sample && sample.fps) ? `${sample.fps.toFixed(1)} FPS` : '--';

  const gpuOk = gpu.length > 0;
  setDetailMeta('gpu-detail-meta', 'gpu-detail-quality', session, 'gpu', gpuOk);
  setStatValue('gpu-now-detail', gpuOk && Number.isFinite(sample && sample.gpu) ? `${sample.gpu.toFixed(1)}%` : '不支持', gpuOk && Number.isFinite(sample && sample.gpu));
  if ($('gpu-unsupported')) $('gpu-unsupported').hidden = gpuOk;
  if ($('gpu-chart-wrap')) $('gpu-chart-wrap').hidden = !gpuOk;
  if ($('gpu-unsupported-reason')) $('gpu-unsupported-reason').textContent = `${metricSource(session, 'gpu')}。不会用 0 填充。`;
  if ($('gpu-detail-label')) $('gpu-detail-label').textContent = gpuOk && Number.isFinite(sample && sample.gpu) ? `${sample.gpu.toFixed(1)}%` : '--';

  const netOk = down.length > 0 || up.length > 0;
  setDetailMeta('net-detail-meta', 'net-detail-quality', session, 'network', netOk);
  const downNow = sample && (Number.isFinite(sample.networkDown) ? sample.networkDown : sample.networkDownMiB);
  const upNow = sample && (Number.isFinite(sample.networkUp) ? sample.networkUp : sample.networkUpMiB);
  setStatValue('net-up-detail', netOk && Number.isFinite(upNow) ? `${upNow.toFixed(1)} MB` : '不支持', netOk && Number.isFinite(upNow));
  setStatValue('net-down-detail', netOk && Number.isFinite(downNow) ? `${downNow.toFixed(1)} MB` : '不支持', netOk && Number.isFinite(downNow));
  setStatValue('net-up-total', netOk && up.length ? `${up[up.length - 1].toFixed(1)} MB` : '不支持', netOk);
  setStatValue('net-down-total', netOk && down.length ? `${down[down.length - 1].toFixed(1)} MB` : '不支持', netOk);
  if ($('net-unsupported')) $('net-unsupported').hidden = netOk;
  if ($('net-chart-wrap')) $('net-chart-wrap').hidden = !netOk;

  setDetailMeta('bat-detail-meta', 'bat-detail-quality', session, 'battery', false);
  setStatValue('bat-level-detail', '不支持', false);
  setStatValue('bat-charge-detail', '不支持', false);
  const logOn = logcatEnabled(session);
  setDetailMeta('log-detail-meta', 'log-detail-quality', session, 'logs', logOn);

  const tab = session.metricTab || state.metricTab || 'overview';
  if (tab !== 'overview' && tab !== 'logs' && tab !== 'battery') drawDetailCharts(session, nums);
}

function drawDetailCharts(session, samples) {
  const extras = (session && session.extras) || {};
  const list = samples || [];
  const times = sampleTimes(list);
  const marker = inspectMarkerTime(session, list);
  const tab = (session && session.metricTab) || state.metricTab || 'overview';
  if (tab === 'cpu' && $('cpu-detail-chart')) {
    const cpuValues = list.map((item) => item.cpu);
    const cpuY = chartYAxis('pct', [cpuValues]);
    drawChart($('cpu-detail-chart'), {
      series: [{ values: cpuValues, color: '#5f91d3', label: 'App CPU' }],
      times,
      yMin: cpuY.yMin,
      yMax: cpuY.yMax,
      yStep: cpuY.step,
      ySuffix: '%',
      markerTime: marker,
    });
  }
  if (tab === 'fps' && $('fps-detail-chart')) {
    const fpsValues = list.map((item) => item.fps);
    const fpsY = chartYAxis('fps', [fpsValues]);
    drawChart($('fps-detail-chart'), {
      series: [{ values: fpsValues, color: '#ed775f', label: 'FPS' }],
      times,
      yMin: fpsY.yMin,
      yMax: fpsY.yMax,
      yStep: fpsY.step,
      ySuffix: 'FPS',
      markerTime: marker,
    });
  }
  if (tab === 'memory' && $('mem-detail-chart')) {
    const memValues = list.map((item) => item.memory);
    const nativeValues = list.map((item) => item.nativePss);
    const swapValues = list.map((item) => item.swapPss);
    const memPool = [
      ...memValues.filter(Number.isFinite),
      ...(extras.nativePss ? nativeValues.filter(Number.isFinite) : []),
      ...(extras.swapPss ? swapValues.filter(Number.isFinite) : []),
    ];
    const memSeries = [{ values: memValues, color: '#2c9b6d', label: 'TOTAL PSS' }];
    if (extras.nativePss) memSeries.push({ values: nativeValues, color: '#6b5ce7', label: 'Native PSS' });
    if (extras.swapPss) memSeries.push({ values: swapValues, color: '#e6a84a', label: 'Swap PSS' });
    const memY = chartYAxis('mem', [memPool]);
    drawChart($('mem-detail-chart'), {
      series: memSeries,
      times,
      yMin: memY.yMin,
      yMax: memY.yMax,
      yStep: memY.step,
      ySuffix: 'MB',
      markerTime: marker,
    });
  }
  if (tab === 'gpu' && $('gpu-detail-chart')) {
    const gpuValues = list.map((item) => item.gpu);
    if (gpuValues.some(Number.isFinite)) {
      const gpuY = chartYAxis('pct', [gpuValues]);
      drawChart($('gpu-detail-chart'), {
        series: [{ values: gpuValues, color: '#6b5ce7', label: 'GPU' }],
        times,
        yMin: gpuY.yMin,
        yMax: gpuY.yMax,
        yStep: gpuY.step,
        ySuffix: '%',
        markerTime: marker,
      });
    }
  }
  if (tab === 'network' && $('net-detail-chart')) {
    const downValues = list.map((item) => (Number.isFinite(item.networkDown) ? item.networkDown : item.networkDownMiB));
    const upValues = list.map((item) => (Number.isFinite(item.networkUp) ? item.networkUp : item.networkUpMiB));
    if (downValues.some(Number.isFinite) || upValues.some(Number.isFinite)) {
      const netY = chartYAxis('mem', [downValues, upValues]);
      drawChart($('net-detail-chart'), {
        series: [
          { values: downValues, color: '#2c9b6d', label: 'Download' },
          { values: upValues, color: '#5f91d3', label: 'Upload' },
        ],
        times,
        yMin: netY.yMin,
        yMax: netY.yMax,
        yStep: netY.step,
        ySuffix: 'MB',
        markerTime: marker,
      });
    }
  }
}

function inspectSample(session) {
  if (!session || !Array.isArray(session.samples) || session.inspectIndex == null) return null;
  const index = Math.max(0, Math.min(session.inspectIndex, session.samples.length - 1));
  return session.samples[index] || null;
}

function inspectMarkerTime(session, samples) {
  if (!session || session.inspectIndex == null) return null;
  const list = samples || session.samples || [];
  if (!list.length) return null;
  const times = sampleTimes(list);
  const index = Math.max(0, Math.min(session.inspectIndex, times.length - 1));
  return times[index];
}

function formatInspectClock(sample) {
  if (sample && Number.isFinite(sample.time) && sample.time > 1e9) {
    const date = new Date(sample.time * 1000);
    if (!Number.isNaN(date.getTime())) {
      return date.toLocaleTimeString('zh-CN', { hour12: false });
    }
  }
  if (sample && Number.isFinite(sample.elapsedMs)) return formatChartTime(sample.elapsedMs);
  return '--';
}

function fmtInspectValue(value, suffix, empty) {
  if (!Number.isFinite(value)) return empty || '不支持';
  return `${value.toFixed(1)}${suffix || ''}`;
}

function hideTimelineInspect() {
  const panel = $('timeline-inspect');
  if (panel) panel.hidden = true;
  const grid = document.querySelector('.metric-grid');
  if (grid) grid.classList.remove('is-inspecting');
}

function renderInspectPanel(session, sample) {
  const panel = $('timeline-inspect');
  if (!panel) return;
  const inspecting = Boolean(session && session.inspectIndex != null && sample);
  panel.hidden = !inspecting;
  const grid = document.querySelector('.metric-grid');
  if (grid) grid.classList.toggle('is-inspecting', inspecting);
  if (!inspecting) return;
  const extras = session.extras || {};
  $('inspect-clock').textContent = formatInspectClock(sample);
  $('inspect-fps').textContent = fmtInspectValue(sample.fps, '', '--');
  $('inspect-cpu').textContent = fmtInspectValue(sample.cpu, '%', '--');
  $('inspect-memory').textContent = fmtInspectValue(sample.memory, ' MB', '--');
  $('inspect-gpu').textContent = fmtInspectValue(sample.gpu, '%', '不支持');
  const down = Number.isFinite(sample.networkDown) ? sample.networkDown : sample.networkDownMiB;
  const up = Number.isFinite(sample.networkUp) ? sample.networkUp : sample.networkUpMiB;
  $('inspect-network').textContent = Number.isFinite(down) || Number.isFinite(up)
    ? `↓${fmtInspectValue(down, '', '--')} ↑${fmtInspectValue(up, '', '--')}`
    : '不支持';
  const nativeCell = $('inspect-native-cell');
  const swapCell = $('inspect-swap-cell');
  if (nativeCell) {
    nativeCell.hidden = !extras.nativePss;
    if (extras.nativePss) $('inspect-native').textContent = fmtInspectValue(sample.nativePss, ' MB', '--');
  }
  if (swapCell) {
    swapCell.hidden = !extras.swapPss;
    if (extras.swapPss) $('inspect-swap').textContent = fmtInspectValue(sample.swapPss, ' MB', '--');
  }
}

function setInspectIndex(session, index, source) {
  if (!session || !session.samples.length) return;
  session.inspectIndex = Math.max(0, Math.min(index, session.samples.length - 1));
  const sample = inspectSample(session);
  console.info(
    '[TimelineSync] inspect runId=%s index=%s source=%s elapsedMs=%s fps=%s cpu=%s memory=%s',
    session.runId,
    session.inspectIndex,
    source,
    sample && sample.elapsedMs,
    sample && sample.fps,
    sample && sample.cpu,
    sample && sample.memory,
  );
  renderInspectPanel(session, sample);
  updateMetrics(session, sample, true);
}

function clearInspect(session) {
  if (!session) return;
  session.inspectIndex = null;
  console.info('[TimelineSync] live runId=%s', session.runId);
  hideTimelineInspect();
  const latest = session.samples.at(-1);
  if (latest) updateMetrics(session, latest, true);
  else resetMetricDisplay(session);
}

function nearestSampleIndex(samples, timeMs) {
  const times = sampleTimes(samples);
  let best = 0;
  let bestDist = Infinity;
  times.forEach((time, index) => {
    const dist = Math.abs(time - timeMs);
    if (dist < bestDist) {
      bestDist = dist;
      best = index;
    }
  });
  return best;
}

function onChartClick(event) {
  const session = activeSession();
  if (!session || !session.samples.length) return;
  const canvas = event.currentTarget;
  const meta = canvas._chartMeta;
  if (!meta) return;
  const x = event.offsetX;
  if (x < meta.pad.l || x > meta.pad.l + meta.plotW) return;
  const timeMs = meta.t0 + ((x - meta.pad.l) / meta.plotW) * meta.tSpan;
  setInspectIndex(session, nearestSampleIndex(session.samples, timeMs), 'chart');
}

function onEventListClick(event) {
  const button = event.target.closest('.event-item');
  if (!button) return;
  const session = activeSession();
  if (!session) return;
  const raw = button.dataset.inspect;
  if (raw === '' || raw == null) return;
  setInspectIndex(session, Number(raw), 'event');
}

function updateMetrics(session, sample, forceCharts) {
  const nums = session.samples;
  const fps = nums.map((x) => x.fps).filter(Number.isFinite);
  const memory = nums.map((x) => x.memory).filter(Number.isFinite);
  $('fps-value').textContent = Number.isFinite(sample.fps) ? sample.fps.toFixed(1) : '--';
  $('fps-average').textContent = fps.length ? (fps.reduce((a, b) => a + b, 0) / fps.length).toFixed(1) : '--';
  $('fps-min').textContent = fps.length ? Math.min(...fps).toFixed(1) : '--';
  $('cpu-value').textContent = Number.isFinite(sample.cpu) ? `${sample.cpu.toFixed(1)}%` : '--';
  $('cpu-bar').style.width = `${Math.min(sample.cpu || 0, 100)}%`;
  const cpu = nums.map((x) => x.cpu).filter(Number.isFinite);
  const le25 = cpu.length ? (cpu.filter((v) => v <= 25).length / cpu.length) * 100 : null;
  const le50 = cpu.length ? (cpu.filter((v) => v <= 50).length / cpu.length) * 100 : null;
  if ($('cpu-le25')) $('cpu-le25').textContent = le25 == null ? '--' : `${le25.toFixed(1)}%`;
  if ($('cpu-le50')) $('cpu-le50').textContent = le50 == null ? '--' : `${le50.toFixed(1)}%`;
  if (cpu.length === 1 || cpu.length % 30 === 0) {
    console.info('[CpuShare] update samples=%s le25=%s le50=%s', cpu.length, le25 == null ? null : le25.toFixed(1), le50 == null ? null : le50.toFixed(1));
  }
  $('memory-value').textContent = Number.isFinite(sample.memory) ? `${sample.memory.toFixed(1)} MB` : '--';
  $('memory-peak').textContent = memory.length ? `${Math.max(...memory).toFixed(1)} MB` : '--';
  const extras = session.extras || {};
  if ($('memory-native')) {
    $('memory-native').textContent = Number.isFinite(sample.nativePss) ? `${sample.nativePss.toFixed(1)} MB` : '--';
  }
  if ($('memory-swap')) {
    $('memory-swap').textContent = Number.isFinite(sample.swapPss) ? `${sample.swapPss.toFixed(1)} MB` : '--';
  }
  syncMemoryExtrasUi(session);
  $('gpu-value').textContent = Number.isFinite(sample.gpu) ? `${sample.gpu.toFixed(1)}%` : '不支持';
  $('sample-count').textContent = nums.length;
  $('chart-fps-label').textContent = Number.isFinite(sample.fps) ? `${sample.fps.toFixed(1)} FPS` : '--';
  $('chart-resource-label').textContent = Number.isFinite(sample.cpu) ? `${sample.cpu.toFixed(1)}% CPU` : '等待数据';
  const memBits = [];
  if (Number.isFinite(sample.memory)) memBits.push(`${sample.memory.toFixed(1)} MB`);
  if (extras.nativePss && Number.isFinite(sample.nativePss)) memBits.push(`N ${sample.nativePss.toFixed(1)}`);
  if (extras.swapPss && Number.isFinite(sample.swapPss)) memBits.push(`S ${sample.swapPss.toFixed(1)}`);
  if ($('chart-memory-label')) $('chart-memory-label').textContent = memBits.join(' · ') || '--';
  if (session.inspectIndex != null) renderInspectPanel(session, inspectSample(session) || sample);
  updateDetailPane(session, sample);
  if (forceCharts || nums.length % 2 === 0) drawCharts(nums, extras, inspectMarkerTime(session, nums));
}

function drawLine(canvas, values, color, min, max) {
  drawChart(canvas, {
    series: [{ values, color, label: '' }],
    times: values.map((_, i) => i * 1000),
    yMin: min,
    yMax: max,
    ySuffix: '',
  });
}

function finiteValues(lists) {
  const nums = [];
  (lists || []).forEach((list) => {
    (list || []).forEach((value) => {
      if (Number.isFinite(value)) nums.push(value);
    });
  });
  return nums;
}

function niceCeil(value, steps) {
  if (!Number.isFinite(value) || value <= 0) return steps;
  const raw = value * 1.08;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const norm = raw / mag;
  let nice = 1;
  if (norm > 5) nice = 10;
  else if (norm > 2) nice = 5;
  else if (norm > 1) nice = 2;
  const ceil = nice * mag;
  const tick = ceil / steps;
  const tickMag = 10 ** Math.floor(Math.log10(tick || 1));
  let tickNice = 1;
  const tickNorm = tick / tickMag;
  if (tickNorm > 5) tickNice = 10;
  else if (tickNorm > 2) tickNice = 5;
  else if (tickNorm > 1) tickNice = 2;
  return Math.max(steps * tickNice * tickMag, ceil);
}

function fpsAxisMax(values) {
  const peak = Math.max(0, ...values.filter(Number.isFinite));
  if (peak <= 62) return 60;
  if (peak <= 90) return 90;
  if (peak <= 120) return 120;
  if (peak <= 144) return 144;
  if (peak <= 165) return 165;
  return Math.ceil(peak / 30) * 30;
}

function cpuAxisMax(values) {
  const peak = Math.max(0, ...values.filter(Number.isFinite));
  if (peak <= 50) return 50;
  if (peak <= 100) return 100;
  return Math.min(400, Math.ceil(peak / 25) * 25);
}

function memAxisMax(values) {
  const peak = Math.max(0, ...values.filter(Number.isFinite));
  if (peak <= 0) return 64;
  return niceCeil(peak, 4);
}

function chartYAxis(kind, lists) {
  const nums = finiteValues(lists);
  const peak = nums.length ? Math.max(0, ...nums) : 0;
  let yMax = 10;
  if (kind === 'fps') yMax = fpsAxisMax(nums);
  else if (kind === 'pct') yMax = cpuAxisMax(nums);
  else if (kind === 'mem') yMax = memAxisMax(nums);
  else yMax = Math.max(10, niceCeil(peak, 4));
  const step = yMax / 4;
  const prev = chartYAxis._last || {};
  if (prev[kind] !== yMax) {
    console.info('[ChartAxis] fixed kind=%s n=%s peak=%s yMin=0 yMax=%s step=%s', kind, nums.length, peak, yMax, step);
    chartYAxis._last = { ...prev, [kind]: yMax };
  }
  return { yMin: 0, yMax, step };
}

function yTickValues(yMin, yMax, step) {
  if (step > 0) {
    const ticks = [];
    for (let value = yMin; value <= yMax + step * 0.001; value = Number((value + step).toFixed(10))) {
      ticks.push(value);
    }
    if (ticks.length >= 2) return ticks;
  }
  return [0, 1, 2, 3, 4].map((i) => yMin + ((yMax - yMin) * i) / 4);
}

function formatAxisLabel(value, suffix, step) {
  const decimals = step > 0 && step < 1 ? 2 : step > 0 && step < 2 ? 1 : 0;
  if (suffix === '%') return `${value.toFixed(decimals)}%`;
  if (suffix === 'FPS') return value.toFixed(decimals);
  const text = value.toFixed(value >= 100 || decimals === 0 ? 0 : decimals);
  return suffix ? `${text} ${suffix}` : text;
}

function seriesMean(values) {
  const nums = (values || []).filter(Number.isFinite);
  if (!nums.length) return null;
  return nums.reduce((sum, value) => sum + value, 0) / nums.length;
}

function formatAvgLabel(avg, suffix, seriesLabel, seriesCount) {
  const unit = suffix === '%' ? '%' : suffix === 'FPS' ? '' : suffix ? ` ${suffix}` : '';
  const short = String(seriesLabel || '').replace(/\s*PSS$/i, '').trim();
  const name = seriesCount > 1 && short ? `${short}平均 ` : '平均 ';
  return `${name}${avg.toFixed(1)}${unit}`;
}

function formatChartTime(ms) {
  const sec = Math.max(0, Math.floor((ms || 0) / 1000));
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return `${m}:${String(s).padStart(2, '0')}`;
}

function sampleTimes(samples) {
  const first = samples.find((item) => Number.isFinite(item.time));
  const origin = first ? first.time : null;
  return samples.map((item, index) => {
    if (Number.isFinite(item.elapsedMs)) return item.elapsedMs;
    if (origin != null && Number.isFinite(item.time)) return (item.time - origin) * 1000;
    return index * 1000;
  });
}

function drawChart(canvas, options) {
  if (!canvas) return;
  const series = options.series || [];
  const times = options.times || [];
  const yMin = options.yMin ?? 0;
  const yMax = options.yMax === yMin ? yMin + 1 : options.yMax;
  const yStep = options.yStep;
  const ySuffix = options.ySuffix || '';
  const context = canvas.getContext('2d');
  const width = canvas.clientWidth || 500;
  const height = canvas.height;
  canvas.width = width * devicePixelRatio;
  canvas.height = height * devicePixelRatio;
  context.scale(devicePixelRatio, devicePixelRatio);
  context.clearRect(0, 0, width, height);
  const pad = { l: 54, r: 82, t: 18, b: 22 };
  const plotW = Math.max(40, width - pad.l - pad.r);
  const plotH = Math.max(40, height - pad.t - pad.b);
  const t0 = times.length ? times[0] : 0;
  const t1 = times.length ? times[times.length - 1] : 1000;
  const tSpan = Math.max(1000, t1 - t0);
  const xOf = (t) => pad.l + ((t - t0) / tSpan) * plotW;
  const yOf = (v) => pad.t + (1 - (Math.max(yMin, Math.min(yMax, v)) - yMin) / (yMax - yMin)) * plotH;

  context.strokeStyle = '#e8efec';
  context.fillStyle = '#8a999b';
  context.font = '10px Cascadia Mono, Consolas, Microsoft YaHei, monospace';
  context.lineWidth = 1;
  const yTicks = yTickValues(yMin, yMax, yStep);
  yTicks.forEach((value, index) => {
    const y = yOf(value);
    context.beginPath();
    context.moveTo(pad.l, y);
    context.lineTo(pad.l + plotW, y);
    context.stroke();
    context.textAlign = 'right';
    context.textBaseline = 'middle';
    const label = formatAxisLabel(value, ySuffix, yStep);
    context.fillText(index === yTicks.length - 1 && ySuffix === 'FPS' ? `${label} FPS` : label, pad.l - 6, y);
  });
  const xTicks = 4;
  context.textAlign = 'center';
  context.textBaseline = 'top';
  for (let i = 0; i <= xTicks; i += 1) {
    const t = t0 + (tSpan * i) / xTicks;
    const x = xOf(t);
    context.strokeStyle = '#e8efec';
    context.beginPath();
    context.moveTo(x, pad.t);
    context.lineTo(x, pad.t + plotH);
    context.stroke();
    context.fillStyle = '#8a999b';
    context.fillText(formatChartTime(t), x, pad.t + plotH + 6);
  }

  const endLabels = [];
  series.forEach((item) => {
    const values = item.values || [];
    context.strokeStyle = item.color;
    context.lineWidth = 2.5;
    context.beginPath();
    let started = false;
    let last = null;
    values.forEach((value, index) => {
      if (!Number.isFinite(value)) {
        started = false;
        return;
      }
      const x = xOf(times[index] ?? index * 1000);
      const y = yOf(value);
      last = { x, y, value };
      if (started) context.lineTo(x, y);
      else {
        context.moveTo(x, y);
        started = true;
      }
    });
    context.stroke();
    if (item.label && last) endLabels.push({ ...last, color: item.color, text: item.label });
  });

  const avgColor = '#8a999b';
  const avgLabels = [];
  series.forEach((item) => {
    const avg = seriesMean(item.values);
    if (avg == null) return;
    const y = yOf(avg);
    context.save();
    context.setLineDash([6, 4]);
    context.strokeStyle = avgColor;
    context.globalAlpha = 0.95;
    context.lineWidth = 1.4;
    context.beginPath();
    context.moveTo(pad.l, y);
    context.lineTo(pad.l + plotW, y);
    context.stroke();
    context.restore();
    avgLabels.push({
      y,
      color: avgColor,
      text: formatAvgLabel(avg, ySuffix, item.label, series.length),
    });
  });
  avgLabels.sort((a, b) => a.y - b.y);
  for (let i = 1; i < avgLabels.length; i += 1) {
    if (avgLabels[i].y - avgLabels[i - 1].y < 14) avgLabels[i].y = avgLabels[i - 1].y + 14;
  }
  avgLabels.forEach((label) => {
    context.fillStyle = label.color;
    context.textAlign = 'left';
    context.textBaseline = 'bottom';
    context.font = '600 10px Segoe UI, PingFang SC, Microsoft YaHei, sans-serif';
    context.fillText(label.text, pad.l + 6, Math.max(pad.t + 11, label.y - 3));
  });
  if (avgLabels.length && (times.length <= 4 || times.length % 30 === 0)) {
    console.info(
      '[ChartAvg] update chart=%s samples=%s avgs=%s',
      canvas.id || 'chart',
      times.length,
      avgLabels.map((item) => item.text).join(' | '),
    );
  }

  endLabels.sort((a, b) => a.y - b.y);
  for (let i = 1; i < endLabels.length; i += 1) {
    if (endLabels[i].y - endLabels[i - 1].y < 14) {
      endLabels[i].y = endLabels[i - 1].y + 14;
    }
  }
  endLabels.forEach((label) => {
    context.fillStyle = label.color;
    context.textAlign = 'left';
    context.textBaseline = 'bottom';
    context.font = '600 11px Segoe UI, PingFang SC, Microsoft YaHei, sans-serif';
    context.fillText(label.text, Math.min(label.x + 6, width - pad.r + 4), label.y - 2);
  });

  canvas._chartMeta = { pad, t0, tSpan, plotW, plotH };
  if (Number.isFinite(options.markerTime)) {
    const x = xOf(options.markerTime);
    context.save();
    context.strokeStyle = '#ed775f';
    context.lineWidth = 1.5;
    context.setLineDash([5, 4]);
    context.beginPath();
    context.moveTo(x, pad.t);
    context.lineTo(x, pad.t + plotH);
    context.stroke();
    context.setLineDash([]);
    context.fillStyle = '#ed775f';
    context.font = '600 10px Cascadia Mono, Consolas, Microsoft YaHei, monospace';
    context.textAlign = 'center';
    context.textBaseline = 'top';
    context.fillText(formatChartTime(options.markerTime), Math.max(pad.l + 18, Math.min(x, pad.l + plotW - 18)), pad.t + 3);
    context.restore();
  }
}

function drawCharts(samples, extras, markerTime) {
  const list = samples || [];
  const flags = extras || {};
  const times = sampleTimes(list);
  const fpsValues = list.map((x) => x.fps);
  const cpuValues = list.map((x) => x.cpu);
  const memValues = list.map((x) => x.memory);
  const nativeValues = list.map((x) => x.nativePss);
  const swapValues = list.map((x) => x.swapPss);
  const fpsY = chartYAxis('fps', [fpsValues]);
  drawChart($('fps-chart'), {
    series: [{ values: fpsValues, color: '#ed775f', label: 'FPS' }],
    times,
    yMin: fpsY.yMin,
    yMax: fpsY.yMax,
    yStep: fpsY.step,
    ySuffix: 'FPS',
    markerTime,
  });
  const cpuY = chartYAxis('pct', [cpuValues]);
  drawChart($('resource-chart'), {
    series: [{ values: cpuValues, color: '#5f91d3', label: 'CPU' }],
    times,
    yMin: cpuY.yMin,
    yMax: cpuY.yMax,
    yStep: cpuY.step,
    ySuffix: '%',
    markerTime,
  });
  const memCanvas = $('memory-chart');
  if (!memCanvas) return;
  const memPool = [
    ...memValues.filter(Number.isFinite),
    ...(flags.nativePss ? nativeValues.filter(Number.isFinite) : []),
    ...(flags.swapPss ? swapValues.filter(Number.isFinite) : []),
  ];
  const memSeries = [{ values: memValues, color: '#2c9b6d', label: 'TOTAL PSS' }];
  if (flags.nativePss) memSeries.push({ values: nativeValues, color: '#6b5ce7', label: 'Native PSS' });
  if (flags.swapPss) memSeries.push({ values: swapValues, color: '#e6a84a', label: 'Swap PSS' });
  const memY = chartYAxis('mem', [memPool]);
  drawChart(memCanvas, {
    series: memSeries,
    times,
    yMin: memY.yMin,
    yMax: memY.yMax,
    yStep: memY.step,
    ySuffix: 'MB',
    markerTime,
  });
}

function addEvent(runId, title, message) {
  const session = state.sessions[runId];
  if (!session) return;
  session.eventCount += 1;
  const sampleIndex = session.samples.length ? session.samples.length - 1 : null;
  session.events.unshift({ title, message, sampleIndex });
  if (state.activeRunId !== runId) return;
  $('event-count').textContent = String(session.eventCount);
  if ($('event-list').querySelector('.empty-state')) $('event-list').innerHTML = '';
  $('event-list').insertAdjacentHTML('afterbegin', `<button type="button" class="event-item" data-inspect="${sampleIndex == null ? '' : sampleIndex}"><i></i><div><b>${escapeHtml(title)}</b><small>${escapeHtml(message)}</small></div></button>`);
}

function closeSession(runId) {
  const session = state.sessions[runId];
  if (!session) return;
  if (session.stream) session.stream.close();
  delete state.sessions[runId];
  if (state.activeRunId === runId) {
    const next = sessionList()[0];
    state.activeRunId = next ? next.runId : null;
  }
  stopTimerIfIdle();
  paintDeviceList();
  updateStartButton();
  if (visibleSessions().length) renderActiveMonitor();
  else emptyMonitor();
}

async function renderReports() {
  const container = $('report-list');
  if (!container) return;
  container.innerHTML = '<div class="empty-state">正在加载报告记录…</div>';
  try {
    const result = await api('/api/v1/reports');
    if (!result.reports.length) {
      container.innerHTML = '<div class="empty-state">暂无历史报告</div>';
      return;
    }
    container.innerHTML = result.reports.map((report) => {
      const date = new Date((report.createdAt || report.startedAtMs / 1000 || 0) * 1000).toLocaleString();
      const status = report.reportReady ? '报告可用' : `记录已保存 · ${report.error || '报告不可用'}`;
      const runId = report.runId || report.sessionId;
      return `<article class="report-row"><div><b>${escapeHtml(reportListTitle(report))}</b><small>${escapeHtml(report.bundle || report.packageId || '未知应用')} · ${date}</small></div><div class="report-row-meta"><span>${status}</span>${report.reportReady ? `<button class="secondary-button report-open" data-session="${runId}">打开报告</button>` : '<em>无 HTML 报告</em>'}</div></article>`;
    }).join('');
    container.querySelectorAll('.report-open').forEach((button) => {
      button.addEventListener('click', () => window.open(`/api/v1/runs/${button.dataset.session}/report`, '_blank'));
    });
  } catch (error) {
    container.innerHTML = `<div class="empty-state">${error.message}</div>`;
  }
}

function deviceLabel(session) {
  const device = (session && session.device) || {};
  return device.name || device.model || device.id || '该设备';
}

function reportSavedMessage(result, reason, session) {
  const name = deviceLabel(session);
  const discarded = Boolean(result && (result.discarded || result.status === 'discarded'));
  if (discarded) return `${name} 的本次监测数据未保存。`;
  if (reason === 'unplug') {
    const saved = result && result.reportReady ? '报告已保存' : '监测记录已保存';
    const extra = result && result.error ? `\n原因：${result.error}` : '';
    return `${name} 已断开，已停止该设备的监测。\n${saved}，可在「检测报告」中查看。${extra}`;
  }
  if (result && result.reportReady) return `${name} 的报告已保存，可在「检测报告」中查看。`;
  if (result && result.wantReport === false) return `${name} 的监测记录已保存，未生成检测报告。`;
  const extra = result && result.error ? `\n原因：${result.error}` : '';
  return `${name} 的监测记录已保存，可在「检测报告」中查看。${extra}`;
}

async function showReportSaved(runId, result, reason) {
  const session = state.sessions[runId];
  const discarded = Boolean(result && (result.discarded || result.status === 'discarded'));
  const message = reportSavedMessage(result, reason, session);
  const title = discarded ? '监测已结束' : (reason === 'unplug' ? '设备已断开' : '监测已结束');
  closeSession(runId);
  if (visibleSessions().length) renderActiveMonitor();
  else emptyMonitor();
  if (reason === 'background' || reason === 'error') {
    if (visibleSessions().length) renderActiveMonitor();
    else emptyMonitor();
    setView('overview');
    return;
  }
  console.info('[StopMonitor] done runId=%s reason=%s discarded=%s reportReady=%s', runId, reason, discarded, result && result.reportReady);
  await showAppDialog(message, title);
}

function terminalRunStatus(status) {
  return status === 'completed' || status === 'failed' || status === 'discarded';
}

async function finishSession(runId, reason) {
  const session = state.sessions[runId];
  if (!session) return;
  const started = Date.now();
  while (Date.now() - started < 60000) {
    const pushed = session.finishPayload;
    if (pushed && terminalRunStatus(pushed.status)) {
      await showReportSaved(runId, pushed, reason);
      return;
    }
    try {
      const result = await api(`/api/v1/runs/${runId}`);
      if (Array.isArray(result.samples) && result.samples.length > (session.samples || []).length) {
        session.samples = result.samples;
      }
      if (state.activeRunId === runId) {
        $('sample-count').textContent = String((session.samples || []).length);
        paintMonitorStatus(session);
      }
      if (terminalRunStatus(result.status) || result.discarded) {
        await showReportSaved(runId, result, reason);
        return;
      }
    } catch (error) {
      if (error.status === 404) {
        console.info('[StopMonitor] finish 404 treat discarded runId=%s', runId);
        await showReportSaved(runId, { status: 'discarded', reportReady: false, discarded: true }, reason);
        return;
      }
      addEvent(runId, '收尾失败', error.message);
      console.warn('[StopMonitor] finish poll failed runId=%s err=%s', runId, error.message);
      await showReportSaved(runId, { reportReady: false, error: error.message }, reason);
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  await showReportSaved(runId, { reportReady: false, error: '报告生成超时，记录已保留' }, reason);
}

function sampleNumbers(samples, key) {
  return (samples || []).map((item) => item[key]).filter((value) => Number.isFinite(value));
}

function meanNumber(values) {
  if (!values.length) return null;
  return values.reduce((sum, value) => sum + value, 0) / values.length;
}

function formatDurationCn(ms) {
  const total = Math.max(0, Math.floor((ms || 0) / 1000));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  if (hours) return `${hours}小时${minutes}分${seconds}秒`;
  if (minutes) return `${minutes}分${seconds}秒`;
  return `${seconds}秒`;
}

function fillStopDialog(session) {
  const samples = session.samples || [];
  const elapsed = (session.endedAt || Date.now()) - session.startedAt;
  const fps = sampleNumbers(samples, 'fps');
  const cpu = sampleNumbers(samples, 'cpu');
  const mem = sampleNumbers(samples, 'memory');
  const fpsAvg = meanNumber(fps);
  const cpuAvg = meanNumber(cpu);
  const memAvg = meanNumber(mem);
  $('stop-duration').textContent = formatDurationCn(elapsed);
  $('stop-samples').textContent = String(samples.length);
  $('stop-fps-avg').textContent = fpsAvg == null ? '--' : fpsAvg.toFixed(1);
  $('stop-fps-min').textContent = fps.length ? Math.min(...fps).toFixed(1) : '--';
  $('stop-cpu-avg').textContent = cpuAvg == null ? '--' : `${cpuAvg.toFixed(1)}%`;
  $('stop-mem-avg').textContent = memAvg == null ? '--' : `${memAvg.toFixed(1)} MB`;
}

let stopDialogCtl = null;

function dismissStopDialog(result) {
  const dialog = $('stop-dialog');
  if (dialog) dialog.hidden = true;
  const ctl = stopDialogCtl;
  stopDialogCtl = null;
  if (ctl && typeof ctl.resolve === 'function') ctl.resolve(result);
}

function syncStopReportOption() {
  const save = $('stop-save');
  const report = $('stop-report');
  if (!save || !report) return;
  if (!save.checked) {
    report.checked = false;
    report.disabled = true;
  } else {
    report.disabled = false;
  }
}

function showStopDialog(session) {
  const dialog = $('stop-dialog');
  if (!dialog) {
    return Promise.resolve({ save: true, report: true });
  }
  fillStopDialog(session);
  const save = $('stop-save');
  const report = $('stop-report');
  if (save) save.checked = true;
  if (report) {
    report.checked = true;
    report.disabled = false;
  }
  return new Promise((resolve) => {
    stopDialogCtl = { resolve, runId: session.runId };
    dialog.hidden = false;
    const confirm = $('stop-dialog-confirm');
    if (confirm) confirm.focus();
    console.info('[StopMonitor] dialog open runId=%s samples=%s elapsedMs=%s', session.runId, (session.samples || []).length, Date.now() - session.startedAt);
  });
}

async function stopAndFinalize(runId, reason, options = {}) {
  const session = state.sessions[runId];
  if (!session || session.closing || session.stopping) return;
  const save = options.save !== false;
  const report = save && options.report !== false;
  session.stopping = true;
  session.status = 'finalizing';
  session.endedAt = Date.now();
  session.keepData = save;
  session.wantReport = report;
  console.info('[StopMonitor] finalize start runId=%s reason=%s save=%s report=%s samples=%s', runId, reason, save, report, (session.samples || []).length);
  if (state.activeRunId === runId) {
    paintMonitorStatus(session);
    $('stop-button').disabled = true;
    $('elapsed').textContent = formatElapsed(session);
  } else {
    renderActiveMonitor();
  }
  try {
    await api(`/api/v1/runs/${runId}/stop`, { method: 'POST', body: JSON.stringify({ save, report }) });
  } catch (error) {
    console.warn('[StopMonitor] stop request failed runId=%s reason=%s err=%s', runId, reason, error && error.message);
  }
  await finishSession(runId, reason);
}

async function stopSession() {
  const session = activeSession();
  if (!session || session.closing || session.stopping || session.prompting || session.stopPrompting) return;
  if (session.status !== 'running') return;
  session.stopPrompting = true;
  const choice = await showStopDialog(session);
  session.stopPrompting = false;
  if (!choice || !state.sessions[session.runId]) {
    console.info('[StopMonitor] dialog cancel runId=%s', session.runId);
    return;
  }
  const current = state.sessions[session.runId];
  if (!current || current.status !== 'running' || current.stopping) {
    console.info('[StopMonitor] dialog stale runId=%s status=%s', session.runId, current && current.status);
    return;
  }
  console.info('[StopMonitor] confirm runId=%s save=%s report=%s samples=%s', current.runId, choice.save, choice.report, (current.samples || []).length);
  await stopAndFinalize(current.runId, 'manual', choice);
}

async function load() {
  const result = await api('/api/v1/devices?fresh=1');
  applyDeviceSnapshot(result.devices, { removed: [] });
  if (result.agent && result.agent.version) {
    const versionNode = document.querySelector('.agent-status small');
    if (versionNode) versionNode.textContent = `本机服务 · v${result.agent.version}`;
  }
  try {
    const agent = await api('/api/v1/agent');
    if ($('data-dir')) $('data-dir').textContent = agent.dataDir || '';
  } catch (error) { /* ignore */ }
  await restoreRuns();
}

async function refreshData() {
  const button = $('refresh-button');
  if (button) {
    button.disabled = true;
    button.classList.add('is-refreshing');
  }
  try {
    await load();
    const monitorOpen = Boolean($('monitor-view') && $('monitor-view').classList.contains('active-view'));
    const keepLive = monitorOpen && visibleSessions().some((session) => session.status === 'running') && state.apps.length;
    if (state.device && state.devices.some((device) => device.id === state.device.id) && !keepLive) {
      await selectDevice(state.device.id);
    } else if (keepLive) {
      console.info('[MonitorTab] refresh skip app list runId=%s', state.activeRunId);
    }
    const view = document.querySelector('.nav-item.active')?.dataset.view;
    if (view === 'reports') await renderReports();
    if (view === 'devices') renderDevicesTable();
    if (view === 'monitor') renderActiveMonitor();
  } catch (error) {
    $('connection-pill').textContent = error.message || '刷新失败';
    $('connection-pill').className = 'pill muted';
  } finally {
    if (button) {
      button.disabled = false;
      button.classList.remove('is-refreshing');
    }
  }
}

function on(id, event, handler) {
  const node = $(id);
  if (node) node.addEventListener(event, handler);
}

document.querySelectorAll('.nav-item').forEach((button) => button.addEventListener('click', () => setView(button.dataset.view)));
on('refresh-button', 'click', refreshData);
on('app-search', 'input', renderApps);
on('start-button', 'click', startSession);
on('stop-button', 'click', stopSession);
on('stop-dialog-cancel', 'click', () => dismissStopDialog(null));
on('stop-dialog-confirm', 'click', () => {
  const save = $('stop-save');
  const report = $('stop-report');
  dismissStopDialog({
    save: Boolean(save && save.checked),
    report: Boolean(save && save.checked && report && report.checked),
  });
});
on('stop-save', 'change', syncStopReportOption);
document.addEventListener('keydown', (event) => {
  if (event.key !== 'Escape') return;
  const dialog = $('stop-dialog');
  if (dialog && !dialog.hidden) dismissStopDialog(null);
});
on('opt-native-pss', 'change', persistMemoryExtras);
on('opt-swap-pss', 'change', persistMemoryExtras);
on('opt-logcat', 'change', persistMemoryExtras);
on('inspect-live', 'click', () => {
  const session = activeSession();
  if (session) clearInspect(session);
});
on('event-list', 'click', onEventListClick);
on('metric-subtabs', 'click', (event) => {
  const button = event.target.closest('.metric-subtab');
  if (button && button.dataset.metric) setMetricTab(button.dataset.metric);
});
['fps-chart', 'resource-chart', 'memory-chart', 'fps-detail-chart', 'cpu-detail-chart', 'mem-detail-chart', 'gpu-detail-chart', 'net-detail-chart'].forEach((id) => {
  const canvas = $(id);
  if (canvas) canvas.addEventListener('click', onChartClick);
});
load().then(watchDevices).catch((error) => {
  $('connection-pill').textContent = 'Agent 未启动';
  $('connection-pill').className = 'pill muted';
  $('device-list').innerHTML = `<div class="empty-state">${error.message}</div>`;
  watchDevices();
});
